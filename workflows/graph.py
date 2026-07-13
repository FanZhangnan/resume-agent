"""Deterministic eight-stage workflow orchestration.

This module contains no Vercel imports so it can be unit-tested directly and run
under any Python. ``run_workflow_graph`` owns stage ordering, deadline and
cancellation boundaries, the strict delivery gate, and pure report rendering.
The concrete tool execution is supplied as an injected ``operations`` object so
production uses durable Vercel steps while tests use fakes.
"""

import asyncio
import os
import time

import config
from contracts import (
    delivery_is_complete,
    suggestions_are_usable,
    verification_is_deliverable,
)
from report_renderer import render_report
from utils import parse_resume_text_to_struct

# UI stage identifiers (stage 1 parse happens in the API before start).
STAGE_EXTRACT = 2
STAGE_DISCOVER = 3
STAGE_JD = 4
STAGE_MATCH = 5
STAGE_SUGGEST = 6
STAGE_VERIFY = 7
STAGE_REPORT = 8

# Minimum wall-clock seconds required to attempt one targeted repair round.
_REPAIR_MIN_SECONDS = 20.0

_STATE_KEYS = (
    "resume_info", "jd_analysis", "match_result", "suggestions", "verification",
)


def _new_state(payload):
    return {
        "resume_text": str(payload.get("resume_text") or ""),
        "resume_info": None,
        "jd_analysis": None,
        "match_result": None,
        "suggestions": None,
        "verification": None,
        "job_recommendations": None,
        "correction_log": [],
        "user_clarifications": [],
    }


def _with_struct_fallback(suggestions):
    """Mirror the agent's guarantee that a usable struct backs the resume text."""
    suggestions = suggestions or {}
    struct = suggestions.get("optimized_resume_struct")
    has_struct = isinstance(struct, dict) and any(
        struct.get(key) for key in ("education", "experience", "projects")
    )
    if suggestions and not has_struct:
        parsed = parse_resume_text_to_struct(suggestions.get("optimized_resume") or "")
        if parsed:
            suggestions = {**suggestions, "optimized_resume_struct": parsed}
    return suggestions


def _required_fixes(verification):
    verification = verification or {}
    fixes = [str(item) for item in (verification.get("required_fixes") or []) if str(item).strip()]
    return fixes


def _unresolved_fixes(state):
    if delivery_is_complete(state.get("verification"), state.get("suggestions")):
        return []
    fixes = []
    extra = state.get("_extra_fix")
    if extra:
        fixes.append(str(extra))
    fixes.extend(_required_fixes(state.get("verification")))
    if not verification_is_deliverable(state.get("verification")) and not fixes:
        fixes.append("验证结果未满足严格交付契约")
    if not suggestions_are_usable(state.get("suggestions")):
        issue = "优化版简历未生成或结构无效"
        if issue not in fixes:
            fixes.append(issue)
    return fixes


def _error_category(result):
    if not isinstance(result, dict):
        return "invalid_result"
    if getattr(result, "get", None) and result.get("error"):
        return "tool_error"
    return "unsuccessful"


def _category_from_exception(error):
    if getattr(error, "is_run_deadline", False):
        return "deadline"
    return type(error).__name__


async def _run_stage(trace, stage_id, op, args, key, clock, revision_round=None):
    """Execute one stage operation and record its trace. Never raises for
    ordinary tool failures; returns ('completed'|'failed', value)."""
    started = clock() if clock is not None else None
    running = {}
    if revision_round is not None:
        running["revision_round"] = revision_round
    await trace.stage(stage_id, "running", **running)
    try:
        result = await op(*args)
    except Exception as error:  # noqa: BLE001 - convert to a bounded stage failure
        data = {"error_category": _category_from_exception(error)}
        if revision_round is not None:
            data["revision_round"] = revision_round
        await trace.stage(stage_id, "failed", **data)
        return "failed", None
    if clock is not None:
        duration_ms = max(0, int((clock() - started) * 1000))
    elif isinstance(result, dict) and isinstance(result.get("_duration_ms"), (int, float)):
        duration_ms = max(0, int(result["_duration_ms"]))
    else:
        duration_ms = None
    ok = (
        isinstance(result, dict)
        and result.get("success") is True
        and result.get(key) is not None
    )
    if not ok:
        data = {"error_category": _error_category(result)}
        if duration_ms is not None:
            data["duration_ms"] = duration_ms
        if revision_round is not None:
            data["revision_round"] = revision_round
        await trace.stage(stage_id, "failed", **data)
        return "failed", None
    data = {}
    if duration_ms is not None:
        data["duration_ms"] = duration_ms
    if revision_round is not None:
        data["revision_round"] = revision_round
    await trace.stage(stage_id, "completed", **data)
    return "completed", result.get(key)


