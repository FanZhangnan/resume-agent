"""Control-flow tests for the durable eight-stage workflow graph.

These exercise the pure ``run_workflow_graph`` with fake operations and a fake
trace sink, so no Vercel replay context or live LLM is required. An integration
case runs the graph against the real tool adapters under AGENT_MOCK=1.
"""

import asyncio
import importlib
import os

os.environ.setdefault("AGENT_WORKFLOW_TEST", "1")
os.environ.setdefault("AGENT_MOCK", "1")

from workflows.graph import run_workflow_graph  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# ---- fakes ------------------------------------------------------------
_RESUME_INFO = {
    "basic_info": {"name": "张三", "email": "z@example.com"},
    "work_experience": [
        {"company": "A", "title": "工程师", "responsibilities": ["带队交付"],
         "achievements": ["提升转化 20%"]}
    ],
    "skills": ["Python"],
}
_JD = {"job_title": "后端工程师", "hard_requirements": ["Python"], "keywords": ["Python"],
       "gates": {"location": {"required": False, "accepted_values": []},
                 "work_authorization": {"required": False, "accepted_values": []}}}
_MATCH = {"score": 80, "high_matches": [{"requirement": "Python"}], "eligible": True}
_SUGG_OK = {"overall_strategy": "s", "optimized_resume": "张三\n后端工程师\n带队交付，提升转化20%"}
_VERIFY_OK = {"passed": True, "safe_to_deliver": True, "required_fixes": []}
_VERIFY_BAD = {"passed": False, "safe_to_deliver": False, "required_fixes": ["删除夸大表述"]}


class FakeTrace:
    def __init__(self, cancel_after=None):
        self.writes = []          # (stage_id, status)
        self._cancel_after = cancel_after
        self._checks = 0

    async def stage(self, stage_id, status, **data):
        self.writes.append((stage_id, status))

    async def cancelled(self):
        self._checks += 1
        if self._cancel_after is not None and self._checks > self._cancel_after:
            return True
        return False

    async def check_boundary(self, deadline_epoch):
        cancelled = await self.cancelled()
        return {
            "status": "cancelled" if cancelled else None,
            "remaining_seconds": None,
        }

    def statuses(self, stage_id):
        return [s for sid, s in self.writes if sid == stage_id]


class FakeOps:
    def __init__(self, *, verify_sequence=None, fail=None):
        self.calls = []
        self._verify_sequence = list(verify_sequence or [_VERIFY_OK])
        self._fail = fail or set()

    async def extract(self, resume_text):
        self.calls.append("extract")
        if "extract" in self._fail:
            return {"success": False, "error": "boom"}
        return {"success": True, "resume_info": _RESUME_INFO}

    async def analyze_jd(self, jd_text):
        self.calls.append("analyze_jd")
        if "analyze_jd" in self._fail:
            return {"success": False, "error": "boom"}
        return {"success": True, "jd_analysis": _JD}

    async def match(self, resume_info, jd_analysis):
        self.calls.append("match")
        if "match" in self._fail:
            return {"success": False, "error": "boom"}
        return {"success": True, "match_result": _MATCH}

    async def suggest(self, resume_info, jd_analysis, match_result, fix_instructions=None):
        self.calls.append(("suggest", tuple(fix_instructions or ())))
        if "suggest" in self._fail:
            return {"success": False, "error": "boom"}
        return {"success": True, "suggestions": dict(_SUGG_OK)}

    async def verify(self, resume_info, jd_analysis, match_result, suggestions):
        self.calls.append("verify")
        result = self._verify_sequence.pop(0) if self._verify_sequence else _VERIFY_OK
        return {"success": True, "verification": dict(result)}


def _payload(**kw):
    base = {"resume_text": "张三 后端工程师", "jd_text": "招后端工程师，需要 Python",
            "model": "gpt-5.5", "reasoning": "xhigh", "deadline_epoch": None,
            "run_id": "run-test"}
    base.update(kw)
    return base


# ---- tests ------------------------------------------------------------
def test_supplied_jd_happy_path_completes():
    ops, trace = FakeOps(), FakeTrace()
    result = run(run_workflow_graph(_payload(), ops, trace))
    assert result["status"] == "completed"
    assert result["safe_to_deliver"] is True
    assert result["model"] == "gpt-5.5" and result["reasoning"] == "xhigh"
    assert "# 简历优化报告" in result["report"]
    # Order: extract + analyze_jd before match/suggest/verify.
    names = [c if isinstance(c, str) else c[0] for c in ops.calls]
    assert names.index("extract") < names.index("match")
    assert names.index("analyze_jd") < names.index("match")
    assert names.index("match") < names.index("suggest") < names.index("verify")
    # Stage 3 is skipped when a JD is supplied.
    assert "skipped" in trace.statuses(3)
    # Stage 8 report rendered.
    assert "completed" in trace.statuses(8)


def test_graph_runs_inside_real_workflow_sandbox():
    from vercel._internal.workflow.py_sandbox import workflow_sandbox

    async def scenario():
        with workflow_sandbox(random_seed="graph-sandbox"):
            sandboxed_graph = importlib.import_module("workflows.graph")
            return await sandboxed_graph.run_workflow_graph(
                _payload(), FakeOps(), FakeTrace()
            )

    result = run(scenario())
    assert result["status"] == "completed"


def test_missing_time_returns_deadline_without_invoking_tools():
    ops, trace = FakeOps(), FakeTrace()
    # Deadline already in the past.
    result = run(run_workflow_graph(_payload(deadline_epoch=1000.0), ops, trace,
                                    clock=lambda: 2000.0))
    assert result["status"] == "deadline_exceeded"
    assert ops.calls == []          # no tool invoked
    assert result["safe_to_deliver"] is False


