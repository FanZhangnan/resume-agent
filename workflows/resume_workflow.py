"""Top-level Vercel durable workflow and steps for the resume agent.

Every decorated function lives at module scope with a stable name because
``module + qualname`` forms the persisted workflow/step id. Each tool step runs
the existing synchronous tool code on a worker thread inside the per-run model
settings, so the deterministic pipeline logic is reused unchanged. Ordering,
deadlines, cancellation, and delivery gates live in ``workflows.graph`` so the
same control flow is unit-tested without a Vercel replay context.
"""

import asyncio
import os
import time

from vercel.workflow import get_step_metadata

from vercel_trace import TraceStore
from workflows.graph import run_workflow_graph
from workflows.runtime import wf


_UPDATE_RUN_RETRY_DELAYS = (0.25, 0.75)
_BINDING_RETRY_DELAYS = (0.1, 0.2, 0.4, 0.8)
_PUBLIC_WORKFLOW_PAYLOAD_FIELDS = (
    "resume_text",
    "jd_text",
    "model",
    "reasoning",
    "job_search",
    "deadline_epoch",
    "admission_id",
    "credential_ref",
    "session_hash",
    "mock",
)


def _paid_llm_step(func):
    """Register a paid model step without SDK-level replay retries."""
    step = wf.step(func)
    step.max_retries = 0
    return step


def _run_tool_sync(tool_name, arguments, settings):
    from runtime_context import use_run_settings
    from tools import execute_tool

    with use_run_settings(settings):
        return execute_tool(tool_name, arguments)


async def _run_tool(
    tool_name,
    arguments,
    model,
    reasoning,
    deadline_epoch,
    credential_ref=None,
    mock=None,
):
    from runtime_context import RunSettings

    api_key = None
    if credential_ref:
        try:
            from public_security import decrypt_api_key
            from quota_store import QuotaStore

            encrypted_value = await QuotaStore.from_env().get_credential(
                credential_ref
            )
            if not encrypted_value:
                raise ValueError("credential unavailable")
            api_key = decrypt_api_key(
                encrypted_value,
                os.environ.get("AGENT_RUN_SIGNING_KEY", ""),
            )
        except Exception:
            raise RuntimeError("workflow credential unavailable") from None

    settings = RunSettings(
        model,
        reasoning,
        deadline_epoch,
        api_key=api_key,
        mock=mock,
    )
    started = time.monotonic()
    result = await asyncio.to_thread(_run_tool_sync, tool_name, arguments, settings)
    if isinstance(result, dict):
        return {**result, "_duration_ms": max(0, int((time.monotonic() - started) * 1000))}
    return result


@wf.step
async def step_run_id():
    return get_step_metadata().run_id


@wf.step
async def step_trace_stage(run_id, stage_id, status, data):
    """Persist one redacted trace document outside the workflow sandbox."""
    await TraceStore().write_stage(
        run_id, stage_id, {"status": status, **dict(data or {})},
    )
    return True


@wf.step
async def step_trace_cancelled(run_id):
    """Read the cooperative-cancellation marker outside the workflow sandbox."""
    return await TraceStore().is_cancelled(run_id)


@wf.step
async def step_run_boundary(run_id, deadline_epoch):
    """Evaluate cancellation and wall-clock deadline outside the sandbox."""
    if await TraceStore().is_cancelled(run_id):
        return {"status": "cancelled", "remaining_seconds": 0.0}
    if deadline_epoch is None:
        return {"status": None, "remaining_seconds": None}
    remaining = float(deadline_epoch) - time.time()
    return {
        "status": "deadline_exceeded" if remaining <= 0 else None,
        "remaining_seconds": max(0.0, remaining),
    }


async def _require_public_binding(run_id, admission_id, session_hash, *, store=None):
    if not run_id or not admission_id or not session_hash:
        raise RuntimeError("public run binding unavailable")
    if store is None:
        from quota_store import QuotaStore

        store = QuotaStore.from_env()
    try:
        if await store.owns_run(run_id, session_hash):
            return True
        for delay in _BINDING_RETRY_DELAYS:
            await asyncio.sleep(delay)
            if await store.owns_run(run_id, session_hash):
                return True
    except Exception:
        pass
    raise RuntimeError("public run binding unavailable") from None


