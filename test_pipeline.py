"""Deterministic eight-stage pipeline tests (offline only)."""

import json
import io
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
from trace_catalog import _reset_trace_catalog_for_tests


TEST_RESUME = "Candidate resume with verified experience."
TEST_JD = "Target role requiring verified experience."


def _assert_raises(expected, function):
    try:
        function()
    except expected as error:
        return error
    raise AssertionError(f"Expected {expected.__name__}")


def _valid_suggestions(version="first"):
    return {
        "overall_strategy": version,
        "rewrite_suggestions": [],
        "star_rewrites": [],
        "keyword_injection": [],
        "honesty_boundaries": [],
        "optimized_resume": "",
        "optimized_resume_struct": {
            "basic_info": {"name": "Candidate"},
            "summary": "",
            "education": [],
            "experience": [{"company": "Example", "title": "Analyst", "bullets": []}],
            "projects": [],
            "skills": [],
            "extras": [],
        },
    }


class RecordingExecutor:
    def __init__(self, *, revision=False, candidates=None, fail_tool=None,
                 concurrent_barrier=None, revision_without_fixes=False,
                 raise_tool=None, raise_error=None,
                 expire_agent_deadline=False, potential_issues=None,
                 ask_result=None, resume_basic_info=None,
                 unusable_suggestions=False):
        self.revision = revision
        self.revision_without_fixes = revision_without_fixes
        self.candidates = candidates or [{
            "company": "Current Company",
            "role_title": "Current Role",
            "job_type": "Full-time",
            "location": "Brisbane",
            "description": "LIVE CURRENT DESCRIPTION",
            "typical_jd": "LEGACY TYPICAL JD FALLBACK",
        }]
        self.fail_tool = fail_tool
        self.raise_tool = raise_tool
        self.raise_error = raise_error
        self.expire_agent_deadline = expire_agent_deadline
        self.potential_issues = list(potential_issues or [])
        self.ask_result = ask_result
        self.resume_basic_info = resume_basic_info or {"name": "Candidate"}
        self.unusable_suggestions = unusable_suggestions
        self.agent = None
        self.concurrent_barrier = concurrent_barrier
        self.calls = []
        self.arguments_by_tool = {}
        self.analyzed_jds = []
        self.thread_ids = {}
        self.verify_calls = 0
        self.suggestion_calls = 0

    def __call__(self, tool_name, arguments):
        self.calls.append(tool_name)
        self.arguments_by_tool.setdefault(tool_name, []).append(arguments)
        self.thread_ids.setdefault(tool_name, []).append(threading.get_ident())
        if tool_name in ("extract_resume_info", "analyze_jd") and self.concurrent_barrier:
            self.concurrent_barrier.wait(timeout=1)
        if tool_name == self.raise_tool:
            if self.expire_agent_deadline and self.agent is not None:
                self.agent._run_deadline = time.monotonic() - 1
            raise self.raise_error
        if tool_name == self.fail_tool:
            return {
                "success": False,
                "error": "PRIVATE TOOL PAYLOAD MUST NOT REACH REPORT OR TRACE",
            }
        if tool_name == "parse_resume_file":
            return {
                "success": True,
                "text": TEST_RESUME,
                "file_type": "text",
                "char_count": len(TEST_RESUME),
            }
        if tool_name == "extract_resume_info":
            return {
                "success": True,
                "resume_info": {
                    "basic_info": self.resume_basic_info,
                    "work_experience": [{"company": "Example"}],
                    "projects": [],
                    "skills": ["Python"],
                    "potential_issues": self.potential_issues,
                },
            }
        if tool_name == "recommend_jobs":
            return {
                "success": True,
                "recommendations": {
                    "candidates": self.candidates,
                    "overall_advice": "Apply",
                },
            }
        if tool_name == "analyze_jd":
            self.analyzed_jds.append(arguments["jd_text"])
            return {
                "success": True,
                "jd_analysis": {
                    "job_title": "Current Role",
                    "hard_requirements": ["verified experience"],
                    "bonus_points": [],
                    "implicit_requirements": [],
                },
            }
        if tool_name == "calculate_match":
            return {
                "success": True,
                "match_result": {
                    "score": 80,
                    "high_matches": [],
                    "partial_matches": [],
                    "missing_requirements": [],
                },
            }
        if tool_name == "generate_suggestions":
            self.suggestion_calls += 1
            return {
                "success": True,
                "suggestions": (
                    {
                        "optimized_resume": "",
                        "optimized_resume_struct": {},
                    }
                    if self.unusable_suggestions
                    else _valid_suggestions(
                        "revised" if self.suggestion_calls > 1 else "first"
                    )
                ),
            }
        if tool_name == "verify_output":
            self.verify_calls += 1
            failed = self.revision and self.verify_calls == 1
            return {
                "success": True,
                "verification": {
                    "passed": not failed,
                    "safe_to_deliver": not failed,
                    "overall_assessment": "SYNTHESIZED REVIEW ISSUE" if failed else "Passed",
                    "required_fixes": (
                        []
                        if failed and self.revision_without_fixes
                        else ["Fix only failed patch"] if failed else []
                    ),
                    "overstatement_issues": [],
                    "fabrication_risks": [],
                    "logic_issues": [],
                    "match_authenticity_issues": [],
                },
            }
        if tool_name == "ask_user":
            default = {
                "success": True,
                "question": arguments["question"],
                "answer": "Fixed mock answer",
                "answered": True,
                "timed_out": False,
                "skipped": False,
            }
            return {**default, **(self.ask_result or {})}
        raise AssertionError(f"Unexpected tool: {tool_name}")


