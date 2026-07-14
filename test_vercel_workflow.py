"""Control-flow tests for the durable eight-stage workflow graph.

These exercise the pure ``run_workflow_graph`` with fake operations and a fake
trace sink, so no Vercel replay context or live LLM is required. An integration
case runs the graph against the real tool adapters under AGENT_MOCK=1.
"""

import asyncio
import importlib
import os
import sys
from pathlib import Path

os.environ.setdefault("AGENT_WORKFLOW_TEST", "1")
os.environ.setdefault("AGENT_MOCK", "1")

from workflows.graph import _required_fixes, run_workflow_graph  # noqa: E402


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
        self.events = []          # (stage_id, status, metadata)
        self._cancel_after = cancel_after
        self._checks = 0

    async def stage(self, stage_id, status, **data):
        self.writes.append((stage_id, status))
        self.events.append((stage_id, status, dict(data)))

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

    def last_event(self, stage_id):
        return next(event for event in reversed(self.events) if event[0] == stage_id)


class DeadlineBeforeFallbackTrace(FakeTrace):
    async def check_boundary(self, deadline_epoch):
        self._checks += 1
        expired = self._checks >= 6
        return {
            "status": "deadline_exceeded" if expired else None,
            "remaining_seconds": 0 if expired else 60,
        }


class LimitedRepairBudgetTrace(FakeTrace):
    async def check_boundary(self, deadline_epoch):
        self._checks += 1
        return {"status": None, "remaining_seconds": 200}


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
        if "verify" in self._fail:
            return {"success": False, "error": "boom"}
        result = self._verify_sequence.pop(0) if self._verify_sequence else _VERIFY_OK
        return {"success": True, "verification": dict(result)}


class StarSuggestionOps(FakeOps):
    async def suggest(self, resume_info, jd_analysis, match_result, fix_instructions=None):
        result = await super().suggest(
            resume_info, jd_analysis, match_result, fix_instructions,
        )
        result["suggestions"]["star_rewrites"] = [{
            "original": "verified original",
            "rewritten": "UNSAFE OPTIONAL STAR TEXT",
        }]
        return result


class RewriteSuggestionOps(FakeOps):
    async def suggest(self, resume_info, jd_analysis, match_result, fix_instructions=None):
        result = await super().suggest(
            resume_info, jd_analysis, match_result, fix_instructions,
        )
        result["suggestions"]["rewrite_suggestions"] = [{
            "section": "个人简介",
            "before": "UNSAFE OPTIONAL REWRITE TEXT",
            "after": "verified rewrite",
        }]
        return result


class UnsafeResumeOps(FakeOps):
    async def suggest(self, resume_info, jd_analysis, match_result, fix_instructions=None):
        result = await super().suggest(
            resume_info, jd_analysis, match_result, fix_instructions,
        )
        result["suggestions"]["optimized_resume"] = (
            "MODEL_ONLY_UNSAFE_CLAIM: 主导亿级项目"
        )
        return result


class RepairSuggestFailsOps(FakeOps):
    async def suggest(self, resume_info, jd_analysis, match_result, fix_instructions=None):
        if fix_instructions:
            self.calls.append(("suggest", tuple(fix_instructions)))
            return {"success": False, "error": "repair unavailable"}
        return await super().suggest(
            resume_info, jd_analysis, match_result, fix_instructions,
        )


class RepairVerifyFailsOps(FakeOps):
    async def verify(self, resume_info, jd_analysis, match_result, suggestions):
        self.calls.append("verify")
        if self.calls.count("verify") == 1:
            return {"success": True, "verification": dict(_VERIFY_BAD)}
        return {"success": False, "error": "repair verification unavailable"}


def _legacy_conservative_suggestions():
    return {
        "generation_mode": "conservative_fallback",
        "overall_strategy": "legacy fallback",
        "rewrite_suggestions": [],
        "star_rewrites": [],
        "keyword_injection": [],
        "honesty_boundaries": [],
        "optimized_resume": "EXTRACTED_ONLY_CLAIM",
        "optimized_resume_struct": {},
    }


class LegacyConservativeOps(FakeOps):
    async def suggest(self, resume_info, jd_analysis, match_result, fix_instructions=None):
        self.calls.append(("suggest", tuple(fix_instructions or ())))
        return {
            "success": True,
            "suggestions": _legacy_conservative_suggestions(),
        }