@wf.step
async def step_require_public_binding(run_id, admission_id, session_hash):
    return await _require_public_binding(run_id, admission_id, session_hash)


@_paid_llm_step
async def step_extract(
    resume_text, model, reasoning, deadline_epoch, credential_ref=None, mock=None,
):
    return await _run_tool(
        "extract_resume_info", {"resume_text": resume_text},
        model, reasoning, deadline_epoch, credential_ref, mock,
    )


@_paid_llm_step
async def step_analyze_jd(
    jd_text, model, reasoning, deadline_epoch, credential_ref=None, mock=None,
):
    return await _run_tool(
        "analyze_jd", {"jd_text": jd_text}, model, reasoning, deadline_epoch,
        credential_ref, mock,
    )


@_paid_llm_step
async def step_match(
    resume_info, jd_analysis, model, reasoning, deadline_epoch,
    credential_ref=None, mock=None,
):
    return await _run_tool(
        "calculate_match",
        {"resume_info": resume_info, "jd_analysis": jd_analysis},
        model, reasoning, deadline_epoch, credential_ref, mock,
    )


@_paid_llm_step
async def step_suggest(resume_info, jd_analysis, match_result, fix_instructions,
                       model, reasoning, deadline_epoch, credential_ref=None,
                       mock=None):
    arguments = {
        "resume_info": resume_info,
        "jd_analysis": jd_analysis,
        "match_result": match_result,
    }
    if fix_instructions:
        arguments["fix_instructions"] = list(fix_instructions)
    return await _run_tool(
        "generate_suggestions", arguments, model, reasoning, deadline_epoch,
        credential_ref, mock,
    )


@_paid_llm_step
async def step_verify(resume_info, jd_analysis, match_result, suggestions,
                      model, reasoning, deadline_epoch, credential_ref=None,
                      mock=None):
    return await _run_tool(
        "verify_output",
        {
            "resume_info": resume_info,
            "jd_analysis": jd_analysis,
            "match_result": match_result,
            "suggestions": suggestions,
        },
        model, reasoning, deadline_epoch, credential_ref, mock,
    )


async def _finalize_public_run(
    run_id,
    admission_id,
    credential_ref,
    status,
    safe_to_deliver,
    *,
    store=None,
):
    if store is None:
        from quota_store import QuotaStore

        store = QuotaStore.from_env()

    failed = False
    try:
        updated = await store.update_run(run_id, status, safe_to_deliver)
        for delay in _UPDATE_RUN_RETRY_DELAYS:
            if updated:
                break
            await asyncio.sleep(delay)
            updated = await store.update_run(run_id, status, safe_to_deliver)
        if not updated:
            failed = True
    except Exception:
        failed = True
    try:
        await store.release(admission_id, refund_daily=False)
    except Exception:
        failed = True
    try:
        await store.delete_credential(credential_ref)
    except Exception:
        failed = True

    if failed:
        raise RuntimeError("public run finalization failed") from None
    return True


@wf.step
async def step_finalize_public_run(
    run_id, admission_id, credential_ref, status, safe_to_deliver,
):
    return await _finalize_public_run(
        run_id,
        admission_id,
        credential_ref,
        status,
        safe_to_deliver,
    )


