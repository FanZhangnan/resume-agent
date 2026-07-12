"""模型目录与推理档位组合测试（纯离线，不调用API）。"""

from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import config
from llm_client import LLMClient
from webui import server


EXPECTED_LEVELS = {
    "gpt-5.5": ("high", "xhigh"),
    "gpt-5.6-terra": ("high", "xhigh"),
}


def test_model_catalog():
    assert config.SUPPORTED_MODELS == ("gpt-5.5", "gpt-5.6-terra")
    assert config.MODEL_REASONING_LEVELS == EXPECTED_LEVELS
    assert config.DEFAULT_MODEL == "gpt-5.5"
    assert config.DEFAULT_REASONING_BY_MODEL == {
        "gpt-5.5": "xhigh",
        "gpt-5.6-terra": "xhigh",
    }
    assert [item["tier"] for item in config.MODEL_OPTIONS] == [
        "unassigned", "free"
    ]
    assert [item["status"] for item in config.MODEL_OPTIONS] == [
        "stable", "experimental"
    ]


def test_model_reasoning_resolution():
    assert server._resolve_model_reasoning("", "") == ("gpt-5.5", "xhigh")
    assert server._resolve_model_reasoning("gpt-5.5", "high") == ("gpt-5.5", "high")
    assert server._resolve_model_reasoning("gpt-5.6-terra", "") == ("gpt-5.6-terra", "xhigh")
    assert server._resolve_model_reasoning("gpt-5.6-terra", "high") == ("gpt-5.6-terra", "high")


def _assert_rejected(model, reasoning):
    try:
        server._resolve_model_reasoning(model, reasoning)
    except HTTPException as error:
        assert error.status_code == 400
        return
    raise AssertionError(f"组合应被拒绝：{model} / {reasoning}")


def test_invalid_combinations_are_rejected():
    _assert_rejected("gpt-5.5", "none")
    _assert_rejected("gpt-5.5", "max")
    _assert_rejected("gpt-5.6-terra", "max")
    _assert_rejected("gpt-5.6-sol", "low")
    _assert_rejected("gpt-5.6-luna", "xhigh")


def test_frontend_exposes_model_specific_reasoning():
    html = (Path(__file__).parent / "webui" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 'id="model-seg"' in html
    assert 'fd.append("model", selectedModel)' in html
    assert "renderEffortOptions" in html
    assert 'id="byok-model"' not in html


def test_reasoning_level_is_never_silently_removed():
    client = object.__new__(LLMClient)
    client.mock_mode = False
    client.streaming = False
    client.model = "gpt-5.6-terra"
    client.reasoning = "high"
    calls = []

    def reject_reasoning(kwargs):
        calls.append(dict(kwargs))
        if "reasoning_effort" in kwargs:
            raise RuntimeError("reasoning_effort is not supported")
        return object()

    client._chat_once = reject_reasoning
    with (
        patch.object(config, "MODEL_NAME", "gpt-5.5"),
        patch.object(config, "REASONING_EFFORT", "xhigh"),
        patch.object(config, "MAX_RETRIES", 2),
        patch.object(config, "RETRY_DELAY", 0),
        patch.object(config, "RETRY_DELAY_CAP", 0),
        patch("llm_client.time.sleep", return_value=None),
    ):
        try:
            client.chat([{"role": "user", "content": "test"}])
        except RuntimeError:
            pass
        else:
            raise AssertionError("推理档位被移除后请求不应静默成功")

    assert len(calls) == 2
    assert all(call.get("model") == "gpt-5.6-terra" for call in calls)
    assert all(call.get("reasoning_effort") == "high" for call in calls)


def main():
    test_model_catalog()
    test_model_reasoning_resolution()
    test_invalid_combinations_are_rejected()
    test_frontend_exposes_model_specific_reasoning()
    test_reasoning_level_is_never_silently_removed()
    print("模型目录与推理档位测试通过")


if __name__ == "__main__":
    main()