class RepairLegacyConservativeOps(FakeOps):
    async def suggest(self, resume_info, jd_analysis, match_result, fix_instructions=None):
        if fix_instructions:
            self.calls.append(("suggest", tuple(fix_instructions)))
            return {
                "success": True,
                "suggestions": _legacy_conservative_suggestions(),
                "_duration_ms": 222,
            }
        return await super().suggest(
            resume_info, jd_analysis, match_result, fix_instructions,
        )

    async def verify(self, resume_info, jd_analysis, match_result, suggestions):
        result = await super().verify(
            resume_info, jd_analysis, match_result, suggestions,
        )
        result["_duration_ms"] = 333
        return result


class TimedRecoveryOps(FakeOps):
    async def suggest(self, resume_info, jd_analysis, match_result, fix_instructions=None):
        result = await super().suggest(
            resume_info, jd_analysis, match_result, fix_instructions,
        )
        result["_duration_ms"] = 222 if fix_instructions else 111
        return result

    async def verify(self, resume_info, jd_analysis, match_result, suggestions):
        result = await super().verify(
            resume_info, jd_analysis, match_result, suggestions,
        )
        result["_duration_ms"] = 444 if self.calls.count("verify") == 2 else 333
        return result


class TimedInitialVerifyFailureOps(FakeOps):
    async def suggest(self, resume_info, jd_analysis, match_result, fix_instructions=None):
        result = await super().suggest(
            resume_info, jd_analysis, match_result, fix_instructions,
        )
        result["_duration_ms"] = 111
        return result

    async def verify(self, resume_info, jd_analysis, match_result, suggestions):
        self.calls.append("verify")
        return {
            "success": False,
            "error": "verification unavailable",
            "_duration_ms": 333,
        }


def _payload(**kw):
    base = {"resume_text": "张三 后端工程师", "jd_text": "招后端工程师，需要 Python",
            "model": "gpt-5.5", "reasoning": "xhigh", "deadline_epoch": None,
            "run_id": "run-test"}
    base.update(kw)
    return base


def run_without_fact_fallback(ops, trace=None):
    """Exercise narrow auto-resolution rules without the final production fallback."""
    return run(run_workflow_graph(
        _payload(),
        ops,
        trace or FakeTrace(),
        allow_fact_fallback=False,
    ))


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


def test_fact_only_fallback_runs_inside_cold_workflow_sandbox():
    from vercel._internal.workflow.py_sandbox import workflow_sandbox

    assert "tools.analysis" not in sys.modules

    async def scenario():
        with workflow_sandbox(random_seed="fallback-sandbox"):
            sandboxed_graph = importlib.import_module("workflows.graph")
            return await sandboxed_graph.run_workflow_graph(
                _payload(),
                FakeOps(verify_sequence=[_VERIFY_BAD, _VERIFY_BAD]),
                FakeTrace(),
            )

    result = run(scenario())
    assert result["status"] == "completed"
    assert result["safe_to_deliver"] is True


def test_initial_suggestion_failure_uses_original_resume_fallback():
    ops, trace = FakeOps(fail={"suggest"}), FakeTrace()
    original_resume = "ORIGINAL_UPLOAD_ONLY"
    result = run(run_workflow_graph(
        _payload(resume_text=original_resume), ops, trace,
    ))

    assert result["status"] == "completed"
    assert result["safe_to_deliver"] is True
    optimized_resume = result["report"].split("## 【优化版简历】", 1)[1].strip()
    assert optimized_resume == original_resume
    _, status, data = trace.last_event(6)
    assert status == "completed"
    assert data["reason"] == "fact_only_fallback"
    assert data["error_category"] == "tool_error"


def test_initial_verification_failure_uses_original_resume_fallback():
    ops, trace = TimedInitialVerifyFailureOps(), FakeTrace()
    result = run(run_workflow_graph(_payload(), ops, trace))

    assert result["status"] == "completed"
    assert result["safe_to_deliver"] is True
    _, status, data = trace.last_event(7)
    assert status == "completed"
    assert data["reason"] == "fact_only_fallback"
    assert data["error_category"] == "tool_error"
    _, _, suggest_data = trace.last_event(6)
    assert suggest_data["duration_ms"] == 111