class VercelOperations:
    """Durable-step adapter exposing the graph's operation surface."""

    def __init__(
        self, model, reasoning, deadline_epoch, credential_ref=None, mock=None,
    ):
        self._model = model
        self._reasoning = reasoning
        self._deadline = deadline_epoch
        self._credential_ref = credential_ref
        self._mock = mock

    async def extract(self, resume_text):
        return await step_extract(
            resume_text, self._model, self._reasoning, self._deadline,
            self._credential_ref, self._mock,
        )

    async def analyze_jd(self, jd_text):
        return await step_analyze_jd(
            jd_text, self._model, self._reasoning, self._deadline,
            self._credential_ref, self._mock,
        )

    async def match(self, resume_info, jd_analysis):
        return await step_match(
            resume_info, jd_analysis, self._model, self._reasoning, self._deadline,
            self._credential_ref, self._mock,
        )

    async def suggest(self, resume_info, jd_analysis, match_result, fix_instructions=None):
        return await step_suggest(
            resume_info, jd_analysis, match_result, fix_instructions,
            self._model, self._reasoning, self._deadline,
            self._credential_ref, self._mock,
        )

    async def verify(self, resume_info, jd_analysis, match_result, suggestions):
        return await step_verify(
            resume_info, jd_analysis, match_result, suggestions,
            self._model, self._reasoning, self._deadline,
            self._credential_ref, self._mock,
        )


class RunTrace:
    """Bridge the graph's trace surface onto the Private Blob store.

    Stage-status writes are best-effort. Boundary checks are fail-closed because
    silently losing cancellation or deadline state could start more paid work.
    """

    def __init__(self, run_id):
        self._run_id = run_id

    async def stage(self, stage_id, status, **data):
        try:
            await step_trace_stage(self._run_id, stage_id, status, data)
        except Exception:
            pass

    async def cancelled(self):
        try:
            return bool(await step_trace_cancelled(self._run_id))
        except Exception:
            return False

    async def check_boundary(self, deadline_epoch):
        result = await step_run_boundary(self._run_id, deadline_epoch)
        if not isinstance(result, dict):
            raise RuntimeError("workflow boundary step returned an invalid result")
        return result


@wf.workflow
async def resume_workflow(payload):
    run_id = await step_run_id()
    managed_public_run = bool(
        payload.get("admission_id") or payload.get("session_hash")
    )
    if managed_public_run:
        await step_require_public_binding(
            run_id,
            payload.get("admission_id"),
            payload.get("session_hash"),
        )
    trace = RunTrace(run_id)
    operations = VercelOperations(
        payload.get("model"),
        payload.get("reasoning"),
        payload.get("deadline_epoch"),
        payload.get("credential_ref"),
        payload.get("mock"),
    )
    try:
        result = await run_workflow_graph(payload, operations, trace)
    except Exception:
        if managed_public_run:
            try:
                await step_finalize_public_run(
                    run_id,
                    payload.get("admission_id"),
                    payload.get("credential_ref"),
                    "failed",
                    False,
                )
            except Exception:
                pass
        raise

    status = "failed"
    safe_to_deliver = False
    if isinstance(result, dict):
        status = str(result.get("status") or "failed")
        safe_to_deliver = bool(result.get("safe_to_deliver"))
    if managed_public_run:
        try:
            await step_finalize_public_run(
                run_id,
                payload.get("admission_id"),
                payload.get("credential_ref"),
                status,
                safe_to_deliver,
            )
        except Exception:
            pass
    return result


async def start_resume_run(payload):
    """Start the durable workflow and return the SDK ``Run`` handle."""
    from vercel.workflow import start

    if not isinstance(payload, dict) or any(
        key not in _PUBLIC_WORKFLOW_PAYLOAD_FIELDS for key in payload
    ):
        raise ValueError("invalid workflow payload")
    managed_public_payload = any(
        key in payload
        for key in ("admission_id", "credential_ref", "session_hash", "mock")
    )
    credential_ref = payload.get("credential_ref")
    if managed_public_payload and (
        not isinstance(payload.get("admission_id"), str)
        or not payload.get("admission_id")
        or not isinstance(payload.get("session_hash"), str)
        or not payload.get("session_hash")
        or not isinstance(payload.get("mock"), bool)
        or (
            credential_ref is not None
            and (not isinstance(credential_ref, str) or not credential_ref)
        )
    ):
        raise ValueError("invalid workflow payload")
    public_payload = {
        key: payload[key]
        for key in _PUBLIC_WORKFLOW_PAYLOAD_FIELDS
        if key in payload
    }
    return await start(resume_workflow, public_payload)
