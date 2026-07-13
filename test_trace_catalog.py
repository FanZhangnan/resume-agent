"""可观测事件与可靠性预算测试（纯离线）。"""

import asyncio
import io
import json
import os
import stat
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from starlette.requests import Request

import config
import agent as agent_module
import llm_client
from agent import ResumeAgent, RunDeadlineExceeded
from llm_client import LLMClient
from tools import common, interaction
from trace_catalog import (
    TRACE_PREFIX,
    TraceCatalog,
    _reset_trace_catalog_for_tests,
    emit_trace,
)
from webui import server


def _read_trace(trace_dir):
    path = Path(trace_dir) / "trace.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _assert_raises(expected, function):
    try:
        function()
    except expected as error:
        return error
    raise AssertionError(f"Expected {expected.__name__}")


def test_trace_catalog_writes_ordered_redacted_events():
    secret = "sk-live-never-write-this"
    non_sk_secret = "gateway-live-secret-without-known-prefix"
    resume = "姓名：李明\n电话：13800000000"
    with tempfile.TemporaryDirectory() as temp_dir:
        output = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {"AGENT_RUN_ID": "run-fixed-123", "AGENT_TRACE_DIR": temp_dir},
                clear=False,
            ),
            redirect_stdout(output),
        ):
            catalog = TraceCatalog()
            first = catalog.emit(
                "run.started",
                span="run",
                data={
                    "mode": "custom",
                    "api_key": secret,
                    "apiKey": non_sk_secret,
                    "key": "plain-key-secret",
                    "accessKey": "access-key-secret",
                    "plain key": "spaced-key-secret",
                    "monkey": "ordinary-business-value",
                    "keyboard": "ordinary-device-value",
                    "resume_text": resume,
                    "nested": {"prompt": "private prompt", "count": 2},
                },
            )
            second = catalog.emit(
                "run.completed",
                level="info",
                span="run",
                parent=None,
                step=3,
                data={"status": "ok"},
            )

        trace_path = Path(temp_dir) / "trace.jsonl"
        lines = trace_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        persisted = [json.loads(line) for line in lines]
        mirrored = [
            json.loads(line[len(TRACE_PREFIX):])
            for line in output.getvalue().splitlines()
            if line.startswith(TRACE_PREFIX)
        ]
        assert persisted == mirrored == [first, second]

        assert [event["seq"] for event in persisted] == [1, 2]
        assert persisted[0]["mono_ms"] <= persisted[1]["mono_ms"]
        assert all(event["schema"] == "resume-agent.trace.v1" for event in persisted)
        assert all(event["run_id"] == "run-fixed-123" for event in persisted)
        assert all(event["ts"].endswith("Z") for event in persisted)
        assert set(persisted[0]) == {
            "schema", "run_id", "seq", "ts", "mono_ms", "event", "level",
            "span", "parent", "step", "data",
        }

        serialized = "\n".join(lines)
        assert secret not in serialized
        assert non_sk_secret not in serialized
        assert "plain-key-secret" not in serialized
        assert "access-key-secret" not in serialized
        assert "spaced-key-secret" not in serialized
        assert resume not in serialized
        assert "private prompt" not in serialized
        assert persisted[0]["data"]["api_key"] == "[REDACTED]"
        assert persisted[0]["data"]["apiKey"] == "[REDACTED]"
        assert persisted[0]["data"]["key"] == "[REDACTED]"
        assert persisted[0]["data"]["accessKey"] == "[REDACTED]"
        assert persisted[0]["data"]["plain key"] == "[REDACTED]"
        assert persisted[0]["data"]["monkey"] == "ordinary-business-value"
        assert persisted[0]["data"]["keyboard"] == "ordinary-device-value"
        assert persisted[0]["data"]["resume_text"] == "[REDACTED]"
        assert persisted[0]["data"]["nested"]["prompt"] == "[REDACTED]"
        assert persisted[0]["data"]["nested"]["count"] == 2

        assert stat.S_IMODE(trace_path.stat().st_mode) & 0o077 == 0
        assert stat.S_IMODE(trace_path.parent.stat().st_mode) & 0o077 == 0


def test_emit_trace_falls_back_to_redacted_stdout_when_storage_is_unavailable():
    secret = "sk-live-must-never-reach-stdout"
    private_prompt = "private resume prompt must stay private"
    failure_targets = (
        "trace_catalog.Path.mkdir",
        "trace_catalog.os.open",
    )

    for index, failure_target in enumerate(failure_targets, start=1):
        output = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "AGENT_RUN_ID": f"stdout-fallback-{index}",
                    "AGENT_TRACE_DIR": "/var/task/output/traces",
                },
                clear=False,
            ),
            patch(failure_target, side_effect=PermissionError("read-only filesystem")),
            redirect_stdout(output),
        ):
            _reset_trace_catalog_for_tests()
            record = emit_trace(
                "llm.call.started",
                span="llm:test",
                data={
                    "api_key": secret,
                    "prompt": private_prompt,
                    "attempt": 1,
                },
            )
        _reset_trace_catalog_for_tests()

        trace_lines = [
            line for line in output.getvalue().splitlines()
            if line.startswith(TRACE_PREFIX)
        ]
        assert len(trace_lines) == 1
        mirrored = json.loads(trace_lines[0][len(TRACE_PREFIX):])
        assert record == mirrored
        assert mirrored["event"] == "llm.call.started"
        assert mirrored["data"] == {
            "api_key": "[REDACTED]",
            "prompt": "[REDACTED]",
            "attempt": 1,
        }
        assert secret not in output.getvalue()
        assert private_prompt not in output.getvalue()


