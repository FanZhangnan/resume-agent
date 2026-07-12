"""Production model policy and benchmark harness tests (offline only)."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

import config
import agent
import probe_reasoning
from runtime_context import current_settings
from webui import server


ROOT = Path(__file__).parent
EXPECTED_LEVELS = {
    "gpt-5.5": ("high", "xhigh"),
    "gpt-5.6-terra": ("high", "xhigh"),
}


def test_production_default_and_independent_catalog_labels():
    assert config.DEFAULT_MODEL == "gpt-5.5"
    assert config.DEFAULT_REASONING_BY_MODEL["gpt-5.5"] == "xhigh"
    assert config.MODEL_REASONING_LEVELS == EXPECTED_LEVELS

    options = {item["id"]: item for item in config.MODEL_OPTIONS}
    assert tuple(options) == ("gpt-5.5", "gpt-5.6-terra")
    assert options["gpt-5.5"]["status"] == "stable"
    assert options["gpt-5.5"]["status_label"] == "稳定"
    assert options["gpt-5.6-terra"]["status"] == "experimental"
    assert options["gpt-5.6-terra"]["tier"] == "free"
    assert options["gpt-5.5"]["tier"] == "unassigned"
    assert options["gpt-5.5"]["status"] != options["gpt-5.5"]["tier"]


def test_exact_model_reasoning_allowlist():
    assert config.validate_model_reasoning("gpt-5.5", "xhigh") == (
        "gpt-5.5", "xhigh"
    )
    assert config.validate_model_reasoning("gpt-5.6-terra", "high") == (
        "gpt-5.6-terra", "high"
    )
    assert config.validate_model_reasoning("gpt-5.5", "high") == (
        "gpt-5.5", "high"
    )

    invalid = (
        ("gpt-5.5", "none"),
        ("gpt-5.5", "low"),
        ("gpt-5.5", "medium"),
        ("gpt-5.5", "max"),
        ("gpt-5.6-terra", "none"),
        ("gpt-5.6-terra", "low"),
        ("gpt-5.6-terra", "medium"),
        ("gpt-5.6-terra", "max"),
        ("gpt-5.6-sol", "high"),
        ("GPT-5.5", "xhigh"),
        ("gpt-5.6-luna", "xhigh"),
        ("gpt-5.5", "XHIGH"),
    )
    for model, reasoning in invalid:
        try:
            config.validate_model_reasoning(model, reasoning)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid combination accepted: {model}/{reasoning}")


def _assert_web_rejected(model, reasoning):
    try:
        server._resolve_model_reasoning(model, reasoning)
    except HTTPException as error:
        assert error.status_code == 400
        return
    raise AssertionError(f"web combination should be rejected: {model}/{reasoning}")


def test_web_resolution_uses_stable_default_and_exact_allowlist():
    assert server._resolve_model_reasoning("", "") == ("gpt-5.5", "xhigh")
    assert server._resolve_model_reasoning("gpt-5.5", "high") == (
        "gpt-5.5", "high"
    )
    assert server._resolve_model_reasoning("gpt-5.6-terra", "xhigh") == (
        "gpt-5.6-terra", "xhigh"
    )
    _assert_web_rejected("gpt-5.5", "none")
    _assert_web_rejected("gpt-5.5", "max")
    _assert_web_rejected("gpt-5.6-terra", "max")
    _assert_web_rejected("gpt-5.6-sol", "high")
    _assert_web_rejected("gpt-5.6-luna", "xhigh")


def test_benchmark_requires_explicit_live_before_client_construction(tmp_path):
    import benchmark_models

    output = tmp_path / "benchmark.json"
    with patch.object(
        benchmark_models, "LLMClient", side_effect=AssertionError("must stay offline")
    ) as client_class:
        result = benchmark_models.main(["--output", str(output)])
    assert result == 2
    assert client_class.call_count == 0
    assert not output.exists()


class _FakeBenchmarkClient:
    def __init__(self):
        self.last_call_metrics = {}

    def _set_metrics(self, operation):
        totals = {
            "extraction_json": (31, 13, 44),
            "tool_call": (22, 4, 26),
            "grounded_rewrite": (28, 9, 37),
            "verifier": (35, 7, 42),
        }[operation]
        self.last_call_metrics = {
            "finish_reason": "stop",
            "input_tokens": totals[0],
            "output_tokens": totals[1],
            "total_tokens": totals[2],
        }

    def simple_ask(self, prompt, system=None, operation=None, **kwargs):
        self._set_metrics(operation)
        responses = {
            "extraction_json": '{"candidate_id":"SYNTHETIC-001","skills":["Python"]}',
            "grounded_rewrite": (
                '{"fact_id":"F1","rewrite":"Built a Python reporting tool."}'
            ),
            "verifier": (
                '{"passed":true,"safe_to_deliver":true,"required_fixes":[]}'
            ),
        }
        return responses[operation]

    def chat(self, messages, tools=None, operation=None, **kwargs):
        self._set_metrics(operation)
        function = SimpleNamespace(
            name="record_skill_match", arguments='{"fact_id":"F1"}'
        )
        call = SimpleNamespace(function=function)
        return SimpleNamespace(content=None, tool_calls=[call])


def test_concurrent_benchmarks_keep_request_policies_isolated():
    import benchmark_models

    initial = (config.MODEL_NAME, config.REASONING_EFFORT)
    barrier = threading.Barrier(2)
    observed = []
    lock = threading.Lock()

    def client_factory():
        barrier.wait(timeout=1)
        settings = current_settings()
        with lock:
            observed.append((settings.model, settings.reasoning))
        barrier.wait(timeout=1)
        return _FakeBenchmarkClient()

    pairs = (
        ("gpt-5.5", "high"),
        ("gpt-5.6-terra", "xhigh"),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                benchmark_models.run_benchmark,
                model,
                reasoning,
                client_factory,
            )
            for model, reasoning in pairs
        ]
        results = [future.result() for future in futures]

    assert sorted(observed) == sorted(pairs)
    assert sorted(
        (result["model"], result["reasoning"])
        for result in results
    ) == sorted(pairs)
    assert (config.MODEL_NAME, config.REASONING_EFFORT) == initial


class _MaliciousRewriteClient(_FakeBenchmarkClient):
    def __init__(self):
        super().__init__()
        self.verifier_input = ""

    def simple_ask(self, prompt, system=None, operation=None, **kwargs):
        if operation == "grounded_rewrite":
            self._set_metrics(operation)
            return '{"fact_id":"F1","rewrite":"Invented $50M revenue."}'
        if operation == "verifier":
            self._set_metrics(operation)
            self.verifier_input = prompt
            failed = "Invented $50M revenue." in prompt
            return json.dumps({
                "passed": not failed,
                "safe_to_deliver": not failed,
                "required_fixes": ["Remove invented revenue."] if failed else [],
            })
        return super().simple_ask(
            prompt, system=system, operation=operation, **kwargs
        )


class _ToolFailureClient(_FakeBenchmarkClient):
    def chat(self, messages, tools=None, operation=None, **kwargs):
        raise RuntimeError("tool call failed before metrics were available")


class _InvalidRewriteClient(_FakeBenchmarkClient):
    def __init__(self):
        super().__init__()
        self.verifier_called = False

    def simple_ask(self, prompt, system=None, operation=None, **kwargs):
        if operation == "grounded_rewrite":
            self._set_metrics(operation)
            return "not valid json"
        if operation == "verifier":
            self.verifier_called = True
            raise AssertionError("verifier must not audit a default rewrite")
        return super().simple_ask(
            prompt, system=system, operation=operation, **kwargs
        )


def test_benchmark_records_only_safe_metrics(tmp_path):
    import benchmark_models

    output = tmp_path / "benchmark.json"
    result = benchmark_models.main(
        [
            "--live",
            "--model", "gpt-5.5",
            "--reasoning", "xhigh",
            "--output", str(output),
        ],
        client_factory=_FakeBenchmarkClient,
        clock=_FakeClock(),
    )
    assert result == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["model"] == "gpt-5.5"
    assert payload["reasoning"] == "xhigh"
    assert payload["fixture"] == "synthetic"
    assert payload["verifier_status"] == "passed"
    assert [item["operation"] for item in payload["operations"]] == [
        "extraction_json", "tool_call", "grounded_rewrite", "verifier"
    ]
    required = {
        "operation", "model", "reasoning", "latency_ms", "completed",
        "json_valid", "tool_call_success", "input_tokens", "output_tokens",
        "total_tokens", "verifier_status",
    }
    for item in payload["operations"]:
        assert required <= set(item)
        assert item["latency_ms"] >= 0
    serialized = output.read_text(encoding="utf-8").lower()
    for forbidden in ("prompt", "response", "api_key", "resume_text", "jd_text"):
        assert forbidden not in serialized
    assert '"rewrite":' not in serialized


def test_verifier_audits_actual_rewrite_without_persisting_it(tmp_path):
    import benchmark_models

    output = tmp_path / "malicious-benchmark.json"
    holder = {}

    def client_factory():
        holder["client"] = _MaliciousRewriteClient()
        return holder["client"]

    result = benchmark_models.main(
        [
            "--live",
            "--model", "gpt-5.5",
            "--reasoning", "xhigh",
            "--output", str(output),
        ],
        client_factory=client_factory,
        clock=_FakeClock(),
    )
    assert result == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert "Invented $50M revenue." in holder["client"].verifier_input
    assert payload["verifier_status"] == "failed"
    serialized = output.read_text(encoding="utf-8")
    assert "Invented $50M revenue." not in serialized
    assert '"rewrite":' not in serialized


def test_failed_operation_cannot_reuse_previous_token_metrics(tmp_path):
    import benchmark_models

    output = tmp_path / "failed-operation-benchmark.json"
    result = benchmark_models.main(
        [
            "--live",
            "--model", "gpt-5.5",
            "--reasoning", "xhigh",
            "--output", str(output),
        ],
        client_factory=_ToolFailureClient,
        clock=_FakeClock(),
    )
    assert result == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    tool_call = next(
        item for item in payload["operations"]
        if item["operation"] == "tool_call"
    )
    assert tool_call["completed"] is False
    assert tool_call["input_tokens"] is None
    assert tool_call["output_tokens"] is None
    assert tool_call["total_tokens"] is None


def test_invalid_rewrite_skips_verifier_instead_of_using_default_text(tmp_path):
    import benchmark_models

    output = tmp_path / "invalid-rewrite-benchmark.json"
    holder = {}

    def client_factory():
        holder["client"] = _InvalidRewriteClient()
        return holder["client"]

    result = benchmark_models.main(
        [
            "--live",
            "--model", "gpt-5.5",
            "--reasoning", "xhigh",
            "--output", str(output),
        ],
        client_factory=client_factory,
        clock=_FakeClock(),
    )
    assert result == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    verifier = next(
        item for item in payload["operations"]
        if item["operation"] == "verifier"
    )
    assert holder["client"].verifier_called is False
    assert verifier["completed"] is False
    assert verifier["verifier_status"] == "not_completed"
    assert payload["verifier_status"] == "not_completed"


class _FakeClock:
    def __init__(self):
        self.value = 10.0

    def __call__(self):
        self.value += 0.025
        return self.value


def test_cli_and_probe_expose_only_supported_options():
    assert "gpt-5.5       --reasoning high|xhigh" in agent._USAGE
    assert "gpt-5.6-terra --reasoning high|xhigh" in agent._USAGE
    assert "gpt-5.6-sol" not in agent._USAGE
    assert "max" not in agent._USAGE
    assert probe_reasoning.CANDIDATES == ("high", "xhigh")


def main():
    test_production_default_and_independent_catalog_labels()
    test_exact_model_reasoning_allowlist()
    test_web_resolution_uses_stable_default_and_exact_allowlist()
    test_concurrent_benchmarks_keep_request_policies_isolated()
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as directory:
        path = Path(directory)
        test_benchmark_requires_explicit_live_before_client_construction(path)
        test_benchmark_records_only_safe_metrics(path)
        test_verifier_audits_actual_rewrite_without_persisting_it(path)
        test_failed_operation_cannot_reuse_previous_token_metrics(path)
        test_invalid_rewrite_skips_verifier_instead_of_using_default_text(path)
    test_cli_and_probe_expose_only_supported_options()
    print("模型政策与基准测试通过")


if __name__ == "__main__":
    main()
