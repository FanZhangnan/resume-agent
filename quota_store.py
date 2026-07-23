import inspect
import os
import secrets
import time

import httpx


_DAILY_COUNTER_TTL = 86400
_MAX_SESSION_TTL = 86400


# 模块级共享 httpx.AsyncClient（按 (url, token) 缓存，带 keepalive），
# 避免每条 Redis 命令都新建 TCP+TLS 连接。token 是站点级 Upstash 凭据，
# 非用户凭据，模块级缓存安全。
_SHARED_CLIENTS = {}


def _shared_client(url, token):
    key = (url, token)
    client = _SHARED_CLIENTS.get(key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=5.0,
            limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
            headers={"Authorization": f"Bearer {token}"},
        )
        _SHARED_CLIENTS[key] = client
    return client


class QuotaUnavailable(RuntimeError):
    code = "quota_unavailable"

    def __init__(self):
        super().__init__("quota backend unavailable")


class AdmissionDenied(RuntimeError):
    def __init__(self, code, free_left=None):
        self.code = str(code)
        self.free_left = free_left
        super().__init__(self.code)


_ACQUIRE_SCRIPT = r"""
-- quota-acquire-v1
local admission_id = ARGV[1]
local now = tonumber(ARGV[2])
local lease_expiry = tonumber(ARGV[3])
local hourly_limit = tonumber(ARGV[4])
local free_limit = tonumber(ARGV[5])
local site_limit = tonumber(ARGV[6])
local funded = ARGV[7]
local admission_ttl = tonumber(ARGV[8])
local hourly_ttl = tonumber(ARGV[9])
local daily_ttl = tonumber(ARGV[10])
local session_hash = ARGV[11]
local max_concurrent = tonumber(ARGV[12])

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)

local daily_used = tonumber(redis.call('GET', KEYS[4]) or '0')
local free_left = math.max(0, free_limit - daily_used)
if redis.call('EXISTS', KEYS[2]) == 1 then
    return {0, 'ip_concurrent', free_left}
end
if redis.call('ZCARD', KEYS[1]) >= max_concurrent then
    return {0, 'global_concurrent', free_left}
end
if tonumber(redis.call('GET', KEYS[3]) or '0') >= hourly_limit then
    return {0, 'hourly_limit', free_left}
end
if funded == '1' and daily_used >= free_limit then
    return {0, 'free_quota_exhausted', 0}
end
if funded == '1' and tonumber(redis.call('GET', KEYS[5]) or '0') >= site_limit then
    return {0, 'site_quota_exhausted', free_left}
end

local hourly_count = redis.call('INCR', KEYS[3])
if hourly_count == 1 then
    redis.call('EXPIRE', KEYS[3], hourly_ttl)
end
if funded == '1' then
    local daily_count = redis.call('INCR', KEYS[4])
    if daily_count == 1 then
        redis.call('EXPIRE', KEYS[4], daily_ttl)
    end
    local site_count = redis.call('INCR', KEYS[5])
    if site_count == 1 then
        redis.call('EXPIRE', KEYS[5], daily_ttl)
    end
    free_left = math.max(0, free_limit - daily_count)
end

redis.call('ZADD', KEYS[1], lease_expiry, admission_id)
redis.call('SET', KEYS[2], admission_id, 'EX', admission_ttl)
redis.call(
    'HSET', KEYS[6],
    'admission_id', admission_id,
    'global_key', KEYS[1],
    'lease_key', KEYS[2],
    'daily_key', KEYS[4],
    'site_key', KEYS[5],
    'funded', funded,
    'session_hash', session_hash
)
redis.call('EXPIRE', KEYS[6], admission_ttl)
return {1, admission_id, free_left}
"""