def _read_events(trace_dir):
    path = Path(trace_dir) / "trace.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _run_agent(executor, *, jd_text=TEST_JD, resume_is_file=False,
               mock_ask=False, production_question=False, preferences=None):
    from agent import ResumeAgent
    import pipeline

    with tempfile.TemporaryDirectory() as temp_dir:
        trace_dir = Path(temp_dir) / "trace"
        report_dir = Path(temp_dir) / "reports"
        env = {
            "AGENT_MOCK": "1",
            "AGENT_RUN_ID": "pipeline-test-run",
            "AGENT_TRACE_DIR": str(trace_dir),
        }
        if mock_ask:
            env["AGENT_MOCK_ASK"] = "1"
        else:
            env["AGENT_MOCK_ASK"] = "0"
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(config, "ORCHESTRATOR", "pipeline"),
            patch.object(pipeline, "execute_tool", executor),
        ):
            _reset_trace_catalog_for_tests()
            resume_input = str(Path(temp_dir) / "resume.txt") if resume_is_file else TEST_RESUME
            if resume_is_file:
                Path(resume_input).write_text(TEST_RESUME, encoding="utf-8")
            resume_agent = ResumeAgent(
                resume_input,
                jd_text,
                resume_is_file=resume_is_file,
                output_dir=str(report_dir),
                preferences=preferences,
            )
            if production_question:
                resume_agent.client.mock_mode = False

            def planner_must_not_run(*args, **kwargs):
                raise AssertionError("pipeline mode must not call planner/report LLM")

            resume_agent.client.chat = planner_must_not_run
            resume_agent.client.simple_ask = planner_must_not_run
            if hasattr(executor, "agent"):
                executor.agent = resume_agent
            report = resume_agent.run()
            events = _read_events(trace_dir)
            report_files = list(report_dir.glob("*.md"))
            assert len(report_files) == 1
            return resume_agent, report, events


def test_orchestrator_policy_defaults_to_pipeline_and_is_exact():
    assert config.ORCHESTRATOR == "pipeline"
    assert config.validate_orchestrator("pipeline") == "pipeline"
    assert config.validate_orchestrator("react") == "react"
    assert config.validate_orchestrator("") == "pipeline"
    for invalid in ("planner", "PIPELINE", "pipeline ", "reactor"):
        _assert_raises(ValueError, lambda value=invalid: config.validate_orchestrator(value))


