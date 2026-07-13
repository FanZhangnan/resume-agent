"""Source and behavior contract for the self-contained Vercel polling UI.

Validates cookie-owned polling, transient BYOK input, Mock mode, public quota,
session history, safe rendering, eight stable stages, and CSP nonce handling.
The inline application script is checked with `node --check` when available.
"""

import os
import re
import shutil
import subprocess

os.environ.setdefault("AGENT_WORKFLOW_TEST", "1")
os.environ.setdefault("AGENT_RUN_SIGNING_KEY", "unit-test-signing-key")

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "webui", "static", "vercel_app.html")


def _html():
    with open(PAGE, "r", encoding="utf-8") as handle:
        return handle.read()


def test_transient_byok_mock_and_quota_controls_present():
    html = _html()
    assert re.search(r'<input[^>]+type="password"[^>]+id="apiKey"', html)
    assert 'id="mockMode"' in html and 'type="checkbox"' in html
    assert 'id="quotaText"' in html
    assert 'id="recentRuns"' in html
    assert 'value="sk-' not in html
    assert "base_url" not in html.lower() and "baseurl" not in html.lower()


def test_only_exact_models_and_efforts_referenced():
    html = _html()
    assert "gpt-5.6-sol" not in html.lower()
    # No selectable disallowed reasoning options.
    for bad in ("\"low\"", "'low'", "\"medium\"", "'medium'", "\"max\"", "'max'",
                "\"none\"", "'none'"):
        assert bad not in html, f"disallowed effort option {bad} present"


def test_cookie_polling_has_no_browser_token_storage():
    html = _html()
    for forbidden in ("sessionStorage", "localStorage", "Bearer ", "RUN_KEY"):
        assert forbidden not in html, f"cookie UI must not contain {forbidden!r}"
    assert "/api/runs" in html
    assert "/api/config" in html
    assert "2000" in html                 # two-second poll cadence
    assert "addEventListener" in html     # no inline onclick handlers
    assert "onclick=" not in html


def test_submission_includes_transient_key_and_mock_flag():
    html = _html()
    assert 'fd.append("api_key"' in html
    assert 'fd.append("mock"' in html
    assert 'fd.append("jd_text"' in html
    assert 'fd.append("job_description"' not in html
    assert '$("apiKey").value = ""' in html
    assert "saveRun(" not in html and "currentRun()" not in html


def test_quota_errors_focus_byok_and_refresh_quota():
    html = _html()
    assert "free_quota_exhausted" in html
    assert "site_quota_exhausted" in html
    assert '$("apiKey").focus()' in html
    assert "function updateQuota(" in html
    assert "free_left" in html and "free_per_day" in html


def test_recent_history_can_open_delete_and_resume_running_run():
    html = _html()
    for function_name in (
        "loadHistory", "renderHistory", "openRun", "deleteRun",
    ):
        assert f"function {function_name}(" in html
    assert 'method:"DELETE"' in html or 'method: "DELETE"' in html
    assert "find(function(run)" in html
    assert "startPolling(active.run_id)" in html
    assert "refreshPublicState" in html


def test_active_run_locks_start_until_it_stops():
    html = _html()
    start_polling = re.search(
        r"function startPolling\(runId\)(.*?)function stopPolling", html, re.DOTALL,
    ).group(1)
    assert '$("startBtn").disabled = true' in start_polling
    assert html.count('$("startBtn").disabled = false') >= 4


def test_polling_prevents_overlap_and_ignores_stale_responses():
    html = _html()
    assert "var pollInFlight = false" in html
    assert "var pollGeneration = 0" in html
    assert "if(pollInFlight)" in html
    assert "generation !== pollGeneration" in html
    assert "pollInFlight = true" in html and "pollInFlight = false" in html
    assert 'setError("");\n      applyStatus(data);' in html


def test_boot_and_history_ignore_stale_async_results():
    html = _html()
    assert "var historyRequestId = 0" in html
    assert "requestId !== historyRequestId" in html
    assert "var navigationGeneration = 0" in html
    assert "bootGeneration !== navigationGeneration" in html
    assert "var startGeneration = navigationGeneration" in html
    assert "startGeneration !== navigationGeneration" in html
    assert "recoverActiveRun(startGeneration)" in html
    assert 'catch(e){\n      if(startGeneration !== navigationGeneration)' in html


