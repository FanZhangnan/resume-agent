"""Strict result contract and semantic repair tests (offline only)."""

import json
import os
import time
from unittest.mock import patch

from pydantic import ValidationError

import config
from contracts import MatchResult, VerificationResult, verification_is_deliverable
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


def main():
    tests = (
        test_match_result_has_safe_defaults_and_strict_bounded_score,
        test_verification_result_is_strict_and_defaults_to_not_deliverable,
        test_deliverability_requires_all_three_gates_and_a_valid_model,
        test_ask_json_repairs_schema_failure_with_shared_deadlines_and_safe_trace,
        test_ask_json_returns_none_after_second_schema_failure,
        test_ask_json_without_validator_keeps_legacy_default_contract,
        test_validator_trace_redacts_private_extra_field_name,
        test_missing_verification_is_partial_and_never_uses_report_formatter,
    )
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"\n{len(tests)} contract tests passed")


if __name__ == "__main__":
    main()