def test_supplied_jd_runs_eight_local_stages_without_planner_calls():
    executor = RecordingExecutor()
    resume_agent, report, events = _run_agent(executor)

    assert executor.calls[:2] in (
        ["extract_resume_info", "analyze_jd"],
        ["analyze_jd", "extract_resume_info"],
    )
    assert executor.calls[2:] == [
        "calculate_match", "generate_suggestions", "verify_output"
    ]
    started = [event["data"]["stage_id"] for event in events
               if event["event"] == "stage.started"]
    assert started == list(range(1, 9))
    skipped = [event for event in events if event["event"] == "stage.skipped"]
    assert [(item["data"]["stage_id"], item["data"]["reason"]) for item in skipped] == [
        (3, "jd_supplied")
    ]
    report_events = [event for event in events
                     if event["event"] == "report.generate.completed"]
    assert report_events[-1]["data"]["renderer"] == "local"
    assert not [event for event in events
                if (event.get("data") or {}).get("operation") == "report.format"]
    terminal = [event for event in events if event["event"] == "run.completed"][-1]
    assert terminal["data"]["steps"] == 8
    assert resume_agent.step_count == 8
    assert "【优化版简历】" in report


def test_pipeline_never_completes_with_unusable_suggestions():
    resume_agent, report, events = _run_agent(
        RecordingExecutor(unusable_suggestions=True)
    )

    terminal = [
        event for event in events if event["event"] == "run.completed"
    ][-1]
    assert terminal["data"]["status"] == "partial"
    assert resume_agent._report_terminal_status() == "partial"
    assert "本报告不完整" in report
    assert "优化版简历未生成或结构无效" in report


def test_supplied_jd_extract_and_analysis_overlap_on_distinct_threads():
    barrier = threading.Barrier(2)
    executor = RecordingExecutor(concurrent_barrier=barrier)
    _run_agent(executor)
    extract_thread = executor.thread_ids["extract_resume_info"][0]
    jd_thread = executor.thread_ids["analyze_jd"][0]
    assert extract_thread != jd_thread
    assert threading.get_ident() not in (extract_thread, jd_thread)


def test_inverse_worker_completion_still_routes_parallel_observations_to_steps():
    base = RecordingExecutor()
    extract_entered = threading.Event()
    jd_finished = threading.Event()
    worker_completion_order = []

    def executor(tool_name, arguments):
        if tool_name == "extract_resume_info":
            extract_entered.set()
            assert jd_finished.wait(timeout=1)
            result = base(tool_name, arguments)
            worker_completion_order.append(tool_name)
            return result
        if tool_name == "analyze_jd":
            assert extract_entered.wait(timeout=1)
            result = base(tool_name, arguments)
            worker_completion_order.append(tool_name)
            jd_finished.set()
            return result
        return base(tool_name, arguments)

    output = io.StringIO()
    with redirect_stdout(output):
        _, _, events = _run_agent(executor)
    assert worker_completion_order[:2] == ["analyze_jd", "extract_resume_info"]

    completed = [
        (event["step"], event["data"]["name"])
        for event in events if event["event"] == "tool.completed"
    ]
    assert completed[:2] == [
        (4, "analyze_jd"),
        (2, "extract_resume_info"),
    ]

    lines = output.getvalue().splitlines()
    extract_trace = next(i for i, line in enumerate(lines)
                         if '"event":"tool.completed"' in line
                         and '"name":"extract_resume_info"' in line)
    extract_observation = next(i for i, line in enumerate(lines)
                               if "📋 观察：✅ 已提取" in line)
    jd_trace = next(i for i, line in enumerate(lines)
                    if '"event":"tool.completed"' in line
                    and '"name":"analyze_jd"' in line)
    jd_observation = next(i for i, line in enumerate(lines)
                          if "📋 观察：✅ 岗位「Current Role」" in line)
    assert jd_trace < jd_observation < extract_trace < extract_observation


