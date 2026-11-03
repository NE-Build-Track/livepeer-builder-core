# Capability request schemas

## Status

Owner: capability-platform integration. Candidate implementation; human review and
production deployment are pending. This extends schema discovery only; it adds no
model runtime, payment provider or generation retry.

## Goal

Let the Builder console load an operation's public request schema, declared defaults
and correct path from Core instead of assuming every offering accepts a chat payload.

## Acceptance

- [x] Read operator-installed OpenAPI 3.1 through `LPB_CAPABILITY_OPENAPI_FILE`.
- [x] Resolve bounded local references; reject recursive and remote references.
- [x] Return only profiles matching discovered model, deployment revision and path.
- [x] Require discovery authorization and never reveal runner addresses or workflows.
- [x] Preserve false/zero/nested defaults without fabricating required prompt/media.
- [ ] Complete Core/console checks, review the changes and deploy the reviewed revisions.
- [ ] Run five paid Engine API jobs with an existing funded Builder credential and reconcile costs.

## Progress

- 2026-11-02: Added capability profile contract, OpenAPI parser, server configuration
  and authenticated `GET /v1/capabilities`. Focused Linux tests pass.
- 2026-11-02: Console candidate loads profiles and replaces defaults/path when the
  offering changes. All 217 console tests pass.
- 2026-11-02: Production engine discovers all five registered model applications;
  paid submission awaits an existing `lpk_` credential location.

## Decision log

- 2026-11-02: User requested schema/default/path discovery through Builder Core.
  Use an operator-reviewed OpenAPI artifact bound to exact deployment identities;
  do not follow schema URLs supplied by untrusted discovery metadata.
- 2026-11-02: Defaults are suggestions returned to the console before paid submission.
  Core does not silently modify explicitly submitted job bodies.
