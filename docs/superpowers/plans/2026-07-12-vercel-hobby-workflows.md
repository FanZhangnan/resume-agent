# Vercel Hobby Workflows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy an invite-only, observable resume optimization workflow on Vercel Hobby that supports exactly four model/effort combinations and reaches an application terminal state within 780 seconds.

**Architecture:** A dedicated FastAPI serverless entrypoint parses uploads and starts a durable Python Workflow. Top-level workflow steps execute the deterministic eight-stage graph, use strict typed contracts, and write privacy-safe stage status to Private Vercel Blob. The existing local subprocess/SSE server remains available, while the same frontend switches to signed-token polling on Vercel.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, OpenAI-compatible gateway, Vercel Python SDK 0.6.0, Vercel Workflows, Private Vercel Blob, vanilla JavaScript, DOMPurify, executable assertion scripts, Playwright.

---

## File Map

- `config.py`: exact model allowlist and global default budgets.
- `runtime_context.py`: per-run model, reasoning, and absolute deadline context.
- `contracts.py`: strict Pydantic validation and delivery predicates.
- `tools/scoring.py`: deterministic requirement scoring.
- `tools/job_sources.py`: normalized, source-verified recruiting connectors.
- `tools/recommendation.py`: live discovery and local ranking orchestration.
- `report_renderer.py`: pure deterministic final and partial report rendering.
- `run_security.py`: invite verification and stateless signed run tokens.
- `vercel_trace.py`: Private Blob stage status, cancellation, and cleanup.
- `workflows/runtime.py`: one shared `Workflows` registry.
- `workflows/resume_workflow.py`: top-level workflow and durable steps.
- `webui/vercel_server.py`: Vercel FastAPI API and HTML entrypoint.
- `webui/static/index.html`: local SSE and Vercel polling UI modes.
- `pyproject.toml`, `vercel.json`, `requirements.txt`: Python 3.12 and Vercel service configuration.

### Task 1: Exact Model Policy and Per-Run Runtime Context

**Files:**
- Create: `runtime_context.py`
- Create: `test_runtime_policy.py`
- Modify: `config.py`
- Modify: `llm_client.py`
- Modify: `tools/common.py`
- Modify: `agent.py`
- Modify: `probe_reasoning.py`
- Modify: `test_model_policy.py`
- Modify: `test_models.py`

- [ ] **Step 1: Write failing policy and context tests**

Create `test_runtime_policy.py` with assertions for the exact catalog, defaults, budgets, isolated context restoration, and client construction:

```python
import config
from runtime_context import RunSettings, current_settings, use_run_settings

EXPECTED = {
    "gpt-5.5": ("high", "xhigh"),
    "gpt-5.6-terra": ("high", "xhigh"),
}

def test_exact_policy():
    assert config.MODEL_REASONING_LEVELS == EXPECTED
    assert config.DEFAULT_MODEL == "gpt-5.5"
    assert config.DEFAULT_REASONING_BY_MODEL == {
        "gpt-5.5": "xhigh",
        "gpt-5.6-terra": "xhigh",
    }
    assert config.CALL_DEADLINE == 110
    assert config.RUN_TIMEOUT == 720
    assert config.ASK_TIMEOUT == 45
    for pair in (("gpt-5.6-sol", "high"), ("gpt-5.5", "max"),
                 ("gpt-5.6-terra", "max"), ("gpt-5.5", "low")):
        try:
            config.validate_model_reasoning(*pair)
        except ValueError:
            continue
        raise AssertionError(f"accepted invalid pair: {pair}")

def test_run_settings_restore():
    baseline = current_settings()
    chosen = RunSettings("gpt-5.6-terra", "high", 1234.0)
    with use_run_settings(chosen):
        assert current_settings() == chosen
    assert current_settings() == baseline

if __name__ == "__main__":
    test_exact_policy()
    test_run_settings_restore()
    print("Runtime policy tests passed.")
```