def test_parallel_watchdog_does_not_wait_for_blocked_worker_before_partial_report():
    from tools import common

    base = RecordingExecutor()
    release_worker = threading.Event()
    worker_entered = threading.Event()
    worker_exited = threading.Event()
    seen_deadlines = []

    def executor(tool_name, arguments):
        if tool_name == "extract_resume_info":
            worker_entered.set()
            deadline_reader = getattr(common, "current_run_deadline", lambda: "missing")
            seen_deadlines.append(deadline_reader())
            try:
                release_worker.wait(timeout=2)
            finally:
                worker_exited.set()
        return base(tool_name, arguments)

    # Prevent a broken implementation from hanging the offline test forever.
    fallback_release = threading.Timer(0.6, release_worker.set)
    fallback_release.daemon = True
    fallback_release.start()
    started = time.monotonic()
    try:
        with patch.object(config, "RUN_TIMEOUT", 0.05):
            resume_agent, report, events = _run_agent(executor)
        elapsed = time.monotonic() - started
    finally:
        release_worker.set()
        fallback_release.cancel()
        worker_exited.wait(timeout=1)

    assert worker_entered.is_set()
    assert elapsed < 0.3
    assert seen_deadlines and seen_deadlines[0] not in (None, "missing")
    assert resume_agent.interrupted_error
    assert "本报告不完整" in report
    assert resume_agent.state["jd_analysis"]
    completed = [
        (event["step"], event["data"]["name"])
        for event in events if event["event"] == "tool.completed"
    ]
    assert completed[0] == (4, "analyze_jd")
    assert [event for event in events if event["event"] == "run.completed"][-1]["data"]["status"] == "partial"

    # A detached old worker must not leave thread-local deadline state behind or
    # alter the next run's trace catalog.
    assert common.current_run_deadline() is None
    _, _, next_events = _run_agent(RecordingExecutor())
    assert [event for event in next_events if event["event"] == "run.started"]
    assert [event for event in next_events if event["event"] == "run.completed"][-1]["data"]["status"] == "completed"


def test_terminal_trace_deadline_does_not_discard_saved_report():
    import agent as agent_module

    original_emit = agent_module.emit_trace

    def emit_then_timeout(event, **kwargs):
        record = original_emit(event, **kwargs)
        if event == "run.completed":
            raise agent_module.RunDeadlineExceeded(
                "deadline fired while final trace summary was being replaced"
            )
        return record

    with patch.object(agent_module, "emit_trace", side_effect=emit_then_timeout):
        _, report, events = _run_agent(RecordingExecutor())

    assert "# 简历优化报告" in report
    assert [event for event in events if event["event"] == "run.completed"]


def test_tool_json_calls_receive_external_run_deadline():
    from tools import common

    received = []

    class FakeClient:
        last_finish_reason = "stop"

        def simple_ask(self, **kwargs):
            received.append(kwargs)
            return '{"value": 1}'

    deadline = time.monotonic() + 10
    with (
        common.use_run_deadline(deadline),
        patch.object(common, "get_client", return_value=FakeClient()),
    ):
        result = common.ask_json("prompt", "system", {"value": 0})

    assert result == {"value": 1}
    assert received[0]["external_deadline"] == deadline
    assert common.current_run_deadline() is None


def test_llm_client_rejects_an_already_expired_external_run_deadline():
    from llm_client import LLMClient

    calls = []
    client = object.__new__(LLMClient)
    client.mock_mode = False
    client.streaming = False
    client.last_finish_reason = None
    client.last_call_metrics = {}
    client.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: calls.append(kwargs))
        )
    )
    error = _assert_raises(
        TimeoutError,
        lambda: client.chat(
            [{"role": "user", "content": "offline"}],
            external_deadline=time.monotonic() - 0.01,
        ),
    )
    assert getattr(error, "is_run_deadline", False)
    assert calls == []


def test_job_search_discovers_only_without_jd_and_prefers_description():
    executor = RecordingExecutor()
    _run_agent(executor, jd_text="")
    assert executor.calls[:3] == [
        "extract_resume_info", "recommend_jobs", "analyze_jd"
    ]
    assert executor.analyzed_jds == ["LIVE CURRENT DESCRIPTION"]
    assert "LEGACY TYPICAL JD FALLBACK" not in executor.analyzed_jds[0]


