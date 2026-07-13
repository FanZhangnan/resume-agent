"""Strict result contract and semantic repair tests (offline only)."""

import json
import os
import time
from unittest.mock import patch

from pydantic import ValidationError

import config
from contracts import (
    MatchResult,
    SuggestionResult,
    VerificationResult,
    suggestions_are_usable,
    verification_is_deliverable,
)
from tools import common


def _assert_validation_error(function):
    try:
        function()
    except ValidationError as error:
        return error
    raise AssertionError("Expected strict contract validation to fail")


def test_match_result_has_safe_defaults_and_strict_bounded_score():
    result = MatchResult.model_validate({"score": 73}, strict=True)
    assert result.score == 73
    assert result.score_reason == ""
    assert result.high_matches == []
    assert result.partial_matches == []
    assert result.missing_requirements == []
    assert result.redundant_or_irrelevant == []
    assert result.risks == []
    assert result.recommendation == ""

    for invalid in ("73", 73.0, True, -1, 101):
        _assert_validation_error(
            lambda value=invalid: MatchResult.model_validate(
                {"score": value}, strict=True
            )
        )
    _assert_validation_error(
        lambda: MatchResult.model_validate(
            {"score": 73, "unknown": "forbidden"}, strict=True
        )
    )


def test_match_result_rejects_duplicate_requirement_ids_and_repairs_once():
    duplicate_rows = [
        {
            "requirement_id": "hard-001",
            "status": "missing",
            "evidence_ids": [],
        },
        {
            "requirement_id": "hard-001",
            "status": "missing",
            "evidence_ids": [],
        },
    ]
    _assert_validation_error(
        lambda: MatchResult.model_validate(
            {"score": 0, "requirement_evidence": duplicate_rows},
            strict=True,
        )
    )

    fake = _SemanticClient([
        json.dumps({"score": 0, "requirement_evidence": duplicate_rows}),
        json.dumps({
            "score": 0,
            "requirement_evidence": [duplicate_rows[0]],
        }),
    ])
    with patch.object(common, "get_client", return_value=fake):
        result = common.ask_json(
            "prompt",
            "system",
            {"score": 0, "requirement_evidence": []},
            validator=MatchResult,
        )

    assert len(result["requirement_evidence"]) == 1
    assert len(fake.calls) == 2
    assert fake.calls[0]["logical_deadline"] == fake.calls[1]["logical_deadline"]


def test_verification_result_is_strict_and_defaults_to_not_deliverable():
    result = VerificationResult.model_validate({}, strict=True)
    assert result.passed is False
    assert result.safe_to_deliver is False
    assert result.required_fixes == []
    assert result.overstatement_issues == []
    assert result.fabrication_risks == []
    assert result.logic_issues == []
    assert result.match_authenticity_issues == []

    _assert_validation_error(
        lambda: VerificationResult.model_validate(
            {"passed": "false", "safe_to_deliver": True}, strict=True
        )
    )
    _assert_validation_error(
        lambda: VerificationResult.model_validate(
            {"required_fixes": ("fix",)}, strict=True
        )
    )
    _assert_validation_error(
        lambda: VerificationResult.model_validate(
            {"unknown": True}, strict=True
        )
    )


def test_deliverability_requires_all_three_gates_and_a_valid_model():
    valid = {
        "passed": True,
        "safe_to_deliver": True,
        "required_fixes": [],
    }
    assert verification_is_deliverable(valid) is True
    assert verification_is_deliverable({**valid, "passed": False}) is False
    assert verification_is_deliverable({**valid, "safe_to_deliver": False}) is False
    assert verification_is_deliverable({**valid, "required_fixes": ["fix"]}) is False
    assert verification_is_deliverable({**valid, "passed": "true"}) is False
    assert verification_is_deliverable({**valid, "unknown": "forbidden"}) is False
    assert verification_is_deliverable(None) is False


