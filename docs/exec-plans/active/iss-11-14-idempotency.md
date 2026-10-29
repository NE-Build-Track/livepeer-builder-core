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
- **Progress:** slices 1 and 2 built; slices 3 and 4 to do.
- **Decision:** D57.

## Goal

A repeated command never applies twice. Every command that can move money, create a credential or run
paid work is identified by a caller-chosen `operation_ref`, stored with the effect under a unique
constraint, and a repeat is answered from that record. Jobs keep their record on `lpb_jobs`, with final
rules for a repeat; admin commands get an operation ledger.

## Acceptance

From the tickets and the 21 October M3 plan:

- [x] **ISS-14:** the final rules for repeated job submissions replace the interim 409, documented in
  [engine.md](../../contracts/engine.md#idempotency).
- [ ] **ISS-11:** repeating an admin command (provision, fund, rotate) through the same operation
  reference does not apply it twice; the docs separate allowance changes from signer funding and from a
  hard spending guarantee.
- [ ] Every test in the guide's list is `[x]`, `[=]` or `[-]` with a reason; the `AGENTS.md` checks are
  green, and `tests/store` on PostgreSQL.

### Slices

1. [x] **Defects B1 and B2** with their regression tests, and the repeated-provision note pinned.
2. [x] **Job rules** (Part A): `OperationInProgress`, re-dispatch after a failure that never reached a
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

- 29 October: slice 2, test-first.
  - **Rules:** `services/operations.py` holds them as pure functions. `never_reached_a_runner` (the
    closed list) was triangulated over 11 bearings, and each clause was checked by mutation. `retry_of`
    answers a repeat: conflict, in progress, exists, or the id of the failed job to supersede.
  - **Store:** migration `0009` (`retry_of`, `superseded_by`, the index rebuilt with
    `superseded_by IS NULL`, and a downgrade that frees superseded references). In `create_job`, the
    compare-and-set and the insert share one transaction (`store/operation_refs.py`), and
    `job_by_operation_ref` returns only the holder. Store tests cover the supersede, two concurrent
    retries, a retry of a job that did not fail, the migration round trip and retention, on SQLite and
    PostgreSQL 16.
  - **Service:** `JobService` takes the reference through `check_operation` before admission and
    `claim_operation` when it writes the job. A lost race is answered from the holder.
  - **Surfaces:** `OperationInProgress(OperationExists)`; a `code` on every 409 body; `Retry-After: 1`
    while the job runs. Streaming follows the same rules; `FakeEngine` mirrors every row, checked by
    the shared contract test.
  - **Docs:** `engine.md#idempotency` is the final table; D11 points to D57; T2 is closed;
    `extending-the-engine.md` states the store's new duty.
  - **Existing tests changed:** the head revision `0008` became `0009` in `test_packaging.py` and
    `test_cli.py` (a new migration), and the contract test's scripted runner error now repeats once,
    so a re-dispatch has a step to take.
  - **Not pinned by a test:** the compare-and-set's own `superseded_by IS NULL` guard and its row
    count. The unique index gives the same answer through the conflict path, so mutating either
    changes no outcome. They stay as the guide designs them, and they keep the old job's
    `superseded_by` from being overwritten before the rollback.

## Decision log

- **29 October (Mike):** Q1 a repeat of a job that ran stays 409 `OperationExists`, now final, with a
  stable `code`. Q2 `unreachable` and unpaid `timeout` are not re-dispatched. Q3
  `LPB_OPERATION_STALE_S` defaults to 120 s. Q4 references become required on provision, create grant
  and issue/rotate access key. Q5 engine-created grants are tagged with `metadata.lpb_operation`.
  Recorded as D57.
- **29 October:** B1 is fixed in slice 1 without the ledger: a repeat that gets back the key the actor
  already holds returns the actor. Slice 3 moves rotation onto the ledger, which also covers a crash
  between the steps.
- **29 October (confirmed by Mike):** the closed list names the signer refusal `payment`. An unpaid
  `payment_refused` (the signer refused this orchestrator: 400, 401 or 481, an expired challenge) is
  also a signer refusal with nothing sent, and its job fails with the failure kind `payment`. So it is
  on the list too. Without it, a job every orchestrator refused this way would keep its reference,
  while one 402 would free it.
