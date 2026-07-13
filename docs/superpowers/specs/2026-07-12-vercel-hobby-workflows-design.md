# Vercel Hobby Workflows Deployment Design

## Goal and Success Criteria

Deploy the resume agent on Vercel Hobby for invited testers without relying on a long-running FastAPI process. A run must expose useful progress, produce a grounded report or an explicit bounded failure, and reach an application terminal state within 780 seconds. The implementation target is 720 seconds, leaving a 60-second platform and network margin.

The public preview supports exactly these combinations:

```python
MODEL_REASONING_LEVELS = {
    "gpt-5.5": ("high", "xhigh"),
    "gpt-5.6-terra": ("high", "xhigh"),
}
```

`gpt-5.5/xhigh` remains the default. Sol, `none`, `low`, `medium`, `max`, and any unlisted model are rejected in the browser, API, and workflow. All four preview combinations are unlocked; billing and tier enforcement are outside this release.

## Platform Constraints

Vercel Hobby Functions have a 300-second maximum invocation duration, so the current subprocess plus in-memory `JOBS` design cannot host a 10-13 minute run. Vercel Workflows can run without a total duration limit, while each durable step remains subject to the Function limit. Hobby includes 50,000 workflow events and 1 GB written per month, with managed workflow data retained for one day.

The Python Workflow API is beta. This design pins `vercel==0.6.0`, targets Python 3.12, and uses only public APIs: `Workflows`, `start`, `Run`, `get_step_metadata`, and `vercel.blob`. It never imports `vercel._internal`.

## Architecture

The deployment has three explicit boundaries:

1. `webui/vercel_server.py` is the FastAPI entrypoint. It validates the invite code and model policy, parses the upload, starts a workflow, and serves status, cancellation, and cleanup endpoints.
2. `workflows/resume_workflow.py` registers top-level durable workflows and steps. Module paths and decorated function names remain stable because they form persisted workflow IDs.
3. `vercel_trace.py` stores per-stage, privacy-safe status documents in a connected Private Vercel Blob store.

`pyproject.toml` selects Python 3.12. `vercel.json` uses GA `services`: the public rewrite targets `webui/vercel_server.py`, while a private `queue/v2beta` trigger targets `workflows/vercel_worker.py` for `__wkf_*` topics. The adapter imports the durable registry before exporting the queue ASGI app. The existing `webui/server.py` remains the local development server at `127.0.0.1:7860`; shared validation and pipeline code is imported by both entrypoints.

## Request and State Flow

`POST /api/runs` accepts a PDF, DOCX, TXT, or Markdown resume up to 4 MB, an optional JD, structured job preferences, and one allowed model/effort pair. Legacy `.doc` is rejected. The API writes the file to a random `/tmp` path, extracts text, deletes the file in `finally`, and rejects empty or scanned-image-only documents with an actionable `422` response.

File paths and bytes never enter workflow state. The API starts the workflow with JSON-serializable extracted text, the JD or discovery preferences, and model settings. It returns the SDK `run_id` plus a short-lived HMAC-signed run token. Subsequent endpoints require that token, so knowing or guessing a run ID is insufficient.

`GET /api/runs/{run_id}` reconstructs the public `Run(run_id)`, calls `status()`, and reads Private Blob stage documents with caching disabled. It calls `return_value()` only after status is `completed`; calling it earlier would hold a Function open indefinitely. The response includes overall status, eight UI stages, sanitized diagnostics, and the final report when available.

`POST /api/runs/{run_id}/cancel` writes a cancellation marker. Each durable step checks it before and after external calls and returns a partial terminal report at the next boundary. The Python SDK has no public hard-cancel API, so the active LLM request may take up to its 110-second deadline before cancellation is observed.

## Eight-Stage Workflow

The UI retains the product's eight-stage language while stage 7 uses several bounded durable steps:

1. Parse and validate the uploaded resume in the start request, at most 10 seconds.
2. Extract structured facts and stable evidence IDs, at most 60 seconds.
3. Discover verified live jobs when no JD is supplied, at most 90 seconds.
4. Analyze the supplied or selected JD into stable requirement IDs, at most 45 seconds.
5. Calculate evidence-linked fit results and a deterministic bounded score, at most 60 seconds.
6. Generate fact-linked rewrite patches and assemble the resume, at most 105 seconds.
7. Audit grounding at most 90 seconds; if needed, apply only failed patches at most 75 seconds and re-audit them at most 60 seconds.
8. Render the Markdown report deterministically, at most 15 seconds.

With a supplied JD, extraction and JD analysis run concurrently through deterministic `asyncio.gather` calls. This concurrency must pass a deployed smoke test because it is supported by the SDK runtime but not documented as a Python contract. Sequential execution remains within the 720-second budget and is the configured fallback.

Every workflow receives an absolute `deadline_epoch`. A step checks remaining wall-clock time before each operation. One logical LLM call, including the project's own retry, gets at most 110 seconds and never resets its deadline for JSON repair. Expected provider failures and timeouts become structured step results instead of exceptions, preventing the SDK's automatic step retries from multiplying ordinary LLM failures. Unexpected infrastructure crashes may still trigger platform retries; every retry checks the same absolute run deadline before doing work.

## Output and Quality Gates

All LLM JSON is validated with strict Pydantic models. Strings such as `"false"`, out-of-range scores, missing fields, and invalid enums fail validation. Matching scores are calculated locally from requirement weights rather than accepted as an unexplained model number.

Rewrites cite resume evidence IDs and may not introduce unsupported employers, dates, titles, skills, metrics, or responsibilities. Delivery succeeds only when:

```python
passed is True and safe_to_deliver is True and required_fixes == []
```

If that condition still fails after one targeted repair, the run completes with a clearly labeled partial report and the unresolved items; it never labels an unsafe resume as ready to submit.

No-JD discovery recommends only postings checked during the current run from configured recruiting sources. Each result includes company, title, location, source URL, provider ID, visible publication/update time when available, and `checked_at`. Missing location constraints are rejected before the run starts. If no verified posting survives, the report states that result and may offer career directions, but it cannot present synthetic roles as currently hiring.

## Progress and Trace Catalog

Python Workflows exposes overall status and final output, but no public event, step-list, stream, or error-detail API. The application therefore writes one Private Blob JSON document per UI stage: `runs/{run_id}/stage-{number}.json`. A step overwrites only its own path with `running`, then `completed`, `partial`, or `failed`; parallel stages never share a mutable document.

Stage documents contain timestamps, duration, model, reasoning level, attempt count, token counts when available, retry category, validation status, output shape, and redacted error category. They exclude resume text, JD text, prompts, model responses, API keys, contact details, and report content. Structured `@@TRACE@@` lines with the same redacted fields go to Vercel Logs for live developer debugging.

Completed reports stay in managed workflow output for Hobby's one-day retention and are returned through the authenticated status API. `DELETE /api/runs/{run_id}` removes Blob trace documents immediately. A secret-protected daily Hobby Cron deletes traces older than 24 hours; because Hobby scheduling precision is hourly and the job runs daily, automatic deletion may occur up to roughly 48 hours after creation.

## Security and Privacy

- Store a newly rotated gateway credential only in Vercel Environment Variables. Do not expose API keys or the gateway base URL to the browser.
- Remove BYOK and arbitrary base-URL inputs from public mode. The configured gateway URL is server controlled.
- Compare the preview invite code with `hmac.compare_digest`; protect run endpoints with expiring signed tokens and a separate signing key.
- Connect a Private Blob store and keep its read-write token server-side.
- Sanitize rendered Markdown with DOMPurify, disable raw Markdown HTML, and apply a restrictive Content Security Policy.
- Limit upload types, names, size, extracted-text size, and decompression work. Never trust a model-supplied file path.
- Display consent that extracted resume data is persisted in Vercel Workflow state for up to one day. Do not write full prompts or resume content to application logs.
- Store report history in the user's browser only after explicit opt-in. Never store credentials in `localStorage`.