def test_job_search_keeps_legacy_typical_jd_fallback():
    executor = RecordingExecutor(candidates=[{
        "company": "Legacy Company",
        "role_title": "Legacy Role",
        "job_type": "Full-time",
        "location": "Remote",
        "typical_jd": "ONLY AVAILABLE LEGACY JD",
    }])
    _run_agent(executor, jd_text="")
    assert executor.analyzed_jds == ["ONLY AVAILABLE LEGACY JD"]


def test_job_search_mode_passes_preferences_to_discovery_and_match():
    executor = RecordingExecutor()
    _run_agent(executor, jd_text="", preferences="Brisbane roles only")

    assert executor.arguments_by_tool["recommend_jobs"][0]["preferences"] == (
        "Brisbane roles only"
    )
    assert executor.arguments_by_tool["calculate_match"][0]["preferences"] == (
        "Brisbane roles only"
    )


def test_pipeline_passes_preferences_and_explicit_gate_evidence_to_match():
    executor = RecordingExecutor(resume_basic_info={
        "name": "Candidate",
        "location": "Sydney",
        "work_authorization": False,
    })
    _run_agent(executor, preferences="Sydney roles only")

    arguments = executor.arguments_by_tool["calculate_match"][0]
    assert arguments["preferences"] == "Sydney roles only"
    assert arguments["resume_info"]["basic_info"]["location"] == "Sydney"
    assert arguments["resume_info"]["basic_info"]["work_authorization"] is False


def test_match_arguments_discard_planner_preferences_without_user_source():
    from agent import ResumeAgent

    with patch.dict(os.environ, {"AGENT_MOCK": "1"}, clear=False):
        resume_agent = ResumeAgent("resume", TEST_JD, preferences=None)
    resume_agent.state["resume_info"] = {
        "basic_info": {"name": "Candidate"},
    }
    resume_agent.state["jd_analysis"] = {"job_title": "Engineer"}

    arguments = resume_agent._prepare_arguments(
        "calculate_match",
        {"preferences": "I am an expert Python engineer"},
    )

    assert "preferences" not in arguments


def test_missing_recommended_job_description_starts_then_fails_stage_four():
    executor = RecordingExecutor(candidates=[{
        "company": "Missing Description Company",
        "role_title": "Incomplete Posting",
        "location": "Brisbane",
    }])
    resume_agent, report, events = _run_agent(executor, jd_text="")
    stage_four = [
        event["event"] for event in events
        if (event.get("data") or {}).get("stage_id") == 4
    ]
    assert stage_four == ["stage.started", "stage.failed"]
    assert "analyze_jd" not in executor.calls
    assert resume_agent.interrupted_error
    assert "本报告不完整" in report


def test_failed_verification_repeats_only_stages_six_and_seven_once():
    executor = RecordingExecutor(revision=True)
    resume_agent, _, events = _run_agent(executor)
    assert executor.calls.count("extract_resume_info") == 1
    assert executor.calls.count("analyze_jd") == 1
    assert executor.calls.count("calculate_match") == 1
    assert executor.calls.count("generate_suggestions") == 2
    assert executor.calls.count("verify_output") == 2
    assert executor.suggestion_calls == executor.verify_calls == 2
    assert resume_agent.revision_rounds == 1
    assert resume_agent.state["suggestions"]["overall_strategy"] == "revised"
    started = [event["data"]["stage_id"] for event in events
               if event["event"] == "stage.started"]
    assert started == [1, 2, 3, 4, 5, 6, 7, 6, 7, 8]


def test_revision_without_required_fixes_uses_synthesized_correction_issue():
    executor = RecordingExecutor(revision=True, revision_without_fixes=True)
    resume_agent, _, _ = _run_agent(executor)
    revised_arguments = executor.arguments_by_tool["generate_suggestions"][1]
    assert revised_arguments["fix_instructions"] == ["SYNTHESIZED REVIEW ISSUE"]
    assert resume_agent.correction_log[0]["issues"] == ["SYNTHESIZED REVIEW ISSUE"]