def test_only_terminal_history_runs_can_be_deleted():
    html = _html()
    assert "var canDelete = terminal(run.status)" in html
    assert "remove.disabled = !canDelete" in html
    assert "if(canDelete)" in html


def test_cancel_ignores_stale_response_and_surfaces_failure():
    html = _html()
    assert "var activeRunTerminal = false" in html
    assert "activeRunTerminal = terminal(data.status)" in html
    assert "var cancelRunId = activeRunId" in html
    assert "var cancelGeneration = navigationGeneration" in html
    assert "cancelRunId !== activeRunId" in html
    assert "cancelGeneration !== navigationGeneration" in html
    assert "activeRunTerminal" in html
    assert "取消请求失败" in html


def test_uncertain_start_attempts_history_recovery():
    html = _html()
    assert "run_start_uncertain" in html
    assert "function recoverActiveRun(" in html
    assert "await recoverActiveRun(startGeneration)" in html
    assert "function restoreStartButton(" in html


def test_history_uses_text_content_not_server_html():
    html = _html()
    assert "grid-template-columns:minmax(0,1fr) auto auto" in html
    assert 'list.textContent = ""' in html
    assert "button.textContent" in html
    assert "meta.textContent" in html
    assert "recentRuns.innerHTML" not in html


def test_eight_stable_stage_rows():
    html = _html()
    assert len(re.findall(r'data-stage="\d+"', html)) == 8


def test_safe_report_rendering_escapes_first():
    html = _html()
    assert "function escapeHtml(" in html
    assert "function renderMarkdownSafe(" in html
    # The report must be routed through the safe renderer, not raw markdown.
    assert "renderMarkdownSafe(" in html
    assert "marked.parse" not in html or "DOMPurify" in html


def test_trace_detail_drawer_present():
    html = _html()
    # One sanitized detail container per stage row, populated via textContent only.
    assert len(re.findall(r'id="std-\d+"', html)) == 8
    assert "function setStageDetail(" in html
    assert "function toggleStageDetail(" in html
    # The drawer must never inject trace strings as HTML.
    assert "box.textContent" in html
    assert "box.innerHTML" not in html
    assert "s.validation_status" in html
    assert "验证状态" in html


def test_report_export_button_present():
    html = _html()
    assert 'id="downloadBtn"' in html
    assert "function downloadReport(" in html
    assert "resume-report.md" in html


def test_csp_nonce_placeholder_present():
    html = _html()
    assert "__CSP_NONCE__" in html
    assert 'nonce="__CSP_NONCE__"' in html


def test_speed_insights_and_analytics_scripts_are_preserved():
    html = _html()
    assert '/_vercel/insights/script.js' in html
    assert '/_vercel/speed-insights/script.js' in html
    assert html.count('nonce="__CSP_NONCE__"') >= 3


def test_inline_script_is_valid_javascript():
    if not shutil.which("node"):
        print("SKIP node --check (node not installed)")
        return
    html = _html()
    match = re.search(r'<script nonce="__CSP_NONCE__">(.*?)</script>', html, re.DOTALL)
    assert match, "expected one nonce-guarded inline application script"
    script = match.group(1)
    out_path = "/tmp/resume-agent-vercel-inline.js"
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(script)
    result = subprocess.run(["node", "--check", out_path],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_server_injects_nonce_and_csp_header():
    from fastapi.testclient import TestClient
    import webui.vercel_server as server

    client = TestClient(server.app)
    resp = client.get("/")
    assert resp.status_code == 200
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "script-src" in csp and "nonce-" in csp
    assert "object-src 'none'" in csp
    body = resp.text
    assert "__CSP_NONCE__" not in body        # placeholder was substituted
    nonce = re.search(r"'nonce-([A-Za-z0-9_-]+)'", csp).group(1)
    assert f'nonce="{nonce}"' in body          # header nonce matches script tag


if __name__ == "__main__":
    tests = [v for n, v in sorted(globals().items()) if n.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} vercel-ui tests passed")