- [ ] **Step 2: Run the test and verify RED**

Run: `AGENT_MOCK=1 ./venv/bin/python test_runtime_policy.py`

Expected: FAIL because `runtime_context` does not exist and the catalog still includes Sol and unsupported levels.

- [ ] **Step 3: Implement immutable runtime settings and exact defaults**

Create `runtime_context.py`:

```python
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic, time

@dataclass(frozen=True)
class RunSettings:
    model: str
    reasoning: str
    deadline_epoch: float | None = None

_settings = ContextVar("resume_agent_run_settings", default=None)

def current_settings():
    return _settings.get()

def remaining_seconds(limit=110.0):
    settings = current_settings()
    if settings is None or settings.deadline_epoch is None:
        return float(limit)
    return max(0.0, min(float(limit), settings.deadline_epoch - time()))

def monotonic_deadline(limit=110.0):
    return monotonic() + remaining_seconds(limit)

@contextmanager
def use_run_settings(settings):
    token = _settings.set(settings)
    try:
        yield settings
    finally:
        _settings.reset(token)
```

Replace `MODEL_OPTIONS` in `config.py` with only GPT-5.5 and Terra, each using `("high", "xhigh")`. Set `CALL_DEADLINE=110`, `RUN_TIMEOUT=720`, `WATCHDOG_GRACE=15`, and `ASK_TIMEOUT=45`. Remove Sol and unsupported effort text from CLI help, probes, tests, and examples.

Update `LLMClient.__init__` to accept optional `model` and `reasoning`, validate them with `config.validate_model_reasoning`, and use those instance values in requests and trace data. Update `tools.common.get_client()` to cache clients by the active `(model, reasoning)` pair rather than one process-global configuration. At each tool entry, convert the cross-process epoch once with `monotonic_deadline()`; both the initial JSON request and semantic repair reuse that same monotonic deadline.

- [ ] **Step 4: Run policy tests and regression tests**

Run:

```bash
AGENT_MOCK=1 ./venv/bin/python test_runtime_policy.py
AGENT_MOCK=1 ./venv/bin/python test_model_policy.py
AGENT_MOCK=1 ./venv/bin/python test_tools.py
AGENT_MOCK=1 ./venv/bin/python test_agent.py
```

Expected: all scripts exit 0 and no output mentions `gpt-5.6-sol` or `max` as selectable.

- [ ] **Step 5: Commit the isolated task**

```bash
git add runtime_context.py test_runtime_policy.py config.py llm_client.py tools/common.py agent.py probe_reasoning.py test_model_policy.py test_models.py
git commit -m "收口模型策略与单次运行配置"
```

### Task 2: Strict Contracts, Deadline Ownership, and Deterministic Scoring

**Files:**
- Create: `contracts.py`
- Create: `report_renderer.py`
- Create: `tools/scoring.py`
- Create: `test_contracts.py`
- Create: `test_scoring.py`
- Modify: `tools/common.py`
- Modify: `tools/analysis.py`
- Modify: `tools/verification.py`
- Modify: `pipeline.py`
- Modify: `agent.py`

- [ ] **Step 1: Write failing strict-validation and scoring tests**

Create `test_contracts.py`:

```python
from pydantic import ValidationError
from contracts import MatchResult, VerificationResult, verification_is_deliverable

def test_strict_boolean_and_score():
    for bad in ({"score": 101}, {"score": -1}, {"score": "80"}):
        try:
            MatchResult.model_validate(bad)
        except ValidationError:
            continue
        raise AssertionError(f"accepted invalid match result: {bad}")
    try:
        VerificationResult.model_validate({
            "passed": "false", "safe_to_deliver": True, "required_fixes": []
        })
    except ValidationError:
        pass
    else:
        raise AssertionError("accepted string boolean")

def test_delivery_requires_all_three_gates():
    assert verification_is_deliverable({
        "passed": True, "safe_to_deliver": True, "required_fixes": []
    }) is True
    assert verification_is_deliverable({
        "passed": True, "safe_to_deliver": True, "required_fixes": ["fix"]
    }) is False

if __name__ == "__main__":
    test_strict_boolean_and_score()
    test_delivery_requires_all_three_gates()
    print("Contract tests passed.")
```

