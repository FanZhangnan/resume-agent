# Vercel Full Workbench UI Design

## Context

The local UI in `webui/static/index.html` is the complete product workbench,
with a two-column layout and four result views. The Vercel UI in
`webui/static/vercel_app.html` was intentionally reduced during the first public
deployment so the durable workflow, quota, session, and security boundaries
could be validated independently. That reduction is not a Vercel limitation.

The selected direction is visual parity: move the full workbench experience to
Vercel while retaining only behavior that the public durable backend can
reliably provide. The local page and its SSE/subprocess behavior remain
unchanged.

This design depends on the approved Redis trace migration in
`2026-07-14-redis-trace-migration-design.md`. Redis trace migration is deployed
first, but the existing `/api/runs` response shape remains compatible while the
new UI is introduced.

## Goals

- Reproduce the visual hierarchy, controls, responsive layout, and four-tab
  workbench from `index.html` in the Vercel application.
- Preserve supplied-JD analysis, the durable eight-stage workflow, model and
  reasoning selection, Mock mode, free quota, BYOK, cancellation, session-owned
  history, deletion, and safe report delivery.
- Make all three resume templates work from a validated structured resume rather
  than parsing the final Markdown report in the browser.
- Show real, redacted workflow progress without exposing or inventing private
  chain-of-thought, prompts, raw model output, or resume source text.
- Preserve the current CSP, Cookie ownership, rate limits, concurrency limits,
  fixed Wangdefou gateway, and transient API-key handling.

## Non-Goals

- Do not change `webui/static/index.html`, the local SSE routes, or local
  subprocess behavior.
- Do not add no-JD job discovery, live recruiting search, or interactive
  questions during a Vercel run in this release.
- Do not add raw tool logs, raw LLM logs, editable gateway URLs, payments, or new
  model choices.
- Do not persist resume contents, JD contents, reports, API keys, prompts, or
  structured resumes in Redis trace fields or history summaries.
- Do not make a partial or unverified resume appear ready for delivery.

## Workbench Surface

The Vercel page adopts the full local workbench shell: product header and status,
left input/control column, right results column, and the following tabs:

1. **Reasoning process** shows the eight real workflow stages, overall progress,
   elapsed time, retry count, validation status, correction round, and redacted
   error category. Every stage row supports both inline expansion and an
   open-drawer action; both render the same sanitized stage document. Stage copy
   describes observable work and never claims to expose hidden model reasoning.
2. **Analysis report** renders the safe Markdown report, delivery warning,
   unresolved fixes, and available match score. Markdown is rendered through the
   existing CSP-compatible safe renderer; raw HTML remains disabled.
3. **Resume layout** provides the classic, modern, and minimal templates, optional
   local photo, print/PDF, and HTML download. It renders only from the structured
   terminal result. The photo stays in browser memory and is never uploaded.
4. **Report history** lists only runs owned by the current signed Cookie session.
   Users can open any recent run, resume polling an active run, and delete a
   terminal run.

The left column retains file upload, resume text entry, required JD entry,
segmented model and reasoning controls, optional transient BYOK, Mock mode, free
quota display, start, and cancel. Copy that implies no-JD job recommendations,
editable base URLs, or live clarification is omitted. Model options and their
reasoning levels are always populated from `GET /api/config`.

Desktop uses the existing two-column composition. Tablet and mobile collapse to
one column while keeping the tab bar, fixed-format controls, and resume preview
within the viewport. Long filenames, JD text, stage errors, report content, and
resume fields wrap without overlapping adjacent controls.

## Frontend Boundaries

`vercel_app.html` owns the visual shell and uses a Vercel-specific adapter instead
of copying the local transport code. The adapter has four conceptual units:

- **API client:** calls `/api/config`, `/api/runs`,
  `/api/runs/{run_id}`, cancellation, and deletion. It converts HTTP error codes
  into stable UI events and never automatically repeats a run creation request.
- **Run state:** holds the active run identifier, current terminal/non-terminal
  status, stage rows, report, delivery fields, and structured resume. All four
  tabs render from this single state so they cannot diverge.