class _FailingCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError("gateway unavailable")


def test_llm_owns_exactly_two_attempts_and_disables_sdk_retries():
    constructor = {}
    completions = _FailingCompletions()

    def fake_openai(**kwargs):
        constructor.update(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with tempfile.TemporaryDirectory() as trace_dir, patch.dict(
        os.environ,
        {
            "AGENT_MOCK": "0",
            "AGENT_RUN_ID": "llm-retry-run",
            "AGENT_TRACE_DIR": trace_dir,
        },
        clear=False,
    ):
        _reset_trace_catalog_for_tests()
        with (
            patch("llm_client.OpenAI", side_effect=fake_openai),
            patch.object(config, "API_KEY", "test-only-key"),
            patch.object(config, "STREAMING", False),
            patch.object(config, "MAX_RETRIES", 2),
            patch.object(config, "RETRY_DELAY", 0),
            patch.object(config, "RETRY_DELAY_CAP", 0),
            patch.object(config, "CALL_DEADLINE", 10),
            patch("llm_client.time.sleep", return_value=None),
        ):
            client = LLMClient()
            _assert_raises(
                RuntimeError,
                lambda: client.chat(
                    [{"role": "user", "content": "raw prompt must not persist"}],
                    operation="retry.contract",
                ),
            )

        assert constructor["max_retries"] == 0
        assert len(completions.calls) == 2
        assert all(0 < call["timeout"] <= 10 for call in completions.calls)
        events = _read_trace(trace_dir)
        assert [event["event"] for event in events] == [
            "llm.call.started",
            "llm.call.retry",
            "llm.call.started",
            "llm.call.failure",
        ]
        assert [events[0]["data"]["attempt"], events[2]["data"]["attempt"]] == [1, 2]
        assert all(
            event["data"]["operation"] == "retry.contract" for event in events
        )
        metric_keys = {
            "ttft_ms", "finish_reason", "usage_available", "input_tokens",
            "output_tokens", "total_tokens", "tool_call_count",
        }
        for event in (events[1], events[3]):
            assert metric_keys <= set(event["data"])
            assert event["data"]["ttft_ms"] is None
            assert event["data"]["finish_reason"] is None
            assert event["data"]["usage_available"] is False
            assert event["data"]["input_tokens"] is None
            assert event["data"]["output_tokens"] is None
            assert event["data"]["total_tokens"] is None
            assert event["data"]["tool_call_count"] is None
        assert "raw prompt must not persist" not in json.dumps(events, ensure_ascii=False)


def test_llm_completion_trace_includes_usage_and_finish_reason():
    usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18)
    choice = SimpleNamespace(
        finish_reason="stop",
        message=SimpleNamespace(content="private model response", tool_calls=None),
    )
    response = SimpleNamespace(choices=[choice], usage=usage)
    completions = SimpleNamespace(create=lambda **kwargs: response)

    with tempfile.TemporaryDirectory() as trace_dir, patch.dict(
        os.environ,
        {
            "AGENT_MOCK": "0",
            "AGENT_RUN_ID": "llm-success-run",
            "AGENT_TRACE_DIR": trace_dir,
        },
        clear=False,
    ):
        _reset_trace_catalog_for_tests()
        with (
            patch(
                "llm_client.OpenAI",
                return_value=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
            ),
            patch.object(config, "API_KEY", "test-only-key"),
            patch.object(config, "STREAMING", False),
            patch.object(config, "MAX_RETRIES", 2),
            patch.object(config, "CALL_DEADLINE", 10),
        ):
            client = LLMClient()
            message = client.chat(
                [{"role": "user", "content": "private prompt"}],
                operation="usage.contract",
                max_tokens=321,
            )

        assert message.content == "private model response"
        completed = _read_trace(trace_dir)[-1]
        assert completed["event"] == "llm.call.completed"
        assert completed["data"]["finish_reason"] == "stop"
        assert completed["data"]["usage_available"] is True
        assert completed["data"]["input_tokens"] == 11
        assert completed["data"]["output_tokens"] == 7
        assert completed["data"]["total_tokens"] == 18
        assert completed["data"]["max_tokens"] == 321
        assert completed["data"]["ttft_ms"] is None
        assert "private model response" not in json.dumps(
            _read_trace(trace_dir), ensure_ascii=False
        )


def test_stream_consumption_honors_call_deadline():
    request_timeouts = []

    def delayed_stream(**kwargs):
        request_timeouts.append(kwargs["timeout"])

        def chunks():
            time.sleep(0.02)
            delta = SimpleNamespace(content="late", tool_calls=None)
            choice = SimpleNamespace(delta=delta, finish_reason=None)
            yield SimpleNamespace(usage=None, choices=[choice])

        return chunks()

    with tempfile.TemporaryDirectory() as trace_dir, patch.dict(
        os.environ,
        {
            "AGENT_MOCK": "0",
            "AGENT_RUN_ID": "llm-deadline-run",
            "AGENT_TRACE_DIR": trace_dir,
        },
        clear=False,
    ):
        _reset_trace_catalog_for_tests()
        client = object.__new__(LLMClient)
        client.mock_mode = False
        client.streaming = True
        client.last_finish_reason = None
        client.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=delayed_stream),
            )
        )
        with (
            patch.object(config, "MAX_RETRIES", 1),
            patch.object(config, "CALL_DEADLINE", 0.005),
        ):
            _assert_raises(
                TimeoutError,
                lambda: client.chat(
                    [{"role": "user", "content": "private"}],
                    operation="deadline.contract",
                ),
            )

        assert len(request_timeouts) == 1
        assert 0 < request_timeouts[0] <= 0.005
        assert _read_trace(trace_dir)[-1]["event"] == "llm.call.failure"


