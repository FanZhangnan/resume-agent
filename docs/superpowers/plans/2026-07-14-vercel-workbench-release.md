# Vercel Workbench Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Blob-backed Vercel runtime trace with Redis and deliver the full local-workbench visual experience on Vercel without changing the local UI or weakening quota, session, or credential security.

**Architecture:** Redis remains the authoritative short-lived public run store and gains redacted trace fields behind a `TraceStore` facade. Vercel Workflow returns a strictly normalized resume rendering object, while the owner-checked API projects it into terminal detail responses. The self-contained Vercel page adopts the local workbench shell but keeps the Vercel polling transport, transient BYOK, signed-Cookie ownership, and safe renderers.

**Tech Stack:** Python 3.12, FastAPI, Vercel Python Workflows, Upstash Redis REST, Pydantic, vanilla HTML/CSS/JavaScript, CSP nonces, executable assertion scripts, Node syntax checks, Playwright.

**Design references:**

- `docs/superpowers/specs/2026-07-14-redis-trace-migration-design.md`
- `docs/superpowers/specs/2026-07-14-vercel-full-workbench-ui-design.md`

The two specifications remain separate ownership boundaries, but this is one
release plan because the workbench's polling and stage-detail contract depends
directly on the Redis trace migration. Tasks 1-4 and 5-10 each produce testable
checkpoints before the combined Preview rollout.

---

## File Map

- Create `run_trace_store.py`: sanitize trace documents and expose the existing trace method contract over Redis.
- Create `public_resume.py`: normalize and size-bound `optimized_resume_struct` for Workflow and public API use.
- Modify `quota_store.py`: add conditional trace commands, absolute run expiry, history pruning, and explicit history projection.
- Modify `workflows/resume_workflow.py`: construct the Redis trace facade and remove Blob imports.
- Modify `workflows/graph.py`: normalize the final structured resume and fall back only from optimized resume text.
- Modify `webui/vercel_server.py`: use Redis trace, expose owner-scoped structured results, set private/no-store caching, and remove Blob cleanup.
- Replace `webui/static/vercel_app.html`: port the complete workbench visual shell and connect it to Vercel polling APIs.
- Replace `test_vercel_trace.py`; modify `test_quota_store.py`, `test_vercel_workflow.py`, `test_vercel_api.py`, and `test_vercel_ui.py`.
- Modify `test_vercel_deploy_contract.py`, `vercel.json`, and `docs/deployment-vercel.md` to remove runtime Blob requirements.
- Preserve `webui/static/index.html` byte-for-byte.

---

### Task 1: Lock Redis Trace Semantics With Failing Tests

**Files:**
- Modify: `test_quota_store.py`
- Replace: `test_vercel_trace.py`
- Create in Task 2: `run_trace_store.py`

- [ ] **Step 1: Extend the fake Redis executor for trace tests**

Add stable script-marker branches and absolute-expiry recording:

```python
if "trace-write-v1" in script:
    run_key = keys[0]
    if run_key not in self.hashes:
        return 0
    self.hashes[run_key][args[0]] = args[1]
    return 1
if "trace-cancel-v1" in script:
    run_key = keys[0]
    if run_key not in self.hashes:
        return 0
    self.hashes[run_key]["trace:cancelled"] = "1"
    return 1
```

Record `EXPIREAT ra:run:<id> <epoch>` without extending it on later trace writes.

- [ ] **Step 2: Replace Blob-specific tests with Redis trace tests**

Test an injected `QuotaStore` and future `TraceStore`:

```python
async def scenario():
    redis = FakeRedisExecutor()
    quota = QuotaStore("https://redis.example", "token", executor=redis)
    await bind_test_run(quota, redis, "run-1", "session-1", created_at=1000)
    trace = TraceStore(quota)
    assert await trace.write_stage(
        "run-1", 2,
        {"status": "completed", "resume_text": "PRIVATE", "attempt": 1},
    ) is True
    stages = await trace.read_stages("run-1")
    assert stages[2] == {"status": "completed", "attempt": 1}
    assert "PRIVATE" not in json.dumps(stages)
asyncio.run(scenario())
```

Also cover one `HGETALL` read, meta round-trip, active/cancelled state, missing-run exception, backend failure, deleted-run late write, parallel stage fields, and unchanged expiry.