Create `test_scoring.py` with requirement fixtures that prove the 40/25/20/15 weights, location/work-authorization gates, score bounds, and cited evidence IDs.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
AGENT_MOCK=1 ./venv/bin/python test_contracts.py
AGENT_MOCK=1 ./venv/bin/python test_scoring.py
```

Expected: FAIL because the modules and strict models do not exist.

- [ ] **Step 3: Implement strict models and delivery predicate**

Create `contracts.py` using `ConfigDict(strict=True, extra="forbid")`. Define `MatchResult` with `score: int = Field(ge=0, le=100)` and the current result arrays, and `VerificationResult` with strict booleans and issue arrays. Implement:

```python
def verification_is_deliverable(value):
    try:
        result = VerificationResult.model_validate(value)
    except ValidationError:
        return False
    return (
        result.passed is True
        and result.safe_to_deliver is True
        and result.required_fixes == []
    )
```

Change `ask_json()` to accept a `validator` model class. The second semantic JSON attempt must reuse one absolute call deadline rather than starting another `CALL_DEADLINE`. Return `None` on strict validation failure and emit only field names and error categories.

- [ ] **Step 4: Implement local scoring and pure report rendering**

Create `tools/scoring.py` with `score_requirements(requirements, evidence, gates)`. Normalize each requirement to `hard`, `skill`, `business`, or `soft`, assign category totals `40`, `25`, `20`, and `15`, and award `met=1`, `under_evidenced=0.5`, `missing=0`. Set `eligible=False` when a required location or work-authorization gate fails. Every scored line contains `requirement_id`, `status`, `points`, and `evidence_ids`.

Create `report_renderer.py` with `render_report(state, terminal_status, unresolved_fixes)` that never calls an LLM or writes files. Refactor `pipeline.py` and `agent.py` to use the pure renderer and `verification_is_deliverable()`.

- [ ] **Step 5: Verify GREEN and deadline regression behavior**

Run:

```bash
AGENT_MOCK=1 ./venv/bin/python test_contracts.py
AGENT_MOCK=1 ./venv/bin/python test_scoring.py
AGENT_MOCK=1 ./venv/bin/python test_trace_catalog.py
AGENT_MOCK=1 ./venv/bin/python test_pipeline.py
```

Expected: all scripts exit 0; semantic repair cannot consume a second full 110-second budget.

- [ ] **Step 6: Commit the isolated task**

```bash
git add contracts.py report_renderer.py tools/scoring.py test_contracts.py test_scoring.py tools/common.py tools/analysis.py tools/verification.py pipeline.py agent.py
git commit -m "增加严格结果契约与确定性评分"
```

### Task 3: Verified Live Job Discovery

**Files:**
- Create: `tools/job_sources.py`
- Create: `test_job_sources.py`
- Modify: `tools/recommendation.py`
- Modify: `tools/__init__.py`
- Modify: `config.py`
- Modify: `pipeline.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Write failing provider normalization tests**

Create `test_job_sources.py` with inline Greenhouse, Lever, Ashby, and Adzuna-shaped payloads. Assert that every normalized posting contains `provider`, `provider_job_id`, `company`, `title`, `description`, `location`, `source_url`, `published_at`, and `checked_at`. Assert that excluded locations are removed and entries without an HTTPS source URL cannot be recommended.

Include this no-fabrication contract:

```python
def test_empty_sources_never_create_synthetic_jobs():
    result = discover_live_jobs(
        resume_info={"skills": ["Python"]},
        preferences={"locations": ["Brisbane"], "excluded_locations": []},
        sources=[],
    )
    assert result == []
```

