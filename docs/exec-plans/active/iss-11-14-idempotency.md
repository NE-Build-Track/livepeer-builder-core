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
- **Progress:** slices 1 to 4 built; the consumer changes (console and the enterprise apps) to do.
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
- [x] **ISS-11:** repeating an admin command (provision, fund, rotate) through the same operation
  reference does not apply it twice; the docs separate allowance changes from signer funding and from a
  hard spending guarantee.
- [x] Every test in the guide's list is `[x]`, `[=]` or `[-]` with a reason; the `AGENTS.md` checks are
  green, and `tests/store` on PostgreSQL.

### Slices

1. [x] **Defects B1 and B2** with their regression tests, and the repeated-provision note pinned.
2. [x] **Job rules** (Part A): `OperationInProgress`, re-dispatch after a failure that never reached a
   runner, migration `0009`, the compare-and-set, the `code` field, `FakeEngine`, `engine.md`.
3. [x] **Operation ledger** (`lpb_operations`) and its store contract tests, wired into fund, fund grant
   and rotate payment key.
4. [x] **Provision, create grant, issue and rotate access key** on the ledger (B4), required references,
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

- 29 October: slice 3, test-first; every behavior test seen red before its code.
  - **Store:** migration `0010` adds `lpb_operations` (primary key: application, actor, reference).
    `store/operations.py` (`SqlOperations`, mixed into `SqlStore`) implements the new `OperationRepo`
    port: `begin_operation` returns the holder when the reference is held; `resume_operation`,
    `remember_operation`, `complete_operation` and `abandon_operation` are compare-and-sets on the
    `fencing_token`. Store contract tests in `tests/store/test_operation_ledger.py` and a migration and
    retention test, on SQLite and PostgreSQL 16.
  - **Service:** `services/ledger.py` (`OperationLedger`, built once in `runtime/assembly.py`): begin,
    conflict, replay, in progress, resume after `LPB_OPERATION_STALE_S`, abandon on a definite refusal,
    and the fenced answer of a slow run. `fund`, `fund_grant` and `rotate_key` run on it.
    `FakeAllocationManager.fail_once` injects a provider fault for the crash-between-steps tests.
  - **The slice-1 gap is closed:** a rotation whose old-key revoke timed out after the new key was
    stored left the old key live, and a repeat returned at once (`assert False is True` on the old key's
    revoked flag). The resume now reads the old key from the ledger's context and revokes it.
  - **Errors and REST:** the three operation errors carry `job` or `operation`; an admin 409 has the
    command and no job header; a replay has `Idempotent-Replayed: true` (`OPERATION_REPLAYED`).
    `http/bodies.py` holds the request bodies (a tidy: `http/app.py` was near the size cap).
  - **Existing tests changed:** the head revision `0009` became `0010` in `test_packaging.py` and
    `test_cli.py` (a new migration).

- 29 October: slice 4, test-first.
  - **Required references (Q4):** `ProvisionRequest.operation_ref` (required field), and a required
    keyword `operation_ref` on `create_grant`, `keys.issue` and `keys.rotate`; REST bodies 422 without
    one (`ProvisionBody`, `GrantBody`, `IssueKeyBody`, `RotateKeyBody`, whose body is now required).
  - **Provision and create grant** run on the ledger. A grant carries `metadata.lpb_operation`; a
    resumed create adopts it (Q5). `BatteriesClient.create_grant` sends the tag, `grant_from_item`
    parses it, `list_grants` reads every page. `GrantRecord.metadata` is new.
  - **Access keys (B4):** `KeyRepo.insert_access_key` and `rotate_access_key` take the `done` record
    and write it with the key in one transaction; `KeyRepo.get_operation` answers a repeat. Seen red:
    a second key on a repeated issue, and 400 "no active key" on a repeated rotation. A replay has
    `token: null`. `store/keys.py` (`SqlKeys`) holds the key SQL (a tidy: `store/sql.py` was near the cap).
  - **FakeEngine:** no change. It implements `ActorApi` only; it has no admin commands to mirror.
  - **Existing tests changed** (Q4 is a breaking change): every `ProvisionRequest(...)` in the tests
    gained `operation_ref=f"provision:{application}:{actor}"`; every `keys.issue`, `keys.rotate` and
    `create_grant` call a unique reference; REST provisioning, grant, key and rotation bodies a
    reference. Three tests changed meaning: `test_provisioning_repeated_after_a_lost_answer...`
    advances the clock past `LPB_OPERATION_STALE_S` before the repeat (an unknown outcome is now in
    progress until stale); `test_reprovisioning_after_revoke_is_a_new_operation` uses a new reference
    for the second provisioning (the same one replays); `test_http.py`'s 403 check and
    `test_error_mapping`'s 400 check send a reference so they still reach the check they test.

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
- **29 October (slice 3):** the ledger record has a `fencing_token` the guide's table does not list.
  A resume bumps it by a compare-and-set, and every later write checks it, so a run presumed dead that
  was only slow can neither overwrite the result nor write context. The guide asks for this fencing
  (`complete` guarded by `state = 'started'`), and the token is what makes the guard per run.
- **29 October (slice 3):** on a resume, the secret stored by an earlier run is **reused** from the
  context, not deleted as the guide's provision plan says. Deleting it races a slow original that is
  about to save the actor with that reference; reusing it is the same value (the provider replays the
  same key). A run that a resume took over deletes the copy it wrote itself.
- **29 October (slice 4):** a "management API is not configured" error is raised before the ledger
  begins (it was inside the steps, where the ledger took it for an unknown outcome and kept the record
  `started`). Nothing was sent, so no record is written.
- **29 October (slice 4):** the guide calls the grant tag "the derived idempotency key". It is
  `base64url(sha256(engine_id:create-grant:{application}:{actor}:{operation_ref}:grant))`, namespaced by
  the engine like every provider key. Batteries' `metadata` is a string field (at `ca09e47` every
  management field must be a string), so the tag travels as JSON text and the adapter parses it back;
  free-text metadata reads as `{}`. `list_grants` now follows `next_cursor` (Batteries pages at 100 by
  default), or a resume could miss its grant.
- **29 October (slice 4, Mike):** the guide's `token_replayed` would always be `false` (a token is never
  stored), so it is `replayed` instead: `true` on a replay, which has `token: null`, and `false` on the
  first answer. Embedded callers, who see no `Idempotent-Replayed` header, tell the two apart by it.
- **29 October (review):** `create_grant` reads the grant back (`GET /v1/grants/{id}`): Batteries answers
  the create with its id only, so the ledger recorded and replayed a grant with no name, status or total.
  A failed read after the create is `ProviderUnavailable`, an unknown outcome, so it never frees the
  reference; a resume adopts the grant by its tag.
- **29 October (slice 4):** the three operation errors now carry `job` *or* `operation`, so `exc.job` is
  `Job | None` (it was `Job`). No consumer reads it (checked in ChatLPT and inference-gateway).