def test_legacy_conservative_marker_cannot_bypass_original_resume_fallback():
    original_resume = "ORIGINAL_UPLOAD_ONLY"
    ops, trace = LegacyConservativeOps(), FakeTrace()
    result = run(run_workflow_graph(
        _payload(resume_text=original_resume), ops, trace,
    ))

    assert result["status"] == "completed"
    assert ops.calls.count("verify") == 0
    optimized_resume = result["report"].split("## 【优化版简历】", 1)[1].strip()
    assert optimized_resume == original_resume
    assert "EXTRACTED_ONLY_CLAIM" not in result["report"]


def test_repair_legacy_conservative_marker_uses_original_resume_fallback():
    original_resume = "ORIGINAL_UPLOAD_ONLY"
    ops = RepairLegacyConservativeOps(verify_sequence=[_VERIFY_BAD])
    trace = FakeTrace()
    result = run(run_workflow_graph(
        _payload(resume_text=original_resume), ops, trace,
    ))

    assert result["status"] == "completed"
    assert ops.calls.count("verify") == 1
    optimized_resume = result["report"].split("## 【优化版简历】", 1)[1].strip()
    assert optimized_resume == original_resume
    assert "EXTRACTED_ONLY_CLAIM" not in result["report"]
    _, _, suggest_data = trace.last_event(6)
    assert suggest_data["duration_ms"] == 222
    assert suggest_data["revision_round"] == 1
    _, _, verify_data = trace.last_event(7)
    assert verify_data["duration_ms"] == 333
    assert verify_data["validation_status"] == "rejected_ai_draft"


def test_repair_is_skipped_when_two_calls_do_not_fit_remaining_time():
    ops = FakeOps(verify_sequence=[_VERIFY_BAD, _VERIFY_BAD])
    trace = LimitedRepairBudgetTrace()
    result = run(run_workflow_graph(_payload(deadline_epoch=1000), ops, trace))

    assert result["status"] == "completed"
    assert result["safe_to_deliver"] is True
    assert len([call for call in ops.calls if isinstance(call, tuple)]) == 1
    assert ops.calls.count("verify") == 1
    assert all(
        data.get("revision_round") != 1
        for _, _, data in trace.events
    )
    assert trace.last_event(6)[2]["reason"] == "fact_only_fallback"


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


def test_star_only_verification_removes_optional_section_without_paid_repair():
    star_issue = {
        "passed": False,
        "safe_to_deliver": False,
        "required_fixes": ["删除STAR改写中的推断场景"],
        "fabrication_risks": ["STAR改写中包含未证实场景"],
    }
    ops, trace = StarSuggestionOps(verify_sequence=[star_issue]), FakeTrace()
    result = run(run_workflow_graph(_payload(), ops, trace))

    assert result["status"] == "completed"
    assert result["safe_to_deliver"] is True
    assert "UNSAFE OPTIONAL STAR TEXT" not in result["report"]
    assert "未证实场景" not in result["report"]
    assert len([call for call in ops.calls if isinstance(call, tuple)]) == 1
    assert ops.calls.count("verify") == 1
    assert trace.statuses(7).count("completed") == 1


def test_revision_star_only_residual_is_removed_without_third_llm_call():
    final_star_issue = {
        "passed": False,
        "safe_to_deliver": False,
        "required_fixes": [
            "删除STAR中的推断背景",
            "中性改写STAR中的未证实任务",
        ],
    }
    ops = StarSuggestionOps(verify_sequence=[_VERIFY_BAD, final_star_issue])
    result = run(run_workflow_graph(_payload(), ops, FakeTrace()))

    assert result["status"] == "completed"
    assert result["safe_to_deliver"] is True
    assert "UNSAFE OPTIONAL STAR TEXT" not in result["report"]
    assert len([call for call in ops.calls if isinstance(call, tuple)]) == 2
    assert ops.calls.count("verify") == 2


def test_rewrite_suggestion_only_verification_removes_optional_section():
    rewrite_issue = {
        "passed": False,
        "safe_to_deliver": False,
        "required_fixes": [
            "将rewrite_suggestions第1项before中的“负责店铺运营”"
            "改为原始范围内的活动配置、商品上下架、数据整理。",
            "将rewrite_suggestions第3项before/problem改为保留“协助优化”"
            "及原有12%点击率成果，避免称其未量化。",
        ],
        "overstatement_issues": [
            "rewrite_suggestions第1项before范围过宽。",
            "rewrite_suggestions第3项problem弱化了原始参与边界。",
        ],
    }
    ops, trace = RewriteSuggestionOps(verify_sequence=[rewrite_issue]), FakeTrace()
    result = run(run_workflow_graph(_payload(), ops, trace))

    assert result["status"] == "completed"
    assert result["safe_to_deliver"] is True
    assert "UNSAFE OPTIONAL REWRITE TEXT" not in result["report"]
    assert len([call for call in ops.calls if isinstance(call, tuple)]) == 1
    assert ops.calls.count("verify") == 1
    assert trace.statuses(7).count("completed") == 1