- **Poll controller:** uses one non-overlapping timeout, the approved 5/10/15
  second schedule, immediate refresh on visibility restoration, and stale-response
  protection when users switch runs.
- **View renderers:** update input, trace, report, layout, and history regions with
  DOM APIs and escaped text. Sanitized Markdown is the only generated HTML
  assigned to a report container; no runtime content uses inline event
  attributes or unsanitized `innerHTML`.

The page remains compatible with the nonce-bearing CSP emitted by
`webui/vercel_server.py`. It does not depend on an uncontrolled CDN. API keys are
never copied into shared run state or browser storage; the password field is
cleared immediately after `FormData` is created. Remembering a model or reasoning
preference may use non-sensitive browser storage, but credentials and gateway
configuration may not.

## API And Result Contract

Existing create, list, cancel, and delete contracts remain unchanged. A terminal
`GET /api/runs/{run_id}` response adds `optimized_resume_struct` after session
ownership has already been verified. Its value is a canonical object or `null`.
The detail response explicitly projects only `run_id`, `status`, `stages`,
`report`, `safe_to_deliver`, `unresolved_fixes`, `model`, `reasoning`, and
`optimized_resume_struct`. `GET /api/runs` explicitly projects only `run_id`,
`status`, `model`, `reasoning`, `created_at`, and `safe_to_deliver`; it never
includes the structured result or arbitrary Redis hash fields. Both owner-scoped
responses use `Cache-Control: private, no-store`.

The workflow finalizer normalizes
`state["suggestions"]["optimized_resume_struct"]`. If that structure is invalid,
it applies the existing deterministic parser to
`state["suggestions"]["optimized_resume"]` and normalizes once more. It never
reconstructs a resume by splitting the rendered Markdown report. The public
response passes the result through a strict rendering-schema projection and
drops unknown keys. The allowed shape is:

```text
basic_info: name, phone, email, location, target_role
summary: string
education[]: school, degree, major, start, end, highlights[]
experience[]: company, title, start, end, bullets[]
projects[]: name, role, start, end, bullets[]
skills[]: {group, items[]}
extras[]: string
```

The only compatibility mappings are education `bullets` to `highlights` and a
string skill to `{group: "技能", items: [value]}`. The normalized output always
uses the canonical shape above and includes every root key. It does not coerce
objects, numbers, or Boolean values into strings.

The public projection permits at most 50 education, experience, and project
records, 100 skills, 100 extras, and 100 bullets or highlights per record.
Basic and entry scalar fields are limited to 500 Unicode code points, summary
and bullet strings to 4,000 code points, and the final compact JSON encoding to
256 KiB. Unknown keys and disallowed control characters are removed. A wrong
container type, invalid leaf type, unusable structure, or oversized final object
causes the entire public structure to become `null` rather than being partially
guessed. A structure is usable only if at least one education, experience, or
project record contains semantic text. HTML-significant characters remain text
and are escaped only by the renderer. The structured result is part of the
owner-scoped Vercel Workflow result, not the Redis trace, quota hash, or history
index.

The layout tab may preview a non-null structure for terminal results, but
final-resume print and HTML export are enabled only when
`status == "completed"`, `safe_to_deliver == true`, `unresolved_fixes` is empty,
and the structure is non-null. Diagnostic Markdown export remains available for
a partial, unsafe, or structureless result.

## State And Recovery

The UI state machine is:

```text
idle -> submitting -> running -> cancelling -> terminal
terminal = completed | partial | failed | deadline_exceeded | cancelled
```

Submission disables only controls that could create a duplicate run; users can
continue switching tabs and inspecting completed stages. `run_start_uncertain`
never triggers an automatic resubmission and directs users to session history.

A transient status failure preserves the last valid state, shows a reconnecting
notice, and continues the polling backoff. It does not mark the workflow failed.
Cancellation remains `cancelling` until a terminal response arrives. On page
load, the history request identifies session-owned active runs; the newest active
run is restored and polled. The server remains the authority, so an identifier in
the URL or browser storage never grants access without the signed Cookie.

