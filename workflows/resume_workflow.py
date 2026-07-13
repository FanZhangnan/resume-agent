"""Top-level Vercel durable workflow and steps for the resume agent.

Every decorated function lives at module scope with a stable name because
``module + qualname`` forms the persisted workflow/step id. Each tool step runs
the existing synchronous tool code on a worker thread inside the per-run model
settings, so the deterministic pipeline logic is reused unchanged. Ordering,
deadlines, cancellation, and delivery gates live in ``workflows.graph`` so the
same control flow is unit-tested without a Vercel replay context.
"""

import asyncio
import time

from vercel.workflow import get_step_metadata

from vercel_trace import TraceStore
from workflows.graph import run_workflow_graph
from workflows.runtime import wf


def _run_tool_sync(tool_name, arguments, settings):
    from runtime_context import use_run_settings
    from tools import execute_tool

    with use_run_settings(settings):
        return execute_tool(tool_name, arguments)


async def _run_tool(tool_name, arguments, model, reasoning, deadline_epoch):
    from runtime_context import RunSettings

    settings = RunSettings(model, reasoning, deadline_epoch)
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


@wf.step
async def step_extract(resume_text, model, reasoning, deadline_epoch):
    return await _run_tool(
        "extract_resume_info", {"resume_text": resume_text},
        model, reasoning, deadline_epoch,
    )


@wf.step
async def step_analyze_jd(jd_text, model, reasoning, deadline_epoch):
    return await _run_tool(
        "analyze_jd", {"jd_text": jd_text}, model, reasoning, deadline_epoch,
    )


@wf.step
async def step_match(resume_info, jd_analysis, model, reasoning, deadline_epoch):
    return await _run_tool(
        "calculate_match",
        {"resume_info": resume_info, "jd_analysis": jd_analysis},
        model, reasoning, deadline_epoch,
    )


@wf.step
async def step_suggest(resume_info, jd_analysis, match_result, fix_instructions,
                       model, reasoning, deadline_epoch):
    arguments = {
        "resume_info": resume_info,
        "jd_analysis": jd_analysis,
        "match_result": match_result,
    }
    if fix_instructions:
        arguments["fix_instructions"] = list(fix_instructions)
    return await _run_tool(
        "generate_suggestions", arguments, model, reasoning, deadline_epoch,
    )


@wf.step
async def step_verify(resume_info, jd_analysis, match_result, suggestions,
                      model, reasoning, deadline_epoch):
    return await _run_tool(
        "verify_output",
        {
            "resume_info": resume_info,
            "jd_analysis": jd_analysis,
            "match_result": match_result,
            "suggestions": suggestions,
        },
        model, reasoning, deadline_epoch,
    )


class VercelOperations:
    """Durable-step adapter exposing the graph's operation surface."""

    def __init__(self, model, reasoning, deadline_epoch):
        self._model = model
        self._reasoning = reasoning
        self._deadline = deadline_epoch

    async def extract(self, resume_text):
        return await step_extract(resume_text, self._model, self._reasoning, self._deadline)

    async def analyze_jd(self, jd_text):
        return await step_analyze_jd(jd_text, self._model, self._reasoning, self._deadline)

    async def match(self, resume_info, jd_analysis):
        return await step_match(
            resume_info, jd_analysis, self._model, self._reasoning, self._deadline,
        )

    async def suggest(self, resume_info, jd_analysis, match_result, fix_instructions=None):
        return await step_suggest(
            resume_info, jd_analysis, match_result, fix_instructions,
            self._model, self._reasoning, self._deadline,
        )

    async def verify(self, resume_info, jd_analysis, match_result, suggestions):
        return await step_verify(
            resume_info, jd_analysis, match_result, suggestions,
            self._model, self._reasoning, self._deadline,
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
    trace = RunTrace(run_id)
    operations = VercelOperations(
        payload.get("model"), payload.get("reasoning"), payload.get("deadline_epoch"),
    )
    return await run_workflow_graph(payload, operations, trace)


async def start_resume_run(payload):
    """Start the durable workflow and return the SDK ``Run`` handle."""
    from vercel.workflow import start

    return await start(resume_workflow, payload)