def test_revision_rewrite_suggestion_residual_avoids_third_llm_call():
    final_issue = {
        "passed": False,
        "safe_to_deliver": False,
        "required_fixes": ["rewrite_suggestions第1项before中仍有夸大表述。"],
        "overstatement_issues": ["rewrite_suggestions第1项before范围过宽。"],
    }
    ops = RewriteSuggestionOps(verify_sequence=[_VERIFY_BAD, final_issue])
    result = run(run_workflow_graph(_payload(), ops, FakeTrace()))

    assert result["status"] == "completed"
    assert result["safe_to_deliver"] is True
    assert "UNSAFE OPTIONAL REWRITE TEXT" not in result["report"]
    assert len([call for call in ops.calls if isinstance(call, tuple)]) == 2
    assert ops.calls.count("verify") == 2


def test_rewrite_suggestion_fix_cannot_hide_main_resume_issue():
    mixed_issue = {
        "passed": False,
        "safe_to_deliver": False,
        "required_fixes": [
            "删除rewrite_suggestions和优化版简历正文中的夸大表述。",
        ],
    }
    ops = RewriteSuggestionOps(verify_sequence=[mixed_issue, mixed_issue])
    result = run_without_fact_fallback(ops)

    assert result["status"] == "partial"
    assert result["safe_to_deliver"] is False


def test_rewrite_suggestion_fix_cannot_hide_non_rewrite_risk_row():
    mixed_issue = {
        "passed": False,
        "safe_to_deliver": False,
        "required_fixes": ["rewrite_suggestions第1项before中有夸大表述。"],
        "fabrication_risks": ["优化版简历正文新增了未证实技能。"],
    }
    ops = RewriteSuggestionOps(verify_sequence=[mixed_issue, mixed_issue])
    result = run_without_fact_fallback(ops)

    assert result["status"] == "partial"
    assert result["safe_to_deliver"] is False


def test_rewrite_suggestion_fix_cannot_override_main_resume_assessment():
    conflicting_issue = {
        "passed": False,
        "safe_to_deliver": False,
        "overall_assessment": (
            "优化版简历正文仍有未证实内容，rewrite_suggestions也需修正。"
        ),
        "required_fixes": ["rewrite_suggestions第1项before中有夸大表述。"],
        "overstatement_issues": ["rewrite_suggestions第1项before范围过宽。"],
    }
    ops = RewriteSuggestionOps(
        verify_sequence=[conflicting_issue, conflicting_issue],
    )
    result = run_without_fact_fallback(ops)

    assert result["status"] == "partial"
    assert result["safe_to_deliver"] is False


def test_structured_rewrite_suggestion_risk_is_not_auto_resolved():
    structured_risk = {
        "passed": False,
        "safe_to_deliver": False,
        "required_fixes": ["rewrite_suggestions第1项before中有夸大表述。"],
        "overstatement_issues": [{
            "section": "rewrite_suggestions",
            "issue": "before范围过宽",
        }],
    }
    ops = RewriteSuggestionOps(verify_sequence=[structured_risk, structured_risk])
    result = run_without_fact_fallback(ops)

    assert result["status"] == "partial"
    assert result["safe_to_deliver"] is False


def test_rewrite_suggestion_fix_requires_nonempty_optional_section():
    rewrite_issue = {
        "passed": False,
        "safe_to_deliver": False,
        "required_fixes": ["rewrite_suggestions第1项before中有夸大表述。"],
    }
    ops = FakeOps(verify_sequence=[rewrite_issue, rewrite_issue])
    result = run_without_fact_fallback(ops)

    assert result["status"] == "partial"
    assert result["safe_to_deliver"] is False


def test_rewrite_suggestion_field_name_must_match_exactly():
    backup_issue = {
        "passed": False,
        "safe_to_deliver": False,
        "required_fixes": ["rewrite_suggestions_backup中有夸大表述。"],
    }
    ops = RewriteSuggestionOps(verify_sequence=[backup_issue, backup_issue])
    result = run_without_fact_fallback(ops)

    assert result["status"] == "partial"
    assert result["safe_to_deliver"] is False


