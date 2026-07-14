# Vercel Public Quotas and Sessions Design

## Goal

Publish the repository to GitHub and operate the resume agent on Vercel Hobby
for a small public test without an access code. Restore the original public-mode
controls on the serverless path: two site-funded runs per IP per day, hourly
rate limits, global and per-IP concurrency limits, BYOK on the configured
gateway, and session-isolated run/report history.

The existing eight-stage workflow, model catalog, reasoning choices, report
format, local Web UI, and Private Blob trace format remain unchanged.

## Selected Architecture

Use Upstash Redis Free provisioned through Vercel Marketplace in `iad1`, with
`autoUpgrade=false`. Redis provides atomic counters, expiring leases, session
ownership, and history indexes across Vercel instances. Private Vercel Blob
continues to hold redacted stage traces. Vercel Workflow continues to hold the
resume/JD input and final result for its one-day retention period.

The application fails closed when Redis is unavailable: it returns `503` before
starting a workflow or spending a gateway request. Blob-only counters and
process-memory limits are explicitly rejected because they cannot enforce a
fixed cost ceiling across concurrent serverless instances.

## Identity and Session Isolation

The first HTML or API response issues an `agent_sid` cookie containing a random
session ID, expiry, and HMAC signature derived from `AGENT_RUN_SIGNING_KEY`.
Cookie attributes are `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`, and a
24-hour maximum age. Invalid or expired cookies are replaced.

Redis stores only HMAC hashes of session IDs and normalized IP addresses. Run
ownership is recorded as `run -> session_hash`; session history is a sorted set
of at most five recent run IDs with a 24-hour TTL. All status, cancel, delete,
and report endpoints verify cookie ownership. The Vercel frontend no longer
stores a bearer token in `sessionStorage`.

`GET /api/runs` returns only the current session's recent run metadata. A user
cannot enumerate or operate another session's runs even if they know a run ID.

## Atomic Admission Control

`quota_store.py` wraps Upstash Redis and exposes `acquire`, `release`,
`bind_run`, `owns_run`, `list_runs`, and `update_run`. One Lua script performs
admission atomically:

1. Remove expired members from the global active-run sorted set.
2. Reject when the per-IP active lease exists.
3. Reject when active global leases equal `AGENT_MAX_CONCURRENT` (default 3).
4. Reject when the relevant hourly counter is exhausted: real/BYOK runs use
   `AGENT_RUNS_PER_HOUR` (default 6); Mock uses `AGENT_MOCK_PER_HOUR` (default
   20).
5. For a real run without BYOK, reject when the daily counter reaches
   `AGENT_FREE_PER_DAY` (default 2) or when the site-funded global daily
   counter reaches `AGENT_SITE_FREE_PER_DAY` (default 20).
6. Increment applicable counters and create global/per-IP leases with a
   15-minute TTL.

BYOK and Mock never consume the daily site-funded quota. BYOK still consumes
the real hourly allowance and all runs consume concurrency. A validation,
upload-parse, credential-storage, or pre-binding workflow-start failure
releases the lease and refunds both applicable daily free counters. The hourly
counter is never refunded, preventing malformed requests from bypassing rate
limits. Once a workflow starts, completed, partial, failed, cancelled, and
deadline outcomes count as usage. The workflow first waits for its API-side
ownership binding, then releases its lease in explicit durable terminal
success/error branches; SDK suspension must not execute finalization. TTL is
the crash fallback.

The API returns stable error codes for `free_quota_exhausted`,
`site_quota_exhausted`, `hourly_limit`, `ip_concurrent`, `global_concurrent`,
and `quota_unavailable`, plus `free_left` where applicable. Both free-quota
errors focus the BYOK field in the frontend.

## BYOK Handling

The public form accepts only an API key. Users cannot provide or override a
Base URL; all requests use the fixed server-side `AGENT_BASE_URL` value
`https://api.wangdefou.studio/v1`. A supplied key is
encrypted with AES-GCM using a purpose-derived key from
`AGENT_RUN_SIGNING_KEY`, stored in Redis under a random credential reference,
and given the same 15-minute TTL as the admission lease.

Workflow tool steps fetch and decrypt the credential inside the serverless
step, bind it through `RunSettings`, and never return it in workflow state,
traces, logs, or reports. The credential is deleted on terminal release,
cancellation, start failure, or TTL expiry. Runs without BYOK read the site's
`OPENAI_API_KEY` from Vercel environment variables.