- [ ] **Step 2: Run the provider test and verify RED**

Run: `AGENT_MOCK=1 ./venv/bin/python test_job_sources.py`

Expected: FAIL because `tools.job_sources` does not exist.

- [ ] **Step 3: Implement bounded source adapters**

Create `tools/job_sources.py` with a `LiveJob` strict model and pure normalizers for Greenhouse, Lever, Ashby, and Adzuna. Use an injected `httpx.Client` with a 10-second timeout, response size cap, HTTPS-only URLs, and no redirects to private or loopback addresses. Read configured ATS boards from `AGENT_JOB_SOURCES_JSON`; use Adzuna only when its application ID and key are configured.

Implement `discover_live_jobs()` to fetch sources checked in the current run, apply hard location exclusions, add `checked_at`, and return an empty list when nothing verified survives. Never call the LLM to invent a posting or source URL.

- [ ] **Step 4: Replace synthetic recommendation generation**

Modify `tools/recommendation.py` so the LLM may produce career-direction query terms but cannot produce job facts. Rank normalized postings locally from resume evidence, preferred regions, remote/relocation settings, and posting freshness. Preserve provider descriptions as the JD passed to stage 4.

Require `locations`, `remote_preference`, `relocation_willingness`, and `work_authorization` before the no-JD branch. Return a successful empty-result report with `reason="no_verified_postings"`, not five typical JDs.

- [ ] **Step 5: Run provider, pipeline, and tool regression tests**

Run:

```bash
AGENT_MOCK=1 ./venv/bin/python test_job_sources.py
AGENT_MOCK=1 ./venv/bin/python test_pipeline.py
AGENT_MOCK=1 ./venv/bin/python test_tools.py
```

Expected: all scripts exit 0, and mock recommendations carry checked source URLs rather than `typical_jd` claims.

- [ ] **Step 6: Commit the isolated task**

```bash
git add tools/job_sources.py test_job_sources.py tools/recommendation.py tools/__init__.py config.py pipeline.py requirements.txt
git commit -m "接入可核验的实时岗位来源"
```

### Task 4: Signed Run Access and Private Blob Trace Store

**Files:**
- Create: `run_security.py`
- Create: `vercel_trace.py`
- Create: `test_run_security.py`
- Create: `test_vercel_trace.py`
- Modify: `.gitignore`
- Modify: `requirements.txt`

- [ ] **Step 1: Write failing security and trace-store tests**

Create `test_run_security.py` to prove constant-time invite validation, signed-token round trips, expiry, run-ID mismatch rejection, and tamper rejection. Create `test_vercel_trace.py` with an injected in-memory Blob client and assert:

```python
async def test_stage_paths_are_isolated():
    store = TraceStore(client=FakeBlobClient())
    await store.write_stage("run-a", 2, {"status": "running", "resume": "secret"})
    await store.write_stage("run-a", 4, {"status": "completed"})
    stages = await store.read_stages("run-a")
    assert stages[2]["status"] == "running"
    assert stages[4]["status"] == "completed"
    assert "secret" not in repr(stages)
```