- [ ] **Step 3: Run tests to prove the contract is red**

```bash
.venv312/bin/python test_vercel_trace.py
.venv312/bin/python test_quota_store.py
```

Expected: FAIL because `run_trace_store.TraceStore` and Redis trace commands do not exist.

- [ ] **Step 4: Commit the failing tests**

```bash
git add test_vercel_trace.py test_quota_store.py
git commit -m "定义Redis运行轨迹契约"
```

### Task 2: Implement Atomic Redis Trace Storage

**Files:**
- Create: `run_trace_store.py`
- Modify: `quota_store.py`
- Test: `test_vercel_trace.py`
- Test: `test_quota_store.py`

- [ ] **Step 1: Add conditional trace operations to `QuotaStore`**

```python
_TRACE_WRITE_SCRIPT = r"""
-- trace-write-v1
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
return 1
"""

_TRACE_CANCEL_SCRIPT = r"""
-- trace-cancel-v1
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
redis.call('HSET', KEYS[1], 'trace:cancelled', '1')
return 1
"""
```

Implement `write_trace_field(run_id, field, value)`, `read_trace_fields(run_id)`, `write_cancel(run_id)`, and `read_cancel(run_id)`. Permit only `trace:meta` and `trace:stage:1` through `trace:stage:8`; use one `HGETALL` for reads.

- [ ] **Step 2: Preserve absolute retention and prune history**

Change `_BIND_RUN_SCRIPT` to calculate `expires_at = floor(created_at) + session_ttl`, use `EXPIREAT` on the run hash, and prune history scores older than `created_at - session_ttl`. Make `list_runs()` remove missing members and return only:

```python
{
    "run_id": run_id,
    "model": fields.get("model", ""),
    "reasoning": fields.get("reasoning", ""),
    "created_at": self._parse_created_at(fields.get("created_at")),
    "status": fields.get("status", "running"),
    "safe_to_deliver": fields.get("safe_to_deliver") == "1",
}
```

- [ ] **Step 3: Add `run_trace_store.TraceStore`**

Move the current allow-list sanitizer from `vercel_trace.py` and implement:

```python
class MissingRun(RuntimeError):
    pass

class TraceStore:
    def __init__(self, quota=None):
        self._quota = quota or QuotaStore.from_env(
            session_ttl=_session_ttl_seconds(),
        )

    async def write_stage(self, run_id, stage_id, doc, *, created_epoch=None):
        payload = json.dumps(_sanitize(doc), ensure_ascii=False, separators=(",", ":"))
        return await self._quota.write_trace_field(
            run_id, f"trace:stage:{int(stage_id)}", payload,
        )

    async def read_stages(self, run_id):
        return _decode_stage_fields(await self._quota.read_trace_fields(run_id))

    async def write_cancel(self, run_id):
        return await self._quota.write_cancel(run_id)

    async def is_cancelled(self, run_id):
        state = await self._quota.read_cancel(run_id)
        if state is None:
            raise MissingRun(run_id)
        return state
```

Add equivalent `write_meta` and `read_meta` methods. Keep `created_epoch` for call compatibility but never use it to extend TTL.

- [ ] **Step 4: Run focused tests**

```bash
.venv312/bin/python test_quota_store.py
.venv312/bin/python test_vercel_trace.py
.venv312/bin/python -m compileall -q quota_store.py run_trace_store.py
```

Expected: all assertions pass and the trace tests contain no Blob fake.

- [ ] **Step 5: Commit Redis trace storage**

```bash
git add quota_store.py run_trace_store.py test_quota_store.py test_vercel_trace.py
git commit -m "迁移运行轨迹到Redis热状态"
```

### Task 3: Switch API And Workflow Runtime Off Blob

**Files:**
- Modify: `workflows/resume_workflow.py`
- Modify: `webui/vercel_server.py`
- Modify: `test_vercel_workflow.py`
- Modify: `test_vercel_api.py`
- Delete: `vercel_trace.py`

- [ ] **Step 1: Add failing runtime-wiring tests**

```python
def test_runtime_uses_redis_trace_facade():
    api_source = Path("webui/vercel_server.py").read_text(encoding="utf-8")
    workflow_source = Path("workflows/resume_workflow.py").read_text(encoding="utf-8")
    assert "from run_trace_store import TraceStore" in api_source
    assert "from run_trace_store import TraceStore" in workflow_source
    assert "vercel_trace" not in api_source + workflow_source
```