def test_suggestion_result_requires_usable_text_or_struct():
    for invalid in (
        {},
        {"optimized_resume": "   ", "optimized_resume_struct": {}},
        {
            "optimized_resume": "",
            "optimized_resume_struct": {"basic_info": {"name": "Candidate"}},
        },
        {
            "optimized_resume": "",
            "optimized_resume_struct": {
                "basic_info": {"name": "Candidate"},
                "experience": [False],
            },
        },
    ):
        _assert_validation_error(
            lambda value=invalid: SuggestionResult.model_validate(
                value, strict=True
            )
        )
        assert suggestions_are_usable(invalid) is False

    text_result = SuggestionResult.model_validate(
        {"optimized_resume": "Candidate\nPython Engineer"}, strict=True
    )
    assert suggestions_are_usable(text_result) is True

    struct_result = SuggestionResult.model_validate({
        "optimized_resume_struct": {
            "basic_info": {"name": "Candidate"},
            "experience": [{"company": "Example", "title": "Engineer"}],
        },
    }, strict=True)
    assert suggestions_are_usable(struct_result) is True


def test_suggestion_result_semantic_repair_reuses_one_deadline():
    fake = _SemanticClient([
        json.dumps({"optimized_resume": "", "optimized_resume_struct": {}}),
        json.dumps({"optimized_resume": "Candidate\nPython Engineer"}),
    ])
    with patch.object(common, "get_client", return_value=fake):
        result = common.ask_json(
            "prompt",
            "system",
            {"optimized_resume": "", "optimized_resume_struct": {}},
            validator=SuggestionResult,
        )

    assert result["optimized_resume"] == "Candidate\nPython Engineer"
    assert len(fake.calls) == 2
    assert fake.calls[0]["logical_deadline"] == fake.calls[1]["logical_deadline"]


def test_truncated_retry_respects_explicit_token_cap():
    class TruncatedClient(_SemanticClient):
        def simple_ask(self, **kwargs):
            self.calls.append(kwargs)
            self.last_finish_reason = "length" if len(self.calls) == 1 else "stop"
            return next(self.responses)

    fake = TruncatedClient([
        '{"optimized_resume":',
        json.dumps({"optimized_resume": "Candidate\nPython Engineer"}),
    ])
    with patch.object(common, "get_client", return_value=fake):
        result = common.ask_json(
            "prompt",
            "system",
            {"optimized_resume": "", "optimized_resume_struct": {}},
            max_tokens=4096,
            retry_max_tokens=4096,
            validator=SuggestionResult,
        )

    assert result["optimized_resume"] == "Candidate\nPython Engineer"
    assert [call["max_tokens"] for call in fake.calls] == [4096, 4096]


class _SemanticClient:
    last_finish_reason = "stop"

    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def simple_ask(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.responses)


def test_ask_json_repairs_schema_failure_with_shared_deadlines_and_safe_trace():
    private_value = "PRIVATE INVALID SCORE"
    fake = _SemanticClient([
        json.dumps({"score": private_value}),
        json.dumps({"score": 62, "score_reason": "evidence based"}),
    ])
    traces = []
    run_deadline = time.monotonic() + 30

    with (
        patch.object(common, "get_client", return_value=fake),
        patch.object(common, "emit_trace", side_effect=lambda *args, **kwargs: traces.append((args, kwargs))),
        patch.object(config, "CALL_DEADLINE", 12),
        common.use_run_deadline(run_deadline),
    ):
        result = common.ask_json(
            "private prompt",
            "private system",
            {"score": 0},
            validator=MatchResult,
        )

    assert result["score"] == 62
    assert result["score_reason"] == "evidence based"
    assert len(fake.calls) == 2
    assert fake.calls[0]["logical_deadline"] == fake.calls[1]["logical_deadline"]
    assert fake.calls[0]["external_deadline"] == run_deadline
    assert fake.calls[1]["external_deadline"] == run_deadline
    assert "private prompt" in fake.calls[0]["prompt"]
    assert fake.calls[1]["prompt"] != fake.calls[0]["prompt"]

    assert traces[0][0] == ("llm.semantic_json.retry",)
    trace_data = traces[0][1]["data"]
    assert trace_data["reason"] == "schema_validation"
    assert trace_data["validation_errors"] == [
        {"category": "int_type", "path": "score"}
    ]
    serialized = json.dumps(traces, ensure_ascii=False)
    assert private_value not in serialized
    assert "private prompt" not in serialized
    assert "private system" not in serialized


