# ISS-04: Agree payment provisioning, authorization, and reporting contracts

## Status

Owners: John, Mike. GitHub: #10. **Decided on 12 October; contract and implementation are in this
scaffold.** Waiting on the SDK #71 upstream position.

## Goal

Record credential and allocation mapping, permitted management operations, reporting access, units,
stable references and repeat-command semantics, each with implementation evidence or a maintainer
decision on the gap.

## Acceptance

- [x] Allocation mapping and modes; explicit provisioning: [contracts/payments.md](../../contracts/payments.md)
- [x] Least-privilege management operations
- [x] Reporting access (`/v1/usage`, allocation read); units (`Money`, USD default)
- [x] Stable references: `(allocation_id, payment_session_id)`; `manifest_id` is a label
- [x] Repeat-command semantics (idempotency keys; replays)
- [x] Batteries gaps delivered upstream (#5, #6, #7)
- [ ] SDK #71: upstream maintainers' position recorded on the PR (John asks; Josh helps if it stalls)
- [ ] Mike's review

## Progress

- [x] 2026-10-12: Payments service, Batteries adapter, cost sync, classification and reconciliation,
      with tests.
- [ ] Re-run the attribution evidence on the new baseline (see the evidence plan).

## Decision log

- 2026-10-12: `per_actor` is the default; no auto-provisioning; USD default; rejected-ticket charges
  documented; Mode B deferred to M4 (D17–D23).