Replace Blob fake setup with one shared fake Redis-backed facade. Assert cancellation fails closed on missing state and deletion calls only `QuotaStore.delete_run()`.

- [ ] **Step 2: Run focused tests to verify failure**

```bash
.venv312/bin/python test_vercel_workflow.py
.venv312/bin/python test_vercel_api.py
```

Expected: FAIL while runtime modules still import `vercel_trace` and cleanup exists.

- [ ] **Step 3: Update runtime construction and deletion**

In `webui/vercel_server.py`, construct `TraceStore(_quota())`, remove `TRACE_RETENTION_SECONDS` and `/api/maintenance/cleanup`, and remove `_trace().delete_run()` from DELETE. In `workflows/resume_workflow.py`, import the new facade and construct it from the same Redis environment. Keep stage write failures best-effort after admission; make cancellation reads fail closed.

- [ ] **Step 4: Delete Blob trace code and rerun tests**

```bash
git rm vercel_trace.py
.venv312/bin/python test_vercel_workflow.py
.venv312/bin/python test_vercel_api.py
.venv312/bin/python test_vercel_trace.py
```

Expected: PASS with Redis-only trace state.

- [ ] **Step 5: Commit runtime migration**

```bash
git add workflows/resume_workflow.py webui/vercel_server.py \
  test_vercel_workflow.py test_vercel_api.py run_trace_store.py
git commit -m "切换Vercel运行时到Redis轨迹"
```

### Task 4: Remove Blob Deployment Requirements

**Files:**
- Modify: `vercel.json`
- Modify: `docs/deployment-vercel.md`
- Modify: `test_vercel_deploy_contract.py`

- [ ] **Step 1: Add a failing zero-Blob deployment contract**

```python
def test_application_runtime_has_no_blob_dependency():
    sources = [
        Path("webui/vercel_server.py"),
        Path("workflows/resume_workflow.py"),
        Path("run_trace_store.py"),
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in sources)
    assert "vercel.blob" not in text
    assert "BLOB_READ_WRITE_TOKEN" not in text
    assert "list_objects(" not in text
    config = json.loads(Path("vercel.json").read_text(encoding="utf-8"))
    assert "crons" not in config
```

- [ ] **Step 2: Run the deployment test to verify failure**

```bash
.venv312/bin/python test_vercel_deploy_contract.py
```

Expected: FAIL because the cleanup cron and Blob documentation still exist.

- [ ] **Step 3: Remove Blob configuration and rewrite operator steps**

Remove the cleanup cron from `vercel.json` and replace Blob setup/cleanup documentation with Upstash trace fields, 24-hour Redis expiry, adaptive polling, and the old-workflow drain gate. Keep the `vercel` package because Python Workflows still require it. State that the connected Blob store can be deleted manually only after Production has no old active workflow.

- [ ] **Step 4: Verify zero runtime Blob references**

```bash
.venv312/bin/python test_vercel_deploy_contract.py
rg -n "vercel\.blob|BLOB_READ_WRITE_TOKEN|list_objects\(" \
  webui workflows run_trace_store.py quota_store.py vercel.json
```

Expected: the test passes and `rg` prints no runtime match.

- [ ] **Step 5: Commit deployment cleanup**

```bash
git add vercel.json docs/deployment-vercel.md test_vercel_deploy_contract.py
git commit -m "移除Vercel Blob运行依赖"
```

### Task 5: Define The Public Resume Rendering Contract

**Files:**
- Create: `public_resume.py`
- Create: `test_public_resume.py`

- [ ] **Step 1: Write strict normalization tests**

Cover canonical output, unknown key removal, education `bullets` compatibility, string-skill normalization, wrong types, control characters, record/list limits, 256 KiB total size, and the requirement for at least one semantic education/experience/project record.