def test_rewrite_suggestion_alias_is_not_auto_resolved():
    alias_issue = {
        "passed": False,
        "safe_to_deliver": False,
        "required_fixes": ["逐段修改建议中有夸大表述。"],
    }
    ops = RewriteSuggestionOps(verify_sequence=[alias_issue, alias_issue])
    result = run_without_fact_fallback(ops)

    assert result["status"] == "partial"
    assert result["safe_to_deliver"] is False


def test_rewrite_suggestion_fix_cannot_include_other_optional_section():
    mixed_optional_issue = {
        "passed": False,
        "safe_to_deliver": False,
        "required_fixes": [
            "rewrite_suggestions和star_rewrites中都有夸大表述。",
        ],
    }
    ops = RewriteSuggestionOps(
        verify_sequence=[mixed_optional_issue, mixed_optional_issue],
    )
    result = run_without_fact_fallback(ops)

    assert result["status"] == "partial"
    assert result["safe_to_deliver"] is False


def test_rewrite_suggestion_fix_cannot_include_chinese_optional_sections():
    for section in ("关键词补充", "诚实边界", "总体策略"):
        mixed_issue = {
            "passed": False,
            "safe_to_deliver": False,
            "required_fixes": [
                f"rewrite_suggestions和{section}中都有夸大表述。",
            ],
        }
        ops = RewriteSuggestionOps(verify_sequence=[mixed_issue, mixed_issue])
        result = run_without_fact_fallback(ops)

        assert result["status"] == "partial", section
        assert result["safe_to_deliver"] is False, section


def test_structured_rewrite_suggestion_fix_is_not_auto_resolved():
    structured_issue = {
        "passed": False,
        "safe_to_deliver": False,
        "required_fixes": [{
            "fix": "删除rewrite_suggestions第1项的夸大表述。",
            "issue": "优化稿正文也含虚构内容。",
        }],
    }
    ops = RewriteSuggestionOps(verify_sequence=[structured_issue, structured_issue])
    result = run_without_fact_fallback(ops)

    assert result["status"] == "partial"
    assert result["safe_to_deliver"] is False


def test_star_named_fix_does_not_hide_non_star_risk_rows():
    mixed_issue = {
        "passed": False,
        "safe_to_deliver": False,
        "required_fixes": ["删除STAR改写中的推断场景"],
        "fabrication_risks": ["优化版简历正文新增了未证实技能"],
    }
    ops = StarSuggestionOps(verify_sequence=[mixed_issue, mixed_issue])
    result = run_without_fact_fallback(ops)

    assert result["status"] == "partial"
    assert result["safe_to_deliver"] is False
    assert len([call for call in ops.calls if isinstance(call, tuple)]) == 2


def test_start_prefix_is_not_treated_as_star_section():
    start_issue = {
        "passed": False,
        "safe_to_deliver": False,
        "required_fixes": ["修正START日期字段"],
    }
    ops = StarSuggestionOps(verify_sequence=[start_issue, start_issue])
    result = run_without_fact_fallback(ops)

    assert result["status"] == "partial"
    assert result["safe_to_deliver"] is False


def test_star_and_main_resume_fix_is_not_auto_resolved():
    mixed_fix = {
        "passed": False,
        "safe_to_deliver": False,
        "required_fixes": ["删除STAR改写及优化版简历正文中的虚构内容"],
    }
    ops = StarSuggestionOps(verify_sequence=[mixed_fix, mixed_fix])
    result = run_without_fact_fallback(ops)

    assert result["status"] == "partial"
    assert result["safe_to_deliver"] is False


def test_star_fix_cannot_include_rewrite_suggestion_section():
    for section in ("rewrite_suggestions", "逐段修改建议"):
        mixed_fix = {
            "passed": False,
            "safe_to_deliver": False,
            "required_fixes": [
                f"删除STAR中的推断和{section}的夸大表述。",
            ],
        }
        ops = StarSuggestionOps(verify_sequence=[mixed_fix, mixed_fix])
        result = run_without_fact_fallback(ops)

        assert result["status"] == "partial", section
        assert result["safe_to_deliver"] is False, section


def test_star_and_optimized_draft_fix_is_not_auto_resolved():
    mixed_fix = {
        "passed": False,
        "safe_to_deliver": False,
        "required_fixes": ["删除STAR改写和优化稿中的虚构内容"],
    }
    ops = StarSuggestionOps(verify_sequence=[mixed_fix, mixed_fix])
    result = run_without_fact_fallback(ops)

    assert result["status"] == "partial"
    assert result["safe_to_deliver"] is False