def _stream_chunk(content="ok", finish_reason="stop"):
    delta = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(usage=None, choices=[choice])


def test_stream_fallback_classifier_requires_explicit_unsupported_error():
    assert llm_client._is_streaming_unsupported(
        RuntimeError("unsupported parameter: stream")
    ) is True
    assert llm_client._is_streaming_unsupported(
        RuntimeError("streaming is not supported by this endpoint")
    ) is True
    assert llm_client._is_streaming_unsupported(
        RuntimeError("upstream overloaded")
    ) is False
    assert llm_client._is_streaming_unsupported(
        RuntimeError("stream connection closed")
    ) is False


def test_upstream_error_retries_without_disabling_streaming():
    calls = []

    def create(**kwargs):
        calls.append(bool(kwargs.get("stream")))
        if len(calls) == 1:
            raise RuntimeError("upstream overloaded")
        return iter([_stream_chunk()])

    with tempfile.TemporaryDirectory() as trace_dir, patch.dict(
        os.environ,
        {"AGENT_RUN_ID": "upstream-retry", "AGENT_TRACE_DIR": trace_dir},
        clear=False,
    ):
        _reset_trace_catalog_for_tests()
        client = object.__new__(LLMClient)
        client.mock_mode = False
        client.streaming = True
        client.last_finish_reason = None
        client.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        with (
            patch.object(config, "MAX_RETRIES", 2),
            patch.object(config, "RETRY_DELAY", 0),
            patch.object(config, "RETRY_DELAY_CAP", 0),
            patch.object(config, "CALL_DEADLINE", 1),
        ):
            message = client.chat([{"role": "user", "content": "private"}])

        assert message.content == "ok"
        assert calls == [True, True]
        assert client.streaming is True
        retry = next(
            event for event in _read_trace(trace_dir)
            if event["event"] == "llm.call.retry"
        )
        assert retry["data"]["reason"] == "network_error"


def test_explicit_stream_unsupported_error_falls_back_once():
    calls = []
    response = SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content="ok", tool_calls=None),
        )],
        usage=None,
    )

    def create(**kwargs):
        calls.append(bool(kwargs.get("stream")))
        if kwargs.get("stream"):
            raise RuntimeError("unsupported parameter: stream")
        return response

    with tempfile.TemporaryDirectory() as trace_dir, patch.dict(
        os.environ,
        {"AGENT_RUN_ID": "stream-fallback", "AGENT_TRACE_DIR": trace_dir},
        clear=False,
    ):
        _reset_trace_catalog_for_tests()
        client = object.__new__(LLMClient)
        client.mock_mode = False
        client.streaming = True
        client.last_finish_reason = None
        client.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        with (
            patch.object(config, "MAX_RETRIES", 2),
            patch.object(config, "CALL_DEADLINE", 1),
        ):
            message = client.chat([{"role": "user", "content": "private"}])

        assert message.content == "ok"
        assert calls == [True, False]
        assert client.streaming is False
        retry = next(
            event for event in _read_trace(trace_dir)
            if event["event"] == "llm.call.retry"
        )
        assert retry["data"]["reason"] == "stream_fallback"


def test_deadline_expiry_during_failed_request_still_traces_failure():
    def delayed_failure(**kwargs):
        time.sleep(0.01)
        raise RuntimeError("late gateway failure")

    with tempfile.TemporaryDirectory() as trace_dir, patch.dict(
        os.environ,
        {
            "AGENT_MOCK": "0",
            "AGENT_RUN_ID": "llm-late-failure-run",
            "AGENT_TRACE_DIR": trace_dir,
        },
        clear=False,
    ):
        _reset_trace_catalog_for_tests()
        client = object.__new__(LLMClient)
        client.mock_mode = False
        client.streaming = False
        client.last_finish_reason = None
        client.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=delayed_failure),
            )
        )
        with (
            patch.object(config, "MAX_RETRIES", 2),
            patch.object(config, "RETRY_DELAY", 0),
            patch.object(config, "RETRY_DELAY_CAP", 0),
            patch.object(config, "CALL_DEADLINE", 0.005),
        ):
            _assert_raises(
                TimeoutError,
                lambda: client.chat(
                    [{"role": "user", "content": "private"}],
                    operation="late.failure.contract",
                ),
            )

        events = _read_trace(trace_dir)
        assert events[-1]["event"] == "llm.call.failure"
        assert events[-1]["data"]["error_category"] == "timeout"


