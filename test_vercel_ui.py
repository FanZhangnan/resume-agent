"""Source, security, and behavior contract for the Vercel workbench UI."""

import hashlib
import os
import re
import shutil
import subprocess

os.environ.setdefault("AGENT_WORKFLOW_TEST", "1")
os.environ.setdefault("AGENT_RUN_SIGNING_KEY", "unit-test-signing-key")

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "webui", "static", "vercel_app.html")
LOCAL_PAGE = os.path.join(HERE, "webui", "static", "index.html")
LOCAL_PAGE_SHA256 = "b362337baff36daa051d675e0844d1ad55f2204c3c48d780bad7dfc95cde10ca"


def _html():
    with open(PAGE, "r", encoding="utf-8") as handle:
        return handle.read()


def _script():
    scripts = re.findall(
        r'<script nonce="__CSP_NONCE__">(.*?)</script>', _html(), re.DOTALL,
    )
    assert len(scripts) == 1
    return scripts[0]


def test_local_workbench_remains_byte_for_byte_unchanged():
    with open(LOCAL_PAGE, "rb") as handle:
        assert hashlib.sha256(handle.read()).hexdigest() == LOCAL_PAGE_SHA256


def test_full_workbench_surface_is_present():
    html = _html()
    for element_id in (
        "app-shell", "formPane", "resume-drop", "resume-file", "resume-text",
        "jdText", "model-seg", "effort-seg", "byok-key", "mockMode",
        "startBtn", "cancelBtn", "quotaText", "run-meta", "pipe",
        "tab-stream", "tab-report", "tab-layout", "tab-history",
        "view-stream", "view-report", "view-layout", "view-history",
        "swarm", "steps", "trace-drawer", "report-md", "recentRuns",
        "resume-frame", "photo-file", "printResumeBtn", "downloadResumeBtn",
    ):
        assert f'id="{element_id}"' in html
    assert set(re.findall(r'data-tpl="([a-z]+)"', html)) == {
        "classic", "modern", "minimal",
    }


def test_required_jd_and_vercel_scope_are_explicit():
    html = _html()
    assert "目标岗位 JD" in html and "必填" in html
    assert "留空 → 自动推荐" not in html
    assert 'id="prefs"' not in html
    assert 'id="ask"' not in html


def test_transient_byok_mock_and_quota_controls_are_preserved():
    html = _html()
    assert re.search(r'<input[^>]+type="password"[^>]+id="byok-key"', html)
    assert 'id="mockMode"' in html and 'type="checkbox"' in html
    assert 'id="quotaText"' in html
    assert 'value="sk-' not in html
    assert "base_url" not in html.lower() and "baseurl" not in html.lower()


def test_public_page_has_no_local_transport_or_credential_storage():
    html = _html()
    for forbidden in (
        "sessionStorage", "localStorage", "Bearer ", "EventSource",
        "/api/events/", "/api/answer/", "onclick=", "onchange=",
    ):
        assert forbidden not in html, forbidden
    assert "/api/runs" in html and "/api/config" in html
    assert "addEventListener" in html


def test_models_and_reasoning_are_loaded_from_public_config():
    html = _html()
    assert "model_options" in html
    assert "default_reasoning" in html
    assert "renderModelOptions" in html
    assert "renderReasoningOptions" in html
    assert "gpt-5.6-sol" not in html.lower()


def test_submission_uses_vercel_fields_and_clears_api_key():
    script = _script()
    for field in (
        'fd.append("api_key"', 'fd.append("mock"', 'fd.append("jd_text"',
        'fd.append("resume_text"', 'fd.append("model"',
        'fd.append("reasoning"', 'fd.append("job_search", "0")',
    ):
        assert field in script
    assert '$("byok-key").value = ""' in script
    assert 'fd.append("job_description"' not in script


def test_explicit_run_state_machine_is_shared_by_all_views():
    script = _script()
    assert "const runState =" in script
    for phase in ("idle", "submitting", "running", "cancelling", "terminal"):
        assert f'"{phase}"' in script
    for field in (
        "runId", "stages", "report", "safeToDeliver", "unresolvedFixes",
        "resumeStruct", "model", "reasoning",
    ):
        assert field in script
    assert "function renderRunState(" in script