def test_star_and_other_section_fix_is_not_auto_resolved():
    mixed_fix = {
        "passed": False,
        "safe_to_deliver": False,
        "required_fixes": ["删除STAR中的推断和其他段落的虚构内容"],
    }
    ops = StarSuggestionOps(verify_sequence=[mixed_fix, mixed_fix])
    result = run_without_fact_fallback(ops)

    assert result["status"] == "partial"
    assert result["safe_to_deliver"] is False


def test_star_and_second_scoped_draft_fix_is_not_auto_resolved():
    mixed_fix = {
        "passed": False,
        "safe_to_deliver": False,
        "required_fixes": ["删除STAR中的推断和改后文案中的未证实内容"],
    }
    ops = StarSuggestionOps(verify_sequence=[mixed_fix, mixed_fix])
    result = run_without_fact_fallback(ops)

    assert result["status"] == "partial"
    assert result["safe_to_deliver"] is False


def test_structured_star_fix_cannot_hide_main_draft_issue():
    structured_fix = {
        "passed": False,
        "safe_to_deliver": False,
        "required_fixes": [{
            "fix": "删除STAR中的推断",
            "issue": "优化稿正文也含虚构内容",
        }],
    }
    ops = StarSuggestionOps(verify_sequence=[structured_fix, structured_fix])
    result = run_without_fact_fallback(ops)

    assert result["status"] == "partial"
    assert result["safe_to_deliver"] is False


def test_cancellation_after_repair_skips_second_verification():
    ops = FakeOps(verify_sequence=[_VERIFY_BAD, _VERIFY_OK])
    # Checks occur before initial work, match, suggest, verify repair, then re-verify.
    trace = FakeTrace(cancel_after=4)
    result = run(run_workflow_graph(_payload(), ops, trace))
    assert result["status"] == "cancelled"
    assert ops.calls.count("verify") == 1


def test_unrecovered_verification_uses_fact_only_fallback():
    ops = FakeOps(verify_sequence=[_VERIFY_BAD, _VERIFY_BAD])
    trace = FakeTrace()
    result = run(run_workflow_graph(_payload(), ops, trace))
    assert result["status"] == "completed"
    assert result["safe_to_deliver"] is True
    assert result["unresolved_fixes"] == []
    assert "可靠性降级结果" in result["report"]
    assert "张三" in result["report"]
    assert "已修正并通过复检" not in result["report"]
    assert "已回退至原始简历，无AI改写" in result["report"]
    assert "AI解析仅供核对" in result["report"]
    assert len([call for call in ops.calls if isinstance(call, tuple)]) == 2
    assert ops.calls.count("verify") == 2
    for stage_id in (6, 7):
        _, status, data = trace.last_event(stage_id)
        assert status == "completed"
        assert data["reason"] == "fact_only_fallback"


def test_fact_only_fallback_preserves_latest_stage_timings():
    ops = TimedRecoveryOps(verify_sequence=[_VERIFY_BAD, _VERIFY_BAD])
    trace = FakeTrace()
    result = run(run_workflow_graph(_payload(), ops, trace))

    assert result["status"] == "completed"
    _, _, suggest_data = trace.last_event(6)
    assert suggest_data["duration_ms"] == 222
    assert suggest_data["revision_round"] == 1
    _, _, verify_data = trace.last_event(7)
    assert verify_data["duration_ms"] == 444
    assert verify_data["revision_round"] == 1
    assert verify_data["validation_status"] == "rejected_ai_draft"


def test_fact_only_fallback_repairs_trace_after_revision_suggest_failure():
    ops = RepairSuggestFailsOps(verify_sequence=[_VERIFY_BAD])
    trace = FakeTrace()
    result = run(run_workflow_graph(_payload(), ops, trace))

    assert result["status"] == "completed"
    assert result["safe_to_deliver"] is True
    _, status, data = trace.last_event(6)
    assert status == "completed"
    assert data["reason"] == "fact_only_fallback"