def test_ask_user_times_out_to_existing_neutral_answer():
    expected = "（用户未提供补充信息。请基于现有内容继续分析，并在最终报告中标注该信息缺失。）"
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd, "r", encoding="utf-8") as read_stream, tempfile.TemporaryDirectory() as trace_dir, patch.dict(
            os.environ,
            {"AGENT_RUN_ID": "ask-timeout-run", "AGENT_TRACE_DIR": trace_dir},
            clear=False,
        ):
            _reset_trace_catalog_for_tests()
            with (
                patch.object(interaction.sys, "stdin", read_stream),
                patch.object(config, "ASK_TIMEOUT", 0.01),
                patch("builtins.input", side_effect=AssertionError("input must not block")),
            ):
                result = interaction.ask_user("private question", "private context")

            assert result == {
                "success": True,
                "question": "private question",
                "answer": expected,
                "answered": False,
                "timed_out": True,
                "skipped": False,
            }
            events = _read_trace(trace_dir)
            assert [event["event"] for event in events] == [
                "user_wait.started",
                "user_wait.completed",
            ]
            serialized = json.dumps(events, ensure_ascii=False)
            assert "private question" not in serialized
            assert "private context" not in serialized
            assert expected not in serialized
            assert events[-1]["data"]["timed_out"] is True
            assert events[-1]["data"]["answered"] is False
            assert events[-1]["data"]["skipped"] is False
    finally:
        os.close(write_fd)


def test_ask_user_distinguishes_skip_from_real_answer():
    cases = (
        (("", False), False, True, interaction.NEUTRAL_MISSING_INFO_ANSWER),
        (("verified result", False), True, False, "verified result"),
    )
    for timed_input, answered, skipped, expected_answer in cases:
        with tempfile.TemporaryDirectory() as trace_dir, patch.dict(
            os.environ,
            {"AGENT_RUN_ID": "ask-state-run", "AGENT_TRACE_DIR": trace_dir},
            clear=False,
        ):
            _reset_trace_catalog_for_tests()
            with patch.object(interaction, "_timed_input", return_value=timed_input):
                result = interaction.ask_user("private question")
        assert result["answer"] == expected_answer
        assert result["answered"] is answered
        assert result["timed_out"] is False
        assert result["skipped"] is skipped


class _ReaderProcess:
    def __init__(self, payload):
        self.stdout = io.BytesIO(payload)
        self.returncode = None

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15


class _FailingStdout:
    def read(self, size=-1):
        raise RuntimeError("private reader payload")


class _FailingReaderProcess(_ReaderProcess):
    def __init__(self):
        super().__init__(b"")
        self.stdout = _FailingStdout()


class _AsyncTerminateReaderProcess(_FailingReaderProcess):
    def __init__(self, lifecycle):
        super().__init__()
        self.lifecycle = lifecycle

    def terminate(self):
        self.lifecycle.append("terminate")

    def wait(self, timeout=None):
        self.lifecycle.append(("wait", timeout))
        self.returncode = -15
        return self.returncode


class _IgnoreTerminateReaderProcess(_FailingReaderProcess):
    def __init__(self, lifecycle):
        super().__init__()
        self.lifecycle = lifecycle

    def terminate(self):
        self.lifecycle.append("terminate")

    def kill(self):
        self.lifecycle.append("kill")

    def wait(self, timeout=None):
        self.lifecycle.append(("wait", timeout))
        if timeout is not None:
            raise server.subprocess.TimeoutExpired("agent.py", timeout)
        self.returncode = -9
        return self.returncode


class _ConcurrentWaitProcess(_ReaderProcess):
    def __init__(self):
        super().__init__(b"")
        self.reader_wait_started = server.threading.Event()
        self.release_reader = server.threading.Event()
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.returncode = -9
        self.release_reader.set()

    def wait(self, timeout=None):
        if timeout is None:
            self.reader_wait_started.set()
            self.release_reader.wait()
            if self.returncode is None:
                self.returncode = 0
            return self.returncode
        self.returncode = -15
        self.release_reader.set()
        return self.returncode


def test_server_parses_trace_lines_before_stdout_suppression():
    trace_event = {
        "schema": "resume-agent.trace.v1",
        "run_id": "server-run-1",
        "seq": 9,
        "event": "run.completed",
        "data": {"status": "ok"},
    }
    payload = (
        "💾 报告已保存：does-not-exist.md\n"
        + TRACE_PREFIX
        + json.dumps(trace_event)
        + "\nordinary suppressed line\n"
    ).encode("utf-8")
    job = server.Job(
        _ReaderProcess(payload),
        {"run_id": "server-run-1", "ephemeral": False},
    )

    server._reader_thread(job)

    assert job.id == "server-run-1"
    assert {"type": "trace", "event": trace_event} in job.events
    assert trace_event in job.trace_events


def _consume_sse(response):
    async def consume():
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    return asyncio.run(consume())


def test_reader_failure_always_finalizes_sse_and_burns_once():
    lifecycle = []
    process = _AsyncTerminateReaderProcess(lifecycle)
    job = server.Job(
        process,
        {
            "run_id": "reader-failure",
            "ephemeral": True,
            "run_dir": str(server.RUNS_DIR / "reader-failure"),
            "temp_files": ["private-resume-path"],
        },
    )
    burn_calls = []

    def record_burn(item):
        lifecycle.append("burn")
        burn_calls.append(item)

    with patch("webui.server._burn", side_effect=record_burn):
        server._reader_thread(job)

    assert job.done is True
    assert job.finished.is_set()
    assert job.exit_code == -15
    assert burn_calls == [job]
    assert lifecycle == [
        "terminate",
        ("wait", server.PROCESS_TERMINATE_TIMEOUT),
        "burn",
    ]
    reader_errors = [event for event in job.events if event.get("type") == "reader_error"]
    exits = [event for event in job.events if event.get("type") == "exit"]
    assert reader_errors == [{"type": "reader_error", "error_class": "RuntimeError"}]
    assert exits == [{"type": "exit", "code": -15}]
    assert "private reader payload" not in json.dumps(job.events, ensure_ascii=False)

    with patch.dict(server.JOBS, {job.id: job}, clear=True):
        streamed = _consume_sse(server.events(job.id, _request()))
    assert '"type": "reader_error"' in streamed
    assert '"type": "exit"' in streamed


