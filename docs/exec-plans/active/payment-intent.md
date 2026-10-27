# Durable payment intent

## Status

- Owner: Mike; implementation verified, 27 October 2026. SDK published and automated review
  approved. Mike authorized builder-core commit/push; PR review and merge remain.
- Branch: `tasks/mz/payment-intent`, from `releases/m3` at `5a983a6`.
- SDK companion: `tasks/mz/payment-intent`, from published `releases/final` at `2017a80`.
- SDK PR: [#7](https://github.com/NE-Build-Track/livepeer-python-gateway/pull/7), `202721c`.
  Superseded by [builder-core #62](https://github.com/NE-Build-Track/livepeer-builder-core/pull/62)
  (D53): the engine owns the paid call, so these intent hooks need no SDK change.
- Reference rebase: `rebase/payment-intent-on-62`, this work on top of #62 (upstream SDK `2017a80`,
  decision renumbered D54). `tasks/mz/payment-intent` is unchanged.
- Publication target: `tasks/mz/payment-intent` → `releases/m3`. Remove the temporary builder
  worktree after confirming the commit is retained on the remote. No deployment is part of this change.

## Goal

Record the manifest and allocation before a signing request can incur a network charge. A lost,
cancelled or invalid signing response remains an unknown payment outcome, prevents failover and
keeps job cost pending until accounting evidence arrives. Keep upstream's prompt cancellation.

## Acceptance

- [x] The SDK awaits a payment-attempt callback before signing; a callback error prevents signing.
- [x] Structured signing errors let the adapter distinguish explicit refusal from unknown outcome.
- [x] Intent is durable before signing; a failed write prevents signing.
- [x] Cancellation, timeout and malformed replies retain intent without asserting payment was made.
- [x] Unknown signing outcomes never fail over and never read as job cost `none`.
- [x] Later usage resolves intent with the existing unique allocation/manifest matching rule.
- [x] Explicit refusals can fail over; signed evidence never regresses to unpaid.
- [x] Migration preserves historical signed sessions; SQLite and PostgreSQL checks pass.
- [x] Contracts, generated docs and regression tests are updated.
- [x] Publish the reviewed SDK feature commit; replace the old SDK git pin in `pyproject.toml` and
  `uv.lock`, update the baseline references and run a frozen install without a local override.
- [x] Mike reviewed the change and authorized publication on its feature branch.
- [ ] PR review and merge into `releases/m3`.

## Progress

- [x] 27 October: reproduced six scenarios against the old fork, current M3 pin and upstream head.
- [x] 27 October: isolated feature worktrees; relevant baselines green (SDK 324 tests; builder 51,
  with PostgreSQL tests skipped until a test database is configured).
- [x] 27 October: first SDK test seen red, then green: intent is awaited before the signer call.

- [x] 27 October: SDK full suite 335 passed; lint and wheel/sdist builds passed. Builder full suite
  975 passed, 1 skipped, with both SQLite and disposable PostgreSQL 17; 97.15% branch coverage,
  all layer floors and 100% changed-line coverage. Lint, formatting, strict types, docs checks,
  generated-doc check, build and smoke passed.
- [x] 27 October: clean-environment wheel install and smoke passed with the candidate SDK wheel
  replacing the existing pin. Builder tests used `PYTHONPATH=../livepeer-python-gateway-payment-intent/src`.
  This initial override validation is followed by the published-pin check below.
- [x] 27 October: new behaviors seen red then green: pre-sign awaiting, error-hook notification,
  prompt cancellation, durable unknown state, monotonic paid evidence, no failover after a lost
  reply, usage confirmation, refused-intent exclusion, partial pending costs, and preservation of
  a concurrent signing confirmation. Direct implementation was sufficient for each.
- [x] 27 October: cancellation during buffered/stream signing, first-byte timeout, malformed replies,
  failed intent writes, historical migration and restart recovery passed as specification checks.
- [x] 27 October: removed completed node and superseded SDK-merge worktrees. Retain the two
  implementation worktrees until reviewed changes are committed and safely retained.

- [x] 27 October: Mike authorized committing/pushing the reviewed SDK and opening its PR. Published
  `ca26f3c` on `tasks/mz/payment-intent`; PR #7 targets `releases/m3`, with old M3 ancestry
  reconciled and the tested tree unchanged. Re-ran all 335 SDK tests and lint successfully.

- [x] 27 October: pinned `pyproject.toml` and `uv.lock` to published SDK `ca26f3c`. Frozen
  sync, 44 focused tests, strict types, lint/format, generated-doc checks and smoke passed without
  `PYTHONPATH`. A newly built wheel installed and passed smoke in a fresh environment with the
  published git dependency. Updated SDK baseline references. Removed the clean SDK worktree after
  verifying the remote commit; only builder-core's uncommitted worktree remains for this task.

- [x] 27 October: SDK review identified a metered-renewal regression when a response carried valid
  new signer state but blank segment credentials. Reproduced stale-cursor reuse, then preserved valid
  state before rejecting the segment; invalid types, payment or state leave prior state intact.
  Pushed `202721c` to PR #7: 343 SDK tests, lint and builds pass. Refreshed the local builder pin;
  frozen sync, 54 focused tests and smoke pass. Automated SDK review subsequently approved the head.

- [x] 27 October: publication checks on the final immutable SDK pin, with no local override:
  976 tests passed including SQLite and PostgreSQL 16; 97.15% coverage, all layer floors and
  100% changed-line coverage. Lint, formatting, strict types, docs/generated-doc checks,
  Railway compose validation, wheel/sdist build and fresh-environment install/smoke passed.
- [x] 27 October (John): reference rebase onto #62. The SDK hooks (`on_payment_attempt`,
  `on_payment_error`) are replaced by the engine's own call, which reports `unknown` before signing
  and `unpaid` on explicit refusal; `SdkRunnerTransport` implements both payment-intent protocols
  on top of it. The SDK pin returns to upstream `2017a80`. The unknown-outcome rule is expressed in
  `attempt_verdict(..., unknown=True)` (D52's failover policy otherwise). The intent tests run on the
  loopback runner and signer. `adapters/discovery_inventory.py` is dropped: `adapters/sdk.py` is
  small again. 1141 passed, 27 skipped (PostgreSQL cases, no local server).

## Decision log

- 27 October, Mike in conversation: proceed with SDK + builder-core changes in isolated worktrees;
  SDK starts from `origin/releases/final`, builder from `origin/releases/m3`, both target M3.
- Preserve the existing success callback; add generic pre-sign and signing-error hooks. No signer
  or Batteries wire change, no real-network payment, no unbounded cancellation drain.
- Extend session evidence with `unknown` / `unpaid` / `paid`; retain `payment_sent` as confirmed
  evidence for compatibility. Historical session rows are `paid`. Intent and confirmed signing
  must remain distinct. D41's absence-of-callback assumption is superseded by this work.
- 27 October, Mike in conversation: "commit and push the sdk changes (a pr correct?)".
  Published the SDK feature branch and opened PR #7; builder-core remains uncommitted.
- 27 October, Mike in conversation: "commit and push for builder core too (branch)". Publish the
  feature branch for a PR into `releases/m3`; merge and deployment are separate actions.
- Keep D20's correlation key and ambiguity behavior. A missing usage row is never proof of no charge.