def test_provider_failure_produces_partial_not_exception():
    ops, trace = FakeOps(fail={"match"}), FakeTrace()
    result = run(run_workflow_graph(_payload(), ops, trace))
    assert result["status"] == "partial"
    assert result["safe_to_deliver"] is False
    assert "failed" in trace.statuses(5)
    assert "本报告不完整" in result["report"]


def test_match_timeout_category_survives_into_stage_trace():
    class TimeoutMatchOps(FakeOps):
        async def match(self, resume_info, jd_analysis):
            self.calls.append("match")
            return {
                "success": False,
                "error": "private upstream timeout detail",
                "error_category": "timeout",
            }

    class CapturingTrace(FakeTrace):
        def __init__(self):
            super().__init__()
            self.details = []

        async def stage(self, stage_id, status, **data):
            await super().stage(stage_id, status, **data)
            self.details.append((stage_id, status, data))

    trace = CapturingTrace()
    result = run(run_workflow_graph(_payload(), TimeoutMatchOps(), trace))

    assert result["status"] == "partial"
    failed_match = next(
        data for stage_id, status, data in trace.details
        if stage_id == 5 and status == "failed"
    )
    assert failed_match["error_category"] == "timeout"
    assert "private upstream timeout detail" not in str(trace.details)


def test_foundation_failure_is_failed_without_empty_report():
    ops = FakeOps(fail={"extract", "analyze_jd"})
    trace = FakeTrace()
    result = run(run_workflow_graph(_payload(), ops, trace))

    assert result["status"] == "failed"
    assert result["safe_to_deliver"] is False
    assert result["report"] == ""
    for stage_id in (5, 6, 7):
        assert trace.statuses(stage_id) == ["skipped"]
    assert trace.statuses(8) == ["failed"]


def test_cancellation_stops_at_next_boundary():
    ops = FakeOps()
    # Allow the first two boundary checks, cancel before stage 5 (match).
    trace = FakeTrace(cancel_after=1)
    result = run(run_workflow_graph(_payload(), ops, trace))
    assert result["status"] == "cancelled"
    names = [c if isinstance(c, str) else c[0] for c in ops.calls]
    assert "match" not in names       # stopped before match ran


def test_boundary_failure_stops_before_paid_operations():
    class BrokenBoundaryTrace(FakeTrace):
        async def check_boundary(self, deadline_epoch):
            raise RuntimeError("boundary step failed after retries")

    ops = FakeOps()
    try:
        run(run_workflow_graph(_payload(), ops, BrokenBoundaryTrace()))
    except RuntimeError as error:
        assert "boundary step failed" in str(error)
    else:
        raise AssertionError("boundary failure was silently ignored")
    assert ops.calls == []


def test_targeted_repair_runs_once_and_recovers():
    ops = FakeOps(verify_sequence=[_VERIFY_BAD, _VERIFY_OK])
    trace = FakeTrace()
    result = run(run_workflow_graph(_payload(), ops, trace))
    assert result["status"] == "completed"
    # suggest called twice (initial + repair), verify called twice.
    suggest_calls = [c for c in ops.calls if isinstance(c, tuple) and c[0] == "suggest"]
    assert len(suggest_calls) == 2
    assert suggest_calls[1][1] == ("删除夸大表述",)     # repair passed the fixes
    assert ops.calls.count("verify") == 2


def test_cancellation_after_repair_skips_second_verification():
    ops = FakeOps(verify_sequence=[_VERIFY_BAD, _VERIFY_OK])
    # Checks occur before initial work, match, suggest, verify repair, then re-verify.
    trace = FakeTrace(cancel_after=4)
    result = run(run_workflow_graph(_payload(), ops, trace))
    assert result["status"] == "cancelled"
    assert ops.calls.count("verify") == 1


def test_unrecovered_verification_is_partial_with_fixes():
    ops = FakeOps(verify_sequence=[_VERIFY_BAD, _VERIFY_BAD])
    trace = FakeTrace()
    result = run(run_workflow_graph(_payload(), ops, trace))
    assert result["status"] == "partial"
    assert result["safe_to_deliver"] is False
    assert any("删除夸大表述" in f for f in result["unresolved_fixes"])


def test_no_jd_path_is_disabled_in_preview():
    ops, trace = FakeOps(), FakeTrace()
    result = run(run_workflow_graph(_payload(jd_text="", job_search=True), ops, trace))
    assert result["status"] == "partial"
    assert ops.calls == []
    assert "failed" in trace.statuses(3)


def test_sequential_fallback_matches_parallel():
    ops, trace = FakeOps(), FakeTrace()
    result = run(run_workflow_graph(_payload(), ops, trace, parallel=False))
    assert result["status"] == "completed"
    names = [c if isinstance(c, str) else c[0] for c in ops.calls]
    assert names[:2] == ["extract", "analyze_jd"]


def test_integration_real_tools_under_mock():
    """The real (non-durable) tool adapter drives a full run under AGENT_MOCK=1."""
    from workflows.graph import ToolOperations

    class MemTrace:
        def __init__(self): self.writes = []
        async def stage(self, stage_id, status, **data): self.writes.append((stage_id, status))
        async def cancelled(self): return False
        async def check_boundary(self, deadline_epoch):
            return {"status": None, "remaining_seconds": None}

    ops = ToolOperations("gpt-5.5", "xhigh")
    trace = MemTrace()
    result = run(run_workflow_graph(_payload(), ops, trace))
    assert result["status"] in {"completed", "partial"}
    assert "# 简历优化报告" in result["report"]
    assert result["model"] == "gpt-5.5"


if __name__ == "__main__":
    tests = [v for n, v in sorted(globals().items()) if n.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} workflow tests passed")
