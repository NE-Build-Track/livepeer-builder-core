# ISS-04: Agree payment provisioning, authorization, and reporting contracts

## Status

Owners: John, Mike. GitHub: #10. **Decided on 12 October; contract and implementation are in this
scaffold.** Paid jobs on public orchestrators reconciled on 14 October. The correlation key is
resolved (D20, T11): the SDK reports `manifest_id` ([#77](https://github.com/livepeer/livepeer-python-gateway/pull/77))
and the session log resolves it to `payment_session_id`. Still waiting on the SDK merge (T1) and a
caller-owned reference (T13).

## Goal

Record credential and allocation mapping, permitted management operations, reporting access, units,
stable references and repeat-command semantics, each with implementation evidence or a maintainer
decision on the gap.

## Acceptance

- [x] Allocation mapping and modes; explicit provisioning: [contracts/payments.md](../../contracts/payments.md)
- [x] Least-privilege management operations
- [x] Reporting access (`/v1/usage`, allocation read); units (`Money`, USD default)
- [x] Stable references: `(allocation_id, payment_session_id)` after the session log resolves `manifest_id` (D20, T11). An ambiguous manifest is `uncertain` (T13)
- [x] Repeat-command semantics (idempotency keys; replays)
- [x] Batteries gaps delivered upstream (#5, #6, #7)
- [x] SDK [#77](https://github.com/livepeer/livepeer-python-gateway/pull/77) (formerly #71 and eliteprox#5): reports `manifest_id`, no signer-state decoding. Merge into `main` is T1
- [ ] Mike's review

## Progress

- [x] 2026-10-12: Payments service, Batteries adapter, cost sync, classification and reconciliation,
      with tests.
- [x] 2026-10-14: Paid jobs on public orchestrators with no shim; reconciliation balanced
      ([evidence](../../references/evidence/2026-10-14-dev-stack-public-orchestrators.md)).
- [x] 2026-10-16: Cost sync became the sole projection writer; cost reads now use persisted
      projections, including attempt-level classifications. The attempt list is migration `0004`.
      Revision `0003` stays the session-log migration and still drops `lpb_attempt_auth_ids`.
      Sync rebuilds an observed projection whose attempt list is empty. Recent cost reads are the
      newest stored projections. Grant list and create require `LPB_BATTERIES_OPERATOR_TOKEN`.
- [x] 2026-10-14: Session log matches on `(allocation_id, manifest_id)` and keeps `(allocation_id, payment_session_id)` as the cost key (D20).
- [x] 2026-10-16: Removed `LPB_GRANT_ID` (D28, #15). Grant funding and provisioning take the request's grant, or the caller's own allocation's grant. Cost sync reads the unfiltered usage feed. A cursor saved with the old grant filter restarts once and skips rows already stored.
- [x] 2026-10-16: The Railway Batteries image in this repository writes `grants.fund` on the builder-engine credential. Production pushes deploy that image with the signer and the engine.
- [ ] Finish the attribution re-run: forged-manifest and rejected-ticket cases
      ([m2-attribution-rerun](m2-attribution-rerun.md)).

## Decision log

- 2026-10-12: `per_actor` is the default; no auto-provisioning; USD default; rejected-ticket charges
  documented; Mode B deferred to M4 (D17–D23).
- 2026-10-16: Do not edit `0003` after #9. Persisted projection attempts ship as `0004`.
- 2026-10-16: Drop the configured grant (D28). An admin may fund any grant it names, because Batteries `grants.fund` already covers every grant and the engine cannot scope it. The usage feed is unfiltered so actors on grants created in the operator desk still reconcile.
- 2026-10-16: Production Batteries is deployed from this repository. The builder-engine allow list matches `deploy/dev-stack/config/init-secrets.py`, including `grants.fund`. `tests/structure/test_deploy.py` fails when the two lists drift.
