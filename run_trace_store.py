"""Redis-backed, privacy-safe status trace for public Vercel runs."""

import json

from quota_store import QuotaStore


_ALLOWED_KEYS = frozenset({
    "status", "stage_id", "stage", "name", "total_stages", "duration_ms",
    "model", "reasoning", "attempt", "tokens_prompt", "tokens_completion",
    "retry_category", "validation_status", "output_shape", "error_category",
    "started_at", "completed_at", "created_at", "revision_round", "reason",
    "safe_to_deliver", "terminal", "elapsed_ms",
})
_MAX_STR = 240


class MissingRun(RuntimeError):
    """The Redis run hash has expired or was deleted."""


def _coerce_scalar(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:_MAX_STR]
    return None


def _sanitize(doc):
    if not isinstance(doc, dict):
        return {}
    clean = {}
    for key, value in doc.items():
        if key not in _ALLOWED_KEYS:
            continue
        if isinstance(value, list):
            clean[key] = [
                item for item in (_coerce_scalar(entry) for entry in value)
                if item is not None
            ]
        elif isinstance(value, dict):
            clean[key] = {
                str(nested_key): coerced
                for nested_key, nested_value in value.items()
                if (coerced := _coerce_scalar(nested_value)) is not None
            }
        else:
            coerced = _coerce_scalar(value)
            if coerced is not None or value is None:
                clean[key] = coerced
    return clean


def _decode_json(value):
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return _sanitize(decoded)


def _decode_stage_fields(fields):
    if fields is None:
        raise MissingRun("run trace is unavailable")
    stages = {}
    for field, value in fields.items():
        if not field.startswith("trace:stage:"):
            continue
        raw_stage_id = field.removeprefix("trace:stage:")
        if raw_stage_id.isdigit() and 1 <= int(raw_stage_id) <= 8:
            stages[int(raw_stage_id)] = _decode_json(value)
    return stages


class TraceStore:
    """Expose the former trace contract over one Redis run hash."""

    def __init__(self, quota=None):
        self._quota = quota or QuotaStore.from_env()

    async def write_stage(
        self, run_id, stage_id, doc, *, created_epoch=None,
    ):
        try:
            parsed_stage_id = int(stage_id)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("stage_id must be an integer from 1 to 8") from None
        if not 1 <= parsed_stage_id <= 8:
            raise ValueError("stage_id must be an integer from 1 to 8")
        payload = json.dumps(
            _sanitize(doc), ensure_ascii=False, separators=(",", ":"),
        )
        return await self._quota.write_trace_field(
            run_id, f"trace:stage:{parsed_stage_id}", payload,
        )

    async def read_stages(self, run_id):
        return _decode_stage_fields(await self._quota.read_trace_fields(run_id))

    async def write_meta(self, run_id, doc, *, created_epoch=None):
        payload = json.dumps(
            _sanitize(doc), ensure_ascii=False, separators=(",", ":"),
        )
        return await self._quota.write_trace_field(run_id, "trace:meta", payload)

    async def read_meta(self, run_id):
        fields = await self._quota.read_trace_fields(run_id)
        if fields is None:
            raise MissingRun(run_id)
        return _decode_json(fields.get("trace:meta", "{}"))

    async def write_cancel(self, run_id):
        return await self._quota.write_cancel(run_id)

    async def is_cancelled(self, run_id):
        state = await self._quota.read_cancel(run_id)
        if state is None:
            raise MissingRun(run_id)
        return state
