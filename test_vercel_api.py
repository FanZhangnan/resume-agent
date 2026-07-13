"""API contract tests for the Vercel FastAPI entrypoint with injected fakes."""

import io
import os
import time
import zipfile

os.environ.setdefault("AGENT_WORKFLOW_TEST", "1")
os.environ["AGENT_RUN_SIGNING_KEY"] = "unit-test-signing-key"
os.environ["CRON_SECRET"] = "cron-secret"

from fastapi.testclient import TestClient  # noqa: E402

from test_vercel_trace import FakeBlobClient  # noqa: E402
from vercel_trace import TraceStore  # noqa: E402
import webui.vercel_server as server  # noqa: E402

# API contract tests use an injected backend and never contact the gateway.
server.config.API_KEY = "unit-test-gateway-key"


class FakeBackend:
    def __init__(self):
        self.started = []
        self._status = {}
        self._result = {}
        self.result_calls = 0
        self._counter = 0

    async def start(self, payload):
        self._counter += 1
        run_id = f"run-{self._counter}"
        self.started.append((run_id, payload))
        self._status[run_id] = "running"
        return run_id

    async def status(self, run_id):
        return self._status.get(run_id, "pending")

    async def result(self, run_id):
        self.result_calls += 1
        return self._result.get(run_id, {})

    def complete(self, run_id, result):
        self._status[run_id] = "completed"
        self._result[run_id] = result


def _client():
    backend = FakeBackend()
    trace = TraceStore(client=FakeBlobClient())
    server.set_runtime(backend=backend, trace=trace)
    return TestClient(server.app), backend, trace


def _start_ok(client, **overrides):
    data = {"jd_text": "招聘后端工程师，需要 Python 经验",
            "model": "gpt-5.5", "reasoning": "xhigh"}
    data.update(overrides.pop("data", {}))
    files = overrides.pop("files", {
        "resume_file": ("resume.txt", "张三\n后端工程师\n负责核心系统，提升性能30%".encode("utf-8"),
                        "text/plain"),
    })
    return client.post("/api/runs", data=data, files=files)


def test_config_exposes_exact_models_only():
    client, _, _ = _client()
    body = client.get("/api/config").json()
    assert body["models"] == {"gpt-5.5": ["high", "xhigh"],
                              "gpt-5.6-terra": ["high", "xhigh"]}
    assert body["default_model"] == "gpt-5.5"
    text = str(body)
    for forbidden in ("sol", "\"max\"", "'max'", "\"low\"", "medium", "none"):
        assert forbidden not in text.lower() or forbidden == "none"  # 'none' not a level here
    assert "sol" not in text.lower()


def test_status_reports_vercel_mode():
    client, _, _ = _client()
    body = client.get("/api/status").json()
    assert body["deployment_mode"] == "vercel"


def test_model_validation_rejects_forbidden():
    client, _, _ = _client()
    assert _start_ok(client, data={"model": "gpt-5.6-sol", "reasoning": "high"}).status_code == 400
    assert _start_ok(client, data={"model": "gpt-5.5", "reasoning": "max"}).status_code == 400


def test_missing_jd_is_rejected_in_preview():
    client, _, _ = _client()
    resp = _start_ok(client, data={"jd_text": "   "})
    assert resp.status_code == 422


def test_missing_gateway_key_rejected_before_workflow_start():
    client, backend, _ = _client()
    original_key = server.config.API_KEY
    original_mock = os.environ.get("AGENT_MOCK")
    server.config.API_KEY = ""
    os.environ["AGENT_MOCK"] = "0"
    try:
        resp = _start_ok(client)
    finally:
        server.config.API_KEY = original_key
        if original_mock is None:
            os.environ.pop("AGENT_MOCK", None)
        else:
            os.environ["AGENT_MOCK"] = original_mock

    assert resp.status_code == 503
    assert resp.json()["code"] == "gateway_not_configured"
    assert backend.started == []


def test_oversize_jd_is_rejected_before_workflow_start():
    client, backend, _ = _client()
    resp = _start_ok(client, data={"jd_text": "x" * (server.MAX_JD_CHARS + 1)})
    assert resp.status_code == 413
    assert backend.started == []


def test_oversize_upload_rejected():
    client, _, _ = _client()
    big = b"a" * (4 * 1024 * 1024 + 1)
    resp = _start_ok(client, files={"resume_file": ("r.txt", big, "text/plain")})
    assert resp.status_code == 413