Also assert cancellation markers, immediate deletion, and age-based cleanup.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
/opt/homebrew/bin/python3.12 test_run_security.py
/opt/homebrew/bin/python3.12 test_vercel_trace.py
```

Expected: FAIL because both modules are absent.

- [ ] **Step 3: Implement stateless run tokens**

Create `run_security.py` with URL-safe base64 payloads containing `run_id` and `exp`, signed with HMAC-SHA256 from `AGENT_RUN_SIGNING_KEY`. Verify signatures with `hmac.compare_digest`, reject expired tokens, and never log token contents. Implement `verify_invite()` against `AGENT_INVITE_CODE` with the same constant-time comparison.

- [ ] **Step 4: Implement Private Blob stage documents**

Create `vercel_trace.py` using public `vercel.blob.AsyncBlobClient`. Write fixed private paths `runs/{safe_run_id}/stage-{stage_id}.json` with `overwrite=True`. Sanitize keys using the existing Trace Catalog policy, permit only stage status metadata, and read with `use_cache=False`. Use separate paths for `cancel.json` and `meta.json` so parallel stages never race on one object.

Implement `delete_run(run_id)` and `cleanup_before(epoch)` using prefix listing. Blob failures emit a redacted log and do not expose resume content.

- [ ] **Step 5: Verify GREEN under Python 3.12**

Create `.venv312` with `/opt/homebrew/bin/python3.12 -m venv .venv312`, install requirements, then run:

```bash
.venv312/bin/python test_run_security.py
.venv312/bin/python test_vercel_trace.py
.venv312/bin/python -m compileall -q run_security.py vercel_trace.py
```

Expected: all commands exit 0.

Add `.venv312/` and `.playwright-cli/` to `.gitignore` before creating generated artifacts.

- [ ] **Step 6: Commit the isolated task**

```bash
git add run_security.py vercel_trace.py test_run_security.py test_vercel_trace.py requirements.txt .gitignore
git commit -m "增加签名访问与私有运行轨迹"
```

### Task 5: Durable Eight-Stage Vercel Workflow

**Files:**
- Create: `workflows/__init__.py`
- Create: `workflows/runtime.py`
- Create: `workflows/resume_workflow.py`
- Create: `test_vercel_workflow.py`
- Modify: `pipeline.py`
- Modify: `tools/common.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Write failing workflow graph tests**

Create `test_vercel_workflow.py`. Import the registry with `Workflows(as_vercel_job=False)` through an environment-controlled factory and call a pure `run_workflow_graph(payload, operations)` helper with fake tool operations and trace storage. Prove:

- supplied-JD order is parse, concurrent extract/JD, match, rewrite, audit, optional targeted repair/re-audit, report;
- no-JD order includes verified discovery and selected posting analysis;
- each step receives the same `deadline_epoch` and exact model pair;
- missing time returns `deadline_exceeded` without invoking a tool;
- provider failure produces a terminal partial result rather than raising;
- cancellation stops at the next stage boundary;
- final delivery uses all three strict verification gates.

- [ ] **Step 2: Run workflow tests and verify RED**

Run: `.venv312/bin/python test_vercel_workflow.py`

Expected: FAIL because the workflow package is absent.

- [ ] **Step 3: Register stable top-level workflow and steps**

Create `workflows/runtime.py`:

```python
import os
from vercel.workflow import Workflows

wf = Workflows(as_vercel_job=os.environ.get("AGENT_WORKFLOW_TEST") != "1")
```

Define every `@wf.step` and `@wf.workflow` at module top level in `workflows/resume_workflow.py`. Do not dynamically create or rename decorated functions because module plus qualname forms the persisted ID.

Each step obtains `get_step_metadata().run_id`, writes `running`, checks cancellation and `deadline_epoch`, enters `use_run_settings(RunSettings(...))`, executes synchronous tool code with `asyncio.to_thread`, catches expected exceptions into a JSON-serializable result, and writes its terminal stage status. Decorated functions delegate to `run_workflow_graph()` and focused operation adapters so tests exercise the same control flow without requiring a Vercel replay context.

- [ ] **Step 4: Implement deterministic workflow orchestration**

The workflow validates its model pair again, starts extraction and supplied-JD analysis with deterministic `asyncio.gather`, and falls back to sequential calls when `AGENT_WORKFLOW_PARALLEL=0`. It passes only required state slices between steps, never file bytes or paths. Stage 7 invokes targeted repair and targeted re-audit only when strict verification fails. Stage 8 calls the pure renderer and returns:

```python
{
    "status": "completed" | "partial" | "cancelled" | "deadline_exceeded",
    "safe_to_deliver": bool,
    "report": str,
    "unresolved_fixes": list[str],
    "model": str,
    "reasoning": str,
}
```

