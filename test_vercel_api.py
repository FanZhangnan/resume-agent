"""API contract tests for the Vercel FastAPI entrypoint with injected fakes."""

import asyncio
import inspect
import io
import json
import os
import time
import zipfile
from unittest.mock import patch

os.environ.setdefault("AGENT_WORKFLOW_TEST", "1")
os.environ["AGENT_RUN_SIGNING_KEY"] = "unit-test-signing-key"
os.environ["CRON_SECRET"] = "cron-secret"

from fastapi.testclient import TestClient  # noqa: E402

from public_security import hash_ip, issue_session  # noqa: E402
from quota_store import QuotaStore  # noqa: E402
from test_quota_store import FailingExecutor, FakeRedisExecutor  # noqa: E402
from test_vercel_trace import FakeBlobClient  # noqa: E402
from vercel_trace import TraceStore  # noqa: E402
import webui.vercel_server as server  # noqa: E402

# API contract tests use an injected backend and never contact the gateway.
server.config.API_KEY = "unit-test-gateway-key"


def test_runtime_accepts_injected_quota_store():
    assert "quota" in inspect.signature(server.set_runtime).parameters


class FakeBackend:
    def __init__(self):
        self.started = []
        self._status = {}
        self._result = {}
        self.result_calls = 0
        self._counter = 0
        self.start_error = None
        self.cancelled = []

    async def start(self, payload):
        if self.start_error is not None:
            raise self.start_error
        self._counter += 1
        run_id = f"run-{self._counter}"
        self.started.append((run_id, payload))
        self._status[run_id] = "running"
        return run_id

    async def status(self, run_id):
        return self._status.get(run_id, "pending")

    async def result(self, run_id):
        self.result_calls += 1
        return self._result.get(run_id, {})

    async def cancel(self, run_id):
        self.cancelled.append(run_id)
        self._status[run_id] = "cancelled"

    def complete(self, run_id, result):
        self._status[run_id] = "completed"
        self._result[run_id] = result


def _client(*, backend=None, trace=None, quota=None, quota_options=None,
            client_ip="203.0.113.10"):
    backend = backend or FakeBackend()
    trace = trace or TraceStore(client=FakeBlobClient())
    if quota is None:
        redis = FakeRedisExecutor()
        quota = QuotaStore(
            "https://redis.example",
            "redis-token",
            executor=redis,
            **dict(quota_options or {}),
        )
    else:
        redis = getattr(quota, "_executor", None)
    server.set_runtime(backend=backend, trace=trace, quota=quota)
    client = TestClient(
        server.app,
        base_url="https://testserver",
        client=(client_ip, 50_000),
    )
    return client, backend, trace, quota, redis


def _second_client(backend, trace, quota, client_ip="203.0.113.10"):
    server.set_runtime(backend=backend, trace=trace, quota=quota)
    return TestClient(
        server.app,
        base_url="https://testserver",
        client=(client_ip, 50_001),
    )


def _run(coro):
    return asyncio.run(coro)


def _start_ok(client, **overrides):
    data = {"jd_text": "招聘后端工程师，需要 Python 经验",
            "model": "gpt-5.5", "reasoning": "xhigh"}
    data.update(overrides.pop("data", {}))
    files = overrides.pop("files", {
        "resume_file": ("resume.txt", "张三\n后端工程师\n负责核心系统，提升性能30%".encode("utf-8"),
                        "text/plain"),
    })
    return client.post("/api/runs", data=data, files=files, **overrides)


def _release_started(quota, backend, index=-1):
    payload = backend.started[index][1]
    assert _run(quota.release(payload["admission_id"])) is True
    if payload.get("credential_ref"):
        _run(quota.delete_credential(payload["credential_ref"]))
    return payload