def test_adaptive_polling_is_non_overlapping_and_visibility_aware():
    script = _script()
    assert "function pollDelay(" in script
    assert "return 5000" in script
    assert "return 10000" in script
    assert "return 15000" in script
    assert "pollInFlight" in script and "pollGeneration" in script
    assert "generation !== pollGeneration" in script
    assert "document.hidden" in script
    assert 'addEventListener("visibilitychange"' in script
    assert "setTimeout" in script and "setInterval" not in script


def test_history_restores_active_run_and_rejects_stale_results():
    script = _script()
    for function_name in (
        "loadHistory", "renderHistory", "openRun", "deleteRun",
        "recoverActiveRun", "startPolling", "stopPolling",
    ):
        assert f"function {function_name}(" in script
    assert "historyRequestId" in script
    assert "navigationGeneration" in script
    assert "run_start_uncertain" in script
    assert 'method: "DELETE"' in script or 'method:"DELETE"' in script
    assert "terminal(run.status)" in script


def test_eight_stages_support_inline_and_drawer_detail():
    html = _html()
    script = _script()
    assert len(re.findall(r'data-stage="\d+"', html)) == 8
    assert len(re.findall(r'id="stage-detail-\d+"', html)) == 8
    assert "function toggleStageDetail(" in script
    assert "function openTraceDrawer(" in script
    assert "function stageDetailText(" in script
    assert "textContent = detail" in script
    assert "detailBox.innerHTML" not in script
    for field in (
        "duration_ms", "attempt", "retry_category", "validation_status",
        "error_category", "revision_round", "safe_to_deliver",
    ):
        assert field in script


def test_report_rendering_escapes_before_formatting():
    script = _script()
    assert "function escapeHtml(" in script
    assert "function renderMarkdownSafe(" in script
    assert "escapeHtml(String(markdown" in script
    assert "marked.parse" not in script and "DOMPurify" not in script
    assert "renderMarkdownSafe(runState.report)" in script


def test_resume_exports_require_verified_struct():
    html = _html()
    script = _script()
    assert "function canExportResume(" in script
    assert 'runState.status === "completed"' in script
    assert "runState.safeToDeliver === true" in script
    assert "runState.unresolvedFixes.length === 0" in script
    assert "runState.resumeStruct !== null" in script
    assert 'id="resume-frame"' in html and "sandbox=" in html
    assert "function buildResumeHTML(" in script
    assert "function printResume(" in script
    assert "function downloadResumeHTML(" in script
    assert "resumeFallbackText" not in script


def test_photo_is_local_only_and_bounded():
    script = _script()
    assert "5 * 1024 * 1024" in script or "5*1024*1024" in script
    assert "FileReader" in script and "readAsDataURL" in script
    assert "photoData" in script and "clearPhoto" in script
    assert 'fd.append("photo"' not in script


def test_history_and_dynamic_labels_use_text_content():
    script = _script()
    assert 'list.textContent = ""' in script
    assert "button.textContent" in script
    assert "meta.textContent" in script
    assert "recentRuns.innerHTML" not in script


def test_responsive_layout_has_stable_mobile_constraints():
    html = _html()
    assert "@media(max-width:900px)" in html.replace(" ", "")
    assert "overflow-wrap:anywhere" in html.replace(" ", "")
    assert "minmax(0,1fr)" in html.replace(" ", "")


def test_csp_nonce_favicon_and_vercel_telemetry_are_preserved():
    html = _html()
    assert '<link rel="icon" href="data:,">' in html
    assert '/_vercel/insights/script.js' in html
    assert '/_vercel/speed-insights/script.js' in html
    assert html.count('nonce="__CSP_NONCE__"') >= 3
    assert "__CSP_NONCE__" in html


def test_inline_application_script_is_valid_javascript():
    if not shutil.which("node"):
        print("SKIP node --check (node not installed)")
        return
    path = "/tmp/resume-agent-vercel-inline.js"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(_script())
    result = subprocess.run(
        ["node", "--check", path], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_server_injects_matching_nonce_and_csp():
    from fastapi.testclient import TestClient
    import webui.vercel_server as server

    client = TestClient(server.app)
    response = client.get("/")
    assert response.status_code == 200
    csp = response.headers.get("Content-Security-Policy", "")
    assert "script-src" in csp and "nonce-" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    nonce = re.search(r"'nonce-([A-Za-z0-9_-]+)'", csp).group(1)
    assert "__CSP_NONCE__" not in response.text
    assert response.text.count(f'nonce="{nonce}"') >= 3


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} vercel-ui tests passed")