def test_ask_json_returns_none_after_second_schema_failure():
    fake = _SemanticClient([
        '{"score":"bad"}',
        '{"score":101}',
    ])
    traces = []
    with (
        patch.object(common, "get_client", return_value=fake),
        patch.object(common, "emit_trace", side_effect=lambda *args, **kwargs: traces.append((args, kwargs))),
    ):
        result = common.ask_json(
            "prompt", "system", {"score": 0}, validator=MatchResult
        )

    assert result is None
    assert [event[0][0] for event in traces] == [
        "llm.semantic_json.retry",
        "llm.semantic_json.failure",
    ]
    failure_data = traces[-1][1]["data"]
    assert failure_data["reason"] == "schema_validation"
    assert failure_data["validation_errors"] == [
        {"category": "less_than_equal", "path": "score"}
    ]


def test_ask_json_without_validator_keeps_legacy_default_contract():
    fake = _SemanticClient(['{"value":7}'])
    with patch.object(common, "get_client", return_value=fake):
        result = common.ask_json(
            "prompt", "system", {"value": 0, "items": []}
        )
    assert result == {"value": 7, "items": []}
    assert len(fake.calls) == 1


def test_validator_trace_redacts_private_extra_field_name():
    private_field = "candidate-private@example.com"
    fake = _SemanticClient([
        json.dumps({"score": 50, private_field: "private value"}),
        json.dumps({"score": 50}),
    ])
    traces = []
    with (
        patch.object(common, "get_client", return_value=fake),
        patch.object(common, "emit_trace", side_effect=lambda *args, **kwargs: traces.append((args, kwargs))),
    ):
        result = common.ask_json(
            "prompt", "system", {"score": 0}, validator=MatchResult
        )

    assert result["score"] == 50
    assert traces[0][1]["data"]["validation_errors"] == [
        {"category": "extra_forbidden", "path": "<extra>"}
    ]
    assert private_field not in json.dumps(traces, ensure_ascii=False)


def test_missing_verification_is_partial_and_never_uses_report_formatter():
    from agent import ResumeAgent

    with patch.dict(os.environ, {"AGENT_MOCK": "1"}, clear=False):
        resume_agent = ResumeAgent("resume", "JD")
    resume_agent.client.mock_mode = False
    with patch.object(resume_agent.client, "simple_ask") as formatter:
        report = resume_agent._generate_final_report()

    formatter.assert_not_called()
    assert resume_agent._report_terminal_status() == "partial"
    assert "本报告不完整" in report
    assert "验证结果未满足严格交付契约" in report


def test_unusable_suggestions_stay_partial_even_after_verification_passes():
    from agent import ResumeAgent

    with patch.dict(os.environ, {"AGENT_MOCK": "1"}, clear=False):
        resume_agent = ResumeAgent("resume", "JD")
    resume_agent.state.update({
        "suggestions": {
            "optimized_resume": "",
            "optimized_resume_struct": {},
        },
        "verification": {
            "passed": True,
            "safe_to_deliver": True,
            "required_fixes": [],
        },
    })

    assert resume_agent._report_terminal_status() == "partial"
    report = resume_agent._render_local_report()
    assert "本报告不完整" in report
    assert "优化版简历未生成或结构无效" in report
    assert "验证结果**：✅ 通过" in report
    assert "交付内容：⚠️ 优化版简历未生成或结构无效" in report