def test_extracted_resume_text_limit_is_rejected():
    client, backend, _ = _client()
    text = ("简历内容\n" * (server.MAX_RESUME_CHARS // 4 + 1)).encode("utf-8")
    resp = _start_ok(client, files={"resume_file": ("r.txt", text, "text/plain")})
    assert resp.status_code == 413
    assert backend.started == []


def test_docx_expansion_limit_is_rejected():
    client, backend, _ = _client()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "x" * 2048)
    original = server.MAX_DOCX_UNCOMPRESSED_BYTES
    server.MAX_DOCX_UNCOMPRESSED_BYTES = 1024
    try:
        resp = _start_ok(
            client,
            files={"resume_file": (
                "resume.docx", buffer.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )},
        )
    finally:
        server.MAX_DOCX_UNCOMPRESSED_BYTES = original
    assert resp.status_code == 413
    assert backend.started == []


def test_file_parse_timeout_is_bounded():
    from tools import file_parser

    client, backend, _ = _client()
    original_parser = file_parser.parse_resume_file
    original_timeout = server.PARSE_TIMEOUT_SECONDS

    def slow_parser(_path):
        time.sleep(0.05)
        return {"success": True, "text": "late"}

    file_parser.parse_resume_file = slow_parser
    server.PARSE_TIMEOUT_SECONDS = 0.01
    try:
        resp = _start_ok(client)
    finally:
        file_parser.parse_resume_file = original_parser
        server.PARSE_TIMEOUT_SECONDS = original_timeout
    assert resp.status_code == 408
    assert backend.started == []


def test_unsupported_type_rejected():
    client, _, _ = _client()
    resp = _start_ok(client, files={"resume_file": ("resume.doc", b"legacy doc bytes", "application/msword")})
    assert resp.status_code == 415


def test_empty_or_scanned_rejected():
    client, _, _ = _client()
    resp = _start_ok(client, files={"resume_file": ("r.txt", b"     \n   \t  ", "text/plain")})
    assert resp.status_code == 422


def test_start_returns_signed_token_and_writes_stage_one():
    client, backend, trace = _client()
    resp = _start_ok(client)
    assert resp.status_code == 202
    body = resp.json()
    run_id = body["run_id"]
    assert body["token"]
    assert body["expires_at"] > 0
    # Backend received JSON-safe payload with extracted text and deadline.
    started_id, payload = backend.started[0]
    assert started_id == run_id
    assert "张三" in payload["resume_text"]
    assert payload["jd_text"].strip()
    assert payload["model"] == "gpt-5.5" and payload["reasoning"] == "xhigh"
    assert payload["deadline_epoch"] > 0
    # No raw file path or bytes leak into workflow state.
    assert "file_path" not in payload and "resume_file" not in payload
    # Stage 1 recorded as completed.
    import asyncio
    stages = asyncio.get_event_loop().run_until_complete(trace.read_stages(run_id))
    assert stages[1]["status"] == "completed"


def test_status_requires_valid_token():
    client, backend, _ = _client()
    run_id = _start_ok(client).json()["run_id"]
    assert client.get(f"/api/runs/{run_id}").status_code == 401
    assert client.get(f"/api/runs/{run_id}",
                      headers={"Authorization": "Bearer nonsense"}).status_code == 401


def test_status_polling_and_result_only_after_completion():
    client, backend, _ = _client()
    body = _start_ok(client).json()
    run_id, token = body["run_id"], body["token"]
    auth = {"Authorization": f"Bearer {token}"}

    running = client.get(f"/api/runs/{run_id}", headers=auth).json()
    assert running["status"] == "running"
    assert running.get("report") in (None, "")
    assert backend.result_calls == 0        # never read return_value while running
    assert len(running["stages"]) == 8

    backend.complete(run_id, {"status": "completed", "safe_to_deliver": True,
                              "report": "# 简历优化报告\n完成", "unresolved_fixes": [],
                              "model": "gpt-5.5", "reasoning": "xhigh"})
    done = client.get(f"/api/runs/{run_id}", headers=auth).json()
    assert done["status"] == "completed"
    assert done["safe_to_deliver"] is True
    assert "简历优化报告" in done["report"]


def test_cancel_writes_marker():
    import asyncio
    client, backend, trace = _client()
    body = _start_ok(client).json()
    run_id, token = body["run_id"], body["token"]
    auth = {"Authorization": f"Bearer {token}"}
    assert client.post(f"/api/runs/{run_id}/cancel", headers=auth).status_code in (200, 202)
    assert asyncio.get_event_loop().run_until_complete(trace.is_cancelled(run_id)) is True
    # Cancel requires a token.
    assert client.post(f"/api/runs/{run_id}/cancel").status_code == 401


def test_delete_removes_traces():
    import asyncio
    client, backend, trace = _client()
    body = _start_ok(client).json()
    run_id, token = body["run_id"], body["token"]
    auth = {"Authorization": f"Bearer {token}"}
    assert client.delete(f"/api/runs/{run_id}", headers=auth).status_code == 200
    stages = asyncio.get_event_loop().run_until_complete(trace.read_stages(run_id))
    assert stages == {}


def test_cleanup_requires_cron_secret():
    client, _, _ = _client()
    assert client.get("/api/maintenance/cleanup").status_code in (401, 403)
    assert client.get("/api/maintenance/cleanup",
                      headers={"Authorization": "Bearer wrong"}).status_code in (401, 403)
    ok = client.get("/api/maintenance/cleanup",
                    headers={"Authorization": "Bearer cron-secret"})
    assert ok.status_code == 200


if __name__ == "__main__":
    tests = [v for n, v in sorted(globals().items()) if n.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} vercel-api tests passed")
