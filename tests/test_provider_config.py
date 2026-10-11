"""Provider migration and explicit model capability checks without network access."""

from _bootstrap import PROJECT_ROOT

import json
import os
import subprocess
import sys


def run_config(code, **overrides):
    env = dict(os.environ)
    for name in ("AGENT_BASE_URL", "AGENT_MODEL", "AGENT_REASONING_EFFORT", "AGENT_MODEL_OPTIONS"):
        env.pop(name, None)
    env.update(overrides)
    return subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, env=env, text=True, capture_output=True)


def test_new_domain_is_the_default():
    result = run_config("import config; print(config.API_BASE_URL)")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "https://api.defou.ai/v1"


def test_admin_can_configure_an_https_provider():
    result = run_config("import config; print(config.API_BASE_URL)", AGENT_BASE_URL="https://gateway.example.org/openai/v1/")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "https://gateway.example.org/openai/v1"


def test_unsafe_provider_addresses_are_rejected():
    for value in ["http://api.defou.ai/v1", "https://user:secret@api.defou.ai/v1", "https://api.defou.ai/v1?key=secret", "https://api.defou.ai/v1#secret", "https://127.0.0.1/v1", "https://169.254.169.254/v1", "https://localhost/v1", "https://[::1]/v1"]:
        result = run_config("import config", AGENT_BASE_URL=value)
        assert result.returncode != 0, value
        assert "secret" not in result.stderr, "Unsafe URL must not be echoed in an exception"


def test_configured_model_without_reasoning_has_explicit_capabilities():
    options = [{"id": "custom-chat", "label": "简历引擎", "reasoning_levels": [], "default_reasoning": "", "supports_temperature": False, "token_parameter": "max_completion_tokens"}]
    result = run_config(
        "import config,json; print(json.dumps({'model':config.MODEL_NAME,'reasoning':config.REASONING_EFFORT,'option':config.MODEL_OPTION_BY_ID[config.MODEL_NAME]}))",
        AGENT_MODEL_OPTIONS=json.dumps(options),
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["model"] == "custom-chat"
    assert data["reasoning"] == ""
    assert data["option"]["supports_temperature"] is False
    assert data["option"]["token_parameter"] == "max_completion_tokens"


def test_duplicate_model_ids_and_invalid_defaults_are_rejected():
    for options in [[{"id": "one"}, {"id": "one"}], [{"id": "one", "reasoning_levels": ["high"], "default_reasoning": "low"}], [{"id": "one", "token_parameter": "api_key"}], [{"id": "one", "supports_temperature": "false"}]]:
        result = run_config("import config", AGENT_MODEL_OPTIONS=json.dumps(options))
        assert result.returncode != 0, options


def test_client_sends_only_declared_request_parameters():
    options = [{"id": "custom-chat", "reasoning_levels": [], "supports_temperature": False, "token_parameter": "max_completion_tokens"}]
    code = '''
import config
from llm_client import LLMClient
from types import SimpleNamespace
client = object.__new__(LLMClient)
client.mock_mode = False
client.streaming = False
client.model = 'custom-chat'
client.reasoning = ''
def respond(kwargs):
    assert 'reasoning_effort' not in kwargs
    assert 'temperature' not in kwargs
    assert 'max_tokens' not in kwargs
    assert kwargs['max_completion_tokens'] == 77
    return SimpleNamespace(content='ok', tool_calls=None)
client._chat_once = respond
assert client.chat([{'role':'user','content':'test'}], max_tokens=77).content == 'ok'
'''
    result = run_config(code, AGENT_MODEL_OPTIONS=json.dumps(options), AGENT_MOCK="0")
    assert result.returncode == 0, result.stderr


def test_authentication_failure_is_not_retried_or_echoed():
    code = '''
import io
from contextlib import redirect_stdout
from llm_client import LLMClient
class Rejected(Exception):
    status_code = 401
client = object.__new__(LLMClient)
client.mock_mode = False
client.streaming = False
client.model = 'gpt-5.5'
client.reasoning = 'high'
calls = []
def reject(kwargs):
    calls.append(kwargs)
    raise Rejected('secret-credential-and-private-resume')
client._chat_once = reject
output = io.StringIO()
with redirect_stdout(output):
    try:
        client.chat([{'role':'user','content':'test'}])
    except RuntimeError as error:
        assert 'secret-credential' not in str(error)
        assert error.__cause__ is None
    else:
        raise AssertionError('Authentication error must fail')
assert len(calls) == 1
assert 'secret-credential' not in output.getvalue()
'''
    result = run_config(code, AGENT_MOCK="0")
    assert result.returncode == 0, result.stderr


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print("PASS", test.__name__)
    print(f"{len(tests)} provider configuration tests passed")


if __name__ == "__main__":
    main()