def test_react_revision_is_hard_capped_at_one_when_config_is_two():
    from agent import ResumeAgent

    failed_first = {
        "passed": False,
        "safe_to_deliver": False,
        "required_fixes": ["Fix the first failed patch"],
    }
    failed_second = {
        "passed": False,
        "safe_to_deliver": False,
        "required_fixes": ["Second verification still fails"],
    }
    with patch.dict(os.environ, {"AGENT_MOCK": "1"}, clear=False):
        resume_agent = ResumeAgent("resume", "JD")
    resume_agent.state.update({
        "resume_info": {"basic_info": {"name": "Candidate"}},
        "jd_analysis": {"job_title": "Engineer"},
        "match_result": {"score": 0},
        "suggestions": {"optimized_resume": "Candidate"},
        "verification": failed_first,
    })

    with patch.object(config, "MAX_REVISION_ROUNDS", 2):
        first_note = resume_agent._handle_verification(failed_first)
        assert first_note is not None
        assert resume_agent.revision_rounds == 1
        assert resume_agent.pending_revision is True

        resume_agent.state["verification"] = failed_second
        second_note = resume_agent._handle_verification(failed_second)

        assert second_note is None
        assert resume_agent.revision_rounds == 1
        assert resume_agent.pending_revision is False
        assert resume_agent._is_complete() is True
        report = resume_agent._render_local_report()

    assert len(resume_agent.correction_log) == 1
    assert "本报告不完整" in report
    assert "Second verification still fails" in report


def test_jd_analysis_gate_contract_is_strict():
    from contracts import JDAnalysis

    for omitted in (
        {},
        {"gates": {}},
        {
            "gates": {
                "location": {"required": False, "accepted_values": []},
            },
        },
        {
            "gates": {
                "location": {"required": False, "accepted_values": []},
                "work_authorization": {"required": False},
            },
        },
    ):
        _assert_validation_error(
            lambda value=omitted: JDAnalysis.model_validate(value, strict=True)
        )

    result = JDAnalysis.model_validate({
        "job_title": "Platform Engineer",
        "gates": {
            "location": {
                "required": True,
                "accepted_values": ["Brisbane"],
            },
            "work_authorization": {
                "required": False,
                "accepted_values": [],
            },
        },
    }, strict=True)
    assert result.gates.location.accepted_values == ["Brisbane"]
    _assert_validation_error(
        lambda: JDAnalysis.model_validate({
            "job_title": "Platform Engineer",
            "gates": {
                "location": {"required": "true", "accepted_values": []},
                "work_authorization": {
                    "required": False,
                    "accepted_values": [],
                },
            },
        }, strict=True)
    )
    _assert_validation_error(
        lambda: JDAnalysis.model_validate({
            "job_title": "Platform Engineer",
            "gates": {
                "location": {
                    "required": True,
                    "accepted_values": ("Brisbane",),
                },
                "work_authorization": {
                    "required": False,
                    "accepted_values": [],
                },
            },
        }, strict=True)
    )


def test_jd_analysis_rejects_gate_only_payload_and_repairs_once():
    from contracts import JDAnalysis

    gates = {
        "location": {"required": False, "accepted_values": []},
        "work_authorization": {"required": False, "accepted_values": []},
    }
    _assert_validation_error(
        lambda: JDAnalysis.model_validate({"gates": gates}, strict=True)
    )

    fake = _SemanticClient([
        json.dumps({"gates": gates}),
        json.dumps({"job_title": "Platform Engineer", "gates": gates}),
    ])
    with patch.object(common, "get_client", return_value=fake):
        result = common.ask_json(
            "prompt",
            "system",
            {"gates": gates},
            validator=JDAnalysis,
        )

    assert result["job_title"] == "Platform Engineer"
    assert len(fake.calls) == 2
    assert fake.calls[0]["logical_deadline"] == fake.calls[1]["logical_deadline"]