Opening a different history item invalidates pending responses for the previous
run. Terminal status stops polling. Hidden pages schedule no polls; becoming
visible causes one immediate request and resumes the age-appropriate interval.

## Error And Delivery Behavior

Stable error codes map to short Chinese messages and an appropriate next action:

- quota or concurrency denial explains the applicable limit;
- missing gateway configuration directs the user to the operator;
- invalid files, JD, model, or reasoning return focus to the relevant control;
- unavailable status keeps the last valid progress and offers manual retry;
- not-found or ownership denial clears the active view without revealing whether
  another session owns the identifier;
- partial, failed verification, or unsafe delivery shows unresolved fixes and a
  prominent non-delivery warning;
- deadline and cancellation retain completed-stage evidence and any diagnostic
  report returned by the backend.

Stage details may contain status, duration, attempt count, validation status,
correction round, safe-to-deliver flag, and allow-listed error categories. They
must not contain prompt text, API keys, full model responses, resume/JD excerpts,
or arbitrary exception text.

## Security And Privacy

- The existing signed, HttpOnly, SameSite Cookie remains the only run ownership
  authority. All read, cancel, and delete operations fail closed when ownership
  cannot be verified.
- BYOK remains encrypted only for the short workflow credential lease, is never
  returned by an API, and is removed according to the existing lifecycle.
- The Wangdefou base URL is server configuration and is not user-editable.
- CSP keeps the current self-only default, nonce/self script restrictions,
  blocked object embedding, and restricted connections. Resume preview uses a
  sandboxed iframe and escaped template values.
- Markdown raw HTML, event handlers, script URLs, and unsafe links are rejected.
- Redis trace and history keep their approved redacted field allow-lists and
  24-hour retention. The UI migration adds no Blob operation.

## Testing And Acceptance

Implementation follows TDD and covers:

- API contract tests for the structured-result projection, size limits, null
  invalid structures, owner-only access, explicit metadata-only history,
  private/no-store caching, and export gating for unsafe results;
- DOM/JavaScript tests for state transitions, non-overlapping adaptive polling,
  hidden-page pause, stale-response rejection, refresh recovery, terminal stop,
  and no duplicate create retry;
- Mock workflows for all eight stages, success, partial, failure, deadline,
  cancellation, history restoration, and terminal deletion;
- quota tests for daily free usage, BYOK, per-hour real/Mock limits, per-IP
  concurrency, global concurrency, and failure rollback;
- security tests with hostile Markdown and structured fields, no API-key browser
  persistence, enforced CSP, fixed gateway, and cross-session denial;
- Analytics and Speed Insights load from Vercel's own first-party endpoints
  without weakening CSP or blocking the core workbench when telemetry fails;
- template tests for all three layouts, local photo toggle, safe print/PDF, HTML
  download, and disabled final-resume exports for non-deliverable results;
- static checks proving no application runtime path calls Blob and that the
  local `index.html` remains unchanged;
- Playwright screenshots and interaction tests at desktop, tablet, and mobile
  sizes, including long-content overflow checks and comparison against the local
  workbench's visual hierarchy.

Preview acceptance runs one complete Mock workflow plus one real supplied-JD
`gpt-5.5/xhigh` workflow. It also checks cancellation, deletion, refresh/history,
cross-session denial, template export, browser console errors, network failures,
and Vercel logs. Production is promoted only from the accepted Preview commit;
the same Mock and a focused real smoke test run after promotion.

## Rollout

1. Implement and deploy the approved Redis trace migration, including its
   old-workflow drain gate and zero-Blob verification.
2. Add the structured terminal result and API projection behind the unchanged
   owner check; deploy and validate the contract in Preview.
3. Replace the Vercel presentation shell and connect the polling adapter while
   leaving `index.html` untouched.
4. Run the automated, visual, security, and live Preview acceptance matrix.
5. Promote the exact accepted commit to Production and repeat the production
   smoke checks.

Rollback uses the prior Vercel deployment only if it is compatible with the
already-migrated Redis trace contract. The old Blob-dependent deployment must not
be restored while Blob is over quota. The local Web UI remains an independent
fallback throughout the rollout.