def test_config_exposes_exact_models_only():
    client, _, _, _, _ = _client()
    response = client.get("/api/config")
    assert response.status_code == 200
    body = response.json()
    assert body["models"] == {"gpt-5.5": ["high", "xhigh"],
                              "gpt-5.6-terra": ["high", "xhigh"]}
    assert body["default_model"] == "gpt-5.5"
    assert body["free_left"] == 2
    assert body["free_per_day"] == 2
    text = str(body)
    for forbidden in ("sol", "\"max\"", "'max'", "\"low\"", "medium", "none"):
        assert forbidden not in text.lower() or forbidden == "none"  # 'none' not a level here
    assert "sol" not in text.lower()


def test_status_reports_vercel_mode():
    client, _, _, _, _ = _client()
    body = client.get("/api/status").json()
    assert body["deployment_mode"] == "vercel"


def test_entry_responses_issue_secure_http_only_session_and_rotate_invalid_cookie():
    client, _, _, _, _ = _client()
    first = client.get("/api/config")
    cookie_header = first.headers["set-cookie"].lower()
    assert cookie_header.startswith("agent_sid=")
    for attribute in (
        "httponly", "secure", "samesite=lax", "path=/", "max-age=86400",
    ):
        assert attribute in cookie_header
    valid_cookie = client.cookies.get("agent_sid")
    assert valid_cookie

    # A valid session remains stable; invalid input is replaced immediately.
    assert "set-cookie" not in client.get("/api/status").headers
    client.cookies.clear()
    client.cookies.set(
        "agent_sid", valid_cookie + "tampered", domain="testserver.local", path="/",
    )
    rotated = client.get("/")
    assert "agent_sid=" in rotated.headers["set-cookie"].lower()
    assert client.cookies.get("agent_sid") != valid_cookie + "tampered"


def test_session_ttl_environment_controls_cookie_max_age():
    with patch.dict(os.environ, {"AGENT_SESSION_TTL": "3600"}, clear=False):
        client, _, _, _, _ = _client()
        response = client.get("/api/config")
    assert response.status_code == 200
    assert "max-age=3600" in response.headers["set-cookie"].lower()


def test_expired_session_cookie_is_rotated():
    client, _, _, _, _ = _client()
    expired, _, _ = issue_session(
        os.environ["AGENT_RUN_SIGNING_KEY"], int(time.time()) - 86401, 86400,
    )
    client.cookies.set(
        "agent_sid", expired, domain="testserver.local", path="/",
    )
    response = client.get("/api/status")
    assert response.status_code == 200
    assert "agent_sid=" in response.headers["set-cookie"].lower()
    assert client.cookies.get("agent_sid") != expired


def test_stage_rows_expose_fallback_validation_status():
    rows = server._stage_rows({
        6: {
            "status": "completed",
            "reason": "fact_only_fallback",
            "validation_status": "rejected_ai_draft",
        },
    })
    stage = next(row for row in rows if row["stage_id"] == 6)
    assert stage["validation_status"] == "rejected_ai_draft"


def test_model_validation_rejects_forbidden():
    client, _, _, _, _ = _client()
    assert _start_ok(client, data={"model": "gpt-5.6-sol", "reasoning": "high"}).status_code == 400
    assert _start_ok(client, data={"model": "gpt-5.5", "reasoning": "max"}).status_code == 400


def test_api_key_length_is_rejected_before_admission():
    client, backend, _, _, redis = _client()
    response = _start_ok(client, data={"api_key": "k" * 201})
    assert response.status_code == 413
    assert response.json()["code"] == "api_key_too_long"
    assert backend.started == []
    assert redis.active == {}
    assert not any(":hour:" in key for key in redis.counts)


def test_missing_jd_is_rejected_in_preview():
    client, _, _, _, _ = _client()
    resp = _start_ok(client, data={"jd_text": "   "})
    assert resp.status_code == 422