def test_zero_revision_budget_disables_pipeline_revision():
    executor = RecordingExecutor(revision=True)
    with patch.object(config, "MAX_REVISION_ROUNDS", 0):
        resume_agent, _, _ = _run_agent(executor)
    assert executor.suggestion_calls == 1
    assert executor.verify_calls == 1
    assert resume_agent.revision_rounds == 0


def test_parallel_failure_preserves_successful_branch_and_returns_partial_report():
    barrier = threading.Barrier(2)
    executor = RecordingExecutor(
        fail_tool="extract_resume_info",
        concurrent_barrier=barrier,
    )
    resume_agent, report, events = _run_agent(executor)
    assert resume_agent.state["resume_info"] is None
    assert resume_agent.state["jd_analysis"]["job_title"] == "Current Role"
    assert resume_agent.interrupted_error
    assert "本报告不完整" in report
    assert "PRIVATE TOOL PAYLOAD" not in report
    serialized = json.dumps(events, ensure_ascii=False)
    assert "PRIVATE TOOL PAYLOAD" not in serialized
    failed = [event for event in events if event["event"] == "stage.failed"]
    assert failed[-1]["data"]["stage_id"] == 2
    assert [event for event in events if event["event"] == "stage.started"][-1]["data"]["stage_id"] == 8
    assert [event for event in events if event["event"] == "run.completed"][-1]["data"]["status"] == "partial"


def test_first_stage_failure_still_delivers_a_local_partial_report():
    executor = RecordingExecutor(fail_tool="parse_resume_file")
    resume_agent, report, events = _run_agent(executor, resume_is_file=True)
    assert resume_agent.interrupted_error
    assert "本报告不完整" in report
    assert "PRIVATE TOOL PAYLOAD" not in report
    assert [event for event in events if event["event"] == "stage.failed"][-1]["data"]["stage_id"] == 1
    assert [event for event in events if event["event"] == "stage.started"][-1]["data"]["stage_id"] == 8
    assert [event for event in events if event["event"] == "run.completed"][-1]["data"]["status"] == "partial"


def test_deadline_after_completed_stages_delivers_safe_partial_report():
    from agent import RunDeadlineExceeded

    private_cause = "PRIVATE DEADLINE CAUSE MUST NOT LEAK"
    executor = RecordingExecutor(
        raise_tool="calculate_match",
        raise_error=RunDeadlineExceeded(private_cause),
        expire_agent_deadline=True,
    )
    started = time.monotonic()
    resume_agent, report, events = _run_agent(executor)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert resume_agent.state["resume_info"]
    assert resume_agent.state["jd_analysis"]
    assert resume_agent.interrupted_error
    assert private_cause not in resume_agent.interrupted_error
    assert private_cause not in report
    assert private_cause not in json.dumps(events, ensure_ascii=False)
    assert "本报告不完整" in report
    assert not [event for event in events if event["event"] == "run.error"]
    terminal = [event for event in events if event["event"] == "run.completed"][-1]
    assert terminal["data"]["status"] == "partial"
    assert terminal["data"]["report_available"] is True


def test_execute_tool_never_swallows_run_deadline():
    from agent import RunDeadlineExceeded
    from tools import execute_tool

    deadline = RunDeadlineExceeded("PRIVATE TOOL DEADLINE")

    def raise_deadline():
        raise deadline

    with patch("tools.get_tool_function", return_value=raise_deadline):
        caught = _assert_raises(RunDeadlineExceeded, lambda: execute_tool("any", {}))
    assert caught is deadline


def test_mock_question_is_a_control_event_not_a_numbered_stage():
    executor = RecordingExecutor()
    resume_agent, _, events = _run_agent(executor, mock_ask=True)
    assert executor.calls.count("ask_user") == 1
    assert len(resume_agent.user_clarifications) == 1
    started = [event["data"]["stage_id"] for event in events
               if event["event"] == "stage.started"]
    assert started == list(range(1, 9))
    controls = [event for event in events if event["event"] == "control.started"]
    assert len(controls) == 1
    assert controls[0]["data"]["control"] == "user_question"


