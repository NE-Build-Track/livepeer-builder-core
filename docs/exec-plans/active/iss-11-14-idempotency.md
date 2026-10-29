# ISS-11 + ISS-14: repeat-safe commands

## Status

- **Owner:** John (engine), per the guide; built on Mike's branch. **Reviewers:** Mike, John. Test-first in four slices, one pull
  request each, reviewed before the next.
- **Tickets:** ISS-11 ([Network-Engineering-SPE#24](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/24))
  and ISS-14 ([#27](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/27)) (M3). Replaces the
  interim rules of D11 and closes T2.
- **Branch:** `tasks/mz/iss-11-14-idempotency`, off `releases/m3` at `d3df806`.
- **Design:** the idempotency guide, `docs/design-docs/m3-guides/iss-11-14-idempotency.md` (29 October;
  not on this branch yet). Open questions Q1–Q5 locked by Mike on 29 October.
- **Progress:** slice 1 built; slices 2–4 to do.
- **Decision:** D57.

## Goal

A repeated command never applies twice. Every command that can move money, create a credential or run
paid work is identified by a caller-chosen `operation_ref`, stored with the effect under a unique
constraint, and a repeat is answered from that record. Jobs keep their record on `lpb_jobs`, with final
rules for a repeat; admin commands get an operation ledger.

## Acceptance

From the tickets and the 21 October M3 plan:

- [ ] **ISS-14:** the final rules for repeated job submissions replace the interim 409, documented in
  [engine.md](../../contracts/engine.md#idempotency).
- [ ] **ISS-11:** repeating an admin command (provision, fund, rotate) through the same operation
  reference does not apply it twice; the docs separate allowance changes from signer funding and from a
  hard spending guarantee.
- [ ] Every test in the guide's list is `[x]`, `[=]` or `[-]` with a reason; the `AGENTS.md` checks are
  green, and `tests/store` on PostgreSQL.

### Slices

1. [x] **Defects B1 and B2** with their regression tests, and the repeated-provision note pinned.
2. [ ] **Job rules** (Part A): `OperationInProgress`, re-dispatch after a failure that never reached a
   runner, migration `0009`, the compare-and-set, the `code` field, `FakeEngine`, `engine.md`.
3. [ ] **Operation ledger** (`lpb_operations`) and its store contract tests, wired into fund, fund grant
   and rotate payment key.
4. [ ] **Provision, create grant, issue and rotate access key** on the ledger (B4), required references,
   `payments.md`; then the console change.

## Progress

- 29 October: slice 1, test-first; each regression test seen red on `d3df806` before the fix.
  - **B1:** rotating twice with one reference revoked the live key (`assert True is False` on its
    revoked flag). The repeat now returns the actor when it already holds the replayed key.
  - **B2:** a duplicate with another body raised `OperationExists` from the unique index; it is
    `OperationConflict` now, on SQLite and PostgreSQL 16 (red on both without the fix).
  - **Provision note:** the first call dies after the provider created the allocation; the repeat
    replays it. Passed at once (kept as documentation); a mutation that makes the provider key unique
    per call turns it red with a second allocation.

## Decision log

- **29 October (Mike):** Q1 a repeat of a job that ran stays 409 `OperationExists`, now final, with a
  stable `code`. Q2 `unreachable` and unpaid `timeout` are not re-dispatched. Q3
  `LPB_OPERATION_STALE_S` defaults to 120 s. Q4 references become required on provision, create grant
  and issue/rotate access key. Q5 engine-created grants are tagged with `metadata.lpb_operation`.
  Recorded as D57.
- **29 October:** B1 is fixed in slice 1 without the ledger: a repeat that gets back the key the actor
  already holds returns the actor. Slice 3 moves rotation onto the ledger, which also covers a crash
  between the steps.