def test_missing_gateway_key_rejected_before_workflow_start():
    client, backend, _, _, _ = _client()
    original_key = server.config.API_KEY
    original_mock = os.environ.get("AGENT_MOCK")
    server.config.API_KEY = ""
    os.environ["AGENT_MOCK"] = "0"
    try:
        resp = _start_ok(client)
    finally:
        server.config.API_KEY = original_key
        if original_mock is None:
            os.environ.pop("AGENT_MOCK", None)
        else:
            os.environ["AGENT_MOCK"] = original_mock

    assert resp.status_code == 503
    assert resp.json()["code"] == "gateway_not_configured"
    assert backend.started == []


def test_oversize_jd_is_rejected_before_workflow_start():
    client, backend, _, _, _ = _client()
    resp = _start_ok(client, data={"jd_text": "x" * (server.MAX_JD_CHARS + 1)})
    assert resp.status_code == 413
    assert backend.started == []


def test_oversize_upload_rejected():
    client, _, _, _, _ = _client()
    big = b"a" * (4 * 1024 * 1024 + 1)
    resp = _start_ok(client, files={"resume_file": ("r.txt", big, "text/plain")})
    assert resp.status_code == 413


def test_extracted_resume_text_limit_is_rejected():
    client, backend, _, _, _ = _client()
    text = ("简历内容\n" * (server.MAX_RESUME_CHARS // 4 + 1)).encode("utf-8")
    resp = _start_ok(client, files={"resume_file": ("r.txt", text, "text/plain")})
    assert resp.status_code == 413
    assert backend.started == []


def test_docx_expansion_limit_is_rejected():
    client, backend, _, _, _ = _client()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "x" * 2048)
    original = server.MAX_DOCX_UNCOMPRESSED_BYTES
    server.MAX_DOCX_UNCOMPRESSED_BYTES = 1024
    try:
        resp = _start_ok(
            client,
            files={"resume_file": (
                "resume.docx", buffer.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )},
        )
    finally:
        server.MAX_DOCX_UNCOMPRESSED_BYTES = original
    assert resp.status_code == 413
    assert backend.started == []


def test_file_parse_timeout_is_bounded():
    from tools import file_parser

    client, backend, _, _, _ = _client()
    original_parser = file_parser.parse_resume_file
    original_timeout = server.PARSE_TIMEOUT_SECONDS

    def slow_parser(_path):
        time.sleep(0.05)
        return {"success": True, "text": "late"}

    file_parser.parse_resume_file = slow_parser
    server.PARSE_TIMEOUT_SECONDS = 0.01
    try:
        resp = _start_ok(client)
    finally:
        file_parser.parse_resume_file = original_parser
        server.PARSE_TIMEOUT_SECONDS = original_timeout
    assert resp.status_code == 408
    assert backend.started == []


def test_unsupported_type_rejected():
    client, backend, _, _, redis = _client()
    resp = _start_ok(client, files={"resume_file": ("resume.doc", b"legacy doc bytes", "application/msword")})
    assert resp.status_code == 415
    assert backend.started == []
    assert redis.active == {}
    assert sum(
        count for key, count in redis.counts.items() if ":hour:" in key
    ) == 1
    assert all(
        count == 0
        for key, count in redis.counts.items()
        if ":ip-day:" in key or ":site-day:" in key
    )


def test_empty_or_scanned_rejected():
    client, _, _, _, _ = _client()
    resp = _start_ok(client, files={"resume_file": ("r.txt", b"     \n   \t  ", "text/plain")})
    assert resp.status_code == 422


def test_start_uses_cookie_session_and_safe_workflow_payload():
    client, backend, trace, quota, redis = _client()
    secret = "sk-byok-secret-must-never-leak"
    resp = _start_ok(client, data={"api_key": secret, "mock": "0"})
    assert resp.status_code == 202
    body = resp.json()
    run_id = body["run_id"]
    assert "token" not in body and "expires_at" not in body
    assert "credential_ref" not in body and "session_hash" not in body
    assert body["byok"] is True and body["mock"] is False
    assert body["free_left"] == 2
    # Backend received JSON-safe payload with extracted text and deadline.
    started_id, payload = backend.started[0]
    assert started_id == run_id
    assert "张三" in payload["resume_text"]
    assert payload["jd_text"].strip()
    assert payload["model"] == "gpt-5.5" and payload["reasoning"] == "xhigh"
    assert payload["deadline_epoch"] > 0
    assert payload["mock"] is False
    assert payload["credential_ref"]
    assert payload["admission_id"]
    assert payload["session_hash"]
    # No raw file path or bytes leak into workflow state.
    assert "file_path" not in payload and "resume_file" not in payload
    assert set(payload) <= {
        "resume_text", "jd_text", "model", "reasoning", "job_search",
        "deadline_epoch", "admission_id", "credential_ref", "session_hash",
        "mock",
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    assert secret not in serialized
    assert server.config.API_BASE_URL not in serialized
    assert "api_key" not in payload and "base_url" not in payload
    assert secret not in repr(redis.credentials)
    assert _run(quota.owns_run(run_id, payload["session_hash"])) is True
    # Stage 1 recorded as completed.
    stages = _run(trace.read_stages(run_id))
    assert stages[1]["status"] == "completed"


def test_workflow_start_failure_refunds_funded_daily_and_deletes_byok():
    backend = FakeBackend()
    backend.start_error = RuntimeError("gateway secret must not leak")
    client, _, _, quota, redis = _client(backend=backend)

    failed = _start_ok(client)
    assert failed.status_code == 503
    assert failed.json()["code"] == "run_start_failed"
    assert redis.active == {}
    assert all(
        count == 0
        for key, count in redis.counts.items()
        if ":ip-day:" in key or ":site-day:" in key
    )
    assert sum(
        count for key, count in redis.counts.items() if ":hour:" in key
    ) == 1

    secret = "sk-rollback-secret"
    byok_failed = _start_ok(client, data={"api_key": secret})
    assert byok_failed.status_code == 503
    assert byok_failed.json()["code"] == "run_start_failed"
    assert secret not in byok_failed.text
    assert redis.active == {}
    assert redis.credentials == {}
    assert sum(
        count for key, count in redis.counts.items() if ":hour:" in key
    ) == 2

    # Daily/site funding was refunded, while the anti-abuse hourly attempt remains.
    backend.start_error = None
    succeeded = _start_ok(client)
    assert succeeded.status_code == 202
    assert succeeded.json()["free_left"] == 1
    admission_id = backend.started[-1][1]["admission_id"]
    assert _run(quota.release(admission_id)) is True


def test_definitive_post_start_bind_failure_cancels_and_rolls_back():
    backend = FakeBackend()
    redis = FakeRedisExecutor()
    quota = QuotaStore(
        "https://redis.example", "redis-token", executor=redis,
    )

    async def fail_bind(*_args, **_kwargs):
        raise ValueError("private bind detail")

    quota.bind_run = fail_bind
    client, _, _, _, _ = _client(backend=backend, quota=quota)
    failed = _start_ok(client, data={"api_key": "sk-bind-secret"})
    assert failed.status_code == 503
    assert failed.json()["code"] == "run_start_failed"
    assert backend.cancelled == ["run-1"]
    assert redis.active == {}
    assert redis.credentials == {}
    assert redis.runs == {}
    assert "private bind detail" not in failed.text


def test_bind_commit_then_response_loss_is_retried_idempotently():
    class CommitThenLoseResponse(FakeRedisExecutor):
        def __init__(self):
            super().__init__()
            self.bind_attempts = 0

        async def __call__(self, command):
            if command[0] == "EVAL" and "quota-bind-run-v1" in command[1]:
                self.bind_attempts += 1
                result = await super().__call__(command)
                if self.bind_attempts == 1:
                    raise RuntimeError("response lost after commit")
                return result
            return await super().__call__(command)

    backend = FakeBackend()
    redis = CommitThenLoseResponse()
    quota = QuotaStore("https://redis.example", "redis-token", executor=redis)
    client, _, _, _, _ = _client(backend=backend, quota=quota)

    started = _start_ok(client)
    assert started.status_code == 202
    assert started.json()["run_id"] == "run-1"
    assert redis.bind_attempts == 2
    assert backend.cancelled == []
    assert len(redis.active) == 1
    assert "ra:run:run-1" in redis.runs
    assert sum(
        count for key, count in redis.counts.items()
        if ":ip-day:" in key or ":site-day:" in key
    ) == 2


def test_unknown_bind_outcome_keeps_accounting_and_requests_cancel():
    class CommitThenGoOffline(FakeRedisExecutor):
        def __init__(self):
            super().__init__()
            self.committed = False

        async def __call__(self, command):
            if self.committed:
                raise RuntimeError("redis response path unavailable")
            if command[0] == "EVAL" and "quota-bind-run-v1" in command[1]:
                result = await super().__call__(command)
                self.committed = True
                raise RuntimeError("response lost after commit")
            return await super().__call__(command)

    backend = FakeBackend()
    redis = CommitThenGoOffline()
    quota = QuotaStore("https://redis.example", "redis-token", executor=redis)
    client, _, trace, _, _ = _client(backend=backend, quota=quota)

    failed = _start_ok(client, data={"api_key": "sk-uncertain-bind"})
    assert failed.status_code == 503
    assert failed.json()["code"] == "run_start_uncertain"
    assert backend.cancelled == ["run-1"]
    assert len(redis.active) == 1
    assert "ra:run:run-1" in redis.runs
    assert redis.credentials
    assert _run(trace.is_cancelled("run-1")) is True


def test_trace_failure_after_bind_keeps_usage_and_admission_accounted():
    backend = FakeBackend()
    trace = TraceStore(client=FakeBlobClient())

    async def fail_stage(*_args, **_kwargs):
        raise RuntimeError("private trace detail")

    trace.write_stage = fail_stage
    client, _, _, _, redis = _client(backend=backend, trace=trace)
    started = _start_ok(client)
    assert started.status_code == 202
    assert started.json()["free_left"] == 1
    assert backend.cancelled == []
    assert len(redis.active) == 1
    assert "ra:run:run-1" in redis.runs
    assert any("run-1" in run_ids for run_ids in redis.history.values())
    assert sum(
        count
        for key, count in redis.counts.items()
        if ":ip-day:" in key or ":site-day:" in key
    ) == 2
    assert "private trace detail" not in started.text


def test_credential_storage_failure_is_fail_closed_and_releases_admission():
    class CredentialFailingExecutor(FakeRedisExecutor):
        async def __call__(self, command):
            if command[0] == "SET":
                raise RuntimeError("credential redis detail")
            return await super().__call__(command)

    redis = CredentialFailingExecutor()
    quota = QuotaStore(
        "https://redis.example", "redis-token", executor=redis,
    )
    client, backend, _, _, _ = _client(quota=quota)
    response = _start_ok(client, data={"api_key": "sk-storage-failure"})
    assert response.status_code == 503
    assert response.json()["code"] == "quota_unavailable"
    assert backend.started == []
    assert redis.active == {}
    assert redis.credentials == {}


def test_site_funded_runs_allow_two_per_ip_then_return_stable_daily_error():
    client, backend, _, quota, _ = _client()

    first = _start_ok(client)
    assert first.status_code == 202
    assert first.json()["free_left"] == 1
    _release_started(quota, backend)

    second = _start_ok(client)
    assert second.status_code == 202
    assert second.json()["free_left"] == 0
    _release_started(quota, backend)

    denied = _start_ok(client)
    assert denied.status_code == 429
    assert denied.json()["code"] == "free_quota_exhausted"
    assert denied.json()["free_left"] == 0


def test_byok_and_mock_ignore_daily_funding_and_mock_ignores_supplied_key():
    client, backend, _, quota, redis = _client()
    for _ in range(2):
        assert _start_ok(client).status_code == 202
        _release_started(quota, backend)

    byok_secret = "sk-daily-exempt"
    byok = _start_ok(client, data={"api_key": byok_secret})
    assert byok.status_code == 202
    assert byok.json()["free_left"] == 0
    assert byok.json()["byok"] is True and byok.json()["mock"] is False
    assert backend.started[-1][1]["credential_ref"]
    _release_started(quota, backend)

    ignored_secret = "sk-mock-must-be-ignored"
    mock = _start_ok(client, data={"mock": "1", "api_key": ignored_secret})
    assert mock.status_code == 202
    assert mock.json()["free_left"] == 0
    assert mock.json()["mock"] is True and mock.json()["byok"] is False
    mock_payload = backend.started[-1][1]
    assert mock_payload["credential_ref"] is None
    assert ignored_secret not in json.dumps(mock_payload, ensure_ascii=False)
    assert ignored_secret not in repr(redis.credentials)


def test_real_and_mock_hourly_limits_have_stable_error_code():
    real_client, backend, _, quota, _ = _client(
        quota_options={"runs_per_hour": 1},
    )
    assert _start_ok(real_client, data={"api_key": "sk-first"}).status_code == 202
    _release_started(quota, backend)
    real_denied = _start_ok(real_client, data={"api_key": "sk-second"})
    assert real_denied.status_code == 429
    assert real_denied.json()["code"] == "hourly_limit"

    mock_client, mock_backend, _, mock_quota, _ = _client(
        quota_options={"mock_per_hour": 1},
        client_ip="203.0.113.11",
    )
    assert _start_ok(mock_client, data={"mock": "1"}).status_code == 202
    _release_started(mock_quota, mock_backend)
    mock_denied = _start_ok(mock_client, data={"mock": "1"})
    assert mock_denied.status_code == 429
    assert mock_denied.json()["code"] == "hourly_limit"


def test_ip_and_global_concurrency_return_distinct_stable_codes():
    client, backend, trace, quota, _ = _client()
    assert _start_ok(client, data={"mock": "1"}).status_code == 202
    same_ip = _start_ok(client, data={"mock": "1"})
    assert same_ip.status_code == 429
    assert same_ip.json()["code"] == "ip_concurrent"
    _release_started(quota, backend)

    global_client, global_backend, global_trace, global_quota, _ = _client(
        quota_options={"max_concurrent": 1},
        client_ip="203.0.113.20",
    )
    assert _start_ok(global_client, data={"mock": "1"}).status_code == 202
    other_ip = _second_client(
        global_backend, global_trace, global_quota, client_ip="203.0.113.21",
    )
    global_denied = _start_ok(other_ip, data={"mock": "1"})
    assert global_denied.status_code == 429
    assert global_denied.json()["code"] == "global_concurrent"


def test_site_funded_global_daily_limit_has_stable_error_code():
    client, backend, trace, quota, _ = _client(
        quota_options={"site_free_per_day": 1},
        client_ip="203.0.113.30",
    )
    assert _start_ok(client).status_code == 202
    _release_started(quota, backend)
    other_ip = _second_client(
        backend, trace, quota, client_ip="203.0.113.31",
    )
    denied = _start_ok(other_ip)
    assert denied.status_code == 429
    assert denied.json()["code"] == "site_quota_exhausted"


def test_recent_runs_are_cookie_scoped_newest_first_and_capped_at_five():
    client, backend, trace, quota, _ = _client()
    created = []
    for _ in range(6):
        response = _start_ok(client, data={"mock": "1"})
        assert response.status_code == 202
        created.append(response.json()["run_id"])
        _release_started(quota, backend)

    history = client.get("/api/runs")
    assert history.status_code == 200
    body = history.json()
    assert body["free_left"] == 2
    assert [item["run_id"] for item in body["runs"]] == list(reversed(created[-5:]))
    assert all("session_hash" not in item for item in body["runs"])

    other = _second_client(backend, trace, quota)
    assert other.get("/api/runs").json()["runs"] == []


def test_redis_failures_fail_closed_before_workflow_start_and_in_config():
    quota = QuotaStore(
        "https://redis.example", "redis-token", executor=FailingExecutor(),
    )
    client, backend, _, _, _ = _client(quota=quota)
    config_response = client.get("/api/config")
    assert config_response.status_code == 503
    assert config_response.json()["code"] == "quota_unavailable"
    create_response = _start_ok(client)
    assert create_response.status_code == 503
    assert create_response.json()["code"] == "quota_unavailable"
    assert backend.started == []


def test_vercel_identity_uses_only_trusted_header_and_local_ignores_forwarded_headers():
    trusted_ip = "198.51.100.8"
    spoofed_ip = "192.0.2.99"
    with patch.dict(os.environ, {"VERCEL": "1"}):
        client, backend, _, quota, redis = _client()
        response = _start_ok(
            client,
            data={"mock": "1"},
            headers={
                "x-vercel-forwarded-for": trusted_ip,
                "x-forwarded-for": spoofed_ip,
            },
        )
        assert response.status_code == 202
        expected_hash = hash_ip(trusted_ip, os.environ["AGENT_RUN_SIGNING_KEY"])
        spoofed_hash = hash_ip(spoofed_ip, os.environ["AGENT_RUN_SIGNING_KEY"])
        lease_keys = " ".join(redis.leases)
        assert expected_hash in lease_keys
        assert spoofed_hash not in lease_keys
        _release_started(quota, backend)

        missing_trusted = _start_ok(
            client,
            data={"mock": "1"},
            headers={"x-forwarded-for": trusted_ip},
        )
        assert missing_trusted.status_code == 400
        assert missing_trusted.json()["code"] == "invalid_client_ip"

    with patch.dict(os.environ, {"VERCEL": "0"}):
        local_ip = "203.0.113.77"
        client, _, _, _, redis = _client(client_ip=local_ip)
        response = _start_ok(
            client,
            data={"mock": "1"},
            headers={
                "x-vercel-forwarded-for": trusted_ip,
                "x-forwarded-for": spoofed_ip,
            },
        )
        assert response.status_code == 202
        local_hash = hash_ip(local_ip, os.environ["AGENT_RUN_SIGNING_KEY"])
        assert local_hash in " ".join(redis.leases)


def test_cross_session_status_cancel_and_delete_all_return_not_found():
    client, backend, trace, quota, _ = _client()
    run_id = _start_ok(client).json()["run_id"]
    other = _second_client(backend, trace, quota)
    assert other.get(f"/api/runs/{run_id}").status_code == 404
    assert other.post(f"/api/runs/{run_id}/cancel").status_code == 404
    assert other.delete(f"/api/runs/{run_id}").status_code == 404
    # Bearer headers no longer grant access to a different cookie session.
    assert other.get(
        f"/api/runs/{run_id}",
        headers={"Authorization": "Bearer anything"},
    ).status_code == 404


def test_status_polling_and_result_only_after_completion():
    client, backend, _, _, _ = _client()
    body = _start_ok(client).json()
    run_id = body["run_id"]

    running = client.get(f"/api/runs/{run_id}").json()
    assert running["status"] == "running"
    assert running.get("report") in (None, "")
    assert backend.result_calls == 0        # never read return_value while running
    assert len(running["stages"]) == 8

    backend.complete(run_id, {"status": "completed", "safe_to_deliver": True,
                              "report": "# 简历优化报告\n完成", "unresolved_fixes": [],
                              "model": "gpt-5.5", "reasoning": "xhigh"})
    done = client.get(f"/api/runs/{run_id}").json()
    assert done["status"] == "completed"
    assert done["safe_to_deliver"] is True
    assert "简历优化报告" in done["report"]
    history = client.get("/api/runs").json()["runs"]
    assert history[0]["run_id"] == run_id
    assert history[0]["status"] == "completed"
    assert history[0]["safe_to_deliver"] is True


def test_cancel_writes_marker():
    client, _, trace, _, redis = _client()
    body = _start_ok(client).json()
    run_id = body["run_id"]
    assert client.post(f"/api/runs/{run_id}/cancel").status_code == 202
    assert _run(trace.is_cancelled(run_id)) is True
    # Cancellation is cooperative; admission is released by terminal workflow cleanup.
    assert len(redis.active) == 1


def test_delete_rejects_running_run_and_preserves_cancel_marker():
    client, _, trace, _, redis = _client()
    body = _start_ok(client).json()
    run_id = body["run_id"]
    assert client.post(f"/api/runs/{run_id}/cancel").status_code == 202
    refused = client.delete(f"/api/runs/{run_id}")
    assert refused.status_code == 409
    assert refused.json()["code"] == "run_not_terminal"
    assert _run(trace.is_cancelled(run_id)) is True
    assert len(redis.active) == 1
    assert client.get("/api/runs").json()["runs"][0]["run_id"] == run_id


def test_delete_removes_terminal_run_traces_and_history():
    client, backend, trace, _, _ = _client()
    body = _start_ok(client).json()
    run_id = body["run_id"]
    backend.complete(run_id, {
        "status": "completed", "safe_to_deliver": True,
        "report": "# done", "unresolved_fixes": [],
    })
    assert client.get(f"/api/runs/{run_id}").status_code == 200
    assert client.delete(f"/api/runs/{run_id}").status_code == 200
    stages = _run(trace.read_stages(run_id))
    assert stages == {}
    assert client.get("/api/runs").json()["runs"] == []


def test_blob_delete_failure_keeps_run_owner_for_retry():
    class ToggleListFailureClient(FakeBlobClient):
        fail_list = False

        async def list_objects(self, **kwargs):
            if self.fail_list:
                raise RuntimeError("blob list unavailable")
            return await super().list_objects(**kwargs)

    blob = ToggleListFailureClient()
    trace = TraceStore(client=blob)
    client, backend, _, quota, _ = _client(trace=trace)
    body = _start_ok(client).json()
    run_id = body["run_id"]
    session_hash = backend.started[0][1]["session_hash"]
    backend.complete(run_id, {
        "status": "completed", "safe_to_deliver": True,
        "report": "# done", "unresolved_fixes": [],
    })
    assert client.get(f"/api/runs/{run_id}").status_code == 200

    blob.fail_list = True
    failed = client.delete(f"/api/runs/{run_id}")
    assert failed.status_code == 503
    assert failed.json()["code"] == "delete_unavailable"
    blob.fail_list = False
    assert _run(quota.owns_run(run_id, session_hash)) is True
    assert client.get("/api/runs").json()["runs"][0]["run_id"] == run_id

    assert client.delete(f"/api/runs/{run_id}").status_code == 200
    assert _run(quota.owns_run(run_id, session_hash)) is False


def test_cleanup_requires_cron_secret():
    client, _, _, _, _ = _client()
    assert client.get("/api/maintenance/cleanup").status_code in (401, 403)
    assert client.get("/api/maintenance/cleanup",
                      headers={"Authorization": "Bearer wrong"}).status_code in (401, 403)
    ok = client.get("/api/maintenance/cleanup",
                    headers={"Authorization": "Bearer cron-secret"})
    assert ok.status_code == 200


if __name__ == "__main__":
    tests = [v for n, v in sorted(globals().items()) if n.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} vercel-api tests passed")
