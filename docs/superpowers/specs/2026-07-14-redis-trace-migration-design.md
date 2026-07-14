# Redis Trace Migration Design

## Context

The Vercel frontend polls `GET /api/runs/{run_id}` every two seconds. That
endpoint currently lists every Blob under the run prefix and then downloads each
stage document with caching disabled. A nine-minute run therefore creates about
138-275 billed Blob `list()` operations and hundreds of uncached reads, in
addition to the `put()` operations used for stage transitions. This exhausts the
Hobby Blob allowance after only a small number of long runs.

The project already requires Upstash Redis for admission control, free quota,
concurrency leases, session ownership, and recent-run history. Redis is therefore
the authoritative shared service for short-lived operational state and is the
appropriate home for the 24-hour trace catalog as well.

## Goals

- Remove all Vercel Blob reads, writes, lists, and cleanup calls from the runtime.
- Preserve the existing UI, eight stage rows, trace details, cancellation,
  report delivery, history, session isolation, and deletion behavior.
- Keep trace payloads redacted and expire them automatically after 24 hours.
- Reduce status polling without making users manually refresh the page.
- Continue to fail closed when ownership, cancellation, or Redis state cannot be
  verified.

## Non-Goals

- Do not change model choices, reasoning levels, prompts, scoring, report format,
  quotas, or payment tiers.
- Do not persist resume text, JD text, prompts, model output, reports, or API keys
  in trace fields.
- Do not retain a Blob archive or add another paid storage service.
- Do not migrate expired or historical Blob trace objects into Redis.

## Redis Data Model

Trace state will share the existing per-run hash:

```text
ra:run:<run_id>
  session_hash/model/reasoning/created_at/status/safe_to_deliver
  trace:meta       -> sanitized JSON
  trace:stage:1    -> sanitized JSON
  ...
  trace:stage:8    -> sanitized JSON
  trace:cancelled  -> "1"
```

The bind operation applies `EXPIREAT(created_at + AGENT_SESSION_TTL)` to each run
hash. This is an absolute retention window measured from run creation; trace
writes must not extend it. The session history ZSET is shared by multiple runs,
so bind and list operations prune members older than the retention cutoff and
omit members whose run hash has expired. The ZSET itself may remain alive from
the newest bind. Both the API and workflow constructors read the environment
value and reject non-positive values or any value above 86,400 seconds.

Every trace write uses a Lua operation that first verifies the run hash still
exists and then updates one field. A missing hash returns `false` and is never
recreated, preventing late workflow writes or a cancel/delete race from
resurrecting an ownerless run. Parallel stages update distinct hash fields and
therefore cannot overwrite one another.

`read_stages()` performs one `HGETALL` and returns only `trace:stage:*` fields.
The sanitizer and allow-list from the current trace store remain the security
boundary; unrelated run fields are never returned through the trace API.
`QuotaStore.list_runs()` must explicitly project only `run_id`, `model`,
`reasoning`, `created_at`, `status`, and `safe_to_deliver`; trace and cancellation
fields never enter the history response.

Cancellation uses the same conditional Lua write. A missing run produces the
existing not-found response instead of recreating state. `is_cancelled()` keeps
its Boolean contract (`False` for active, `True` for cancelled), raises a
dedicated missing-run exception when the hash does not exist, and propagates
`QuotaUnavailable` for backend failure. Both exceptions make the workflow
boundary fail closed. Stage writes return `false` for a vanished run and
otherwise remain best-effort because observability loss must not duplicate or
refund paid model work.

User deletion calls only the existing ownership-checked Redis delete script; one
Lua operation removes the complete run hash (including trace fields) and its
history member together. Redis TTL replaces the Blob cleanup scan, so the
maintenance endpoint, daily Blob cleanup cron, and `BLOB_READ_WRITE_TOKEN`
runtime requirement are removed. The connected Blob store may remain in Vercel
but is unused by this application.

## Code Boundaries

- Add `run_trace_store.py` as the Redis-backed trace facade, update runtime
  imports, and remove the Blob-backed `vercel_trace.py` implementation.