_RELEASE_SCRIPT = r"""
-- quota-release-v1
local admission_id = ARGV[1]
local refund_daily = ARGV[2]
local stored_id = redis.call('HGET', KEYS[2], 'admission_id')

redis.call('ZREM', KEYS[1], admission_id)
if not stored_id or stored_id ~= admission_id then
    return 0
end

if redis.call('HGET', KEYS[2], 'lease_key') ~= KEYS[3] then
    return 0
end
if redis.call('HGET', KEYS[2], 'daily_key') ~= KEYS[4] then
    return 0
end
if redis.call('HGET', KEYS[2], 'site_key') ~= KEYS[5] then
    return 0
end
if redis.call('GET', KEYS[3]) == admission_id then
    redis.call('DEL', KEYS[3])
end

if refund_daily == '1' and redis.call('HGET', KEYS[2], 'funded') == '1' then
    if tonumber(redis.call('GET', KEYS[4]) or '0') > 0 then
        redis.call('DECR', KEYS[4])
    end
    if tonumber(redis.call('GET', KEYS[5]) or '0') > 0 then
        redis.call('DECR', KEYS[5])
    end
end

redis.call('DEL', KEYS[2])
return 1
"""


_BIND_RUN_SCRIPT = r"""
-- quota-bind-run-v1
if redis.call('HGET', KEYS[1], 'admission_id') ~= ARGV[1] then
    return 0
end
if redis.call('HGET', KEYS[1], 'session_hash') ~= ARGV[3] then
    return -1
end
local current_owner = redis.call('HGET', KEYS[2], 'session_hash')
if current_owner and current_owner ~= ARGV[3] then
    return -1
end

redis.call(
    'HSET', KEYS[2],
    'run_id', ARGV[2],
    'session_hash', ARGV[3],
    'model', ARGV[4],
    'reasoning', ARGV[5],
    'created_at', ARGV[6],
    'status', 'running',
    'safe_to_deliver', '0'
)
local created_at = math.floor(tonumber(ARGV[7]))
local expires_at = created_at + tonumber(ARGV[8])
redis.call('EXPIREAT', KEYS[2], expires_at)
redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', created_at - tonumber(ARGV[8]))
redis.call('ZADD', KEYS[3], tonumber(ARGV[7]), ARGV[2])
local count = redis.call('ZCARD', KEYS[3])
local cap = tonumber(ARGV[9])
if count > cap then
    redis.call('ZREMRANGEBYRANK', KEYS[3], 0, count - cap - 1)
end
redis.call('EXPIREAT', KEYS[3], expires_at)
return 1
"""


_UPDATE_RUN_SCRIPT = r"""
-- quota-update-run-v1
if redis.call('EXISTS', KEYS[1]) == 0 then
    return 0
end
redis.call('HSET', KEYS[1], 'status', ARGV[1], 'safe_to_deliver', ARGV[2])
return 1
"""


_DELETE_RUN_SCRIPT = r"""
-- quota-delete-run-v1
if redis.call('HGET', KEYS[1], 'session_hash') ~= ARGV[1] then
    return 0
end
redis.call('DEL', KEYS[1])
redis.call('ZREM', KEYS[2], ARGV[2])
return 1
"""


_TRACE_WRITE_SCRIPT = r"""
-- trace-write-v1
if redis.call('EXISTS', KEYS[1]) == 0 then
    return 0
end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
return 1
"""


_TRACE_CANCEL_SCRIPT = r"""
-- trace-cancel-v1
if redis.call('EXISTS', KEYS[1]) == 0 then
    return 0
end
redis.call('HSET', KEYS[1], 'trace:cancelled', '1')
return 1
"""


