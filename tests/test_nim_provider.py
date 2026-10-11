"""验证Key目的地址、NIM参数、检测失败准入与届别提示；不使用真实Key。"""

from _bootstrap import PROJECT_ROOT  # noqa: F401

import asyncio
import contextlib
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
import providers
from cohort_check import graduation_warning
from llm_client import LLMClient
from runtime_context import RunSettings, use_run_settings


def test_nim_client_uses_only_official_endpoint_and_supported_parameters():
    captured = {}

    def create(**kwargs):
        captured["request"] = kwargs
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"status":"ok"}', tool_calls=None))], usage=None)

    def factory(**kwargs):
        captured["client"] = kwargs
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    with patch("llm_client.OpenAI", side_effect=factory):
        client = LLMClient(providers.NIM_MODEL_OPTIONS[0]["id"], "", "nvapi-unit-test-key", False, provider=providers.NIM)
        client.streaming = False
        message = client.chat([{"role": "user", "content": "test"}])
    assert captured["client"]["base_url"] == providers.NIM_BASE_URL
    assert captured["client"]["api_key"] == "nvapi-unit-test-key"
    assert "reasoning_effort" not in captured["request"]
    assert "max_tokens" in captured["request"]
    assert captured["request"]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert message.content == '{"status":"ok"}'


def test_nim_key_cannot_be_sent_to_default_gateway():
    with patch("llm_client.OpenAI") as network:
        try:
            LLMClient("gpt-5.5", "high", "nvapi-unit-test-key", False)
        except providers.ProviderError as error:
            assert error.code == "provider_key_mismatch"
        else:
            raise AssertionError("NIM Key reached default gateway")
        network.assert_not_called()


def test_nim_never_falls_back_to_site_key():
    with patch.object(config, "API_KEY", "site-unit-key"), patch("llm_client.OpenAI") as network:
        try:
            LLMClient(providers.NIM_MODEL_OPTIONS[0]["id"], "", mock=False, provider=providers.NIM)
        except providers.ProviderError as error:
            assert error.code == "provider_key_required"
        else:
            raise AssertionError("NIM borrowed site Key")
        network.assert_not_called()


def test_runtime_provider_survives_tool_client_context_without_cached_byok():
    from tools import common
    settings = RunSettings(providers.NIM_MODEL_OPTIONS[0]["id"], "", api_key="nvapi-unit-test-key", mock=False, provider=providers.NIM)
    with patch("llm_client.OpenAI", return_value=SimpleNamespace()) as network, use_run_settings(settings):
        first = common.get_client()
        second = common.get_client()
    assert first.provider == providers.NIM
    assert first is not second
    assert all(call.kwargs["base_url"] == providers.NIM_BASE_URL for call in network.call_args_list)
    assert "nvapi-unit" not in repr(settings)


def test_connection_check_rejects_auth_model_and_invalid_output_safely():
    class Failure(Exception):
        def __init__(self, status):
            self.status_code = status
            super().__init__("nvapi-sensitive-marker upstream body")

    for status, code in ((401, "provider_auth_failed"), (403, "provider_access_denied"), (404, "provider_model_unavailable"), (410, "provider_model_unavailable"), (429, "provider_rate_limited")):
        with patch("llm_client.LLMClient", side_effect=Failure(status)):
            try:
                providers.check_connection(providers.NIM, providers.NIM_MODEL_OPTIONS[0]["id"], "", "nvapi-unit-test-key")
            except providers.ProviderError as error:
                assert error.code == code
                assert "sensitive-marker" not in str(error)
            else:
                raise AssertionError("failed check accepted")
    for content in ("", "[]", "not json", '{"status":"failed"}'):
        fake = SimpleNamespace(chat=lambda *args, **kwargs: SimpleNamespace(content=content))
        with patch("llm_client.LLMClient", return_value=fake):
            try:
                providers.check_connection(providers.NIM, providers.NIM_MODEL_OPTIONS[0]["id"], "", "nvapi-unit-test-key")
            except providers.ProviderError as error:
                assert error.code == "provider_response_invalid"
            else:
                raise AssertionError("invalid response accepted")