All recoverable LLM and provider failures return structured results so Vercel's default platform retries do not repeat ordinary application failures. Unexpected process crashes may retry, but the same absolute epoch prevents new work after 720 seconds.

- [ ] **Step 5: Verify GREEN and local regression tests**

Run:

```bash
AGENT_WORKFLOW_TEST=1 AGENT_MOCK=1 .venv312/bin/python test_vercel_workflow.py
AGENT_MOCK=1 .venv312/bin/python test_pipeline.py
AGENT_MOCK=1 .venv312/bin/python test_agent.py
```

Expected: all scripts exit 0.

- [ ] **Step 6: Commit the isolated task**

```bash
git add workflows test_vercel_workflow.py pipeline.py tools/common.py requirements.txt
git commit -m "实现Vercel八阶段持久工作流"
```

### Task 6: Vercel FastAPI Entry Point and Deployment Configuration

**Files:**
- Create: `webui/vercel_server.py`
- Create: `test_vercel_api.py`
- Create: `pyproject.toml`
- Create: `vercel.json`
- Create: `.vercelignore`
- Modify: `requirements.txt`

- [ ] **Step 1: Write failing API contract tests**

Create `test_vercel_api.py` using FastAPI `TestClient` and injected fake workflow/trace dependencies. Cover `/api/config`, upload type and 4 MB limits, scanned/empty file rejection, missing no-JD preferences, invite rejection, exact model validation, signed start response, status polling, final result retrieval only after completion, cooperative cancellation, trace deletion, and cron authorization.

Assert that `/api/config` returns only:

```python
{
    "gpt-5.5": ["high", "xhigh"],
    "gpt-5.6-terra": ["high", "xhigh"],
}
```

- [ ] **Step 2: Run API tests and verify RED**

Run: `AGENT_WORKFLOW_TEST=1 .venv312/bin/python test_vercel_api.py`

Expected: FAIL because `webui.vercel_server` does not exist.

- [ ] **Step 3: Implement the serverless API**

Create `webui/vercel_server.py` without sweeper threads, subprocesses, global jobs, sessions, or quota files. `POST /api/runs` verifies the invite, reads at most 4 MB plus one overflow byte, writes a randomized `/tmp` file, parses it, deletes it in `finally`, starts `resume_workflow`, writes stage 1 completion, and returns `202` with `run_id`, signed token, and expiry.

`GET /api/runs/{run_id}` verifies `Authorization: Bearer`, calls `Run(run_id).status()`, reads stage documents, and calls `return_value()` only when completed. Add token-protected cancel/delete endpoints and `GET /api/maintenance/cleanup` protected by `CRON_SECRET`.

- [ ] **Step 4: Add exact Vercel configuration**

Create `pyproject.toml`:

```toml
[project]
name = "resume-agent"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = []

[tool.vercel]
entrypoint = "webui.vercel_server:app"
```

Create `vercel.json` with schema, a 60-second FastAPI function duration, the Python workflow worker entrypoint and `__wkf_*` topic, security headers, and one daily cleanup cron. Add `.env`, `.claude`, uploads, outputs, runs, traces, Playwright artifacts, local virtual environments, and quota data to `.vercelignore`.

Pin `vercel==0.6.0` and explicit compatible FastAPI, Pydantic, httpx, multipart, PDF, and DOCX dependencies in `requirements.txt`.

- [ ] **Step 5: Verify GREEN and Vercel build contracts**

Run:

```bash
AGENT_WORKFLOW_TEST=1 .venv312/bin/python test_vercel_api.py
.venv312/bin/python -m compileall -q webui/vercel_server.py workflows
npx --yes vercel@latest build
```

Expected: API tests pass, compilation exits 0, and Vercel build recognizes one FastAPI entrypoint and the workflow worker.

- [ ] **Step 6: Commit the isolated task**