def _render_state(state, analysis_engine, cancelled):
    rendered = dict(state)
    rendered["analysis_engine"] = analysis_engine
    if cancelled:
        rendered["interrupted_error"] = "运行已被取消"
    return rendered


async def _finalize(trace, state, raw_status, model, reasoning):
    deliverable = delivery_is_complete(state.get("verification"), state.get("suggestions"))
    if raw_status == "completed":
        final_status = "completed" if deliverable else "partial"
    else:
        final_status = raw_status
    unresolved = _unresolved_fixes(state)
    render_status = {
        "completed": "completed",
        "deadline_exceeded": "deadline",
    }.get(final_status, "partial")

    if final_status == "failed":
        report = ""
        await trace.stage(
            STAGE_REPORT, "failed", reason=final_status, safe_to_deliver=False,
        )
    else:
        await trace.stage(STAGE_REPORT, "running")
        report = render_report(
            _render_state(state, model, final_status == "cancelled"),
            render_status,
            unresolved or None,
        )
        await trace.stage(
            STAGE_REPORT, "completed",
            reason=final_status, safe_to_deliver=deliverable,
        )
    return {
        "status": final_status,
        "safe_to_deliver": deliverable,
        "report": report,
        "unresolved_fixes": unresolved,
        "model": model,
        "reasoning": reasoning,
    }


async def run_workflow_graph(payload, operations, trace, *, clock=None, parallel=None):
    """Run the supplied-JD eight-stage graph to a bounded terminal state."""
    durable_boundaries = clock is None and hasattr(trace, "check_boundary")
    if clock is None and not durable_boundaries:
        clock = time.time
    model, reasoning = config.validate_model_reasoning(
        payload.get("model"), payload.get("reasoning")
    )
    deadline_epoch = payload.get("deadline_epoch")
    jd_text = str(payload.get("jd_text") or "")
    job_search = bool(payload.get("job_search"))
    if parallel is None:
        parallel = os.environ.get("AGENT_WORKFLOW_PARALLEL", "1") != "0"

    state = _new_state(payload)

    def remaining():
        if deadline_epoch is None:
            return float("inf")
        if clock is None:
            return float("inf")
        return float(deadline_epoch) - clock()

    last_remaining = remaining()

    async def boundary():
        """Return a terminal status if the run must stop before the next stage."""
        nonlocal last_remaining
        if durable_boundaries:
            result = await trace.check_boundary(deadline_epoch)
            if not isinstance(result, dict):
                raise RuntimeError("workflow boundary returned an invalid result")
            value = result.get("remaining_seconds")
            last_remaining = float("inf") if value is None else float(value)
            return result.get("status")
        if await trace.cancelled():
            return "cancelled"
        last_remaining = remaining()
        if last_remaining <= 0:
            return "deadline_exceeded"
        return None

    # No-JD live discovery is not enabled in this preview.
    if job_search or not jd_text.strip():
        await trace.stage(
            STAGE_DISCOVER, "failed",
            reason="nojd_disabled", error_category="not_enabled",
        )
        state["_extra_fix"] = "未提供目标 JD；当前预览未启用在招岗位发现。"
        return await _finalize(trace, state, "partial", model, reasoning)

    # Stage 3 is skipped because a JD was supplied.
    await trace.stage(STAGE_DISCOVER, "skipped", reason="jd_supplied")

    term = await boundary()
    if term:
        return await _finalize(trace, state, term, model, reasoning)

    async def extract_stage():
        return await _run_stage(
            trace, STAGE_EXTRACT, operations.extract, (state["resume_text"],),
            "resume_info", clock,
        )

    async def jd_stage():
        return await _run_stage(
            trace, STAGE_JD, operations.analyze_jd, (jd_text,), "jd_analysis", clock,
        )

    if parallel:
        (s2, v2), (s4, v4) = await asyncio.gather(extract_stage(), jd_stage())
    else:
        s2, v2 = await extract_stage()
        s4, v4 = await jd_stage()
    if s2 == "completed":
        state["resume_info"] = v2
    if s4 == "completed":
        state["jd_analysis"] = v4
    if s2 != "completed" or s4 != "completed":
        state["_extra_fix"] = "基础分析未完成，请稍后重试；如持续失败请联系管理员。"
        for stage_id in (STAGE_MATCH, STAGE_SUGGEST, STAGE_VERIFY):
            await trace.stage(stage_id, "skipped", reason="upstream_failed")
        return await _finalize(trace, state, "failed", model, reasoning)

    term = await boundary()
    if term:
        return await _finalize(trace, state, term, model, reasoning)
    s5, v5 = await _run_stage(
        trace, STAGE_MATCH, operations.match,
        (state["resume_info"], state["jd_analysis"]), "match_result", clock,
    )
    if s5 != "completed":
        return await _finalize(trace, state, "partial", model, reasoning)
    state["match_result"] = v5

    term = await boundary()
    if term:
        return await _finalize(trace, state, term, model, reasoning)
    s6, v6 = await _run_stage(
        trace, STAGE_SUGGEST, operations.suggest,
        (state["resume_info"], state["jd_analysis"], state["match_result"], None),
        "suggestions", clock,
    )
    if s6 != "completed":
        return await _finalize(trace, state, "partial", model, reasoning)
    state["suggestions"] = _with_struct_fallback(v6)

    term = await boundary()
    if term:
        return await _finalize(trace, state, term, model, reasoning)
    s7, v7 = await _run_stage(
        trace, STAGE_VERIFY, operations.verify,
        (state["resume_info"], state["jd_analysis"], state["match_result"],
         state["suggestions"]),
        "verification", clock,
    )
    if s7 != "completed":
        return await _finalize(trace, state, "partial", model, reasoning)
    state["verification"] = v7

    # One targeted repair round if the strict delivery gate is not yet met.
    if not delivery_is_complete(state["verification"], state["suggestions"]):
        term = await boundary()
        if term is None and last_remaining > _REPAIR_MIN_SECONDS:
            fixes = _required_fixes(state["verification"])
            r6s, r6v = await _run_stage(
                trace, STAGE_SUGGEST, operations.suggest,
                (state["resume_info"], state["jd_analysis"], state["match_result"], fixes),
                "suggestions", clock, revision_round=1,
            )
            if r6s == "completed":
                state["suggestions"] = _with_struct_fallback(r6v)
                term = await boundary()
                if term:
                    return await _finalize(trace, state, term, model, reasoning)
                r7s, r7v = await _run_stage(
                    trace, STAGE_VERIFY, operations.verify,
                    (state["resume_info"], state["jd_analysis"], state["match_result"],
                     state["suggestions"]),
                    "verification", clock, revision_round=1,
                )
                if r7s == "completed":
                    state["verification"] = r7v
                resolved = delivery_is_complete(
                    state["verification"], state["suggestions"]
                )
                state["correction_log"].append(
                    {"round": 1, "issues": fixes, "resolved": resolved}
                )

    term = await boundary()
    return await _finalize(trace, state, term or "completed", model, reasoning)