def test_real_transport_failure_preserves_safe_status_for_connection_check():
    class Failure(Exception):
        def __init__(self, status):
            self.status_code = status
            super().__init__("nvapi-sensitive-transport-marker")

    for status, code in ((401, "provider_auth_failed"), (403, "provider_access_denied"), (404, "provider_model_unavailable"), (410, "provider_model_unavailable"), (429, "provider_rate_limited")):
        def create(**kwargs):
            raise Failure(status)

        transport = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        output = io.StringIO()
        with patch("llm_client.OpenAI", return_value=transport), patch.object(config, "MAX_RETRIES", 1), contextlib.redirect_stdout(output):
            try:
                providers.check_connection(providers.NIM, providers.NIM_MODEL_OPTIONS[0]["id"], "", "nvapi-unit-test-key")
            except providers.ProviderError as error:
                assert error.code == code
                assert "sensitive-transport-marker" not in str(error)
            else:
                raise AssertionError("transport failure accepted")
        assert "sensitive-transport-marker" not in output.getvalue()


def test_unknown_provider_model_and_reasoning_are_rejected():
    for provider, model, reasoning in (("https://untrusted.example", "", ""), (providers.NIM, "gpt-5.5", ""), (providers.NIM, providers.NIM_MODEL_OPTIONS[0]["id"], "high")):
        try:
            providers.resolve_model(provider, model, reasoning)
        except providers.ProviderError:
            pass
        else:
            raise AssertionError("unsupported configuration accepted")


def test_stream_transport_is_closed_after_success_and_deadline():
    from llm_client import CallDeadlineExceeded

    class Stream:
        closed = False

        def __iter__(self):
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="{}", tool_calls=None), finish_reason="stop")], usage=None)

        def close(self):
            self.closed = True

    for fail in (False, True):
        stream = Stream()
        network = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: stream)))
        with patch("llm_client.OpenAI", return_value=network):
            client = LLMClient(providers.NIM_MODEL_OPTIONS[0]["id"], "", "nvapi-unit-test-key", False, provider=providers.NIM)
            if fail:
                with patch.object(client, "_check_deadline", side_effect=CallDeadlineExceeded("unit deadline")):
                    try:
                        client.chat([{"role": "user", "content": "test"}])
                    except CallDeadlineExceeded:
                        pass
                    else:
                        raise AssertionError("deadline was ignored")
            else:
                assert client.chat([{"role": "user", "content": "test"}]).content == "{}"
        assert stream.closed


def test_failed_connection_does_not_create_run_or_consume_site_quota():
    from test_vercel_api import _client, _start_ok, server
    from unittest.mock import AsyncMock
    client, backend, _, _, _ = _client()
    failure = providers.ProviderError("provider_auth_failed", "Key被拒绝", 401)
    with patch.object(server, "_check_provider_connection", new=AsyncMock(side_effect=failure)):
        response = _start_ok(client, data={"api_key": "unit-bad-key"})
        assert response.status_code == 401
        assert backend.started == []
        assert client.get("/api/config").json()["free_left"] == 2
    response = _start_ok(client)
    assert response.status_code == 202 and response.json()["free_left"] == 1


def test_nim_routes_are_byok_only_and_payload_keeps_provider_without_key():
    from test_vercel_api import _client, _start_ok, server
    client, backend, _, _, _ = _client()
    model = providers.NIM_MODEL_OPTIONS[0]["id"]
    missing = _start_ok(client, data={"provider": providers.NIM, "model": model, "reasoning": ""})
    assert missing.status_code == 400 and missing.json()["code"] == "provider_key_required"
    with patch.object(server, "_check_provider_connection", new=AsyncMock(return_value={"ok": True})):
        accepted = _start_ok(client, data={"provider": providers.NIM, "model": model, "reasoning": "", "api_key": "nvapi-unit-test-key"})
    assert accepted.status_code == 202
    payload = backend.started[0][1]
    assert payload["provider"] == providers.NIM and payload["reasoning"] == ""
    assert "api_key" not in payload and "nvapi-unit-test-key" not in str(payload)
    assert accepted.json()["free_left"] == 2