def test_production_question_uses_first_resume_issue_and_reaches_all_downstream_tools():
    issue = "项目职责边界不清，缺少本人负责环节"
    executor = RecordingExecutor(potential_issues=[issue, "第二个问题不应再追问"])
    resume_agent, _, events = _run_agent(
        executor,
        production_question=True,
    )

    assert executor.calls.count("ask_user") == 1
    assert issue in executor.arguments_by_tool["ask_user"][0]["question"]
    assert len(resume_agent.user_clarifications) == 1
    for tool_name in ("calculate_match", "generate_suggestions", "verify_output"):
        resume_info = executor.arguments_by_tool[tool_name][0]["resume_info"]
        clarifications = resume_info["user_clarifications"]
        assert len(clarifications) == 1
        assert clarifications[0]["answer"] == "Fixed mock answer"
        assert "不得夸大" in clarifications[0]["note"]
    controls = [event for event in events if event["event"] == "control.started"]
    assert len(controls) == 1


def test_production_question_is_skipped_when_resume_has_no_issues():
    executor = RecordingExecutor(potential_issues=[])
    resume_agent, _, events = _run_agent(
        executor,
        production_question=True,
    )
    assert "ask_user" not in executor.calls
    assert resume_agent.user_clarifications == []
    assert not [event for event in events if event["event"] == "control.started"]


def test_skipped_timed_out_or_blank_question_answer_is_not_a_trusted_fact():
    neutral = (
        "（用户未提供补充信息。请基于现有内容继续分析，"
        "并在最终报告中标注该信息缺失。）"
    )
    non_answers = (
        {
            "answer": neutral,
            "answered": False,
            "timed_out": False,
            "skipped": True,
        },
        {
            "answer": neutral,
            "answered": False,
            "timed_out": True,
            "skipped": False,
        },
        {
            "answer": "   ",
            "answered": True,
            "timed_out": False,
            "skipped": False,
        },
    )
    for ask_result in non_answers:
        executor = RecordingExecutor(
            potential_issues=["需要用户确认的信息"],
            ask_result=ask_result,
        )
        resume_agent, _, _ = _run_agent(
            executor,
            production_question=True,
        )
        assert executor.calls.count("ask_user") == 1
        assert resume_agent.user_clarifications == []
        for tool_name in (
            "calculate_match", "generate_suggestions", "verify_output"
        ):
            resume_info = executor.arguments_by_tool[tool_name][0]["resume_info"]
            assert "user_clarifications" not in resume_info


def test_legacy_nonempty_question_answer_remains_backward_compatible():
    executor = RecordingExecutor(
        potential_issues=["需要用户确认的信息"],
        ask_result={
            "answer": "Legacy real answer",
            "answered": None,
            "timed_out": False,
            "skipped": False,
        },
    )
    resume_agent, _, _ = _run_agent(
        executor,
        production_question=True,
    )
    assert resume_agent.user_clarifications[0]["answer"] == "Legacy real answer"
    merged = executor.arguments_by_tool["calculate_match"][0]["resume_info"]
    assert merged["user_clarifications"][0]["answer"] == "Legacy real answer"


def test_terminal_output_keeps_web_compatible_step_and_tool_lines():
    output = io.StringIO()
    with redirect_stdout(output):
        _run_agent(RecordingExecutor())
    text = output.getvalue()
    assert "--- 步骤 1/8 ---" in text
    assert "--- 步骤 8/8 ---" in text
    for tool_name in (
        "extract_resume_info", "analyze_jd", "calculate_match",
        "generate_suggestions", "verify_output",
    ):
        assert f"🔧 调用工具：{tool_name}" in text