class QuotaStore:
    def __init__(
        self,
        url,
        token,
        free_per_day=2,
        site_free_per_day=20,
        runs_per_hour=6,
        mock_per_hour=20,
        max_concurrent=3,
        session_ttl=86400,
        history_cap=5,
        admission_ttl=900,
        executor=None,
    ):
        self._url = str(
            os.environ.get("KV_REST_API_URL", "") if url is None else url
        ).strip().rstrip("/")
        self._token = str(
            os.environ.get("KV_REST_API_TOKEN", "") if token is None else token
        ).strip()
        self.free_per_day = self._non_negative_int(free_per_day, "free_per_day")
        self.site_free_per_day = self._non_negative_int(
            site_free_per_day, "site_free_per_day"
        )
        self.runs_per_hour = self._non_negative_int(runs_per_hour, "runs_per_hour")
        self.mock_per_hour = self._non_negative_int(mock_per_hour, "mock_per_hour")
        self.max_concurrent = self._positive_int(max_concurrent, "max_concurrent")
        self.session_ttl = self._bounded_positive_int(
            session_ttl, "session_ttl", _MAX_SESSION_TTL,
        )
        self.history_cap = self._positive_int(history_cap, "history_cap")
        self.admission_ttl = self._positive_int(admission_ttl, "admission_ttl")
        self._executor = executor

    @classmethod
    def from_env(cls, **kwargs):
        return cls(None, None, **kwargs)

    async def acquire(
        self,
        ip_hash,
        session_hash,
        kind="real",
        funded=True,
        now=None,
    ):
        ip_hash = self._required_text(ip_hash, "ip_hash")
        session_hash = self._required_text(session_hash, "session_hash")
        if kind not in ("real", "mock"):
            raise ValueError("kind must be 'real' or 'mock'")
        current = self._timestamp(now)
        admission_id = secrets.token_urlsafe(24)
        day_bucket = current // 86400
        hour_bucket = current // 3600
        effective_funded = bool(funded) and kind == "real"
        hourly_limit = self.mock_per_hour if kind == "mock" else self.runs_per_hour
        keys = [
            "ra:quota:active",
            self._key("ra:quota:ip-lease", ip_hash),
            self._key("ra:quota:hour", kind, ip_hash, hour_bucket),
            self._key("ra:quota:ip-day", ip_hash, day_bucket),
            self._key("ra:quota:site-day", day_bucket),
            self._key("ra:quota:admission", admission_id),
        ]
        args = [
            admission_id,
            str(current),
            str(current + self.admission_ttl),
            str(hourly_limit),
            str(self.free_per_day),
            str(self.site_free_per_day),
            "1" if effective_funded else "0",
            str(self.admission_ttl),
            "3600",
            str(_DAILY_COUNTER_TTL),
            session_hash,
            str(self.max_concurrent),
        ]
        result = await self._execute(
            ["EVAL", _ACQUIRE_SCRIPT, len(keys), *keys, *args]
        )
        if not isinstance(result, (list, tuple)) or len(result) < 3:
            raise QuotaUnavailable()
        allowed = self._as_int(result[0])
        code_or_id = self._as_text(result[1])
        free_left = self._as_int(result[2])
        if allowed == 1:
            return {"admission_id": code_or_id, "free_left": free_left}
        if allowed == 0 and code_or_id in {
            "free_quota_exhausted",
            "site_quota_exhausted",
            "hourly_limit",
            "ip_concurrent",
            "global_concurrent",
        }:
            raise AdmissionDenied(code_or_id, free_left)
        raise QuotaUnavailable()

    async def release(self, admission_id, refund_daily=False):
        admission_id = str(admission_id or "")
        if not admission_id:
            return False
        admission_key = self._key("ra:quota:admission", admission_id)
        metadata = self._decode_hash(
            await self._execute(["HGETALL", admission_key])
        )
        keys = [
            "ra:quota:active",
            admission_key,
            metadata.get("lease_key", admission_key),
            metadata.get("daily_key", admission_key),
            metadata.get("site_key", admission_key),
        ]
        result = await self._execute([
            "EVAL",
            _RELEASE_SCRIPT,
            len(keys),
            *keys,
            admission_id,
            "1" if refund_daily else "0",
        ])
        return self._as_int(result) == 1

    async def bind_run(
        self,
        admission_id,
        run_id,
        session_hash,
        model,
        reasoning,
        created_at,
    ):
        admission_id = self._required_text(admission_id, "admission_id")
        run_id = self._required_text(run_id, "run_id")
        session_hash = self._required_text(session_hash, "session_hash")
        self._required_text(created_at, "created_at")
        try:
            score = float(created_at)
        except (TypeError, ValueError):
            score = float(self._timestamp(None))
        created_text = self._format_score(score)
        keys = [
            self._key("ra:quota:admission", admission_id),
            self._key("ra:run", run_id),
            self._key("ra:history", session_hash),
        ]
        result = await self._execute([
            "EVAL",
            _BIND_RUN_SCRIPT,
            len(keys),
            *keys,
            admission_id,
            run_id,
            session_hash,
            str(model or ""),
            str(reasoning or ""),
            created_text,
            self._format_score(score),
            str(self.session_ttl),
            str(self.history_cap),
        ])
        result_code = self._as_int(result)
        if result_code == 1:
            return True
        if result_code == -1:
            raise ValueError("run is owned by another session")
        raise ValueError("admission is unavailable")

    async def owns_run(self, run_id, session_hash):
        run_id = str(run_id or "")
        session_hash = str(session_hash or "")
        if not run_id or not session_hash:
            return False
        owner = await self._execute([
            "HGET", self._key("ra:run", run_id), "session_hash"
        ])
        if owner is None:
            return False
        return secrets.compare_digest(self._as_text(owner), session_hash)

    async def list_runs(self, session_hash):
        session_hash = self._required_text(session_hash, "session_hash")
        run_ids = await self._execute([
            "ZREVRANGE",
            self._key("ra:history", session_hash),
            0,
            self.history_cap - 1,
        ])
        if run_ids is None:
            return []
        if not isinstance(run_ids, (list, tuple)):
            raise QuotaUnavailable()
        runs = []
        for raw_run_id in run_ids:
            run_id = self._as_text(raw_run_id)
            raw_fields = await self._execute([
                "HGETALL", self._key("ra:run", run_id)
            ])
            fields = self._decode_hash(raw_fields)
            if not fields or fields.get("session_hash") != session_hash:
                await self._execute([
                    "ZREM", self._key("ra:history", session_hash), run_id,
                ])
                continue
            runs.append({
                "run_id": run_id,
                "model": fields.get("model", ""),
                "reasoning": fields.get("reasoning", ""),
                "created_at": self._parse_created_at(fields.get("created_at")),
                "status": fields.get("status", "running"),
                "safe_to_deliver": fields.get("safe_to_deliver") == "1",
            })
        return runs

    async def update_run(self, run_id, status, safe_to_deliver):
        run_id = self._required_text(run_id, "run_id")
        status = self._required_text(status, "status")
        result = await self._execute([
            "EVAL",
            _UPDATE_RUN_SCRIPT,
            1,
            self._key("ra:run", run_id),
            status,
            "1" if safe_to_deliver else "0",
        ])
        return self._as_int(result) == 1

    async def delete_run(self, run_id, session_hash):
        run_id = str(run_id or "")
        session_hash = str(session_hash or "")
        if not run_id or not session_hash:
            return False
        keys = [
            self._key("ra:run", run_id),
            self._key("ra:history", session_hash),
        ]
        result = await self._execute([
            "EVAL",
            _DELETE_RUN_SCRIPT,
            len(keys),
            *keys,
            session_hash,
            run_id,
        ])
        return self._as_int(result) == 1

    async def write_trace_field(self, run_id, field, value):
        run_id = self._required_text(run_id, "run_id")
        field = self._required_text(field, "field")
        value = self._required_text(value, "value")
        allowed = field == "trace:meta" or (
            field.startswith("trace:stage:")
            and field.removeprefix("trace:stage:").isdigit()
            and 1 <= int(field.removeprefix("trace:stage:")) <= 8
        )
        if not allowed:
            raise ValueError("invalid trace field")
        result = await self._execute([
            "EVAL",
            _TRACE_WRITE_SCRIPT,
            1,
            self._key("ra:run", run_id),
            field,
            value,
        ])
        return self._as_int(result) == 1

    async def read_trace_fields(self, run_id):
        run_id = self._required_text(run_id, "run_id")
        fields = self._decode_hash(await self._execute([
            "HGETALL", self._key("ra:run", run_id),
        ]))
        if not fields:
            return None
        return {
            key: value
            for key, value in fields.items()
            if key == "trace:meta" or key.startswith("trace:stage:")
        }

    async def write_cancel(self, run_id):
        run_id = self._required_text(run_id, "run_id")
        result = await self._execute([
            "EVAL",
            _TRACE_CANCEL_SCRIPT,
            1,
            self._key("ra:run", run_id),
        ])
        return self._as_int(result) == 1

    async def read_cancel(self, run_id):
        run_id = self._required_text(run_id, "run_id")
        fields = self._decode_hash(await self._execute([
            "HGETALL", self._key("ra:run", run_id),
        ]))
        if not fields:
            return None
        return fields.get("trace:cancelled") == "1"

    async def store_credential(self, encrypted_value):
        encrypted_value = self._required_text(encrypted_value, "encrypted_value")
        for _ in range(3):
            reference = secrets.token_urlsafe(24)
            result = await self._execute([
                "SET",
                self._key("ra:credential", reference),
                encrypted_value,
                "EX",
                self.admission_ttl,
                "NX",
            ])
            if self._as_text(result or "").upper() == "OK":
                return reference
        raise QuotaUnavailable()

    async def get_credential(self, reference):
        reference = str(reference or "")
        if not reference:
            return None
        value = await self._execute([
            "GET", self._key("ra:credential", reference)
        ])
        return None if value is None else self._as_text(value)

    async def delete_credential(self, reference):
        reference = str(reference or "")
        if not reference:
            return False
        result = await self._execute([
            "DEL", self._key("ra:credential", reference)
        ])
        return self._as_int(result) > 0

    async def free_left(self, ip_hash, now=None):
        current = self._timestamp(now)
        key = self._key("ra:quota:ip-day", ip_hash, current // 86400)
        raw_used = await self._execute(["GET", key])
        used = 0 if raw_used is None else self._as_int(raw_used)
        return max(0, self.free_per_day - used)

    async def _execute(self, command):
        try:
            if self._executor is not None:
                target = (
                    self._executor.execute
                    if hasattr(self._executor, "execute")
                    else self._executor
                )
                response = target(command)
                if inspect.isawaitable(response):
                    response = await response
            else:
                if not self._url or not self._token:
                    raise QuotaUnavailable()
                client = _shared_client(self._url, self._token)
                http_response = await client.post(self._url, json=command)
                http_response.raise_for_status()
                response = http_response.json()
            if isinstance(response, dict):
                if response.get("error") is not None:
                    raise QuotaUnavailable()
                if "result" in response:
                    return response["result"]
                raise QuotaUnavailable()
            return response
        except QuotaUnavailable:
            raise
        except Exception:
            raise QuotaUnavailable() from None

    @staticmethod
    def _key(prefix, *parts):
        return ":".join([prefix, *(str(part) for part in parts)])

    @staticmethod
    def _timestamp(value):
        if value is None:
            return int(time.time())
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("now must be an epoch timestamp") from None

    @staticmethod
    def _positive_int(value, name):
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{name} must be a positive integer") from None
        if parsed <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return parsed

    @classmethod
    def _bounded_positive_int(cls, value, name, maximum):
        parsed = cls._positive_int(value, name)
        if parsed > maximum:
            raise ValueError(f"{name} must not exceed {maximum}")
        return parsed

    @staticmethod
    def _non_negative_int(value, name):
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{name} must be a non-negative integer") from None
        if parsed < 0:
            raise ValueError(f"{name} must be a non-negative integer")
        return parsed

    @staticmethod
    def _required_text(value, name):
        if value is None:
            raise ValueError(f"{name} is required")
        text = str(value)
        if not text:
            raise ValueError(f"{name} is required")
        return text

    @staticmethod
    def _as_text(value):
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError:
                raise QuotaUnavailable() from None
        return str(value)

    @classmethod
    def _as_int(cls, value):
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            raise QuotaUnavailable() from None

    @classmethod
    def _decode_hash(cls, value):
        if value is None:
            return {}
        if isinstance(value, dict):
            return {
                cls._as_text(key): cls._as_text(item)
                for key, item in value.items()
            }
        if not isinstance(value, (list, tuple)) or len(value) % 2:
            raise QuotaUnavailable()
        return {
            cls._as_text(value[index]): cls._as_text(value[index + 1])
            for index in range(0, len(value), 2)
        }

    @staticmethod
    def _format_score(value):
        if value.is_integer():
            return str(int(value))
        return repr(value)

    @staticmethod
    def _parse_created_at(value):
        if value is None:
            return None
        try:
            if any(character in value for character in ".eE"):
                return float(value)
            return int(value)
        except (TypeError, ValueError):
            return value