## UI Behavior

The model selector derives its choices from `GET /api/config`; reasoning options update when the model changes. The run view shows eight stable stage rows, elapsed time, current status, and an expandable details drawer containing only the sanitized Trace Catalog fields. Complete, partial, failed, cancelled, and deadline-exceeded states use distinct labels and actions.

The start form collects essential facts before submission, including target location, remote preference, relocation willingness, and work authorization for the no-JD path. The preview does not pause mid-run for questions. This avoids a hidden 120-second wait and avoids depending on beta Hook behavior. Desktop and mobile layouts preserve the existing Silent Swarm visual rules and must not overlap, resize, or shift as stage content changes.

## Deployment Configuration

The Vercel project requires these server-side values:

- `OPENAI_API_KEY` or the existing gateway key variable, using a rotated value;
- `AGENT_BASE_URL`, fixed by the operator;
- `AGENT_INVITE_CODE`;
- `AGENT_RUN_SIGNING_KEY`;
- `BLOB_READ_WRITE_TOKEN`, created when the Private Blob store is connected;
- `CRON_SECRET` for cleanup;
- `AGENT_DEFAULT_MODEL=gpt-5.5` and `AGENT_REASONING=xhigh`.

No Sol or `max` value may remain in Vercel environment settings. The deployment starts as a Vercel Preview deployment, runs the full acceptance matrix, and promotes the same tested commit to Production. Invite-only access remains enabled for the initial tester release to protect Hobby workflow, Function, gateway, and Blob quotas.

## Verification and Acceptance

Offline tests cover the exact model allowlist, strict schemas, fixed deadlines, retry ownership, cancellation, trace redaction, Blob path isolation, signed run tokens, file limits, live-job normalization, deterministic scoring, grounded patches, partial reports, and all API states. Workflow functions are registered with `Workflows(as_vercel_job=False)` in unit tests. Existing mock tests, Python compilation, `git diff --check`, and a credential scan must also pass under Python 3.12.

Deployment verification includes:

- `vercel build` and Preview deployment health checks;
- a deployed concurrency smoke test for extraction plus supplied-JD analysis;
- eight live runs: two models by two reasoning levels by supplied-JD/no-JD paths;
- terminal status under 780 seconds for every live run, with the target fixture producing a verifier-approved report;
- no synthetic job presented as live, and source links opening correctly;
- progress, details, cooperative cancellation, report export, and terminal-state recovery after a page reload;
- Playwright checks at desktop and mobile widths, with no console errors or overlapping controls;
- confirmation that no API key appears in browser or response data, and that resume text, JD text, and raw model output are confined to the TLS-protected start request, one-day workflow state, and authenticated final result rather than public config, Blob traces, browser credential storage, or Vercel Logs.

If any model/effort combination fails this matrix, it is not exposed to testers until it passes. Production is complete only when the deployed URL, environment configuration, Blob connection, and all four combinations meet these checks.

## Official References

- [Vercel Function limits](https://vercel.com/docs/functions/limitations)
- [Vercel Python runtime](https://vercel.com/docs/functions/runtimes/python)
- [FastAPI on Vercel](https://vercel.com/docs/frameworks/backend/fastapi)
- [Vercel Workflows with Python](https://vercel.com/docs/workflows/python)
- [Workflow pricing and limits](https://vercel.com/docs/workflows/pricing)
- [Vercel Blob](https://vercel.com/docs/vercel-blob)
- [Vercel Blob pricing](https://vercel.com/docs/vercel-blob/usage-and-pricing)
- [Cron pricing and limits](https://vercel.com/docs/cron-jobs/usage-and-pricing)