def test_reader_kills_and_reaps_process_that_ignores_terminate():
    lifecycle = []
    process = _IgnoreTerminateReaderProcess(lifecycle)
    job = server.Job(
        process,
        {"run_id": "reader-kill", "ephemeral": True},
    )

    with patch(
        "webui.server._burn",
        side_effect=lambda item: lifecycle.append("burn"),
    ):
        server._reader_thread(job)

    assert lifecycle == [
        "terminate",
        ("wait", server.PROCESS_TERMINATE_TIMEOUT),
        "kill",
        ("wait", None),
        "burn",
    ]
    assert job.exit_code == -9
    assert len([event for event in job.events if event.get("type") == "exit"]) == 1
    assert job.done is True
    assert job.finished.is_set()


def test_watchdog_can_terminate_while_reader_waits_without_locking_it_out():
    process = _ConcurrentWaitProcess()
    job = server.Job(process, {"run_id": "reader-watchdog-race"})
    watchdog_done = server.threading.Event()
    reader_thread = server.threading.Thread(target=server._reader_thread, args=(job,))

    def run_watchdog():
        try:
            server._job_watchdog(job, 0, 0)
        finally:
            watchdog_done.set()

    watchdog_thread = server.threading.Thread(target=run_watchdog)
    reader_thread.start()
    assert process.reader_wait_started.wait(0.2)
    watchdog_thread.start()
    completed_without_reader_release = watchdog_done.wait(0.05)
    if not completed_without_reader_release:
        process.release_reader.set()
    watchdog_thread.join(0.2)
    reader_thread.join(0.2)

    assert completed_without_reader_release is True
    assert process.terminated is True
    assert job.exit_code == -15
    assert not reader_thread.is_alive()
    assert not watchdog_thread.is_alive()
    assert job.done is True


def test_reader_normal_public_path_burns_and_exits_once():
    job = server.Job(
        _ReaderProcess(b"normal line\n"),
        {
            "run_id": "reader-normal",
            "ephemeral": True,
            "run_dir": str(server.RUNS_DIR / "reader-normal"),
        },
    )
    burn_calls = []

    with patch("webui.server._burn", side_effect=lambda item: burn_calls.append(item)):
        server._reader_thread(job)

    assert burn_calls == [job]
    assert not [event for event in job.events if event.get("type") == "reader_error"]
    assert len([event for event in job.events if event.get("type") == "exit"]) == 1
    assert job.done is True
    assert job.finished.is_set()


def test_corrupt_optional_report_struct_does_not_fail_markdown_report():
    markdown = "# Safe markdown report"
    private_corrupt_json = '{"victim@example.com":'
    with tempfile.TemporaryDirectory(dir=server.PROJECT_DIR) as temp_dir:
        report_path = Path(temp_dir) / "resume_report_20260712_000000.md"
        report_path.write_text(markdown, encoding="utf-8")
        report_path.with_suffix(".json").write_text(
            private_corrupt_json,
            encoding="utf-8",
        )
        payload = f"💾 报告已保存：{report_path}\n".encode("utf-8")
        job = server.Job(
            _ReaderProcess(payload),
            {"run_id": "corrupt-struct", "ephemeral": False},
        )

        server._reader_thread(job)

    warnings = [
        event for event in job.events
        if event.get("type") == "report_struct_error"
    ]
    reports = [event for event in job.events if event.get("type") == "report"]
    assert warnings == [{
        "type": "report_struct_error",
        "error_class": "JSONDecodeError",
    }]
    assert len(reports) == 1
    assert reports[0]["content"] == markdown
    assert reports[0]["struct"] is None
    assert not [event for event in job.events if event.get("type") == "reader_error"]
    assert [event for event in job.events if event.get("type") == "exit"] == [
        {"type": "exit", "code": 0}
    ]
    assert private_corrupt_json not in json.dumps(job.events, ensure_ascii=False)
    assert job.done is True
    assert job.finished.is_set()


class _SpawnedProcess:
    def __init__(self):
        self.stdout = io.BytesIO()
        self.stdin = io.BytesIO()
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class _ThreadCapture:
    created = []

    def __init__(self, target=None, args=(), daemon=None):
        self.target = target
        self.args = args
        self.daemon = daemon
        self.started = False
        self.__class__.created.append(self)

    def start(self):
        self.started = True


def _request():
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/api/run",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    })


def test_server_assigns_run_id_and_trace_dir_before_spawn():
    captured = {}
    process = _SpawnedProcess()

    def fake_popen(cmd, cwd, env, stdin, stdout, stderr):
        captured.update({"cmd": cmd, "cwd": cwd, "env": env})
        return process

    _ThreadCapture.created = []
    with (
        patch("webui.server.subprocess.Popen", side_effect=fake_popen),
        patch("webui.server.threading.Thread", _ThreadCapture),
        patch.dict(server.JOBS, {}, clear=True),
    ):
        result = server.run(
            _request(),
            mode="demo",
            resume_text="",
            jd_text="",
            preferences="",
            mock="1",
            api_key="",
            base_url="",
            model="",
            reasoning="",
            resume_file=None,
        )
        spawned_job = server.JOBS[result["job_id"]]

    job_id = result["job_id"]
    assert captured["env"]["AGENT_RUN_ID"] == job_id
    assert Path(captured["env"]["AGENT_TRACE_DIR"]).name == job_id
    assert spawned_job.id == job_id
    targets = [thread.target for thread in _ThreadCapture.created]
    assert server._reader_thread in targets
    assert server._job_watchdog in targets
    watchdog = next(
        thread for thread in _ThreadCapture.created
        if thread.target is server._job_watchdog
    )
    assert watchdog.args == (
        spawned_job,
        config.RUN_TIMEOUT,
        config.WATCHDOG_GRACE,
    )