def test_trace_routing_events_precede_their_stdout_echoes():
    output = io.StringIO()
    with redirect_stdout(output):
        _run_agent(RecordingExecutor())
    lines = output.getvalue().splitlines()

    stage_trace = next(
        index for index, line in enumerate(lines)
        if '"event":"stage.started"' in line and '"stage_id":1' in line
    )
    stage_header = lines.index("--- 步骤 1/8 ---")
    assert stage_trace < stage_header

    tool_trace = next(
        index for index, line in enumerate(lines)
        if '"event":"tool.started"' in line and '"name":"extract_resume_info"' in line
    )
    tool_line = lines.index("🔧 调用工具：extract_resume_info")
    assert tool_trace < tool_line


def test_tool_clients_are_thread_local_but_explicit_injection_still_wins():
    from tools import common

    created = []

    class FakeClient:
        pass

    def factory(model=None, reasoning=None):
        client = FakeClient()
        client.model = model
        client.reasoning = reasoning
        created.append(client)
        return client

    common._client = None
    common._thread_clients = threading.local()
    barrier = threading.Barrier(2)

    def get_from_worker():
        first = common.get_client()
        barrier.wait(timeout=1)
        second = common.get_client()
        assert first is second
        return first

    with patch.object(common, "LLMClient", side_effect=factory):
        with ThreadPoolExecutor(max_workers=2) as pool:
            clients = [future.result() for future in (
                pool.submit(get_from_worker), pool.submit(get_from_worker)
            )]
    assert clients[0] is not clients[1]
    assert len(created) == 2
    assert all(
        (client.model, client.reasoning) == ("gpt-5.5", "xhigh")
        for client in created
    )

    injected = FakeClient()
    common._client = injected
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert [future.result() for future in (
                pool.submit(common.get_client), pool.submit(common.get_client)
            )] == [injected, injected]
    finally:
        common._client = None
        common._thread_clients = threading.local()


def main():
    tests = [
        test_orchestrator_policy_defaults_to_pipeline_and_is_exact,
        test_supplied_jd_runs_eight_local_stages_without_planner_calls,
        test_pipeline_never_completes_with_unusable_suggestions,
        test_supplied_jd_extract_and_analysis_overlap_on_distinct_threads,
        test_inverse_worker_completion_still_routes_parallel_observations_to_steps,
        test_parallel_watchdog_does_not_wait_for_blocked_worker_before_partial_report,
        test_terminal_trace_deadline_does_not_discard_saved_report,
        test_tool_json_calls_receive_external_run_deadline,
        test_llm_client_rejects_an_already_expired_external_run_deadline,
        test_job_search_discovers_only_without_jd_and_prefers_description,
        test_job_search_keeps_legacy_typical_jd_fallback,
        test_job_search_mode_passes_preferences_to_discovery_and_match,
        test_pipeline_passes_preferences_and_explicit_gate_evidence_to_match,
        test_match_arguments_discard_planner_preferences_without_user_source,
        test_missing_recommended_job_description_starts_then_fails_stage_four,
        test_failed_verification_repeats_only_stages_six_and_seven_once,
        test_revision_without_required_fixes_uses_synthesized_correction_issue,
        test_zero_revision_budget_disables_pipeline_revision,
        test_parallel_failure_preserves_successful_branch_and_returns_partial_report,
        test_first_stage_failure_still_delivers_a_local_partial_report,
        test_deadline_after_completed_stages_delivers_safe_partial_report,
        test_execute_tool_never_swallows_run_deadline,
        test_mock_question_is_a_control_event_not_a_numbered_stage,
        test_production_question_uses_first_resume_issue_and_reaches_all_downstream_tools,
        test_production_question_is_skipped_when_resume_has_no_issues,
        test_skipped_timed_out_or_blank_question_answer_is_not_a_trusted_fact,
        test_legacy_nonempty_question_answer_remains_backward_compatible,
        test_terminal_output_keeps_web_compatible_step_and_tool_lines,
        test_trace_routing_events_precede_their_stdout_echoes,
        test_tool_clients_are_thread_local_but_explicit_injection_still_wins,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print("All deterministic pipeline tests passed.")


if __name__ == "__main__":
    main()
