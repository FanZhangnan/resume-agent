# Defou Gateway Migration Design

## Goal

Make defou the repository's only supported OpenAI-compatible gateway and remove
all legacy gateway naming from the active project. Preserve the current UI,
model catalog, eight-stage workflow, report behavior, Git history, and physical
workspace path.

## Runtime Configuration

Use `OPENAI_API_KEY` as the only site and local gateway-key variable. Remove the
legacy key alias from runtime code, tests, documentation, and deployment
helpers. The default `AGENT_BASE_URL` becomes
`https://api.wangdefou.studio/v1`.

The root URL `https://api.wangdefou.studio` serves the defou web application,
not the OpenAI API. Configuration normalizes that exact root URL, with or
without a trailing slash, to `/v1`. The application rejects other gateway hosts
on public/BYOK paths, preserving the existing current-gateway-only policy.

## Repository Cleanup

Update tracked Python, HTML, tests, README, deployment documentation, and design
specifications so a case-insensitive scan of tracked content contains no legacy
gateway name. Update the two user-approved deployment `.command` helpers to read
and configure `OPENAI_API_KEY` and the defou `/v1` URL. Do not edit the untracked
Claude handoff document, rewrite Git history, or move the parent workspace
directory because those operations would break historical references and local
tool bindings.

## Vercel Configuration

Remove the legacy key variable from Preview and Production if it exists. Set
`AGENT_BASE_URL=https://api.wangdefou.studio/v1` in both environments. A newly
rotated defou key must be stored as sensitive `OPENAI_API_KEY`; it is never
printed, committed, logged, passed through workflow state, or persisted in the
browser. Production is not promoted until that key exists and a live
`gpt-5.5/xhigh` run passes.

## Testing and Delivery

Add failing tests for the new default URL, removal of the legacy key fallback,
root-URL normalization, and rejection of non-defou hosts. Then implement the
smallest configuration and request-path changes required to pass. Run the full
offline suite, Python compilation, Vercel build/deploy contracts, and
case-insensitive residue scans over tracked files and the two deployment
helpers.

Deploy Mock Preview first and verify all eight stages plus log redaction. After
the operator configures a rotated `OPENAI_API_KEY`, deploy the same commit to
Production and complete one real `gpt-5.5/xhigh` acceptance run. Push the branch
to GitHub; PR creation remains separate if the active GitHub CLI identity still
lacks write permission.