def test_job_watchdog_terminates_at_configured_deadline():
    process = _SpawnedProcess()
    job = server.Job(process, {"run_id": "watchdog-run"})
    started = time.monotonic()

    server._job_watchdog(job, 0.01, 0.005)

    elapsed = time.monotonic() - started
    assert 0.013 <= elapsed < 0.5
    assert process.terminated is True
    timeout_event = next(
        event for event in job.events if event.get("type") == "timeout"
    )
    assert timeout_event == {
        "type": "timeout",
        "reason": "run_timeout_grace_exceeded",
        "budget_seconds": 0.01,
        "grace_seconds": 0.005,
        "hard_timeout_seconds": 0.015,
    }


class _SemanticClient:
    def __init__(self, responses, finish_reason="stop"):
        self.responses = iter(responses)
        self.last_finish_reason = finish_reason
        self.operations = []

    def simple_ask(self, **kwargs):
        self.operations.append(kwargs.get("operation"))
        return next(self.responses)


def test_semantic_json_retry_is_distinct_from_network_retry():
    fake = _SemanticClient(["not-json", '{"value": 7}'])
    original_client = common._client
    with tempfile.TemporaryDirectory() as trace_dir, patch.dict(
        os.environ,
        {"AGENT_RUN_ID": "semantic-run", "AGENT_TRACE_DIR": trace_dir},
        clear=False,
    ):
        _reset_trace_catalog_for_tests()
        common._client = fake
        try:
            result = common.ask_json(
                "private prompt body",
                "private system body",
                {"value": 0},
                label="extract_resume",
            )
        finally:
            common._client = original_client

        assert result == {"value": 7}
        assert fake.operations == ["tool.extract_resume", "tool.extract_resume"]
        events = _read_trace(trace_dir)
        assert [event["event"] for event in events] == ["llm.semantic_json.retry"]
        assert events[0]["data"]["reason"] == "invalid_json"
        assert events[0]["data"]["attempt"] == 1
        serialized = json.dumps(events, ensure_ascii=False)
        assert "private prompt body" not in serialized
        assert "private system body" not in serialized
        assert all(event["event"] != "llm.call.retry" for event in events)


def _tool_call(name, arguments, tool_id="tool-call-1"):
    return SimpleNamespace(
        id=tool_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def test_agent_tool_trace_contains_shapes_not_payloads():
    private_resume = "姓名：不应写入trace，手机13800000000"
    private_key = "victim@example.com"
    private_result_key = "result-owner@example.com"
    malicious_tool_id = "call-victim@example.com-private"
    with tempfile.TemporaryDirectory() as trace_dir, patch.dict(
        os.environ,
        {
            "AGENT_MOCK": "1",
            "AGENT_RUN_ID": "tool-run",
            "AGENT_TRACE_DIR": trace_dir,
        },
        clear=False,
    ):
        _reset_trace_catalog_for_tests()
        resume_agent = ResumeAgent(private_resume, "private JD", resume_is_file=False)
        with patch(
            "agent.execute_tool",
            return_value={
                "success": False,
                private_result_key: private_resume,
            },
        ):
            resume_agent._execute_tool_calls(
                "private reasoning",
                [_tool_call(
                    "unknown_tool",
                    {private_key: private_resume, "safe_count": 3},
                    tool_id=malicious_tool_id,
                )],
            )

        events = _read_trace(trace_dir)
        assert [event["event"] for event in events] == [
            "tool.started",
            "tool.completed",
        ]
        assert events[0]["data"]["name"] == "unknown"
        assert events[0]["data"]["arguments"] == {
            "kind": "object",
            "field_count": 2,
            "value_types": {"int": 1, "str": 1},
        }
        assert "tool_call_id" not in events[0]["data"]
        assert events[1]["data"]["result"] == {
            "kind": "object",
            "field_count": 2,
            "value_types": {"bool": 1, "str": 1},
        }
        assert events[1]["data"]["success"] is False
        serialized = json.dumps(events, ensure_ascii=False)
        assert private_resume not in serialized
        assert "private reasoning" not in serialized
        assert private_key not in serialized
        assert private_result_key not in serialized
        assert malicious_tool_id not in serialized


def test_agent_lifecycle_report_and_revision_events_are_summaries():
    with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
        os.environ,
        {
            "AGENT_MOCK": "1",
            "AGENT_RUN_ID": "agent-run",
            "AGENT_TRACE_DIR": str(Path(temp_dir) / "traces"),
        },
        clear=False,
    ):
        _reset_trace_catalog_for_tests()
        resume_agent = ResumeAgent(
            "private resume text",
            "private JD text",
            resume_is_file=False,
            output_dir=str(Path(temp_dir) / "reports"),
        )
        resume_agent._loop = lambda: None
        failed_verification = {
            "passed": False,
            "safe_to_deliver": False,
            "required_fixes": ["private revision detail"],
        }
        passed_verification = {
            "passed": True,
            "safe_to_deliver": True,
            "required_fixes": [],
        }
        resume_agent._handle_verification(failed_verification)
        resume_agent._handle_verification(passed_verification)
        resume_agent.state["suggestions"] = {
            "optimized_resume": "Candidate\nVerified experience",
        }
        resume_agent.state["verification"] = passed_verification

        with patch.object(config, "ORCHESTRATOR", "react"):
            report = resume_agent.run()

        assert report
        events = _read_trace(Path(temp_dir) / "traces")
        names = [event["event"] for event in events]
        assert names[:2] == ["revision.started", "revision.completed"]
        for required in (
            "run.started",
            "report.generate.started",
            "report.generate.completed",
            "report.saved",
            "run.completed",
        ):
            assert required in names
        completed = next(event for event in events if event["event"] == "run.completed")
        assert completed["data"]["status"] == "completed"
        assert completed["data"]["report_available"] is True
        saved = next(event for event in events if event["event"] == "report.saved")
        assert saved["data"]["success"] is True
        assert saved["data"]["bytes"] > 0
        serialized = json.dumps(events, ensure_ascii=False)
        for private in (
            "private resume text",
            "private JD text",
            "private revision detail",
        ):
            assert private not in serialized


