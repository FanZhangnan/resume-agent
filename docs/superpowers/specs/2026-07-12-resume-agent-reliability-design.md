# Resume Agent Reliability and Live Jobs Design

## Goal

Turn the current ReAct demo into a bounded, observable eight-stage resume pipeline. Use GPT-5.5 xhigh as the production baseline, keep GPT-5.6 variants experimental, and recommend only currently published jobs fetched from checked recruiting sources.

## Runtime Architecture

The default runtime is a harness-controlled task graph:

1. Parse resume locally.
2. Extract structured resume facts.
3. Discover verified live jobs when no JD is supplied.
4. Analyze the supplied or selected live JD.
5. Map resume evidence to requirements and calculate a deterministic score.
6. Produce fact-linked resume patches and assemble the optimized resume.
7. Verify grounding and revise failed patches once.
8. Render and save the report locally.

With a supplied JD, resume extraction and JD analysis may run concurrently. Job discovery is conditional. User questions are control events rather than numbered stages. Revision repeats stages 6 and 7 without restarting the pipeline.

## Trace Catalog

Every run has a stable `run_id` and emits versioned JSON events. Local mode writes `output/traces/<run_id>/trace.jsonl` and `summary.json`; public mode keeps trace data inside the ephemeral run directory. The same events flow through SSE.

Required events cover run, stage, LLM call, network retry, semantic JSON retry, tool, user wait, revision, report, error, and completion. Events record timing, model, reasoning, token usage, finish reason, retry category, and redacted input/output summaries. Full prompts, resumes, JDs, answers, API keys, and raw model responses are not persisted by default. Raw capture requires local-only `AGENT_DEBUG_RAW=1` and expires after 24 hours.

The Web UI adds a debug drawer and per-stage expansion. It shows a waterfall, current blocking state, retry history, model/effort, latency, tokens, tool summaries, and a trace download.

## Reliability Budgets

- Disable OpenAI SDK retries and keep one project-owned retry policy.
- Default to two attempts per logical LLM call.
- Enforce a per-call wall-clock budget and a 15-minute run budget.
- Check streaming wall-clock deadlines while consuming chunks.
- Give Web questions a countdown and automatically skip after the configured timeout.
- Generate a local partial report whenever earlier stages succeeded.
- Render the final Markdown report locally; report formatting never calls an LLM.

## Model Policy

`gpt-5.5` with `xhigh` is the production default because it is the only configuration with an observed successful quality baseline. `gpt-5.6-sol` and `gpt-5.6-terra` remain selectable but are labeled experimental. Additional 5.6 models may be enabled only through an allowlist and after benchmark results are recorded.

The selected model is not automatically used at maximum effort for every operation. Mechanical extraction and classification use the production baseline initially; later benchmark evidence may safely lower their effort. `max` is reserved for rewrite or audit experiments and is never used for local parsing or report rendering.

## Verified Job Discovery

Job facts must come from a source checked during the current run. Supported providers are:

- Greenhouse Job Board API;
- Lever Postings API;
- Ashby public job posting API;
- Adzuna search API when credentials are configured.

Company ATS boards are supplied through an allowlisted configuration. The initial no-auth list contains verified Australian employers; Adzuna provides broad location search when configured.

Every recommended posting contains source URL, source type, provider job ID, company, title, description, visible location/work mode, published or updated timestamp when available, and `checked_at`. A posting absent from the current provider response is not live. Results without a checked URL are `search_query_only` and cannot be recommended.

Before discovery, the harness requires company track, preferred or acceptable regions, excluded regions, remote preference, and relocation willingness. Excluded locations are hard rejected. Location-unclear roles rank below exact matches. When no verified role survives, the system reports the blocker and never fabricates a fallback job.

## Tool Contracts

Resume facts receive stable evidence IDs. JD requirements receive stable requirement IDs plus category, mandatory flag, weight, and threshold fields. Matching labels each requirement `met`, `under_evidenced`, or `missing` and cites evidence IDs.

The deterministic score uses the approved rubric: hard requirements 40%, skills 25%, business/industry fit 20%, soft/implied requirements 15%. Location and work authorization are gates rather than bonus points.

Rewrite output is a set of patches linked to evidence IDs. Local code applies patches to the structured resume. Verification passes only when `passed is True`, `safe_to_deliver is True`, and `required_fixes` is empty. A revision changes only failed patches.

## Verification

Offline tests cover trace redaction, retry counts, deadlines, question timeout, model policy, task graph order, provider normalization, posting freshness/location gates, deterministic scoring, patch grounding, strict verification, partial reports, and Web API contracts. Live provider smoke tests are opt-in. Live LLM benchmarks are explicit and never run in the default test suite.
