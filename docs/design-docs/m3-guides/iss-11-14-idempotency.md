# ISS-11 + ISS-14: Idempotency for job submission and admin commands

**Status:** proposal for discussion, 29 October 2026. Nothing here is decided until it is locked; locked
rules become a decision in [m3-decisions.md](../m3-decisions.md) (numbered then).\
**Trackers:** ISS-11 ([Network-Engineering-SPE#24](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/24)),
ISS-14 ([#27](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/27)). Replaces the interim rules
of D11 and closes T2.\
**Owner:** John (engine). **Reviewers:** Mike, John.\
**Verified against:** builder-core `releases/m3` at `d3df806`; Clearinghouse Batteries `ca09e47` (the pin).

## Done when (from the 21 October M3 plan)

- **ISS-14:** the final rules for repeated submissions replace the interim 409, documented in the engine
  contract.
- **ISS-11:** repeating an admin command (provision, fund, rotate) through the same operation reference
  does not apply it twice; the docs separate allowance changes from signer funding and from a hard
  spending guarantee.

## Summary of the proposal

1. **One rule for both:** every command that can move money, create a credential or run paid work is
   identified by a caller-chosen `operation_ref`, scoped to the caller `(application_id, actor_id)`. The
   engine stores the reference **with the effect, under a unique constraint**, before the effect starts.
   A repeat is answered from that record. A provider idempotency key alone is not enough, because some
   commands span several steps and some providers or steps have no idempotency of their own.
2. **Jobs keep their record on `lpb_jobs`.** The 409 for a repeat of a job that already ran becomes the
   final rule, because immediate outputs are not retained and so cannot be replayed. Two cases change:
   a repeat while the job is still running gets `OperationInProgress`, and a repeat after a failure that
   **provably never reached a runner** dispatches again under the same reference.
3. **Admin commands get an operation ledger** (`lpb_operations`). It records `started` before the first
   provider call and `done` with the public result. A repeat replays that result; a stale `started`
   resumes the command, whose steps are each safe to repeat.
4. **Fix four defects found while verifying** (below), each test-first.

## Current state (verified)

### Entry points

| Command | Path | Reference today | Guard today |
| --- | --- | --- | --- |
| Run job, stream job | `POST /v1/jobs`, `JobsService.run/_stream` | optional `operation_ref` | Unique partial index `lpb_jobs_operation_ref` on `(application_id, actor_id, operation_ref)`; fingerprint check in `_check_operation` |
| Provision actor | `POST /v1/admin/applications/{app}/actors/{actor}/allocation` | none | Returns the stored actor if it exists; Batteries key derived from `provision:{app}:{actor}` |
| Fund allocation | `.../fund` | required (`FundRequest`) | Batteries `Idempotency-Key` from `fund:{allocation}:{ref}` |
| Fund grant | `POST /v1/admin/grant/fund` | required | Batteries key from `fund-grant:{grant}:{ref}` |
| Create grant | `POST /v1/admin/grants` (GrantDesk) | none | **None.** Batteries has no idempotency on `POST /v1/grants` |
| Rotate payment key | `.../rotate-key` | required (`RotateBody`) | Batteries key from `rotate:{allocation}:{ref}`; the engine steps after it are not repeat-safe (B1) |
| Issue access key | `POST /v1/admin/keys` (KeyService) | none | **None.** A repeat issues a second live key |
| Rotate access key | `POST /v1/admin/keys/{id}/rotate` | none | Atomic in the engine; a repeat after a lost response answers 400 "no active key" |
| Set status, revoke allocation, revoke key | various | none | Set-to-state or repeat-safe upstream |

Batteries (`internal/store/idempotency.go` at `ca09e47`): keys are scoped per grant, never expire, and
replay the original status and body (including a key secret). A different method, route, target or body
under the same key is `409`. Lookup, mutation and the stored response share one transaction.

### Defects

| # | Defect | Evidence |
| --- | --- | --- |
| **B1** | **Repeating `rotate_key` with the same `operation_ref` revokes the live key.** The second call gets the same new key back from the provider's replay, then calls `revoke_key(actor.api_key_id)`, and the stored actor's key id *is* the new key. | Reproduced 29 Oct with `FakeAllocationManager` (it mimics Batteries replay): same key id, and the live key is revoked. `services/payments.py` `rotate_key` |
| **B2** | **A concurrent duplicate with a different body is reported as `OperationExists`, not `OperationConflict`.** `SqlStore.create_job` catches the unique violation and raises `OperationExists` without comparing fingerprints. | `store/sql.py` `create_job` |
| **B3** | **A 503 with `Retry-After` burns the reference.** A job that failed before dispatch (`no_capacity`, all runners `refused`) invites a retry, but a retry with the same `operation_ref` gets 409 forever. | `engine.md` failure table; `_check_operation` |
| **B4** | **Create grant and issue access key have no idempotency.** A lost response and a repeat create a second grant or a second live credential. | Batteries `API.md` (idempotency covers allocations, keys and funding only); `services/keys.py` `issue` |

The 21 October console note (a repeated provision created a second allocation) was at pin `ad6bba9`.
Today's provision derives its Batteries key from the actor, so a repeat should replay. Pin it with a
regression test; if the test passes at once, keep it as documentation.

## Principles applied

- **Every remote mutation carries a client-generated request ID, stored with the effect under a unique
  constraint** (Kleppmann, *DDIA*, the end-to-end argument). The ID is checked where the effect lands:
  the engine's own table, and the provider's `Idempotency-Key`.
- **Every remote call has three outcomes:** success, failure, unknown. A command whose provider call
  timed out is *unknown*, not failed. It is resumed under the same key, never retried under a new one.
- **Multi-step commands are resumable, not transactional.** Each step is either idempotent itself or
  guarded by a state check, so replaying the whole command from the top is always safe.
- **Secrets never ride on records** (core belief 8). The ledger stores the public result only. A replay
  of a command whose first answer carried a token says so, and does not repeat the token.

## Part A: job submission (ISS-14)

### Final rules

Scope `(application_id, actor_id, operation_ref)`. Fingerprint as today: `app`, `path`, `method`,
`payload`, `capability`, `model`. Limits (`timeout_s`, `max_attempts`) may differ between repeats.

| The reference names a job that… | Answer | Dispatch? |
| --- | --- | --- |
| does not exist | Run it | yes |
| exists with a different fingerprint | 409 `OperationConflict` | no |
| is `running` | **409 `OperationInProgress`**, `Retry-After: 1`, the job in the body | no |
| `succeeded`, or ended `uncertain` | 409 `OperationExists`, the job in the body (final rule, unchanged) | no |
| `failed`, and **every attempt ended before a runner accepted the work** | **Run a new job** under the same reference; the new job has `retry_of` = the old id | yes |
| `failed` in any other way | 409 `OperationExists` | no |
| no reference given | Run it | yes |

**"Before a runner accepted the work"** is a closed list. The job's failure is one of `no_offering`,
`no_capacity`, `not_provisioned`, and every attempt's outcome is `refused` (the orchestrator answered
404, 409 or 503 before the app ran) or `payment` (the signer refused; nothing was sent). No attempt has
`payment_sent`. `unreachable` and unpaid `timeout` are **excluded**: `network_kind` maps a disconnect after
the request was sent to `unreachable`, and a free runner may have run the work (open question Q2).

**Why the 409 stays for a job that ran.** Immediate outputs are returned inline and not retained
(`engine.md#results`), so the engine cannot replay the original response. A 2xx with a different body
than the first answer would break clients that parse the first. ChatLPT, inference-gateway and the
enterprise desk already handle `OperationExists` by reading the job. The rule becomes final, documented,
with a stable `code` so clients can branch without parsing the message.

**Lifetime.** A reference lives as long as its job record. `LPB_RETENTION_JOBS_DAYS` purges `succeeded`
and `failed` jobs, which frees their references; `uncertain` jobs are kept. Document that a reference
older than the retention window may dispatch again, and recommend UUIDs or ULIDs.

### Storage

- Migration (next number): add `lpb_jobs.retry_of` (nullable job id) and `lpb_jobs.superseded_by`
  (nullable job id). Rebuild `lpb_jobs_operation_ref` as unique **where `operation_ref IS NOT NULL AND
  superseded_by IS NULL`** (both SQLite and PostgreSQL support the partial predicate). The old job keeps
  its reference for history.
- **Re-dispatch is one transaction**, a compare-and-set:
  `UPDATE lpb_jobs SET superseded_by = :new WHERE id = :old AND superseded_by IS NULL AND state = 'failed'`,
  then insert the new job. A row count of 0 means another retry won: re-read and answer from the job now
  holding the reference (normally `OperationInProgress`).
- `create_job` on a unique violation re-reads the holder and **compares fingerprints** (fixes B2).
- The retryable-failure test is a pure function of the job and its attempts
  (`services/request_identity.py` or a new `services/operations.py`), so it is unit-tested without I/O.

### Surfaces

- **Errors** (`errors.py`): add `OperationInProgress(OperationExists)`. It subclasses `OperationExists`,
  so consumers that catch `OperationExists` keep working. Keep `OperationConflict`.
- **HTTP** (`http/app.py`): all three stay 409 with the job id header. Add a `code` field to the problem
  body: `operation_exists`, `operation_in_progress`, `operation_conflict`. `OperationInProgress` adds
  `Retry-After: 1`.
- **Facade** (`api.py`): unchanged signatures. A retryable re-dispatch is transparent: the caller gets
  the new job's result.
- **Streaming:** the same table. A replay never re-streams.
- **FakeEngine** (`testing/fake_engine.py`) mirrors every row of the table; `test_fake_engine_contract.py`
  proves parity.
- **Docs:** rewrite `engine.md#idempotency` as the final table (drop "interim"); the D11 row in
  `m2-decisions.md` points to the new decision; close T2.

## Part B: admin commands (ISS-11)

### Which commands take a reference

| Command | Reference | Why |
| --- | --- | --- |
| Provision, fund allocation, fund grant, create grant, rotate payment key | **required** | Moves money or creates a provider credential |
| Issue access key, rotate access key | **required** | Creates a live engine credential |
| Set status, revoke allocation, revoke access key | not accepted | Set-to-state or repeat-safe; a repeat cannot apply twice |

A required reference is a 422 when missing, at REST and in the facade's request contracts. This breaks
callers that do not send one today: the console sends one only where accepted, and the facade's
`provision`, `issue` and `create_grant` gain a field (open question Q4).

### The operation ledger

New table `lpb_operations`:

| Column | Notes |
| --- | --- |
| `application_id`, `actor_id`, `operation_ref` | The caller's scope; **unique together** |
| `command` | `provision`, `fund`, `fund_grant`, `create_grant`, `rotate_payment_key`, `issue_key`, `rotate_access_key` |
| `fingerprint` | sha256 of the command, its target and the validated request, `operation_ref` excluded |
| `state` | `started` → `done` |
| `context` | JSON the command needs to resume (for rotation: the previous key id and secret ref). Never a secret |
| `result` | JSON of the public result. Never a token |
| `started_at`, `completed_at` | From the injected `Clock` |

Flow, in a new `OperationLedger` service over a new `OperationRepo` port:

1. **Begin.** Insert `started`. On a unique violation, read the row:
   - different fingerprint → `OperationConflict` (409), before any provider call;
   - `done` → **replay**: the stored result with the original status and `Idempotent-Replayed: true`;
   - `started`, younger than `LPB_OPERATION_STALE_S` → `OperationInProgress` (409, `Retry-After`);
   - `started`, stale → **resume**: run the command again from the top.
2. **Run** the command's steps. Provider calls keep their derived `Idempotency-Key`s, so a resume replays
   them at the provider.
3. **Complete.** Set `done` and the result, guarded by `state = 'started'`.

A command that fails with a definite refusal (validation, 4xx from the provider, `NotFound`) deletes its
`started` row, so the caller can fix the input and repeat. A command that ends *unknown* (timeout,
transport error, crash) leaves `started` for a resume.

**Keep the existing Batteries key derivations** (`fund:{allocation}:{ref}`, `fund-grant:{grant}:{ref}`,
`rotate:{allocation}:{ref}`, `provision:{app}:{actor}`), so a retry that spans the deploy still replays.

### Per-command step plans

- **Provision:** begin → (existing non-revoked actor? record and replay it) → resolve grant →
  `create_key_for_grant` (actor-derived key, so two references for one actor still meet at Batteries) →
  `secrets.put` → `save_actor` → complete. A resume after `secrets.put` writes a second secret ref; the
  plan deletes the orphan ref on resume (store the ref in `context` before `save_actor`).
- **Fund, fund grant:** begin → provider fund (keyed) → status update → complete. Mostly a wrapper; it
  adds conflict detection before money moves.
- **Create grant:** Batteries has no `Idempotency-Key` on `POST /v1/grants`, but it stores and lists a
  grant's `metadata` (`internal/store/manage.go` at `ca09e47`). The engine tags each grant it creates with
  `metadata = {"lpb_operation": <the derived idempotency key>}`. Begin → on a resume only, `list_grants`
  and adopt the grant whose metadata carries this key → otherwise `create_grant` with the tag → complete.
  The tag is the correlation key, so grant names stay free-form. The adapter's `grant_from_item` parses
  `metadata`; `create_grant` sends it.
- **Rotate payment key (fixes B1):** begin, storing `previous_api_key_id` and `previous_secret_ref` in
  `context` → `create_key` (keyed) → `secrets.put` → `save_actor` → **revoke the previous key only if it
  differs from the new key id** → delete the previous secret ref → complete. Every step reads its target
  from `context`, never from the actor as it is now.
- **Issue access key, rotate access key:** the key write and the ledger's `done` happen **in the same
  database transaction** (one `SqlStore` method that takes both, since two port calls cannot share a
  transaction), so there is no unknown outcome. A replay returns the key record with
  `token: null` and `token_replayed: false`; the admin who lost the token rotates the key.

### Docs (ISS-11's second criterion)

Add a section to `payments.md`, **"Allowance, signer funding and spending guarantees"**, with three
terms side by side:

- **Allowance change:** an admin command moves value between a grant and an allocation in Batteries. It
  is bookkeeping in the payment operator's ledger, and idempotent per `operation_ref`.
- **Signer funding:** the payer's TicketBroker deposit and reserve on Arbitrum, funded by the operator
  outside the engine. No engine command touches it.
- **Spending guarantee:** none in M3. The allowance is a cap that can overshoot (existing text); hard
  limits are stretch scope.

Also document in `payments.md` which admin commands take a reference, the replay and in-progress
answers, and the ledger's lifetime (kept, no purge in M3).

## Upstream dependencies

**None.** Every change is in builder-core. Checked 29 October 2026:

| Upstream | Needed? | Why |
| --- | --- | --- |
| livepeer/node | No | Orchestrators and the signer never see an `operation_ref`. Re-dispatch decisions use the engine's own attempt records |
| livepeer-python-gateway | No | Job idempotency sits above the runner call, which the engine already owns (D53, `adapters/runner_call.py`). Q2's split of `unreachable` would change the engine's `network_kind`, not the SDK |
| clearinghouse-batteries | No | Its `Idempotency-Key` already covers allocation and key creation and funding, scoped per grant, never expiring, replaying the original body. `revoke` on an allocation or API key is repeat-safe (`SetStatus`, `coalesce(revoked_at_ms, …)`), so a resumed rotation can revoke again. The one gap, no key on `POST /v1/grants`, is closed engine-side with the metadata tag above |

Batteries is a separate Go service, so its behavior cannot be vendored the way John copied SDK
functions into the engine (D53, #61); a fork is also refused (`pins.test.ts`, 28 October). Idempotency
on grant creation would be an upstream nicety, not a dependency.

## Architecture and dependency injection

Follow the repository's own DI standard ([di-review-2026-10-23.md](../di-review-2026-10-23.md),
[extending-the-engine.md](../extending-the-engine.md)) and `tests/structure/`:

| Layer | Change |
| --- | --- |
| `contracts/` | `operation_ref` on `ProvisionRequest`, grant and key request contracts; `OperationRecord`; `IssuedKey.token` becomes optional for replays |
| `errors.py` | `OperationInProgress` |
| `protocols/` | `OperationRepo` port (`begin`, `complete`, `abandon`, `get`); `EngineStore` includes it |
| `store/` | `lpb_operations` table and migration; `lpb_jobs` columns and index rebuild; `SqlStore` implements `OperationRepo`; the job re-dispatch CAS |
| `services/` | `OperationLedger` (pure orchestration over the port and `Clock`); `PaymentsService`, `GrantDesk`, `KeyService` take it by constructor injection; the retryable-failure function |
| `runtime/` (composition) | Wire the ledger once; no service builds its own |
| `http/` | Bodies gain `operation_ref`; `code` in 409 problems; `Retry-After`; `Idempotent-Replayed` |
| `testing/` | `FakeEngine` parity; `FakeAllocationManager` gains a hook to fail one call (fault injection) |

The ledger is an internal service, not a new extension point: enterprises replace the store, not the
ledger. Do not add a protocol for the ledger itself.

## Test plan (test-driven)

**Levels.** Most behavior is tested in-process through the services, with the real `SqlStore` on
in-memory SQLite and `FakeAllocationManager` (the existing classicist style). Uniqueness and the
compare-and-set are tested as **store contract tests on SQLite and PostgreSQL** (`tests/store/`, PostgreSQL via
`LPB_TEST_POSTGRES_URL`). HTTP mapping is tested once per answer in `test_http_routes.py`. `FakeEngine` parity is
tested in `test_fake_engine_contract.py`.

**Seams.** `Clock` (`FixedClock`) for staleness; `FakeAllocationManager` with a one-shot failure hook for
crash-between-steps; an `asyncio.Event`-gated transport to hold a job `running`. No mocks of value
objects; no new seam for the ledger itself.

**Starter test:** the B1 regression: rotate the payment key twice with the same reference, and assert
the live key is still active and the provider minted one key.

**Ordered test list:**

```text
Defects first (each must be seen red on releases/m3 before the fix)
[ ] B1 repeated rotate with the same ref leaves the new key active, one key minted
[ ] B2 concurrent duplicate with a different body is OperationConflict (store, both backends)
[ ] B3 repeat after no_capacity runs a new job under the same ref; old.superseded_by == new.id
[ ] B4 issue access key twice with the same ref yields one key; the replay has no token

Jobs
[ ] repeat of a succeeded job is OperationExists; the transport is not called again
[ ] repeat while running is OperationInProgress; one dispatch in total
[ ] repeat after a signer refusal (payment, unpaid) re-dispatches
[ ] repeat after any attempt with payment_sent is OperationExists
[ ] repeat after uncertain is OperationExists
[ ] repeat after unreachable is OperationExists            [?] Q2
[ ] different body after a retryable failure is OperationConflict, no re-dispatch
[ ] two concurrent retries of one failed job: exactly one new job (store, both backends)
[ ] streaming repeat follows the same table and never re-streams
[ ] after retention purges a job, its ref dispatches again
[ ] HTTP: 409 + code + job id header for each case; Retry-After on in-progress
[ ] FakeEngine matches every row above

Admin
[ ] provision repeated after a lost response: one provider create, same actor
[ ] provision with the same ref and a different body: OperationConflict before any provider call
[ ] two concurrent provisions with one ref: one provider create
[ ] fund twice with one ref: funded once                    [=] exists today
[ ] fund with one ref and a different amount: OperationConflict before the provider call
[ ] create grant twice with one ref: one grant
[ ] create grant resumed after the provider created it: adopts the tagged grant, no second grant
[ ] two grants with the same name and different refs: both created (names stay free-form)
[ ] rotate payment key resumed after secrets.put failed once: old key revoked once, new key live
[ ] rotate access key repeated after success: one rotation; replay has no token
[ ] stale started (FixedClock past LPB_OPERATION_STALE_S) resumes; fresh started is InProgress
[ ] a definite refusal frees the ref for a corrected repeat
[ ] required ref missing: 422 at REST, validation error in the facade
[ ] no token appears in lpb_operations (scan result and context after issue and rotate)
[ ] ledger contract: unique scope, started→done guarded (store, both backends)
```

**Gears.** Obvious Implementation for the ledger and the HTTP mapping; Triangulate the retryable-failure
classification across failure kinds before generalizing it.

**Definition of done.** Every item `[x]`, `[=]` or `[-]` with a reason, every `[?]` answered; the checks in
`AGENTS.md` green (`uv run pytest`, the coverage gates, `ruff`, `mypy` strict, `scripts/check_docs.py`), and
`tests/store` green on PostgreSQL (`LPB_TEST_POSTGRES_URL`); `engine.md`, `payments.md`, `m3-decisions.md`, `m2-decisions.md` (D11),
the tech-debt tracker (T2) and the generated OpenAPI updated; consumers' pins and code updated (below).

## Consumer impact

| Consumer | Change |
| --- | --- |
| Builder console | Send `operation_ref` on provision, create grant and issue/rotate key; drop the "a repeat could apply twice" warning; handle `operation_in_progress`; show replays |
| ChatLPT, inference-gateway (embedded) | None required: `OperationInProgress` is an `OperationExists`. Optional: retry with the same ref after a retryable failure |
| Enterprise desk | Branch on `code` instead of the bare 409 |

## Alternatives considered

- **HTTP middleware that stores responses per `Idempotency-Key` (Stripe style).** Rejected: embedded mode
  would be unprotected (core belief 12), it would store tokens (belief 8), and it cannot replay streamed
  or unretained job output.
- **Rely on Batteries' idempotency alone.** Rejected: it does not cover grants, engine-only writes or the
  engine steps after the provider call (B1).
- **One ledger table for jobs too.** Rejected for M3: jobs already store the reference with the effect,
  and moving it means a data migration on the busiest table for no new guarantee.
- **Retry a failed job under the same reference whenever nothing was paid.** Rejected: free runners and
  post-send disconnects can run work with nothing paid.

## Open questions to lock

- **Q1.** Keep 409 `OperationExists` as the final answer for a job that ran (recommended), or return 200
  with the job record and `Idempotent-Replayed`?
- **Q2.** Exclude `unreachable` from retryable failures (recommended for M3), or split it into
  connect-phase and after-send outcomes so connect failures become retryable?
- **Q3.** `LPB_OPERATION_STALE_S` default: 120 s, four times `batteries_timeout_s` (30 s)?
- **Q4.** Make the reference required on provision, create grant and key issue/rotate now (one coordinated
  console release), or optional in M3 and required in M4?
- **Q5.** Tag engine-created grants with `metadata.lpb_operation` (recommended), or require unique grant
  names as the natural key? The tag needs no rule change for operators.

## Delivery slices

Each slice is one pull request, test-first, reviewed before the next.

1. **Defect fixes B1 and B2** with their regression tests. Small and independent; can merge first.
2. **Job rules** (Part A): errors, migration, CAS, HTTP codes, FakeEngine, `engine.md`.
3. **Operation ledger** and its store contract tests, wired into fund, fund grant and rotate payment key.
4. **Provision, create grant, issue and rotate access key** on the ledger (B4), the required references,
   `payments.md`, the decision record; then the console change.