def test_connection_route_uses_caller_key_without_starting_or_storing_run():
    from test_vercel_api import _client
    client, backend, _, _, _ = _client()
    response = client.post("/api/providers/check", data={"provider": providers.NIM, "model": providers.NIM_MODEL_OPTIONS[0]["id"], "api_key": "nvapi-unit-test-key"})
    assert response.status_code == 200 and backend.started == []
    assert "no-store" in response.headers["cache-control"]
    assert "nvapi-unit" not in response.text


def test_probe_admission_reserves_capacity_converts_atomically_and_has_separate_limit():
    from quota_store import QuotaStore, AdmissionDenied
    from test_quota_store import FakeRedisExecutor

    async def check():
        redis = FakeRedisExecutor()
        quota = QuotaStore("https://redis.example", "unit", executor=redis, max_concurrent=1, runs_per_hour=1)
        probe = await quota.acquire_probe("ip", now=1000, for_run=True)
        try:
            await quota.acquire("ip", "session", funded=False, now=1000)
        except AdmissionDenied as error:
            assert error.code == "provider_check_busy"
        else:
            raise AssertionError("run stole probe reservation")
        admission = await quota.acquire("ip", "session", funded=False, now=1000, probe_id=probe)
        assert admission["free_left"] == 2 and not redis.probe_active
        try:
            await quota.acquire_probe("ip", now=1000)
        except AdmissionDenied as error:
            assert error.code == "ip_concurrent"
        else:
            raise AssertionError("probe bypassed running task")
        await quota.release(admission["admission_id"])
        try:
            await quota.acquire_probe("ip", now=1000, for_run=True)
        except AdmissionDenied as error:
            assert error.code == "hourly_limit"
        else:
            raise AssertionError("start probed past hourly limit")
        for _ in range(19):
            probe = await quota.acquire_probe("ip", now=1000)
            assert await quota.release_probe("ip", probe)
        try:
            await quota.acquire_probe("ip", now=1000)
        except AdmissionDenied as error:
            assert error.code == "provider_check_limit"
        else:
            raise AssertionError("unlimited checks accepted")

    asyncio.run(check())


def test_active_run_rejects_check_and_start_before_calling_vendor():
    from test_vercel_api import _client, _start_ok, server
    client, backend, _, _, _ = _client()
    assert _start_ok(client).status_code == 202
    vendor = AsyncMock(return_value={"ok": True})
    with patch.object(server, "_check_provider_connection", new=vendor):
        response = client.post("/api/providers/check", data={"api_key": "unit-key", "model": "gpt-5.5", "reasoning": "high"})
        assert response.status_code == 429 and response.json()["code"] == "ip_concurrent"
        response = _start_ok(client, data={"api_key": "unit-key"})
        assert response.status_code == 429 and response.json()["code"] == "ip_concurrent"
    vendor.assert_not_awaited()
    assert len(backend.started) == 1


def test_probe_response_deadline_keeps_reservation_until_underlying_check_finishes():
    from test_vercel_api import _client, server
    client, _, _, quota, _ = _client()
    request = SimpleNamespace(client=SimpleNamespace(host="203.0.113.10"), headers={})

    async def check():
        finish = asyncio.Event()

        async def slow(*args):
            await finish.wait()
            return {"ok": True}

        with patch.object(server, "_check_provider_connection", new=slow), patch.object(server, "_PROVIDER_CHECK_TIMEOUT", 0.01):
            try:
                await server._guarded_provider_check(request, providers.DEFOU, "gpt-5.5", "high", "unit-key")
            except providers.ProviderError as error:
                assert error.code == "provider_timeout"
            else:
                raise AssertionError("unbounded check response")
            assert server._pending_provider_checks
            try:
                await quota.acquire_probe(server._client_ip_hash(request))
            except Exception as error:
                assert getattr(error, "code", None) == "provider_check_busy"
            else:
                raise AssertionError("timed-out in-flight probe lost its lease")
            finish.set()
            for _ in range(10):
                await asyncio.sleep(0)
            assert not server._pending_provider_checks

    asyncio.run(check())