```bash
git add webui/vercel_server.py test_vercel_api.py pyproject.toml vercel.json .vercelignore requirements.txt
git commit -m "增加Vercel Serverless入口与配置"
```

### Task 7: Vercel Polling UI, Trace Details, and Markdown Security

**Files:**
- Create: `test_vercel_ui.py`
- Modify: `webui/static/index.html`
- Modify: `test_web_trace_ui.py`
- Modify: `webui/vercel_server.py`

- [ ] **Step 1: Write failing frontend source and behavior tests**

Create `test_vercel_ui.py` to extract and run JavaScript functions under Node. Assert exact fallback model options, reasoning updates, Vercel mode detection, signed Authorization headers, two-second polling, page-reload recovery, cooperative cancellation, eight stable stage rows, sanitized detail rendering, and terminal statuses.

Add static assertions that public mode contains no BYOK input, arbitrary base URL control, Sol, `max`, or API key persistence. Assert final report rendering calls `DOMPurify.sanitize(marked.parse(...))` and the document includes a restrictive CSP.

- [ ] **Step 2: Run UI tests and verify RED**

Run:

```bash
AGENT_MOCK=1 .venv312/bin/python test_vercel_ui.py
AGENT_MOCK=1 .venv312/bin/python test_web_trace_ui.py
```

Expected: the new test fails because the page still contains old models, BYOK persistence, raw `marked.parse()` output, and SSE-only execution.

- [ ] **Step 3: Implement dual local/Vercel transport**

Keep local `/api/run` plus SSE behavior when `/api/status` reports `deployment_mode="local"`. For `deployment_mode="vercel"`, submit to `/api/runs`, keep the signed token in `sessionStorage`, poll status every two seconds with the Authorization header, restore the active run after reload, and call the cooperative cancel endpoint.

Render the eight stage rows from API status documents without changing their dimensions. The detail drawer shows only duration, attempts, token counts, retry category, validation state, and redacted errors. It never renders arbitrary trace strings as HTML.

- [ ] **Step 4: Remove public credential controls and sanitize reports**

In Vercel mode, hide BYOK and base URL controls before interaction and never read or write API credentials to `localStorage`. Add pre-run location, remote, relocation, and work-authorization controls for the no-JD path.

Load Marked and DOMPurify with pinned versions and integrity attributes, configure Marked's HTML renderer to return escaped text, and sanitize all generated report HTML. Remove inline `onclick` attributes and register handlers with `addEventListener`. In Vercel mode, read the HTML template, replace a nonce placeholder on the application script, and return a per-response CSP containing that nonce, `strict-dynamic`, self-only connections, and no object, frame, or base targets. Keep `textContent` or `escapeHtml` for all stage and diagnostic text.

- [ ] **Step 5: Verify GREEN and browser source regression tests**

Run:

```bash
AGENT_MOCK=1 .venv312/bin/python test_vercel_ui.py
AGENT_MOCK=1 .venv312/bin/python test_web_trace_ui.py
node --check /tmp/resume-agent-inline.js
```

Generate `/tmp/resume-agent-inline.js` in the test script from inline page scripts before the `node --check` assertion. Expected: all commands exit 0.

- [ ] **Step 6: Commit the isolated task**

```bash
git add test_vercel_ui.py webui/static/index.html test_web_trace_ui.py webui/vercel_server.py
git commit -m "增加Vercel轮询界面并收紧前端安全"
```