def test_agent_fatal_error_records_stage_without_error_payload():
    private_error = "private resume leaked in exception"
    with tempfile.TemporaryDirectory() as trace_dir, patch.dict(
        os.environ,
        {
            "AGENT_MOCK": "1",
            "AGENT_RUN_ID": "agent-error-run",
            "AGENT_TRACE_DIR": trace_dir,
        },
        clear=False,
    ):
        _reset_trace_catalog_for_tests()
        resume_agent = ResumeAgent("private resume", "private JD")

        def fail_loop():
            raise RuntimeError(private_error)

        resume_agent._loop = fail_loop
        with patch.object(config, "ORCHESTRATOR", "react"):
            _assert_raises(RuntimeError, resume_agent.run)
        events = _read_trace(trace_dir)
        assert events[-1]["event"] == "run.error"
        assert events[-1]["data"]["stage"] == "analysis"
        assert events[-1]["data"]["error_class"] == "RuntimeError"
        assert private_error not in json.dumps(events, ensure_ascii=False)


def test_direct_agent_run_enforces_total_wall_clock_timeout():
    with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
        os.environ,
        {
            "AGENT_MOCK": "1",
            "AGENT_RUN_ID": "direct-timeout-run",
            "AGENT_TRACE_DIR": str(Path(temp_dir) / "traces"),
        },
        clear=False,
    ):
        _reset_trace_catalog_for_tests()
        resume_agent = ResumeAgent(
            "private resume",
            "private JD",
            output_dir=str(Path(temp_dir) / "reports"),
        )
        resume_agent._loop = lambda: time.sleep(0.1)
        started = time.monotonic()
        with (
            patch.object(config, "ORCHESTRATOR", "react"),
            patch.object(config, "RUN_TIMEOUT", 0.02),
        ):
            _assert_raises(TimeoutError, resume_agent.run)
        elapsed = time.monotonic() - started

        assert elapsed < 0.08
        events = _read_trace(Path(temp_dir) / "traces")
        assert events[-1]["event"] == "run.error"
        assert events[-1]["data"]["stage"] == "analysis"
        assert events[-1]["data"]["error_category"] == "run_timeout"


def test_run_timeout_is_not_delayed_by_llm_retry_handler():
    choice = SimpleNamespace(
        finish_reason="stop",
        message=SimpleNamespace(content="late", tool_calls=None),
    )
    response = SimpleNamespace(choices=[choice], usage=None)

    def delayed_response(**kwargs):
        time.sleep(0.1)
        return response

    with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
        os.environ,
        {
            "AGENT_MOCK": "1",
            "AGENT_RUN_ID": "run-timeout-llm",
            "AGENT_TRACE_DIR": str(Path(temp_dir) / "traces"),
        },
        clear=False,
    ):
        _reset_trace_catalog_for_tests()
        resume_agent = ResumeAgent("private resume", "private JD")
        real_client = object.__new__(LLMClient)
        real_client.mock_mode = False
        real_client.streaming = False
        real_client.last_finish_reason = None
        real_client.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=delayed_response),
            )
        )
        resume_agent.client = real_client
        started = time.monotonic()
        with (
            patch.object(config, "ORCHESTRATOR", "react"),
            patch.object(config, "RUN_TIMEOUT", 0.02),
            patch.object(config, "CALL_DEADLINE", 1),
            patch.object(config, "MAX_RETRIES", 2),
            patch.object(config, "RETRY_DELAY", 0),
            patch.object(config, "RETRY_DELAY_CAP", 0),
        ):
            _assert_raises(TimeoutError, resume_agent.run)

        assert time.monotonic() - started < 0.08
        events = _read_trace(Path(temp_dir) / "traces")
        assert [event["event"] for event in events] == [
            "run.started",
            "llm.call.started",
            "llm.call.failure",
            "run.error",
        ]
        failures = [event for event in events if event["event"] == "llm.call.failure"]
        assert len(failures) == 1
        failure = failures[0]
        assert failure["span"] == events[1]["span"]
        assert failure["data"]["error_category"] == "run_timeout"
        assert failure["data"]["ttft_ms"] is None
        assert failure["data"]["finish_reason"] is None
        assert failure["data"]["usage_available"] is False
        assert failure["data"]["input_tokens"] is None
        assert failure["data"]["output_tokens"] is None
        assert failure["data"]["total_tokens"] is None
        assert failure["data"]["tool_call_count"] is None
        assert "Agent run exceeded" not in json.dumps(failure, ensure_ascii=False)


