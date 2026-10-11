# Resume Agent 维护实施计划

> **For agentic workers:** Use superpowers:subagent-driven-development task briefs and reviews. Follow file ownership and preserve the original local checkout.

**Goal:** 新域名下的真实分析与可编辑简历交付在原 Vercel 生产网址正常运行。

**Architecture:** 保留 Python FastAPI + Vercel durable workflow。服务端配置管理网关/模型，纯 Python 模块负责结果摘要和 Word 生成，现有单文件工作台消费这些接口。

**Tech Stack:** Python 3.12, OpenAI-compatible API, FastAPI, Vercel Python Workflows, Redis, python-docx, inline HTML/CSS/JavaScript.

**Spec:** `docs/superpowers/specs/2026-10-11-provider-maintenance-design.md`

## Global Constraints

- Default gateway: `https://api.defou.ai/v1`, server-side only.
- Keep cookie ownership, quotas, cancellation, deadlines and honesty gates.
- Never log keys or resume contents; never silently substitute Mock for a real run.
- UI output is Chinese; scores are evidence fit, not hiring probability.
- Python 3.12; tests remain executable scripts under `tests/`.

### Task 1: 服务端 API 配置与运行可靠性

**Files:** `config.py`, `llm_client.py`, `webui/server.py`, `webui/vercel_server.py`, `workflows/resume_workflow.py`, `tests/test_models.py`, new `tests/test_provider_config.py`.

**Interfaces:** `config.normalize_gateway_base_url(value) -> str`; `config.MODEL_OPTIONS`; `config.MODEL_OPTION_BY_ID`; `config.validate_model_reasoning(model, reasoning) -> (str,str)`; public config adds `service: {available: bool, provider_name: str, message: str}`. No URL or secret enters public config.

- [ ] Test HTTPS admin URL normalization and reject private hosts, userinfo/query/fragments.
- [ ] Test `AGENT_MODEL_OPTIONS` JSON, no-reasoning models, exact request capabilities and invalid model rejection in isolated subprocesses.
- [ ] Configure new default endpoint and curated model metadata, retain legacy aliases only as compatibility constants.
- [ ] Ensure public request Base URL cannot override server configuration; add maintenance guard before quota admission.
- [ ] Run model/runtime/API/workflow tests and a tiny synthetic request to the authorized new provider.
- [ ] Review the task diff and commit it independently.

### Task 2: 结果交付界面

**Files:** only `webui/static/vercel_app.html`, frontend-specific new or updated checks in `tests/`.

**Interfaces consumed:** `/api/config.service` above; completed run data `delivery_summary` object `{match_score, strengths, gaps, priority_actions, changes:[{section,before,after,reason}], verified}`; `GET /api/runs/{id}/resume.docx`. Missing summary in old runs must degrade gracefully.

- [ ] Improve heading and input instructions while retaining paper/ink/signal-color design, source tabs, model controls and mobile behavior.
- [ ] Render maintenance and Mock state plainly; dynamic reasoning controls support an empty level without exposing “none” as a technical choice.
- [ ] Add summary and before/after comparison, using textContent for all dynamic text. Explain that fit is not offer probability.
- [ ] Add editable Word download, gated by completed+safeToDeliver+no unresolved fixes; keep HTML/PDF/Markdown routes intact.
- [ ] Add retry using current form state; never persist or restore BYOK keys.
- [ ] Run frontend syntax/source checks, record UI screenshots after integration, and review the diff.

### Task 3: 摘要与 Word 导出

**Files:** new `delivery_summary.py`, new `resume_export.py`, new tests for both; controller integrates `workflows/graph.py` and `webui/vercel_server.py`.

**Interfaces produced:** `build_delivery_summary(state, verified=False) -> dict`; `normalize_delivery_summary(value) -> dict`; `render_resume_docx(resume_struct) -> bytes`.

- [ ] Map only existing match/suggestion facts into bounded summary fields. Clamp scores to 0–100 and do not invent values when missing.
- [ ] Normalize rewrite objects to `section/before/after/reason`; text limits 600 characters and lists max 8, changes max 6.
- [ ] Produce A4 single-column DOCX from normalize_public_resume schema, preserving dates/names/facts, including Chinese font configuration.
- [ ] Verify file ZIP/document XML, text fidelity, invalid input rejection and absence of invented values.
- [ ] Attach summary to private workflow result and GET run response.
- [ ] Add owned/safe-terminal DOCX download route; test cross-session denial and nondeliverable rejection.

### Task 4: 发布验证

**Files:** new `.github/workflows/ci.yml`, new `scripts/check.py`, requirements/lock, `.vercelignore`, README and deployment documentation.

- [ ] Run executable offline test scripts in a subprocess runner with AGENT_MOCK=1 and AGENT_WORKFLOW_TEST=1; exclude test_llm and test_vercel_build_output.
- [ ] Add GitHub Actions Python 3.12 checks and Node syntax support. Bound public provider requests through existing limits.
- [ ] Pin dependencies after tests pass; verify clean upload manifest contains no env, evidence, generated resumes, venv or SDD scratch.
- [ ] Update Preview/Production AGENT_BASE_URL via Vercel CLI to the new endpoint, keeping stored key configuration.
- [ ] Deploy Preview and validate an owned Mock run, summary, DOCX, HTML/PDF UI and mobile viewport.
- [ ] Complete one synthetic real run on the new provider, validate honesty state and exports, inspect workflow logs for runtime failure.
- [ ] Complete whole-branch review and appropriate GitHub delivery; attach any created PR.
- [ ] Deploy/promote the verified revision to production, verify original domain and actual real-run result, save release evidence and paths.