## Request and Workflow Flow

`POST /api/runs` performs, in order: session issuance, request size/extension
and model/input validation, IP normalization and HMAC hashing, atomic admission,
optional BYOK encryption, upload parsing, Workflow start, run/session binding,
and a best-effort stage-one trace write. Pre-binding failures after admission
release the lease, delete any temporary credential, and refund applicable daily
counters while retaining the hourly counter. Once ownership is bound, trace
degradation cannot refund or release a possibly running workflow.

The Workflow payload contains the admission ID, credential reference, session
hash, Mock flag, model, reasoning, deadline, resume text, and JD text. Each tool
uses per-run settings. A final durable step updates Redis history, releases the
lease, and deletes credentials for every application terminal state.

## Frontend Behavior

Do not require an additional access gate. Keep the current visual system and
eight stable stage rows. Add a Mock checkbox, an optional password field for
the user's API key, a quota indicator, and a compact recent-runs list. The API
key is never persisted by JavaScript. When free quota is exhausted, focus the
BYOK field and show the server message. Refresh resumes the active session from
the HttpOnly cookie and recent-run endpoint.

## Configuration and Infrastructure

Required server values are `OPENAI_API_KEY`,
`AGENT_BASE_URL=https://api.wangdefou.studio/v1`,
`AGENT_RUN_SIGNING_KEY`, `BLOB_READ_WRITE_TOKEN`, `CRON_SECRET`,
`KV_REST_API_URL`, and `KV_REST_API_TOKEN`. Public controls retain these
defaults:

```dotenv
OPENAI_API_KEY=你的得否网关密钥
AGENT_BASE_URL=https://api.wangdefou.studio/v1
```

- `AGENT_FREE_PER_DAY=2`
- `AGENT_SITE_FREE_PER_DAY=20`
- `AGENT_RUNS_PER_HOUR=6`
- `AGENT_MOCK_PER_HOUR=20`
- `AGENT_MAX_CONCURRENT=3`
- `AGENT_SESSION_TTL=86400`
- `AGENT_SESSION_REPORT_CAP=5`
- `AGENT_ADMISSION_TTL=900`

The retired access-code setting is absent from code, tests, documentation, and Vercel.
Upstash is installed with the free plan, `primaryRegion=iad1`, and
`autoUpgrade=false` so the resource cannot silently switch to paid usage.
The Redis client uses Upstash's HTTP command protocol through the existing
`httpx` dependency. Python dependencies add `cryptography` for AES-GCM and pin
it consistently in the project lock and Vercel requirements.

## Security and Privacy

- Trust Vercel's forwarded client IP only on the Vercel entrypoint, validate it
  with `ipaddress`, and store only its HMAC hash.
- Use constant-time signature checks and key separation between cookies, IP
  hashes, and BYOK encryption.
- Never expose the site gateway key, gateway Base URL, Redis credentials,
  credential references, or session hashes to the browser.
- Keep CSP nonce rendering and escape-before-Markdown behavior unchanged.
- Do not log resume/JD content, raw model responses, cookies, or user keys.
- Redis and Workflow records expire after 24 hours; encrypted BYOK material and
  active leases expire after 15 minutes.

## Testing and Acceptance

Tests use an in-memory Redis-protocol fake and exercise the real Lua contract
where practical. Required cases include daily and hourly boundaries, Mock and
BYOK exemptions, the global site-funded daily ceiling, atomic global/per-IP
leases, daily-only rollback with retained hourly usage, TTL recovery, signed
cookie rotation, cross-session denial, five-report cap, history expiry, BYOK
round trip and deletion, missing-Redis fail-closed behavior, and absence of
extra access-gate UI/API fields.

Run the full existing offline suite, clean Vercel build audit, and browser checks
at desktop and mobile widths. Preview acceptance covers anonymous free, BYOK,
Mock, quota exhaustion, concurrency denial, cancel/release, refresh/history,
cross-session denial, Trace redaction, and log leak scans. Production promotion
requires a rotated site gateway key; previously exposed keys are never reused.

## GitHub and Vercel Delivery

Commit only project changes, leaving the user's untracked handoff document
untouched. Push the existing `codex/reliability-live-jobs` branch to the linked
GitHub repository and open a draft pull request. Deploy the verified commit to
Vercel Preview, complete acceptance, then deploy the same commit to Production.
Report both URLs, the commit, the pull request, resource configuration, and any
remaining live-model test gap.