def test_fact_only_fallback_repairs_trace_after_revision_verify_failure():
    ops = RepairVerifyFailsOps()
    trace = FakeTrace()
    result = run(run_workflow_graph(_payload(), ops, trace))

    assert result["status"] == "completed"
    assert result["safe_to_deliver"] is True
    _, status, data = trace.last_event(7)
    assert status == "completed"
    assert data["reason"] == "fact_only_fallback"
    assert data["safe_to_deliver"] is True
    assert data["error_category"] == "tool_error"
    assert data["revision_round"] == 1


def test_fact_only_fallback_does_not_leak_rejected_verifier_text():
    leaked_marker = "MODEL_ONLY_UNSAFE_CLAIM"
    leaking_verification = {
        "passed": False,
        "safe_to_deliver": False,
        "required_fixes": [f"删除{leaked_marker}中的虚构内容"],
    }
    ops = UnsafeResumeOps(
        verify_sequence=[leaking_verification, leaking_verification],
    )
    result = run(run_workflow_graph(_payload(), ops, FakeTrace()))

    assert result["status"] == "completed"
    assert leaked_marker not in result["report"]


def test_cancellation_before_fact_only_fallback_stays_cancelled():
    ops = FakeOps(verify_sequence=[_VERIFY_BAD, _VERIFY_BAD])
    trace = FakeTrace(cancel_after=6)
    result = run(run_workflow_graph(_payload(), ops, trace))

    assert result["status"] == "cancelled"
    assert result["safe_to_deliver"] is False
    assert all(
        data.get("reason") != "fact_only_fallback"
        for _, _, data in trace.events
    )


def test_deadline_before_fact_only_fallback_stays_deadline_exceeded():
    ops = FakeOps(verify_sequence=[_VERIFY_BAD, _VERIFY_BAD])
    trace = DeadlineBeforeFallbackTrace()
    result = run(run_workflow_graph(_payload(deadline_epoch=1000), ops, trace))

    assert result["status"] == "deadline_exceeded"
    assert result["safe_to_deliver"] is False
    assert all(
        data.get("reason") != "fact_only_fallback"
        for _, _, data in trace.events
    )


def test_fact_only_fallback_discards_model_generated_resume():
    ops = UnsafeResumeOps(verify_sequence=[_VERIFY_BAD, _VERIFY_BAD])
    original_resume = "ORIGINAL FACTS\n未负责团队\nExcel"
    result = run(run_workflow_graph(
        _payload(resume_text=original_resume), ops, FakeTrace(),
    ))

    assert result["status"] == "completed"
    assert result["safe_to_deliver"] is True
    assert "MODEL_ONLY_UNSAFE_CLAIM" not in result["report"]
    assert "可靠性降级结果" in result["report"]
    optimized_resume = result["report"].split("## 【优化版简历】", 1)[1].strip()
    assert optimized_resume == original_resume
    assert "带队交付" not in optimized_resume
    assert "提升转化20%" not in optimized_resume


def test_fact_only_fallback_failure_remains_partial():
    import workflows.graph as graph_module

    original = graph_module._fact_only_fallback

    def broken_fallback(resume_text):
        return None

    graph_module._fact_only_fallback = broken_fallback
    try:
        ops = FakeOps(verify_sequence=[_VERIFY_BAD, _VERIFY_BAD])
        trace = FakeTrace()
        result = run(run_workflow_graph(_payload(), ops, trace))
    finally:
        graph_module._fact_only_fallback = original

    assert result["status"] == "partial"
    assert result["safe_to_deliver"] is False
    assert any("删除夸大表述" in fix for fix in result["unresolved_fixes"])
    assert all(
        data.get("reason") != "fact_only_fallback"
        for _, _, data in trace.events
    )


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


def test_required_fixes_extracts_action_text_from_structured_items():
    verification = {
        "required_fixes": [
            {"priority": "high", "fix": "Remove unsupported claim"},
            "Keep the date factual",
            {"issue": "Fallback item"},
        ],
    }
    assert _required_fixes(verification) == [
        "Remove unsupported claim",
        "Keep the date factual",
        "Fallback item",
    ]


def test_durable_runtime_uses_redis_trace_facade():
    workflow_source = Path("workflows/resume_workflow.py").read_text(
        encoding="utf-8"
    )
    api_source = Path("webui/vercel_server.py").read_text(encoding="utf-8")
    assert "from run_trace_store import TraceStore" in workflow_source
    assert "from run_trace_store import TraceStore" in api_source
    assert "vercel_trace" not in workflow_source + api_source


if __name__ == "__main__":
    tests = [v for n, v in sorted(globals().items()) if n.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} workflow tests passed")