def test_resume_info_contract_rejects_malformed_nested_evidence_records():
    from contracts import ResumeInfo

    valid = ResumeInfo.model_validate({
        "education": [{"degree": "BSc"}],
        "work_experience": [{"company": "Example"}],
        "projects": [{"description": "Built a service"}],
        "skills": ["Python", {"name": "SQL"}],
    }, strict=True)
    assert valid.education[0].degree == "BSc"
    assert valid.work_experience[0].company == "Example"
    assert valid.projects[0].description == "Built a service"
    assert valid.skills[0] == "Python"
    assert valid.skills[1].name == "SQL"

    invalid_shapes = (
        {"education": [False]},
        {"education": [{}]},
        {"education": [{"school": "   "}]},
        {"work_experience": [7]},
        {"work_experience": [{}]},
        {"work_experience": [{"company": ""}]},
        {"work_experience": [{"achievement": "Growth"}]},
        {"work_experience": [{"company": "Example", "responsibilities": [""]}]},
        {"projects": ["not a record"]},
        {"projects": [{}]},
        {"projects": [{"name": "   "}]},
        {"projects": [{"name": "Example", "technologies": ["   "]}]},
        {"skills": [False]},
        {"skills": [7]},
        {"skills": [""]},
        {"skills": ["   "]},
        {"skills": [{}]},
        {"skills": [{"name": ""}]},
    )
    for invalid in invalid_shapes:
        _assert_validation_error(
            lambda value=invalid: ResumeInfo.model_validate(value, strict=True)
        )


def test_resume_info_semantic_repair_reuses_one_deadline():
    from contracts import ResumeInfo

    fake = _SemanticClient([
        json.dumps({"education": [False]}),
        json.dumps({"education": [{"degree": "BSc"}]}),
    ])
    with patch.object(common, "get_client", return_value=fake):
        result = common.ask_json(
            "prompt",
            "system",
            {"education": []},
            validator=ResumeInfo,
        )

    assert result["education"][0]["degree"] == "BSc"
    assert len(fake.calls) == 2
    assert fake.calls[0]["logical_deadline"] == fake.calls[1]["logical_deadline"]


def test_resume_info_rejects_fully_empty_payload_and_repairs_once():
    from contracts import ResumeInfo

    for empty in ({}, {"basic_info": {}}):
        _assert_validation_error(
            lambda value=empty: ResumeInfo.model_validate(value, strict=True)
        )

    assert ResumeInfo.model_validate(
        {"basic_info": {"name": "Candidate"}}, strict=True
    ).basic_info.name == "Candidate"
    assert ResumeInfo.model_validate(
        {"skills": ["Python"]}, strict=True
    ).skills == ["Python"]

    fake = _SemanticClient([
        json.dumps({"basic_info": {}}),
        json.dumps({"basic_info": {"name": "Candidate"}}),
    ])
    with patch.object(common, "get_client", return_value=fake):
        result = common.ask_json(
            "prompt",
            "system",
            {"basic_info": {}},
            validator=ResumeInfo,
        )

    assert result["basic_info"]["name"] == "Candidate"
    assert len(fake.calls) == 2
    assert fake.calls[0]["logical_deadline"] == fake.calls[1]["logical_deadline"]


def main():
    tests = (
        test_match_result_has_safe_defaults_and_strict_bounded_score,
        test_match_result_rejects_duplicate_requirement_ids_and_repairs_once,
        test_verification_result_is_strict_and_defaults_to_not_deliverable,
        test_deliverability_requires_all_three_gates_and_a_valid_model,
        test_suggestion_result_requires_usable_text_or_struct,
        test_suggestion_result_semantic_repair_reuses_one_deadline,
        test_truncated_retry_respects_explicit_token_cap,
        test_ask_json_repairs_schema_failure_with_shared_deadlines_and_safe_trace,
        test_ask_json_returns_none_after_second_schema_failure,
        test_ask_json_without_validator_keeps_legacy_default_contract,
        test_validator_trace_redacts_private_extra_field_name,
        test_missing_verification_is_partial_and_never_uses_report_formatter,
        test_unusable_suggestions_stay_partial_even_after_verification_passes,
        test_react_revision_is_hard_capped_at_one_when_config_is_two,
        test_jd_analysis_gate_contract_is_strict,
        test_jd_analysis_rejects_gate_only_payload_and_repairs_once,
        test_resume_info_contract_rejects_malformed_nested_evidence_records,
        test_resume_info_semantic_repair_reuses_one_deadline,
        test_resume_info_rejects_fully_empty_payload_and_repairs_once,
    )
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"\n{len(tests)} contract tests passed")


if __name__ == "__main__":
    main()