```python
def test_normalizes_only_rendering_schema():
    value = {
        "basic_info": {"name": "Candidate", "api_key": "secret"},
        "education": [{"school": "Example", "bullets": ["Dean list"]}],
        "experience": [], "projects": [],
        "skills": ["Python"], "extras": [], "prompt": "PRIVATE",
    }
    result = normalize_public_resume(value)
    assert result["basic_info"] == {
        "name": "Candidate", "phone": "", "email": "",
        "location": "", "target_role": "",
    }
    assert result["education"][0]["highlights"] == ["Dean list"]
    assert result["skills"] == [{"group": "技能", "items": ["Python"]}]
    assert "secret" not in json.dumps(result)
    assert "PRIVATE" not in json.dumps(result)
```

- [ ] **Step 2: Run the test to verify failure**

```bash
.venv312/bin/python test_public_resume.py
```

Expected: FAIL because `public_resume.py` does not exist.

- [ ] **Step 3: Implement normalization and limits**

Use fixed key tuples, reject non-string leaves, strip disallowed control characters, preserve HTML-significant text for renderer escaping, cap section records at 50, skills/extras at 100, nested lists at 100, scalar strings at 500, summaries/bullets at 4,000, and compact JSON at 256 KiB.

```python
def normalize_public_resume(value):
    if not isinstance(value, dict):
        return None
    result = {
        "basic_info": _basic_info(value.get("basic_info")),
        "summary": _text(value.get("summary", ""), 4000),
        "education": _records(value.get("education"), _EDUCATION_FIELDS),
        "experience": _records(value.get("experience"), _EXPERIENCE_FIELDS),
        "projects": _records(value.get("projects"), _PROJECT_FIELDS),
        "skills": _skills(value.get("skills")),
        "extras": _strings(value.get("extras"), 100, 4000),
    }
    if not _has_factual_record(result):
        return None
    encoded = json.dumps(
        result, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return result if len(encoded) <= 256 * 1024 else None
```

- [ ] **Step 4: Run the contract tests**

```bash
.venv312/bin/python test_public_resume.py
.venv312/bin/python -m compileall -q public_resume.py
```

Expected: all normalization and fail-closed cases pass.

- [ ] **Step 5: Commit the public resume contract**

```bash
git add public_resume.py test_public_resume.py
git commit -m "增加简历排版公共数据契约"
```

### Task 6: Return Structured Resume Through Workflow And Owner API

**Files:**
- Modify: `workflows/graph.py`
- Modify: `webui/vercel_server.py`
- Modify: `test_vercel_workflow.py`
- Modify: `test_vercel_api.py`
- Modify: `test_quota_store.py`

- [ ] **Step 1: Add failing final-result and API tests**

Assert that the graph returns a normalized structure, falls back from `optimized_resume` but never report Markdown, returns `None` for invalid structure, and that the API returns it only after ownership verification. Inject malicious backend fields and verify the detail response allow-list and history projection.

```python
def test_terminal_detail_returns_owner_scoped_resume_struct():
    client, backend, _, _, _ = _client()
    run_id = _start_ok(client).json()["run_id"]
    backend.complete(run_id, {
        "status": "completed", "safe_to_deliver": True,
        "unresolved_fixes": [], "report": "# Report",
        "model": "gpt-5.5", "reasoning": "xhigh",
        "optimized_resume_struct": VALID_PUBLIC_RESUME,
        "api_key": "PRIVATE",
    })
    response = client.get(f"/api/runs/{run_id}")
    assert response.json()["optimized_resume_struct"] == VALID_PUBLIC_RESUME
    assert "PRIVATE" not in response.text
    assert response.headers["cache-control"] == "private, no-store"
```

- [ ] **Step 2: Run focused tests to verify failure**

```bash
.venv312/bin/python test_vercel_workflow.py
.venv312/bin/python test_vercel_api.py
.venv312/bin/python test_quota_store.py
```

Expected: FAIL because final results do not contain the structured field and responses lack private cache headers.

- [ ] **Step 3: Normalize in the graph finalizer**

In `_finalize`, call `normalize_public_resume()` on the suggestion structure. If it returns `None`, parse only `suggestions["optimized_resume"]` with `parse_resume_text_to_struct()` and normalize once more. Return the canonical object or `None` as `optimized_resume_struct`.

- [ ] **Step 4: Project terminal detail and cache headers**

In `get_run`, assign:

```python
response["optimized_resume_struct"] = normalize_public_resume(
    result.get("optimized_resume_struct")
)
```

Return list and detail responses with `Cache-Control: private, no-store`. Cross-session reads must return 404 before `backend.result()` is called.