- Extend `QuotaStore` with narrowly scoped trace commands rather than exposing a
  generic Redis executor.
- Keep the public `TraceStore` method contract (`write_stage`, `read_stages`,
  `write_meta`, `read_meta`, `write_cancel`, `is_cancelled`) so the workflow graph
  and API response schema do not change. HTTP deletion moves entirely to the
  ownership-checked `QuotaStore.delete_run()` path.
- The API constructs the trace facade from its configured quota store; durable
  workflow steps construct it from the same Redis environment variables and TTL
  validation rules.
- Remove Blob cleanup from `vercel.json`, the API, and deployment documentation.

No compatibility fallback will call Blob. After each environment switches to the
new deployment, release validation waits the old workflow maximum window (15
minutes from the last possible old-code start), prunes expired lease scores,
requires `ra:quota:active` to be empty, and checks recent Vercel Workflow states
for any remaining old-deployment `pending` or `running` run. Old terminal history
entries may show empty stage detail but their reports remain available from
Vercel Workflow output until normal expiry. They are not copied into Redis trace
fields.

## Adaptive Polling

Replace the fixed interval with a non-overlapping `setTimeout` schedule:

- poll immediately when a run starts or an active history item is opened;
- first 30 seconds: every 5 seconds;
- 30-150 seconds: every 10 seconds;
- after 150 seconds: every 15 seconds;
- while the page is hidden: stop scheduling;
- when visible again: poll immediately and resume the appropriate interval;
- stop permanently at a terminal state or when the active run changes.

A nine-minute run falls from roughly 138-275 Blob status reads to about 45 Redis
reads. The visible layout, controls, and stage labels remain unchanged.

## Failure Handling

- Missing or unavailable Redis during status reads returns the existing
  `status_unavailable` or quota-unavailable response; no empty success is shown.
- Cancellation write/read failures remain explicit and fail closed.
- Stage write failure is redacted and best-effort; it cannot refund quota or
  release a possibly running workflow.
- Deletion remains terminal-only and session-owned.
- TTL expiration naturally makes the run unavailable after the retention window.

## Testing

Implementation follows TDD and must prove:

- one Redis read reconstructs all eight stage rows without any Blob call;
- trace writes are sanitized and preserve the original 24-hour expiry;
- cancellation returns `False`/`True` for active/cancelled and raises for missing
  state or Redis failure;
- late stage/cancel writes cannot recreate a deleted run;
- deletion removes trace, ownership, and history atomically;
- history responses use an explicit field projection and never expose trace
  fields;
- each run hash expires at its absolute creation deadline, trace writes do not
  extend it, and shared history indexes prune expired members;
- no runtime source imports `vercel.blob`, calls `list_objects()`, or requires
  `BLOB_READ_WRITE_TOKEN`;
- adaptive polling uses the exact 5/10/15-second schedule, prevents overlap,
  pauses while hidden, resumes immediately, and ignores stale responses;
- all existing API, workflow, security, UI, and deployment-contract tests pass.

Preview acceptance runs one Mock workflow, one real `gpt-5.5/xhigh` supplied-JD
workflow, cancellation, deletion, refresh/history, and cross-session denial.
Production is promoted only after the eight-stage result and report render
without Blob access or Vercel errors.

## Rollout and Rollback

1. Deploy the Redis trace implementation to Preview with the existing Redis
   integration, complete the old-workflow drain gate described above, then run
   the acceptance matrix.
2. Confirm no application code path references the Blob SDK and that application
   testing does not issue Blob operations. Dashboard browsing is excluded because
   Vercel documents that its Blob file browser creates operations independently.
3. Deploy the same commit to Production, complete the Production drain gate, and
   repeat a Mock and real smoke test.
4. Keep the prior Vercel deployment available for code rollback, but do not roll
   back while Blob is over quota because the prior runtime depends on Blob.

References:

- [Vercel Blob usage and pricing](https://vercel.com/docs/vercel-blob/usage-and-pricing)
- [Upstash Redis pricing](https://upstash.com/pricing/redis)
