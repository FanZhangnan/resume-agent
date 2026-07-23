# Resume Agent Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a bounded and observable eight-stage resume pipeline with GPT-5.5 as the production baseline and source-verified live job discovery.

**Architecture:** A deterministic harness calls focused tools and emits one structured trace stream. LLM calls have owned retry/deadline policies, while job facts come only from provider connectors and scoring/report assembly are deterministic.

**Tech Stack:** Python 3.9, OpenAI-compatible Python SDK, FastAPI/SSE, vanilla JavaScript, JSONL, standard-library HTTP plus Pydantic validation.

---

### Task 1: Trace Catalog and Reliability Budgets

**Files:**
- Create: `trace_catalog.py`
- Create: `test_trace_catalog.py`
- Modify: `config.py`, `llm_client.py`, `tools/common.py`, `tools/interaction.py`, `agent.py`, `webui/server.py`

- [ ] Write failing tests proving trace redaction, ordered events, exactly two owned network attempts, stream deadline checks, run deadlines, and automatic question skipping.
- [ ] Run `./venv/bin/python test_trace_catalog.py` and confirm failures identify missing trace and deadline behavior.
- [ ] Implement `TraceCatalog.emit()` with schema `resume-agent.trace.v1`, monotonic sequence numbers, secure JSONL files, summaries, and `@@TRACE@@` stdout events.
- [ ] Set SDK `max_retries=0`; apply `AGENT_MAX_RETRIES=2`, `AGENT_CALL_DEADLINE=180`, `AGENT_RUN_TIMEOUT=900`, and `AGENT_ASK_TIMEOUT=120`.
- [ ] Instrument LLM, semantic JSON retries, tools, user waits, revisions, reports, and terminal run status.
- [ ] Parse trace events before ordinary stdout suppression in the Web reader and enforce an exact per-job watchdog.
- [ ] Re-run the trace test plus existing mock tests and confirm all pass.

### Task 2: Production Model Policy and Benchmark Harness

**Files:**
- Create: `benchmark_models.py`
- Create: `test_model_policy.py`
- Modify: `config.py`, `agent.py`, `webui/server.py`, `webui/static/index.html`, `README.md`

- [ ] Write failing tests for GPT-5.5 xhigh default, experimental 5.6 labels, exact model/effort validation, and an opt-in benchmark CLI.
- [ ] Run `./venv/bin/python test_model_policy.py` and confirm the current catalog fails the new baseline.
- [ ] Add `gpt-5.5` as the stable default; label Terra and Sol experimental without inferring quality from names.
- [ ] Make benchmark execution require `--live`, record per-operation latency, completion, JSON validity, tool-call success, tokens, and report verifier status.
- [ ] Update the Web selector to display stable/experimental status and prevent accidental global `max` use without an explicit warning.
- [ ] Re-run model, mock, and compile tests.

### Task 3: Deterministic Eight-Stage Harness

**Files:**
- Create: `pipeline.py`
- Create: `test_pipeline.py`
- Modify: `agent.py`, `tools/common.py`, `webui/static/index.html`, `README.md`

- [ ] Write failing tests for stage order, conditional discovery, supplied-JD extraction/analysis concurrency, one revision, partial report delivery, and no planner LLM calls.
- [ ] Run `AGENT_MOCK=1 ./venv/bin/python test_pipeline.py` and confirm failure because the ReAct loop is still the default.
- [ ] Implement a harness state machine with explicit stage gates and trace spans.
- [ ] Use thread-local LLM clients so supplied-JD extraction and JD analysis can run concurrently without shared finish state.
- [ ] Keep `AGENT_ORCHESTRATOR=react` only as a diagnostic fallback; default to the deterministic pipeline.
- [ ] Render final reports locally and preserve completed stage outputs on errors.
- [ ] Re-run pipeline and existing workflow tests.

### Task 4: Verified Live Jobs and Evidence-Based Quality Gates

**Files:**
- Create: `tools/job_sources.py`
- Create: `tools/scoring.py`
- Create: `test_job_sources.py`, `test_quality_gates.py`
- Modify: `tools/recommendation.py`, `tools/resume_tools.py`, `tools/analysis.py`, `tools/verification.py`, `agent.py`, `webui/server.py`, `webui/static/index.html`, `README.md`, `requirements.txt`

- [ ] Write failing provider tests with recorded Greenhouse, Lever, Ashby, and Adzuna-shaped fixtures; assert URLs, freshness, normalization, excluded-location rejection, and no synthetic fallback.
- [ ] Write failing quality tests for evidence IDs, requirement IDs, rubric weights, deterministic totals, grounded patches, strict verification, and score bounds.
- [ ] Implement provider interfaces with bounded HTTP, response-size caps, current-run `checked_at`, and normalized live-posting statuses.
- [ ] Require complete discovery preferences and configure Adzuna through environment variables; ship only checked no-auth ATS boards as defaults.
- [ ] Replace generated `typical_jd` values with provider descriptions and rank verified jobs against resume evidence.
- [ ] Calculate scores locally from the 40/25/20/15 rubric and treat location/work authorization as gates.
- [ ] Make rewrites evidence-linked patches and require both verifier booleans plus an empty fix list.
- [ ] Add UI fields for location constraints and show source, freshness, status, and links for every recommended role.
- [ ] Run all offline tests, then opt-in provider smoke tests without invoking an LLM.

### Task 5: Debug UI and End-to-End Verification

**Files:**
- Modify: `webui/server.py`, `webui/static/index.html`
- Create: `test_web_trace.py`

- [ ] Write failing source/API tests for trace listing, trace download, per-stage expansion, drawer controls, and privacy-safe rendering.
- [ ] Add a debug drawer, inline step expansion, waterfall timeline, retry/error badges, current blocking state, and trace download.
- [ ] Keep raw capture unavailable in public mode and escape all trace text before rendering.
- [ ] Run `test_*.py`, Python compilation, `git diff --check`, and secret scanning.
- [ ] Use Playwright to verify desktop and mobile: stable model selection, live-job provenance, user-wait countdown, inline details, drawer navigation, trace download, and report delivery.
- [ ] Restart `127.0.0.1:7860` and verify `/api/status` and offline Mock flow.