def test_run_timeout_is_not_swallowed_by_input_fallback():
    with (
        patch.object(interaction.sys.stdin, "fileno", return_value=0),
        patch.object(
            interaction.select,
            "select",
            side_effect=RunDeadlineExceeded("run deadline"),
        ),
        patch("builtins.input", return_value="must not be returned"),
    ):
        _assert_raises(
            RunDeadlineExceeded,
            lambda: interaction._timed_input("prompt", 1),
        )


def test_terminal_event_writes_atomic_private_summary():
    private_resume = "private resume must not enter summary"
    with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
        os.environ,
        {
            "AGENT_MOCK": "1",
            "AGENT_RUN_ID": "summary-run",
            "AGENT_TRACE_DIR": str(Path(temp_dir) / "traces"),
        },
        clear=False,
    ):
        _reset_trace_catalog_for_tests()
        resume_agent = ResumeAgent(
            private_resume,
            "private JD",
            output_dir=str(Path(temp_dir) / "reports"),
        )
        resume_agent._loop = lambda: None
        with patch.object(config, "ORCHESTRATOR", "react"):
            resume_agent.run()

        summary_path = Path(temp_dir) / "traces" / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert summary["schema"] == "resume-agent.trace.v1"
        assert summary["run_id"] == "summary-run"
        assert summary["status"] == "partial"
        assert summary["terminal_event"] == "run.completed"
        assert summary["timing"]["duration_ms"] >= 0
        assert summary["counts"]["steps"] == 0
        assert summary["counts"]["revision_rounds"] == 0
        assert summary["result"]["report_available"] is True
        assert private_resume not in summary_path.read_text(encoding="utf-8")
        assert stat.S_IMODE(summary_path.stat().st_mode) == 0o600
        assert not list(summary_path.parent.glob(".summary.*.tmp"))


def test_cli_client_construction_failure_emits_terminal_error():
    private_error = "non-sk credential must not enter startup trace"
    with tempfile.TemporaryDirectory() as trace_dir, patch.dict(
        os.environ,
        {
            "AGENT_RUN_ID": "startup-error-run",
            "AGENT_TRACE_DIR": trace_dir,
        },
        clear=False,
    ):
        _reset_trace_catalog_for_tests()
        with (
            patch("sys.argv", ["agent.py", "--text", "private resume", "private JD"]),
            patch("agent.LLMClient", side_effect=RuntimeError(private_error)),
        ):
            _assert_raises(RuntimeError, agent_module.main)

        events = _read_trace(trace_dir)
        assert events[-1]["event"] == "run.error"
        assert events[-1]["data"]["stage"] == "client_initialization"
        assert events[-1]["data"]["error_class"] == "RuntimeError"
        assert events[-1]["data"]["terminal"] is True
        serialized = json.dumps(events, ensure_ascii=False)
        assert private_error not in serialized
        assert "private resume" not in serialized
        assert "private JD" not in serialized

        summary_path = Path(trace_dir) / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert summary["status"] == "error"
        assert summary["result"]["stage"] == "client_initialization"
        assert stat.S_IMODE(summary_path.stat().st_mode) == 0o600


def main():
    test_trace_catalog_writes_ordered_redacted_events()
    test_emit_trace_falls_back_to_redacted_stdout_when_storage_is_unavailable()
    test_llm_owns_exactly_two_attempts_and_disables_sdk_retries()
    test_llm_completion_trace_includes_usage_and_finish_reason()
    test_stream_consumption_honors_call_deadline()
    test_stream_fallback_classifier_requires_explicit_unsupported_error()
    test_upstream_error_retries_without_disabling_streaming()
    test_explicit_stream_unsupported_error_falls_back_once()
    test_deadline_expiry_during_failed_request_still_traces_failure()
    test_ask_user_times_out_to_existing_neutral_answer()
    test_ask_user_distinguishes_skip_from_real_answer()
    test_server_parses_trace_lines_before_stdout_suppression()
    test_reader_failure_always_finalizes_sse_and_burns_once()
    test_reader_kills_and_reaps_process_that_ignores_terminate()
    test_watchdog_can_terminate_while_reader_waits_without_locking_it_out()
    test_reader_normal_public_path_burns_and_exits_once()
    test_corrupt_optional_report_struct_does_not_fail_markdown_report()
    test_server_assigns_run_id_and_trace_dir_before_spawn()
    test_job_watchdog_terminates_at_configured_deadline()
    test_semantic_json_retry_is_distinct_from_network_retry()
    test_agent_tool_trace_contains_shapes_not_payloads()
    test_agent_lifecycle_report_and_revision_events_are_summaries()
    test_agent_fatal_error_records_stage_without_error_payload()
    test_direct_agent_run_enforces_total_wall_clock_timeout()
    test_run_timeout_is_not_delayed_by_llm_retry_handler()
    test_run_timeout_is_not_swallowed_by_input_fallback()
    test_terminal_event_writes_atomic_private_summary()
    test_cli_client_construction_failure_emits_terminal_error()
    print("Trace catalog tests passed")


if __name__ == "__main__":
    main()