- [ ] **Step 5: Run tests and commit**

```bash
.venv312/bin/python test_public_resume.py
.venv312/bin/python test_vercel_workflow.py
.venv312/bin/python test_vercel_api.py
.venv312/bin/python test_quota_store.py
git add public_resume.py workflows/graph.py webui/vercel_server.py \
  test_vercel_workflow.py test_vercel_api.py test_quota_store.py
git commit -m "向会话所有者返回结构化简历"
```

### Task 7: Port The Complete Workbench Shell

**Files:**
- Modify: `webui/static/vercel_app.html`
- Modify: `test_vercel_ui.py`
- Preserve: `webui/static/index.html`

- [ ] **Step 1: Replace compact-page assertions with workbench assertions**

Add tests for the complete header, left input pane, four tabs, eight stage nodes, detail drawer, report view, three template controls, photo input, resume iframe, history view, and mobile breakpoint.

```python
def test_full_workbench_surface_is_present():
    html = _html()
    for element_id in (
        "tab-stream", "tab-report", "tab-layout", "tab-history",
        "view-stream", "view-report", "view-layout", "view-history",
        "resume-drop", "jdText", "model-seg", "effort-seg",
        "trace-drawer", "resume-frame", "photo-file", "recentRuns",
    ):
        assert f'id="{element_id}"' in html
    assert set(re.findall(r'data-tpl="([a-z]+)"', html)) == {
        "classic", "modern", "minimal",
    }
```

- [ ] **Step 2: Run the UI test to verify failure**

```bash
.venv312/bin/python test_vercel_ui.py
```

Expected: FAIL because the Vercel page still uses the compact preview.

- [ ] **Step 3: Port visual structure and pure browser controls**

Copy the layout tokens, header, fixed input pane, tab bar, scrollable views, stage/swarm presentation, report panel, template cards, and responsive breakpoints from `index.html`. Keep all events in the nonce script with `addEventListener`; do not copy `onclick`, CDN `marked`, editable base URL, live-question panel, or local API routes.

Use this top-level structure:

```html
<main class="app-shell">
  <header class="topbar"></header>
  <section class="workspace">
    <aside class="input-pane" id="formPane"></aside>
    <section class="result-pane">
      <nav class="tabs" aria-label="结果视图"></nav>
      <section class="view on" id="view-stream"></section>
      <section class="view" id="view-report"></section>
      <section class="view" id="view-layout"></section>
      <section class="view" id="view-history"></section>
    </section>
  </section>
</main>
<aside class="trace-drawer" id="trace-drawer" aria-hidden="true"></aside>
```

Populate every container with the corresponding existing local workbench controls; empty shell elements are not acceptable. Keep Analytics, Speed Insights, favicon, and every application script nonce-bearing.

- [ ] **Step 4: Bind tabs, model/effort segments, upload/drop, Mock, and BYOK**

Use `addEventListener` for every control. Read combinations only from `/api/config`. The advanced panel contains transient `apiKey` only and no base URL. Mock clears and disables the key field.

- [ ] **Step 5: Run syntax/UI tests and commit shell**

```bash
.venv312/bin/python test_vercel_ui.py
git diff --exit-code 1c4f1de -- webui/static/index.html
git add webui/static/vercel_app.html test_vercel_ui.py
git commit -m "迁移Vercel完整工作台视觉"
```

Expected: UI contract and inline JavaScript syntax pass; `index.html` has no diff.

### Task 8: Implement Vercel Run State And Adaptive Polling

**Files:**
- Modify: `webui/static/vercel_app.html`
- Modify: `test_vercel_ui.py`

- [ ] **Step 1: Add failing state-machine tests**

Assert explicit `idle/submitting/running/cancelling/terminal` rendering, one non-overlapping timeout, 5/10/15-second delay selection, hidden-page pause, immediate visible refresh, generation-based stale response rejection, uncertain-start history recovery, active-run refresh recovery, and terminal stop.

```python
def test_adaptive_polling_contract():
    html = _html()
    assert "function pollDelay(" in html
    assert "return 5000" in html
    assert "return 10000" in html
    assert "return 15000" in html
    assert "document.hidden" in html
    assert 'addEventListener("visibilitychange"' in html
    assert "pollGeneration" in html and "pollInFlight" in html
```