class ToolOperations:
    """Non-durable adapter that runs the real tools on a worker thread.

    Used by the local/integration path. Production uses Vercel durable steps
    (see ``workflows.resume_workflow``) that expose the same method surface.
    """

    def __init__(self, model, reasoning, deadline_epoch=None):
        from runtime_context import RunSettings

        self._settings = RunSettings(model, reasoning, deadline_epoch)

    async def _run(self, tool_name, arguments):
        settings = self._settings

        def call():
            from runtime_context import use_run_settings
            from tools import execute_tool

            with use_run_settings(settings):
                return execute_tool(tool_name, arguments)

        return await asyncio.to_thread(call)

    async def extract(self, resume_text):
        return await self._run("extract_resume_info", {"resume_text": resume_text})

    async def analyze_jd(self, jd_text):
        return await self._run("analyze_jd", {"jd_text": jd_text})

    async def match(self, resume_info, jd_analysis):
        return await self._run(
            "calculate_match",
            {"resume_info": resume_info, "jd_analysis": jd_analysis},
        )

    async def suggest(self, resume_info, jd_analysis, match_result, fix_instructions=None):
        arguments = {
            "resume_info": resume_info,
            "jd_analysis": jd_analysis,
            "match_result": match_result,
        }
        if fix_instructions:
            arguments["fix_instructions"] = list(fix_instructions)
        return await self._run("generate_suggestions", arguments)

    async def verify(self, resume_info, jd_analysis, match_result, suggestions):
        return await self._run(
            "verify_output",
            {
                "resume_info": resume_info,
                "jd_analysis": jd_analysis,
                "match_result": match_result,
                "suggestions": suggestions,
            },
        )