def test_default_durable_step_arguments_remain_legacy_compatible():
    from workflows import resume_workflow as workflow

    async def check():
        default = workflow.VercelOperations("gpt-5.5", "high", 1234)
        vendor = workflow.VercelOperations(providers.NIM_MODEL_OPTIONS[0]["id"], "", 1234, provider=providers.NIM)
        step = AsyncMock(return_value={})
        with patch.object(workflow, "step_extract", new=step):
            await default.extract("resume")
            assert step.call_args.kwargs == {}
            assert len(step.call_args.args) == 6
            await vendor.extract("resume")
            assert step.call_args.kwargs == {"provider": providers.NIM}

    asyncio.run(check())


def test_cohort_warning_compares_only_explicit_dates_without_changing_facts():
    info = {"education": [{"school": "UQ", "end_date": "2026年6月"}, {"school": "QUT", "end_date": "2024年11月"}]}
    warning, mismatch = graduation_warning("2027届校园招聘", info)
    assert mismatch and "2027" in warning and "2026" in warning
    assert info["education"][0]["end_date"] == "2026年6月"
    assert graduation_warning("2026/2027届", info) == ("", False)
    assert graduation_warning("有经验者优先", info) == ("", False)
    warning, mismatch = graduation_warning("2027届", {"education": [{"end_date": "至今"}]})
    assert not mismatch and "未提供明确" in warning


def test_related_major_requirement_is_supported_by_education_not_projects():
    from tools.scoring import _evidence_supports_requirement
    requirement = "数据科学、计算机、AI等相关专业优先"
    education = {"degree": "Master of Computer Science", "major": "计算机科学"}
    assert _evidence_supports_requirement(requirement, "education", education)
    assert _evidence_supports_requirement(requirement, "education", {"degree": "Master of Computer Science", "major": "Computer Science"})
    assert not _evidence_supports_requirement(requirement, "project", {"name": "计算机科学AI项目"})
    assert not _evidence_supports_requirement(requirement, "education", {"degree": "Bachelor", "major": "市场营销"})
    from tools.scoring import normalize_resume_evidence, requirement_ledger_from_match_result
    catalog = normalize_resume_evidence({"education": [education]})
    requirements = [{"requirement_id": "hard-001", "category": "hard", "requirement": requirement}]
    match = {"requirement_evidence": [{"requirement_id": "hard-001", "status": "under_evidenced", "evidence_ids": [catalog[0]["evidence_id"]]}]}
    resolved = requirement_ledger_from_match_result(match, requirements, evidence_catalog=catalog)
    assert resolved[0]["status"] == "met"
    for prefix in ("具备电商背景，", "有电商项目，", "应届毕业生，", "2027届，", "本科及以上，"):
        requirements[0]["requirement"] = prefix + requirement
        resolved = requirement_ledger_from_match_result(match, requirements, evidence_catalog=catalog)
        assert resolved[0]["status"] != "met", prefix
        project_catalog = normalize_resume_evidence({"projects": [{"name": requirement + "项目"}]})
        project_match = {"requirement_evidence": [{"requirement_id": "hard-001", "status": "met", "evidence_ids": [project_catalog[0]["evidence_id"]]}]}
        resolved = requirement_ledger_from_match_result(project_match, requirements, evidence_catalog=project_catalog)
        assert resolved[0]["status"] != "met", "project is not degree evidence: " + prefix


def test_original_portfolio_and_target_role_survive_fallback_and_docx():
    import zipfile
    from utils import parse_resume_text_to_struct
    from workflows.graph import _final_resume_struct
    from resume_export import render_resume_docx
    source = "Ethan\n求职方向：AI应用开发工程师\n个人作品集：https://example.com/portfolio\n教育背景\n2022.02-2024.11 示例大学 | 本科 | 计算机科学\n技能：Python"
    parsed = parse_resume_text_to_struct(source)
    assert parsed["basic_info"]["target_role"] == "AI应用开发工程师"
    struct = _final_resume_struct({"resume_text": source, "suggestions": {"optimized_resume_struct": parsed}})
    with zipfile.ZipFile(io.BytesIO(render_resume_docx(struct))) as archive:
        xml = archive.read("word/document.xml").decode()
    assert "https://example.com/portfolio" in xml and "AI应用开发工程师" in xml


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} NIM/provider checks passed")
