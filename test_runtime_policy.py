"""单次运行模型策略与 deadline 传播测试（纯离线）。"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("AGENT_MOCK", "1")

import config
import tools as tools_module
from agent import ResumeAgent
from llm_client import (
    CallDeadlineExceeded,
    ExternalRunDeadlineExceeded,
    LLMClient,
)
from pipeline import DeterministicPipeline
from runtime_context import (
    RunSettings,
    current_settings,
    monotonic_deadline,
    remaining_seconds,
    use_run_settings,
)
from tools import common


def _assert_raises(error_type, function):
    try:
        function()
    except error_type as error:
        return error
    raise AssertionError(f"未抛出预期异常：{error_type.__name__}")


def test_exact_catalog_defaults_and_budgets():
    assert config.SUPPORTED_MODELS == ("gpt-5.5", "gpt-5.6-terra")
    assert config.MODEL_REASONING_LEVELS == {
        "gpt-5.5": ("high", "xhigh"),
        "gpt-5.6-terra": ("high", "xhigh"),
    }
    assert config.REASONING_LEVELS == ("high", "xhigh")
    assert config.DEFAULT_MODEL == "gpt-5.5"
    assert config.DEFAULT_REASONING_BY_MODEL == {
        "gpt-5.5": "xhigh",
        "gpt-5.6-terra": "xhigh",
    }
    assert (config.MODEL_NAME, config.REASONING_EFFORT) == (
        "gpt-5.5", "xhigh"
    )
    assert config.CALL_DEADLINE == 180
    assert config.RUN_TIMEOUT == 720
    assert config.WATCHDOG_GRACE == 15
    assert config.ASK_TIMEOUT == 45


def test_invalid_model_reasoning_pairs_are_rejected():
    for model in config.SUPPORTED_MODELS:
        for reasoning in ("none", "low", "medium", "max"):
            _assert_raises(
                ValueError,
                lambda model=model, reasoning=reasoning: (
                    config.validate_model_reasoning(model, reasoning)
                ),
            )

    for model, reasoning in (
        ("gpt-5.6-sol", "high"),
        ("gpt-5.6-sol", "xhigh"),
        ("GPT-5.5", "xhigh"),
        ("gpt-5.5", "XHIGH"),
    ):
        _assert_raises(
            ValueError,
            lambda model=model, reasoning=reasoning: (
                config.validate_model_reasoning(model, reasoning)
            ),
        )


def test_run_settings_are_immutable_and_nested_contexts_restore():
    baseline = current_settings()
    outer = RunSettings("gpt-5.6-terra", "high", deadline_epoch=2000.0)
    inner = RunSettings("gpt-5.5", "xhigh", deadline_epoch=1500.0)

    _assert_raises(
        FrozenInstanceError,
        lambda: setattr(outer, "model", "gpt-5.5"),
    )
    with use_run_settings(outer):
        assert current_settings() is outer
        with use_run_settings(inner):
            assert current_settings() is inner
        assert current_settings() is outer
    assert current_settings() == baseline


def test_runtime_budget_converts_epoch_to_monotonic_time():
    settings = RunSettings(
        "gpt-5.5",
        "xhigh",
        deadline_epoch=1040.0,
    )
    with (
        use_run_settings(settings),
        patch("runtime_context.time.time", return_value=1000.0),
        patch("runtime_context.time.monotonic", return_value=50.0),
    ):
        assert remaining_seconds() == 40.0
        assert remaining_seconds(limit=20) == 20.0
        assert monotonic_deadline() == 90.0


def test_run_settings_normalize_api_key_and_keep_it_out_of_repr():
    secret = "runtime-secret-should-never-leak"
    settings = RunSettings(
        "gpt-5.5",
        "xhigh",
        api_key=f"  {secret}  ",
        mock=False,
    )

    assert settings.api_key == secret
    assert settings.mock is False
    assert secret not in repr(settings)
    assert RunSettings("gpt-5.5", "xhigh", api_key=" \t ").api_key is None
    assert RunSettings("gpt-5.5", "xhigh").mock is None


def test_client_explicit_byok_and_real_mode_override_bound_and_global_policy():
    gateway = "https://fixed-gateway.example/v1"
    bound = RunSettings(
        "gpt-5.5",
        "xhigh",
        api_key="bound-runtime-key",
        mock=True,
    )
    with (
        use_run_settings(bound),
        patch.dict(os.environ, {"AGENT_MOCK": "1"}, clear=False),
        patch.object(config, "API_KEY", "site-funded-key"),
        patch.object(config, "API_BASE_URL", gateway),
        patch("llm_client.OpenAI") as openai,
    ):
        client = LLMClient(
            api_key="  explicit-runtime-key  ",
            mock=False,
        )

    assert client.mock_mode is False
    assert openai.call_args.kwargs["api_key"] == "explicit-runtime-key"
    assert openai.call_args.kwargs["base_url"] == gateway


def test_client_uses_bound_byok_and_mock_policy_when_arguments_are_omitted():
    settings = RunSettings(
        "gpt-5.6-terra",
        "high",
        api_key="bound-byok-key",
        mock=False,
    )
    with (
        use_run_settings(settings),
        patch.dict(os.environ, {"AGENT_MOCK": "1"}, clear=False),
        patch.object(config, "API_KEY", "site-funded-key"),
        patch("llm_client.OpenAI") as openai,
    ):
        client = LLMClient()

    assert client.mock_mode is False
    assert openai.call_args.kwargs["api_key"] == "bound-byok-key"


def test_explicit_mock_true_skips_openai_even_when_environment_is_real():
    with (
        patch.dict(os.environ, {"AGENT_MOCK": "0"}, clear=False),
        patch.object(config, "API_KEY", "site-funded-key"),
        patch("llm_client.OpenAI") as openai,
    ):
        client = LLMClient(mock=True)

    assert client.mock_mode is True
    openai.assert_not_called()


def test_real_client_without_byok_uses_site_funded_key_and_preserves_key_error():
    with (
        patch.dict(os.environ, {"AGENT_MOCK": "0"}, clear=False),
        patch.object(config, "API_KEY", "site-funded-key"),
        patch("llm_client.OpenAI") as openai,
    ):
        client = LLMClient(mock=False)

    assert client.mock_mode is False
    assert openai.call_args.kwargs["api_key"] == "site-funded-key"

    with (
        patch.dict(os.environ, {"AGENT_MOCK": "1"}, clear=False),
        patch.object(config, "API_KEY", ""),
        patch("llm_client.OpenAI") as missing_key_openai,
    ):
        error = _assert_raises(ValueError, lambda: LLMClient(mock=False))

    assert "未检测到API密钥" in str(error)
    missing_key_openai.assert_not_called()


def test_client_uses_instance_model_and_reasoning_for_request_and_trace():
    traces = []
    calls = []
    with (
        patch.dict(os.environ, {"AGENT_MOCK": "0"}, clear=False),
        patch.object(config, "API_KEY", "offline-test-key"),
        patch("llm_client.OpenAI"),
        patch("llm_client.emit_trace", side_effect=lambda event, **kwargs: traces.append(
            (event, kwargs)
        )),
    ):
        client = LLMClient(model="gpt-5.6-terra", reasoning="high")
        client.streaming = False

        def respond(kwargs):
            calls.append(dict(kwargs))
            client.last_finish_reason = "stop"
            return SimpleNamespace(content="ok", tool_calls=None)

        client._chat_once = respond
        result = client.chat([{"role": "user", "content": "offline"}])

    assert result.content == "ok"
    assert client.model == "gpt-5.6-terra"
    assert client.reasoning == "high"
    assert calls[0]["model"] == "gpt-5.6-terra"
    assert calls[0]["reasoning_effort"] == "high"
    started = next(item for item in traces if item[0] == "llm.call.started")
    assert started[1]["data"]["model"] == "gpt-5.6-terra"
    assert started[1]["data"]["reasoning"] == "high"


def test_client_rejects_invalid_pair_even_in_mock_mode():
    with patch.dict(os.environ, {"AGENT_MOCK": "1"}, clear=False):
        _assert_raises(
            ValueError,
            lambda: LLMClient(model="gpt-5.6-terra", reasoning="max"),
        )


def test_mock_client_trace_keeps_instance_policy():
    traces = []
    with (
        patch.dict(os.environ, {"AGENT_MOCK": "1"}, clear=False),
        patch("llm_client.emit_trace", side_effect=lambda event, **kwargs: traces.append(
            (event, kwargs)
        )),
    ):
        client = LLMClient(model="gpt-5.6-terra", reasoning="high")
        client.chat([{"role": "user", "content": "offline"}])

    started = next(item for item in traces if item[0] == "llm.call.started")
    assert started[1]["data"]["model"] == "gpt-5.6-terra"
    assert started[1]["data"]["reasoning"] == "high"


def test_tool_clients_are_cached_by_active_model_pair():
    original_client = common._client
    original_thread_clients = common._thread_clients
    common._client = None
    common._thread_clients = threading.local()
    created = []

    def factory(model=None, reasoning=None, api_key=None, mock=None):
        client = SimpleNamespace(
            model=model,
            reasoning=reasoning,
            api_key=api_key,
            mock=mock,
        )
        created.append(client)
        return client

    first_settings = RunSettings("gpt-5.5", "high")
    second_settings = RunSettings("gpt-5.6-terra", "xhigh")
    try:
        with patch.object(common, "LLMClient", side_effect=factory):
            with use_run_settings(first_settings):
                first = common.get_client()
                assert common.get_client() is first
            with use_run_settings(second_settings):
                second = common.get_client()
            with use_run_settings(first_settings):
                assert common.get_client() is first
    finally:
        common._client = original_client
        common._thread_clients = original_thread_clients

    assert second is not first
    assert [(item.model, item.reasoning) for item in created] == [
        ("gpt-5.5", "high"),
        ("gpt-5.6-terra", "xhigh"),
    ]
    assert [(item.api_key, item.mock) for item in created] == [
        (None, None),
        (None, None),
    ]


def test_tool_client_cache_never_retains_byok_clients_or_raw_keys():
    original_client = common._client
    original_thread_clients = common._thread_clients
    common._client = None
    common._thread_clients = threading.local()
    created = []
    first_secret = "sk-runtime-alpha-SUPER-SECRET"
    second_secret = "sk-runtime-beta-SUPER-SECRET"

    def factory(model=None, reasoning=None, api_key=None, mock=None):
        client = SimpleNamespace(
            model=model,
            reasoning=reasoning,
            api_key=api_key,
            mock=mock,
        )
        created.append(client)
        return client

    first_settings = RunSettings(
        "gpt-5.5", "xhigh", api_key=first_secret, mock=False,
    )
    second_settings = RunSettings(
        "gpt-5.5", "xhigh", api_key=second_secret, mock=False,
    )
    mock_settings = RunSettings(
        "gpt-5.5", "xhigh", api_key=second_secret, mock=True,
    )
    try:
        with patch.object(common, "LLMClient", side_effect=factory):
            with use_run_settings(first_settings):
                first = common.get_client()
                first_again = common.get_client()
            with use_run_settings(second_settings):
                second = common.get_client()
            with use_run_settings(mock_settings):
                mock_client = common.get_client()
                assert common.get_client() is mock_client
            with use_run_settings(RunSettings("gpt-5.5", "xhigh")):
                site_client = common.get_client()
                assert common.get_client() is site_client
        cache_keys = tuple(common._thread_clients.clients)
        cached_clients = tuple(common._thread_clients.clients.values())
    finally:
        common._client = original_client
        common._thread_clients = original_thread_clients

    assert first_again is not first
    assert second is not first
    assert mock_client is not second
    assert len(created) == 5
    assert [(item.api_key, item.mock) for item in created] == [
        (first_secret, False),
        (first_secret, False),
        (second_secret, False),
        (None, True),
        (None, None),
    ]
    assert cached_clients == (mock_client, site_client)
    cache_text = repr(cache_keys)
    assert first_secret not in cache_text
    assert second_secret not in cache_text
    assert "SUPER-SECRET" not in cache_text


def test_pipeline_worker_binds_agent_run_settings():
    settings = RunSettings("gpt-5.6-terra", "high")
    agent = SimpleNamespace(settings=settings, _run_deadline=None)
    pipeline = DeterministicPipeline(agent)
    seen = []

    def execute(tool_name, arguments):
        seen.append(current_settings())
        return {"success": True}

    with patch("pipeline.execute_tool", side_effect=execute):
        with ThreadPoolExecutor(max_workers=1) as pool:
            outcome = pool.submit(
                pipeline._call_tool,
                "extract_resume_info",
                {},
                "stage-2",
                2,
            ).result()

    assert outcome["error"] is None
    assert seen == [settings]


def test_resume_agent_owns_one_validated_run_context():
    agent = ResumeAgent(
        "private resume",
        "private JD",
        model="gpt-5.6-terra",
        reasoning="high",
        deadline_epoch=2000.0,
    )

    assert agent.settings == RunSettings(
        "gpt-5.6-terra",
        "high",
        deadline_epoch=2000.0,
    )
    assert (agent.client.model, agent.client.reasoning) == (
        "gpt-5.6-terra",
        "high",
    )


def test_resume_agent_preserves_bound_byok_and_mock_for_tool_workers():
    secret = "sk-bound-agent-runtime-secret"
    bound = RunSettings(
        "gpt-5.5",
        "xhigh",
        deadline_epoch=2000.0,
        api_key=secret,
        mock=False,
    )
    with use_run_settings(bound):
        agent = ResumeAgent("private resume", "private JD")

    assert agent.settings == bound
    assert agent.client.mock_mode is False
    assert secret not in repr(agent.settings)
    assert secret not in repr(agent.client)

    observed = []

    def execute(_tool_name, _arguments):
        observed.append(current_settings())
        return {"success": True}

    with patch("pipeline.execute_tool", side_effect=execute):
        outcome = DeterministicPipeline(agent)._call_tool(
            "extract_resume_info", {}, "stage-2", 2,
        )

    assert outcome["error"] is None
    assert observed == [bound]


def test_resume_agent_run_binds_and_restores_its_context():
    baseline = current_settings()
    agent = ResumeAgent(
        "private resume",
        "private JD",
        model="gpt-5.6-terra",
        reasoning="high",
        deadline_epoch=2000.0,
    )
    observed = []

    def run_traced():
        observed.append(current_settings())
        return "report"

    with (
        patch.object(agent, "_run_traced", side_effect=run_traced),
        patch("agent.monotonic_deadline", return_value=432.0) as convert,
        patch("agent._run_timeout") as run_timeout,
    ):
        run_timeout.return_value.__enter__.return_value = None
        run_timeout.return_value.__exit__.return_value = False
        assert agent.run() == "report"

    assert observed == [agent.settings]
    assert current_settings() == baseline
    assert agent._run_deadline == 432.0
    convert.assert_called_once_with(limit=config.RUN_TIMEOUT)


def test_tool_call_deadline_is_not_misclassified_as_run_timeout():
    original_client = common._client
    with patch.dict(os.environ, {"AGENT_MOCK": "1"}, clear=False):
        common._client = LLMClient(model="gpt-5.5", reasoning="xhigh")
    try:
        with patch.object(config, "CALL_DEADLINE", 0):
            error = _assert_raises(
                CallDeadlineExceeded,
                lambda: common.ask_json("prompt", "system", {"value": 0}),
            )
    finally:
        common._client = original_client

    assert type(error) is CallDeadlineExceeded
    assert getattr(error, "is_run_deadline", False) is False


def test_execute_tool_preserves_privacy_safe_timeout_category():
    def timed_out(**arguments):
        raise CallDeadlineExceeded("private upstream timeout detail")

    with patch.object(tools_module, "get_tool_function", return_value=timed_out):
        result = tools_module.execute_tool("calculate_match", {})

    assert result["success"] is False
    assert result["tool_name"] == "calculate_match"
    assert result["error_category"] == "timeout"


def test_shorter_bound_run_deadline_keeps_run_timeout_classification():
    original_client = common._client
    with patch.dict(os.environ, {"AGENT_MOCK": "1"}, clear=False):
        common._client = LLMClient(model="gpt-5.5", reasoning="xhigh")
    try:
        with common.use_run_deadline(0.0):
            error = _assert_raises(
                ExternalRunDeadlineExceeded,
                lambda: common.ask_json("prompt", "system", {"value": 0}),
            )
    finally:
        common._client = original_client

    assert getattr(error, "is_run_deadline", False) is True


def test_equal_expired_deadlines_prefer_run_source_for_real_client():
    client = object.__new__(LLMClient)
    client.mock_mode = False
    client.streaming = False
    client.model = "gpt-5.5"
    client.reasoning = "xhigh"
    client.last_finish_reason = None
    client.last_call_metrics = {}

    error = _assert_raises(
        ExternalRunDeadlineExceeded,
        lambda: client.chat(
            [{"role": "user", "content": "offline"}],
            logical_deadline=0.0,
            external_deadline=0.0,
        ),
    )

    assert getattr(error, "is_run_deadline", False) is True


def test_equal_expired_deadlines_prefer_run_source_for_mock_client():
    with patch.dict(os.environ, {"AGENT_MOCK": "1"}, clear=False):
        client = LLMClient(model="gpt-5.5", reasoning="xhigh")

    error = _assert_raises(
        ExternalRunDeadlineExceeded,
        lambda: client.simple_ask(
            "offline",
            logical_deadline=0.0,
            external_deadline=0.0,
        ),
    )

    assert getattr(error, "is_run_deadline", False) is True


def _failing_real_client():
    client = object.__new__(LLMClient)
    client.mock_mode = False
    client.streaming = False
    client.model = "gpt-5.5"
    client.reasoning = "xhigh"
    client.last_finish_reason = None
    client.last_call_metrics = {}

    def fail(kwargs):
        raise RuntimeError("gateway unavailable")

    client._chat_once = fail
    return client


def test_retry_delay_uses_run_deadline_error_when_run_budget_is_shorter():
    client = _failing_real_client()
    now = time.monotonic()
    with (
        patch.object(config, "MAX_RETRIES", 2),
        patch.object(config, "RETRY_DELAY", 3),
        patch.object(config, "RETRY_DELAY_CAP", 3),
    ):
        error = _assert_raises(
            ExternalRunDeadlineExceeded,
            lambda: client.chat(
                [{"role": "user", "content": "offline"}],
                logical_deadline=now + 10,
                external_deadline=now + 0.5,
            ),
        )

    assert getattr(error, "is_run_deadline", False) is True


def test_retry_delay_uses_call_deadline_error_when_call_budget_is_shorter():
    client = _failing_real_client()
    now = time.monotonic()
    with (
        patch.object(config, "MAX_RETRIES", 2),
        patch.object(config, "RETRY_DELAY", 3),
        patch.object(config, "RETRY_DELAY_CAP", 3),
    ):
        error = _assert_raises(
            CallDeadlineExceeded,
            lambda: client.chat(
                [{"role": "user", "content": "offline"}],
                logical_deadline=now + 0.5,
                external_deadline=now + 10,
            ),
        )

    assert type(error) is CallDeadlineExceeded
    assert getattr(error, "is_run_deadline", False) is False


def test_semantic_retry_reuses_one_monotonic_deadline():
    class SemanticClient:
        last_finish_reason = "stop"

        def __init__(self):
            self.responses = iter(("not-json", '{"value": 7}'))
            self.deadlines = []

        def simple_ask(self, **kwargs):
            self.deadlines.append((
                kwargs.get("logical_deadline"),
                kwargs.get("external_deadline"),
            ))
            return next(self.responses)

    fake = SemanticClient()
    settings = RunSettings(
        "gpt-5.5",
        "xhigh",
        deadline_epoch=time.time() + 5,
    )
    started = time.monotonic()
    with (
        use_run_settings(settings),
        patch.object(common, "get_client", return_value=fake),
    ):
        result = common.ask_json("prompt", "system", {"value": 0})
    completed = time.monotonic()

    assert result == {"value": 7}
    assert fake.deadlines[0] == fake.deadlines[1]
    logical_deadline, run_deadline = fake.deadlines[0]
    assert started + config.CALL_DEADLINE <= logical_deadline
    assert logical_deadline <= completed + config.CALL_DEADLINE
    assert started < run_deadline < completed + 5.1


def main():
    tests = (
        test_exact_catalog_defaults_and_budgets,
        test_invalid_model_reasoning_pairs_are_rejected,
        test_run_settings_are_immutable_and_nested_contexts_restore,
        test_runtime_budget_converts_epoch_to_monotonic_time,
        test_run_settings_normalize_api_key_and_keep_it_out_of_repr,
        test_client_explicit_byok_and_real_mode_override_bound_and_global_policy,
        test_client_uses_bound_byok_and_mock_policy_when_arguments_are_omitted,
        test_explicit_mock_true_skips_openai_even_when_environment_is_real,
        test_real_client_without_byok_uses_site_funded_key_and_preserves_key_error,
        test_client_uses_instance_model_and_reasoning_for_request_and_trace,
        test_client_rejects_invalid_pair_even_in_mock_mode,
        test_mock_client_trace_keeps_instance_policy,
        test_tool_clients_are_cached_by_active_model_pair,
        test_tool_client_cache_never_retains_byok_clients_or_raw_keys,
        test_pipeline_worker_binds_agent_run_settings,
        test_resume_agent_owns_one_validated_run_context,
        test_resume_agent_preserves_bound_byok_and_mock_for_tool_workers,
        test_resume_agent_run_binds_and_restores_its_context,
        test_tool_call_deadline_is_not_misclassified_as_run_timeout,
        test_execute_tool_preserves_privacy_safe_timeout_category,
        test_shorter_bound_run_deadline_keeps_run_timeout_classification,
        test_equal_expired_deadlines_prefer_run_source_for_real_client,
        test_equal_expired_deadlines_prefer_run_source_for_mock_client,
        test_retry_delay_uses_run_deadline_error_when_run_budget_is_shorter,
        test_retry_delay_uses_call_deadline_error_when_call_budget_is_shorter,
        test_semantic_retry_reuses_one_monotonic_deadline,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print("单次运行模型策略测试通过")


if __name__ == "__main__":
    main()