### Task 8: Full Verification, Preview Deployment, and Production Promotion

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md` only if commands or paths changed from the current guide
- Create: `docs/deployment-vercel.md`

- [ ] **Step 1: Document deployment and privacy behavior**

Document Private Blob creation, environment variable names, one-day Workflow retention, 24-48 hour trace cleanup, invite-only preview, exact model combinations, 4 MB upload limit, cooperative cancellation, and the absence of mid-run questions. State that previously shared credentials must be rotated before Production.

- [ ] **Step 2: Run the complete offline suite under Python 3.12**

Run every non-live root script explicitly:

```bash
AGENT_MOCK=1 .venv312/bin/python test_tools.py
AGENT_MOCK=1 .venv312/bin/python test_agent.py
AGENT_MOCK=1 .venv312/bin/python test_model_policy.py
AGENT_MOCK=1 .venv312/bin/python test_runtime_policy.py
AGENT_MOCK=1 .venv312/bin/python test_contracts.py
AGENT_MOCK=1 .venv312/bin/python test_scoring.py
AGENT_MOCK=1 .venv312/bin/python test_job_sources.py
AGENT_MOCK=1 .venv312/bin/python test_pipeline.py
AGENT_MOCK=1 .venv312/bin/python test_trace_catalog.py
AGENT_MOCK=1 .venv312/bin/python test_run_security.py
AGENT_MOCK=1 .venv312/bin/python test_vercel_trace.py
AGENT_WORKFLOW_TEST=1 AGENT_MOCK=1 .venv312/bin/python test_vercel_workflow.py
AGENT_WORKFLOW_TEST=1 AGENT_MOCK=1 .venv312/bin/python test_vercel_api.py
AGENT_MOCK=1 .venv312/bin/python test_web_trace_ui.py
AGENT_MOCK=1 .venv312/bin/python test_vercel_ui.py
.venv312/bin/python -m compileall -q .
git diff --check
```

Expected: every test exits 0, compilation emits no errors, and `git diff --check` is silent.

- [ ] **Step 3: Run credential and privacy scans**

Run tracked-file scans for API-key patterns, `.env`, resume/JD fixture leakage beyond `samples/`, raw prompt logging, and forbidden `vercel._internal` imports. Expected: no secret values, no credential files, no internal SDK imports, and no raw resume content in trace serializers.

- [ ] **Step 4: Build and deploy a Vercel Preview**

Use `npx vercel@latest whoami`, link or import the GitHub repository, create and connect one Private Blob store, and configure Preview environment values: rotated gateway key, fixed `AGENT_BASE_URL`, `AGENT_INVITE_CODE`, `AGENT_RUN_SIGNING_KEY`, `BLOB_READ_WRITE_TOKEN`, `CRON_SECRET`, `AGENT_DEFAULT_MODEL=gpt-5.5`, and `AGENT_REASONING=xhigh`. Do not configure Sol or `max`.

Run `npx vercel@latest deploy` and record the Preview URL. Verify `/api/config` before starting paid gateway calls.

- [ ] **Step 5: Verify browser behavior on Preview**

Use Playwright at `1440x1000`, `1024x768`, and `390x844`. Test upload validation, supplied-JD/no-JD forms, all four selectors, eight stage rows, detail drawer, reload recovery, cooperative cancellation, report rendering/export, CSP console output, and absence of overlaps or blank states. Save screenshots outside tracked source directories.

- [ ] **Step 6: Run the live acceptance matrix**

Run two models by two efforts by supplied-JD/no-JD paths. Record duration, final status, safe-delivery gate, provider sources, tokens, and failure category. Every run must reach a terminal state under 780 seconds. The supplied fixture must produce a verifier-approved report for each exposed combination; a no-JD empty result is valid only when it explicitly says no verified posting was found.

If any combination fails, block Production promotion, reproduce the cause with a new failing regression test, fix it, and repeat its matrix cells. All four user-selected combinations must pass before the public catalog is enabled.

- [ ] **Step 7: Promote the tested commit and verify Production**

Deploy the exact tested commit with `npx vercel@latest --prod`. Recheck `/api/config`, one invite-protected mock run, one live GPT-5.5/xhigh supplied-JD run, Blob trace deletion, and Vercel Logs redaction. Report the Production URL and any remaining Hobby quota constraints.

- [ ] **Step 8: Commit documentation and final verification record**

```bash
git add README.md AGENTS.md docs/deployment-vercel.md
git commit -m "补充Vercel部署与验收说明"
```
