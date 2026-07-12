"""Source and behavior contract for the self-contained Vercel polling UI.

Validates that public mode carries no BYOK/API-key controls, only the exact
model catalog, signed-token polling, safe report rendering, eight stable stage
rows, and a per-response CSP nonce. The inline application script is extracted
and checked with `node --check` when Node is available.
"""

import os
import re
import shutil
import subprocess

os.environ.setdefault("AGENT_WORKFLOW_TEST", "1")
os.environ.setdefault("AGENT_INVITE_CODE", "let-me-in")
os.environ.setdefault("AGENT_RUN_SIGNING_KEY", "unit-test-signing-key")

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "webui", "static", "vercel_app.html")


def _html():
    with open(PAGE, "r", encoding="utf-8") as handle:
        return handle.read()


def test_no_public_credential_controls():
    html = _html().lower()
    for forbidden in ("api_key", "apikey", "byok", "base_url", "baseurl",
                      "localstorage"):
        assert forbidden not in html, f"public UI must not contain {forbidden!r}"


def test_only_exact_models_and_efforts_referenced():
    html = _html()
    assert "gpt-5.6-sol" not in html.lower()
    # No selectable disallowed reasoning options.
    for bad in ("\"low\"", "'low'", "\"medium\"", "'medium'", "\"max\"", "'max'",
                "\"none\"", "'none'"):
        assert bad not in html, f"disallowed effort option {bad} present"


def test_polling_transport_contract():
    html = _html()
    assert "sessionStorage" in html
    assert "Bearer " in html
    assert "/api/runs" in html
    assert "/api/config" in html
    assert "2000" in html                 # two-second poll cadence
    assert "addEventListener" in html     # no inline onclick handlers
    assert "onclick=" not in html


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


def test_csp_nonce_placeholder_present():
    html = _html()
    assert "__CSP_NONCE__" in html
    assert 'nonce="__CSP_NONCE__"' in html


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