- [ ] **Step 2: Run the test to verify failure**

```bash
.venv312/bin/python test_vercel_ui.py
```

Expected: FAIL because fixed two-second polling remains.

- [ ] **Step 3: Implement one shared run state**

```javascript
const runState = {
  phase: "idle", runId: "", startedAt: 0, status: "",
  stages: [], report: "", safeToDeliver: false,
  unresolvedFixes: [], resumeStruct: null, model: "", reasoning: ""
};
function terminal(status){
  return ["completed","partial","failed","cancelled","deadline_exceeded"].includes(status);
}
function pollDelay(elapsedMs){
  if(elapsedMs < 30000) return 5000;
  if(elapsedMs < 150000) return 10000;
  return 15000;
}
```

Implement `applyStatus`, `renderRunState`, `schedulePoll`, `pollOnce`, `stopPolling`, and visibility handling. A reconnect keeps the last valid stages.

- [ ] **Step 4: Restore, cancel, delete, and switch history safely**

On boot, load recent runs, choose the newest non-terminal session-owned run, open it, and poll. Increment `navigationGeneration` on every run switch. Cancellation stays `cancelling` until terminal; deletion remains terminal-only.

- [ ] **Step 5: Run tests and commit the state adapter**

```bash
.venv312/bin/python test_vercel_ui.py
.venv312/bin/python test_vercel_api.py
git add webui/static/vercel_app.html test_vercel_ui.py
git commit -m "接入Vercel工作台运行状态"
```

### Task 9: Add Real Stage Expansion And Safe Reports

**Files:**
- Modify: `webui/static/vercel_app.html`
- Modify: `test_vercel_ui.py`

- [ ] **Step 1: Add failing trace/report interaction tests**

Assert every stage supports inline detail and drawer detail, both use `textContent`, only allow-listed stage labels appear, raw trace values never reach `innerHTML`, and report Markdown escapes HTML before formatting.

- [ ] **Step 2: Run the test to verify failure**

```bash
.venv312/bin/python test_vercel_ui.py
```

Expected: FAIL until the workbench trace and report views are connected.

- [ ] **Step 3: Render sanitized stage metadata in both surfaces**

Build detail lines only from `duration_ms`, `attempt`, `retry_category`, `validation_status`, `error_category`, `revision_round`, and `safe_to_deliver`. Assign `box.textContent = lines.join("\n")` and copy the same text into the drawer. Never read prompt, response, resume, JD, or arbitrary exception fields.

- [ ] **Step 4: Preserve safe Markdown and delivery warnings**

Keep the escape-first renderer. `partial`, failed verification, or `safe_to_deliver === false` displays unresolved fixes and a non-delivery banner. Markdown report download remains available for every non-empty report.

- [ ] **Step 5: Run tests and commit**

```bash
.venv312/bin/python test_vercel_ui.py
git add webui/static/vercel_app.html test_vercel_ui.py
git commit -m "完善Vercel步骤详情与安全报告"
```

### Task 10: Add Three Resume Templates And Safe Exports

**Files:**
- Modify: `webui/static/vercel_app.html`
- Modify: `test_vercel_ui.py`

- [ ] **Step 1: Add failing template and export tests**

```python
def test_resume_exports_require_verified_struct():
    html = _html()
    assert "function canExportResume(" in html
    assert "safeToDeliver" in html
    assert "unresolvedFixes.length === 0" in html
    assert "resumeStruct !== null" in html
    assert 'id="resume-frame"' in html and "sandbox=" in html
```

Also assert canonical field access, escaped values, three templates, local-only photo data, print/HTML controls, and warning-only preview for partial results.

- [ ] **Step 2: Run the test to verify failure**

```bash
.venv312/bin/python test_vercel_ui.py
```

Expected: FAIL until structured-result templates are wired.

- [ ] **Step 3: Port pure template generation**

Port `classic`, `modern`, and `minimal` CSS plus `buildResumeHTML` from the local page. Remove Markdown fallback parsing. Render only `runState.resumeStruct`, escape every scalar/list value, and set sandboxed iframe `srcdoc` to generated static HTML.

- [ ] **Step 4: Keep photos local and gate exports**

Reject photos over 5 MiB, keep the Data URL only in memory, and clear it on request. Use exactly:

```javascript
function canExportResume(){
  return runState.status === "completed" &&
    runState.safeToDeliver === true &&
    runState.unresolvedFixes.length === 0 &&
    runState.resumeStruct !== null;
}
```

Disable print and HTML download otherwise. Keep a warning preview for a non-null partial structure.

- [ ] **Step 5: Run tests and commit templates**

```bash
.venv312/bin/python test_vercel_ui.py
git add webui/static/vercel_app.html test_vercel_ui.py
git commit -m "恢复Vercel三套简历排版模板"
```

### Task 11: Run Full Offline Regression And Browser Acceptance

**Files:**
- Modify only when a failing test exposes an in-scope defect.
- Generate screenshots under ignored `output/playwright/vercel-workbench/`.

- [ ] **Step 1: Run all Vercel and security contracts**

```bash
for test in \
  test_public_security.py test_quota_store.py test_public_resume.py \
  test_vercel_trace.py test_vercel_workflow.py test_vercel_api.py \
  test_vercel_ui.py test_vercel_deploy_contract.py; do
  AGENT_MOCK=1 .venv312/bin/python "$test" || exit 1
done
```

Expected: every script exits 0.

- [ ] **Step 2: Run shared workflow regressions**

```bash
AGENT_MOCK=1 .venv312/bin/python test_tools.py
AGENT_MOCK=1 .venv312/bin/python test_agent.py
AGENT_MOCK=1 .venv312/bin/python test_pipeline.py
AGENT_MOCK=1 .venv312/bin/python -m compileall -q \
  quota_store.py run_trace_store.py public_resume.py workflows webui
```

Expected: all scripts and compile checks pass.

- [ ] **Step 3: Start the local Vercel entrypoint**

```bash
AGENT_WORKFLOW_TEST=1 AGENT_MOCK=1 AGENT_RUN_SIGNING_KEY=local-ui-test \
  .venv312/bin/uvicorn webui.vercel_server:app --host 127.0.0.1 --port 7861
```

Expected: `http://127.0.0.1:7861/` returns the full workbench with no console error.

- [ ] **Step 4: Run Playwright at three viewports**

Capture 1440x1000, 820x1180, and 390x844 screenshots. Exercise tabs, upload/drop, model/effort selection, Mock start, stage expansion, drawer, report, history, all templates, photo toggle, and export gating. Verify body and iframe pixels are nonblank and assert no incoherent overlap or horizontal page scroll.

- [ ] **Step 5: Commit final in-scope fixes when needed**

```bash
git add quota_store.py run_trace_store.py public_resume.py workflows webui \
  test_*.py vercel.json docs/deployment-vercel.md
git commit -m "完成Vercel工作台离线验收"
```

Skip this commit when Steps 1-4 require no tracked-file change.

### Task 12: Preview, Live Gateway, And Production Rollout

**Files:**
- No source mutation unless acceptance finds a reproducible defect.

- [ ] **Step 1: Push the branch and deploy Preview**

```bash
git push origin codex/reliability-live-jobs
vercel deploy
```

Expected: Preview deploy succeeds with Redis, gateway, signing, and Workflow variables; no Blob token is required.

- [ ] **Step 2: Drain old workflows before zero-Blob acceptance**

Wait at least 15 minutes from the last old-deployment start, verify no old `pending` or `running` Workflow remains, prune expired active leases, and confirm `ra:quota:active` is empty before deleting or disconnecting Blob.

- [ ] **Step 3: Run Preview acceptance**

Complete one Mock supplied-JD run, cancellation, terminal deletion, refresh recovery, history reopen, second-session 404 denial, three template previews, and safe export. Confirm Vercel logs contain no Blob call and the Blob dashboard does not increase from application traffic.

- [ ] **Step 4: Run one real gateway acceptance**

Run `gpt-5.5/xhigh` with a supplied JD. Require eight terminal stages, a non-empty report, correct `safe_to_deliver`, canonical `optimized_resume_struct`, usable template preview, and no API key or resume content in Redis trace/log fields.

- [ ] **Step 5: Promote the accepted commit and smoke Production**

```bash
vercel deploy --prod
```

Repeat one Mock run and one focused real smoke test. Confirm Cookie isolation, quotas, Redis polling, Analytics/Speed Insights requests, and zero application Blob operations before sharing the Production URL.
