# ISS-26: Asynchronous jobs and durable recovery handles

> **Questions locked on 12 and 13 November 2026.** Mike answered every open question; see [Questions
> locked](#questions-locked). Where an answer differs from the proposal below, the answer wins, and that
> section says which part it replaces. The answers are decision D69 in
> [m4-decisions.md](../m4-decisions.md).
>
> **Amended on 18 November 2026** by the payment clearinghouse (D82 to D86). See [Amended on 18
> November 2026](#amended-on-18-november-2026).
>
> **Built on 19 November 2026** on `tasks/m4/iss-26-async-jobs`, from P8's branch (#169), and in review.
> [Current state](#current-state-verified) says what is built and where, the [test plan](#test-plan-test-driven)
> is marked, and the contract is [engine.md](../../contracts/engine.md#async-jobs). The work is tracked in
> [the exec plan](../../exec-plans/active/iss-26-async-jobs.md).

**Status:** proposal for discussion, 29 October 2026. Nothing here is decided until it is locked; locked
rules become a decision in the M4 decision record.\
**Tracker:** ISS-26 ([Network-Engineering-SPE#40](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/40)).\
**Owner:** John (engine). **Reviewers:** Mike, John.\
**Verified against:** builder-core `releases/m3` at `d3df806`; livepeer/node `406b248` (pin) and `a4b68ed`
(main); livepeer-python-gateway `2017a80`; Clearinghouse Batteries `ca09e47`.

## Done when

- A job outlives the request that created it. It has a durable status and, in the end, a result or a
  failure, reached through the supported handle.
- A disconnect or a restart keeps the job's identity and recovery references, and never starts a
  replacement execution.
- **Depends on** ISS-14 (repeat rules, [iss-11-14-idempotency.md](../m3-guides/iss-11-14-idempotency.md)),
  ISS-15 (inputs and results, [iss-15-history-and-results.md](../m3-guides/iss-15-history-and-results.md))
  and ISS-19. **External:** H1 (SDK), H3 (signer, orchestrator). ISS-28 builds its async rows on this guide.

## Current state (verified)

Re-verified on 19 November 2026 against P8's branch at `699d759`, where the work started. What had
changed since 29 October: `application_id` is `tenant_id` and the admin kinds are `clearinghouse_admin`
and `tenant_admin` (#163); the engine has two halves and a role (`LPB_ROLE`, #164, #169), and
`AdmissionLimiter` refuses a job in the `clearinghouse` role; the router split has happened, so the
result route was `http/work.py:99`, not `http/app.py:256`; the latest migration was `0015`; a second
`start()` was already refused (#113); and the line numbers had moved (`services/jobs.py:193-200` for
`submit` and `result`, `:244` for the 501 of `run`, `api.py:109` for `JobsApi`,
`services/failover.py:67` for `recover_interrupted`, `store/sql.py:205` for `finish_job`, and the
FakeEngine's gaps at `testing/fake_engine.py:198,313`). Each row of the 29 October table still held.

| Item | State since ISS-26 | Where |
| --- | --- | --- |
| `JobRequest.mode="async"` | `jobs.submit` queues it. `jobs.run` and a stream refuse it with `InvalidRequest` (400); it was a 501 | `services/jobs.py`, `http/work.py` |
| `jobs.submit` | On `JobsApi`. Checks the permission, the request, the reference and the allocation, stores the job `queued` with its request and the actor's snapshot, and returns it. `POST /v1/jobs` answers 202 with `Location` and `Livepeer-Job-Id`, or 200 with `Idempotent-Replayed` for a repeat | `api.py`, `services/jobs.py`, `http/job_handles.py` |
| The paid call | `Dispatcher`, the one paid call for an immediate job, a stream and an async job (the tidy slice, no behavior change) | `services/dispatch.py` |
| The worker | `AsyncJobWorker`: a slot, then a compare-and-set claim, oldest first, skipping a runner app at its limit; the queue's time limit; `run(stop)`, `run_once()`, `drain()`. Started by `start()` after `recover_interrupted`, counted there, and drained by `stop()` | `services/async_jobs.py`, `runtime/engine.py` |
| `queued` state | Real. `recover_interrupted` requeues a `running` async job with no attempt, three claims at most | `services/failover.py`, `store/job_queue.py` |
| `ResultRef.storage="stored"` | An async job's answer, in `lpb_job_results`, written in the transaction that ends the job; `location` and `expires_at` | `services/async_jobs.py`, `store/job_queue.py` |
| `jobs.result`, `GET /v1/jobs/{id}/result` | The stored bytes; 409 `job_not_finished` before the end; the failure for a failed job; 410 `result_expired` for an immediate job and an expired result. The 501 is gone | `services/jobs.py`, `http/job_handles.py`, `http/app.py` |
| Cancel | `jobs.cancel`, `POST /v1/jobs/{id}/cancel`: one conditional write, `queued` to `failed`, kind `abandoned`, reason `cancelled`; 409 `job_not_queued` otherwise | `services/jobs.py`, `store/job_queue.py` |
| Request bodies | An async job's request is kept in `lpb_job_requests` until the job ends; an immediate job's still only as a fingerprint, which gains `mode` only when it is `async` | `store/job_queue.py`, `services/request_identity.py` |
| `finish_job` | Ends only a `running` job and says whether it did; a miss is logged (`jobs.finish_missed`), not raised. The same transaction deletes the stored request | `store/jobs.py`, `store/job_queue.py`, `services/dispatch.py` |
| Retention | Expired result bytes go on every purge; the job purge also deletes an async job's result and request, by table; `queued` and `running` jobs are never taken. `LPB_RETENTION_RESULTS_DAYS` (7) is on by default and alone purges first one interval after start | `store/retention.py`, `runtime/engine.py` |
| Health | `EngineHealth.queued_jobs`, `oldest_queued_s` | `contracts/health.py`, `services/async_jobs.py` |
| Roles | Async jobs are the work half's: `both` and `work` run them; `clearinghouse` refuses `submit` and a worker step, starts no worker and mounts no job route | `services/admission.py`, `runtime/engine.py`, `http/work.py` |
| Migration | `0016`: `lpb_jobs.started_at`, `claims`; `lpb_job_requests`; `lpb_job_results` | `store/migrations/versions/0016_async_jobs.py`, `store/schema.py` |
| FakeEngine | The three parity gaps are closed: `submit` queues and records `async`, `advance()` runs what is queued, `result()` follows the engine (410 for an immediate job), and `run(mode="async")` is refused | `testing/fake_async.py`, `testing/fake_jobs.py` |
| Deployment | One engine process per database, as before; no lease (T44) | [engine.md](../../contracts/engine.md#deployment-performance-and-recovery-assumptions) |

**Decided where this guide is silent** (19 November 2026, for Mike's review):

- **The cancel's kind.** A cancelled job is `failed` with kind `abandoned` and reason `cancelled`: the
  caller gave the job up, as a caller who stops reading a stream does. `FailureReason` holds the four
  payment reasons and `cancelled`, as ISS-28 proposes. A cancel needs `jobs.run`, as `POST /v1/jobs/{id}/usage` does;
  a cancelled job's result is 410; cancelling it again answers 200 with it. ISS-28 decides its
  `category` and `recovery` (T47).
- **A repeat after a cancel or a queue expiry** queues a new job with `retry_of`: neither reached a
  runner (D57's closed list holds a failed job with no attempt). A repeat after `result_not_stored`
  answers with that job (Q1).
- **An actor with no allocation at submit** gets the job recorded `failed`, `not_provisioned`, and
  `JobFailed` (402), as `run` does, and nothing is queued.
- **A job stopped by `engine.stop()` before its first attempt** goes back to the queue, as a restart
  would put it (Q2's rule, applied at stop).
- **The queue limit** is counted before the insert (T45). **The time limit** counts from the submit,
  across a restart.
- **Results retention alone** purges first one interval after start, so the default settings add no
  write at start.

**Upstream facts.** livepeer/node `406b248` serves single-shot work only through `/apps/{runner_id}/app/...`
(`orchestrator/http.go:68`), its proxy bound to the caller's request (`context.WithCancel(r.Context())`):
a dropped engine connection cancels the runner call. No orchestrator route and no SDK API submits or
polls a job. So **the only durable handle is the engine's own: job id, `operation_ref`, and the attempts'
manifests and payment sessions**, and the worker holds the runner connection for the whole call.

## Decision proposed

**An in-process queue on the engine's own database.** `lpb_jobs` is the queue (state `queued`); a worker
task claims jobs with a compare-and-set and dispatches each through `run`'s paid call. Results are stored
in the transaction that ends the job. Polling is the only delivery in M4.

### Lifecycle

```text
submit ──► queued ──claim (CAS, admission slot held)──► running ──► succeeded | failed | uncertain
              │                                             │
              └── queue TTL ─► failed (never paid)          └── restart, no attempt row ─► queued
```

- **Submit** runs the checks that do not change while a job waits: `jobs.run` permission, path, the
  fingerprint, the `operation_ref` rules, and provisioning (`not_provisioned` fails fast, as in `run`).
  It stores the job `queued` with its request and an actor snapshot (`actor_id`, `application_id`,
  permissions, `credential_kind`, `attributes`; never `auth` or `display_name`), and returns the job.
  No payment, no discovery, no slot. Authorization is decided at submit (Q8).
- **Claim.** The worker takes an admission slot first (`AdmissionLimiter`, shared with immediate jobs),
  then `UPDATE lpb_jobs SET state='running', started_at=:now, claims=claims+1 WHERE id=:id AND
  state='queued'`. A row count of 0 means someone else moved it (a cancel, the TTL): release the slot.
  Oldest first, skipping apps at their per-app cap so one app cannot block the rest.
- **Dispatch** is today's `_dispatch` unchanged: discovery, selection, credential, failover (D52), the
  paid-attempt cap (D55), attempts written before the call, the manifest stored before signing (D54).
  `return_http_errors` and streaming are refused for async at submit (400), as `run` refuses the first.
- **Finish** writes the state, the failure or the `ResultRef`, **the result bytes**, and deletes the
  stored request, in one transaction guarded by `state='running'`.
- **Queue TTL.** Still `queued` after `LPB_ASYNC_QUEUE_TTL_S`: `failed`, `no_capacity`, nothing paid.
  `timeout_s` counts from the claim.

### Crash and disconnect

| Left behind | On start | Why |
| --- | --- | --- |
| `queued` | Stays queued; the worker takes it | Nothing went out |
| `running`, **no attempt row** | Back to `queued` if `claims < 3`, else `failed`, `interrupted` | The attempt row is written before any call (rule 3), so nothing went out. The claim count stops a job that crashes the engine before its first attempt from looping (a dead-letter rule) |
| `running`, with attempts | `recover_interrupted` as today: `uncertain` or `failed` | It may have run or paid. Never re-dispatched (no replacement execution) |

A client disconnect does nothing: the job belongs to the worker. `engine.stop()` stops claiming, waits
`LPB_ASYNC_DRAIN_TIMEOUT_S`, then cancels; a cancelled paid dispatch ends `uncertain` (`_close_after_error`).

### Idempotency (extends the ISS-14 guide)

- The fingerprint gains `mode` **only when it is `async`**, so legacy fingerprints stay byte-for-byte
  stable (`request_identity.py` already adds fields only when set). An immediate and an async request
  under one reference are an `OperationConflict`.
- **A repeated async submit returns the existing job** (200 instead of 202, `Idempotent-Replayed: true`;
  in-process, the same `Job`), in any state. The handle is always replayable, unlike an immediate result,
  so the ISS-14 409 is not needed here (Q1). A different body is still 409 `OperationConflict`.
- A repeat after a failure that never reached a runner follows ISS-14's re-dispatch rule: a new `queued`
  job with `retry_of`, under the same reference.
- Concurrent submits meet at `lpb_jobs_operation_ref`; the loser compares fingerprints (ISS-14 B2).

### Results

- **Where:** new table `lpb_job_results` (job id, bytes, content type, size, sha256, `expires_at`), SQLite
  and PostgreSQL alike; `ResultRef(storage="stored", location="/v1/jobs/{id}/result", expires_at=...)`.
  Written in the finishing transaction, so a job is never `succeeded` without its bytes.
- **Size:** `LPB_ASYNC_RESULT_MAX_BYTES` (proposed default 16 MiB; a FLUX image reply is well under).
  A larger reply is not stored: the job ends `failed`, kind `result_not_stored`, its `ResultRef` keeps
  size and sha256, and the payment stays attributed (cost class: Q4). Large media waits for #73's
  `MediaStore`, which can later hold the bytes behind the same `location`.
- **Retention:** `LPB_RETENTION_RESULTS_DAYS` (proposed 7) deletes result bytes only; the job and its
  `ResultRef` stay until `LPB_RETENTION_JOBS_DAYS` purges the job, which deletes both new tables'
  rows explicitly, as it does attempts. It never selects `queued` or `running` jobs.
- **Reads:** `jobs.result(actor, id)` and `GET /v1/jobs/{id}/result`, after the D44 checks: the bytes and
  content type for a stored result; 409 with the job and `Retry-After` while `queued`/`running`; the
  job's failure mapping for a failed or uncertain job; **410** for an expired result and for an immediate
  job (never retained). The 501 goes.
- **Secrets:** result bytes (and any capability URL in them) go only to the owner, never into logs.
  Stored inputs carry no credential (`headers` rejects them) and are deleted at the end. At rest: Q5.

### Admission, limits and health

`LPB_ASYNC_WORKERS` (default 4) concurrent async dispatches, inside `max_concurrent_jobs`.
`LPB_ASYNC_MAX_QUEUED` (default 1000): a full queue answers `NoCapacity` (503, `Retry-After`) at submit.
`LPB_ASYNC_POLL_S` (1 s), and an in-process wake-up at each submit. `EngineHealth` gains `queued_jobs`
and `oldest_queued_s`: alert on the age of the oldest job, not on depth.

### Payment for queued jobs

Payment happens at dispatch, never at submit, so an allocation revoked or exhausted while the job waited
fails it unpaid (`not_provisioned`, or `payment` with the ISS-12 reason). D52, D55, D54 and cost
classification are as for `run`. A queued job's cost is `pending` (`services/costs.py:323` treats any
state outside `succeeded`/`failed`/`uncertain` as running).

### Surfaces

- **REST:** `POST /v1/jobs` with `mode="async"` → **202**, the `Job`, `Location: /v1/jobs/{id}` and
  `Livepeer-Job-Id`. `GET /v1/jobs/{id}` adds `Retry-After: 1` while not ended. #73's
  `POST /v1/queue/{app}` alias maps onto this later; its `cancel_url` points at the cancel route (Q3, locked).
- **FakeEngine:** `submit` queues; a test hook `advance()` runs queued jobs; `result()` follows the
  real rules (410 for immediate). Fixes the three parity gaps above.
- **Docs:** `engine.md` (async, states, results, failures), `persistence.md`, `RELIABILITY.md`, generated
  OpenAPI and schema; T7 narrowed to sessions and events.

## Code areas

| File | Change |
| --- | --- |
| `contracts/jobs.py` | Comments un-reserved; `ResultRef.expires_at`; `Job.started_at`; `result_not_stored` |
| `api.py` | `JobsApi.submit` |
| `errors.py` | Reuse `OperationConflict`, `NoCapacity`; add `ResultExpired` (410), `JobNotFinished` (409) |
| `protocols/__init__.py` | `JobRepo`: `enqueue_job(job, fingerprint, request, actor)`, `claim_job`, `requeue_unstarted`, `finish_job_with_result`, `read_result`, `queued_stats`; `finish_job` gains the state guard |
| `store/schema.py`, a new migration (numbered when written: #114 and #115 add migrations first) | `lpb_job_requests`, `lpb_job_results`; `lpb_jobs.started_at`, `claims` |
| `store/sql.py`, `store/retention.py` | The methods above; explicit deletes and the result-bytes purge |
| `services/jobs.py` | Tidy first: extract the paid-call core (`_candidates` … `_finish`) into `services/dispatch.py` (`Dispatcher`), no behavior change; then `submit` and `result` |
| `services/async_jobs.py` (new) | `AsyncJobWorker(store, dispatcher, admission, clock, limits)`: `run(stop)`, `run_once()`, `drain()` |
| `services/failover.py` | `recover_interrupted` requeues `running` async jobs with no attempt row |
| `services/request_identity.py` | `mode` in the fingerprint when async |
| `runtime/assembly.py`, `runtime/engine.py` | Build the worker once; start its task after `recover_interrupted`; drain in `stop()` |
| `config.py` | The `LPB_ASYNC_*` and `LPB_RETENTION_RESULTS_DAYS` settings |
| `http/app.py` | 202 + `Location`; `/result` mapping; `Retry-After` on non-ended reads |
| `testing/fake_engine.py`, `testing/fakes.py` | Parity; a gated transport hook |

## Test plan (test-driven)

**Levels.** Service tests through `make_engine` with the real `SqlStore` on SQLite and the scripted
transport (`tests/unit/test_async_jobs.py`); store contract tests for the claim, the finishing
transaction, requeue and purge on **SQLite and PostgreSQL** (`tests/store/`, `LPB_TEST_POSTGRES_URL`);
restart tests that open a second engine on the same file (`tests/store/test_durability.py` style); one
HTTP test per answer; FakeEngine parity in `test_fake_engine_contract.py`.

**Seams.** `FixedClock` for the TTL and result expiry; `AsyncJobWorker.run_once()` drives the worker
step by step (no sleeps); an `asyncio.Event`-gated transport holds a dispatch `running`; a store wrapper
that fails one write for the finishing transaction. No mocks of contracts or the store.

**Starter test:** `submit` returns a `queued` job with `mode="async"` and the transport was not called.

Marked on 19 November 2026. Each row names its tests; a store row runs on SQLite and on PostgreSQL 16.
The rows after the guide's own come from the locked answers and the amendment.

```text
[x] Tidy first (no behavior change): extract Dispatcher; the whole suite stays green (4109382 to c8003e0:
    the paid call, the job writes, the FakeEngine's modules and the cost class each moved, suite green)
[x] submit returns queued, mode async, no transport call, no admission slot held
    (test_async_jobs::test_submit_returns_a_queued_job_and_calls_no_runner)
[x] run_once dispatches the oldest queued job; it succeeds and the result bytes are stored
    (test_async_jobs::test_run_once_dispatches_the_oldest_queued_job_and_stores_its_result)
[x] result(): stored bytes and type (sha256 matches); queued/running 409; failed as run; immediate/expired 410
    (test_async_results.py; over REST test_async_http.py)
[x] the claim is a CAS: two claims of one job, one wins (store, both backends)
    (test_job_queue::test_the_claim_is_a_compare_and_set_and_one_of_two_wins)
[x] the worker never runs a job without an admission slot; immediate and async share the cap
    (test_async_jobs::test_the_worker_never_runs_a_job_without_an_admission_slot)
[x] an app at its per-app cap does not block another app's older job
    (test_async_jobs::test_an_app_at_its_cap_does_not_block_another_apps_job)
[x] queue full: submit is NoCapacity (503, Retry-After); nothing stored
    (test_async_jobs::test_a_full_queue_refuses_the_submit_and_stores_nothing; test_async_http)
[x] queued past LPB_ASYNC_QUEUE_TTL_S: failed, no_capacity, no attempt, cost none
    (test_async_jobs::test_a_job_queued_past_the_time_limit_fails_unpaid)
[x] restart with a queued job, or a running one with no attempt row: it runs once after start
    (test_async_restart::test_a_queued_job_and_one_claimed_with_no_attempt_each_run_once_after_a_restart)
[x] restart with a running job and an in_flight paid attempt: uncertain, never re-dispatched
    (test_async_restart::test_a_job_with_a_paid_attempt_in_flight_is_uncertain_after_a_restart_and_never_run_again)
[x] a job that reached claims=3 without an attempt: failed, interrupted
    (test_async_restart::test_a_job_claimed_three_times_without_an_attempt_fails_interrupted)
[x] the client going away after submit changes nothing (HTTP: close the client, job still succeeds)
    (test_async_client_gone.py)
[x] engine.stop() drains; a dispatch still running after the drain ends uncertain if paid
    (test_async_restart::test_stop_waits_for_a_running_job_and_one_still_running_after_that_ends_uncertain,
    and ::test_a_job_stopped_before_its_first_attempt_goes_back_to_the_queue)
[x] repeat submit, same body: the same job, no second dispatch, Idempotent-Replayed
    (test_async_repeats::test_a_repeated_submit_returns_the_same_job_in_any_state, and over REST
    ::test_a_repeated_submit_over_rest_is_200_and_says_it_was_replayed)
[x] same ref, immediate then async: OperationConflict; after a never-reached-a-runner failure: retry_of
    (test_async_repeats::test_one_reference_is_an_immediate_job_or_an_async_one_never_both,
    ::test_a_job_that_never_reached_a_runner_is_queued_again_under_its_reference)
[x] revoked allocation while queued: failed not_provisioned at dispatch, unpaid
    (test_async_repeats; on both sets of payment adapters, test_async_paying.py)
[x] D52 failover and the D55 cap behave as for run (one representative test each)
    (test_async_repeats::test_an_async_job_fails_over_and_never_pays_the_same_orchestrator_twice,
    ::test_an_async_job_stops_at_the_paid_attempt_cap)
[x] reply above LPB_ASYNC_RESULT_MAX_BYTES: failed result_not_stored, cost failed_after_payment, ResultRef keeps sha256
    (test_async_results::test_a_reply_too_large_to_store_fails_the_job_and_is_never_billed_as_delivered)
[x] the finishing transaction is atomic: a failed write leaves the job running, no bytes stored
    (test_job_queue::test_a_failed_write_leaves_the_job_running_with_no_bytes_stored; the engine then
    ends it as an immediate job: test_async_results::test_a_finish_that_fails_stores_no_bytes_and_the_paid_job_ends_uncertain)
[x] retention: result bytes expire, the job stays; the job purge removes both tables; requests go at the end
    (store test_async_retention.py; unit test_async_retention.py; requests:
    test_job_queue::test_a_failed_end_deletes_the_stored_request_and_only_a_running_job_ends)
[x] cost of a queued job is pending, never none
    (test_fake_engine_async::test_a_queued_job_costs_pending_and_never_none, on the engine and the fake)
[x] REST: 202 + Location + Livepeer-Job-Id; GET carries Retry-After until ended; health counters
    (test_async_http.py; test_async_jobs::test_health_counts_the_queue_and_the_age_of_its_oldest_job)
[x] FakeEngine: submit/advance/result match every row above; the three parity gaps closed
    (test_fake_engine_async.py, each test on the fake and the engine. The fake models no queue limit,
    admission, restart or retention: a test scripts a full queue with Raises(NoCapacity(...)) and an
    expired result with expire_results())

From the locked answers (D69) and the amendment of 18 November
[x] Q3: POST /v1/jobs/{id}/cancel ends a queued job failed, cancelled, unpaid, cost none; a running one is 409
    (test_async_http::test_a_cancel_ends_a_queued_job_and_only_a_queued_one;
    test_job_queue::test_a_cancel_is_one_conditional_write_on_a_queued_job, and
    ::test_a_cancel_and_a_claim_at_the_same_moment_do_not_both_win)
[-] Q3: the events feed reports a cancel as job.finished with reason cancelled. ISS-46 is not built. The
    seam: every end of a job is one conditional statement that says whether it changed a row
    (store/job_queue.py), where the event is written in the same transaction when the feed is built
[x] Q8: an access key revoked while its job is queued: the job runs
    (test_async_repeats::test_a_job_runs_as_the_actor_it_was_submitted_by_after_its_access_key_is_revoked)
[x] Q6: the defaults are 4 workers, 1000 queued, 1 hour, 16 MiB, 7 days (config.py; test_async_jobs and
    test_async_results read them through make_engine)
[x] the migration keeps every job; a downgrade ends a queued job; an interrupted run changes nothing
    (test_async_migration.py)
[x] a clearinghouse queues no job and runs no worker, for an embedding caller too; its job routes are 404
    (test_async_roles.py; test_roles.py and test_route_sets.py with the cancel route added)
[x] a queued job behaves the same on an engine that pays through a clearinghouse (role work) as on a
    both engine: paid at dispatch, refused unpaid when the allocation is spent or revoked (test_async_paying.py)
[-] a provider request id that lets a late result be collected: ISS-48's. The seam is kept: the job
    record and ResultRef can take the id as an optional field, and lpb_job_results is keyed by job id
```

**Definition of done.** Every item `[x]`, `[=]` or `[-]` with a reason, every `[?]` answered; `AGENTS.md`
checks green (pytest, coverage gates, diff-cover ≥ 90, ruff, mypy strict, `check_docs.py`, `gen-docs`,
`lpb smoke`), `tests/store` green on PostgreSQL; the docs above updated.

## Upstream dependencies

| Upstream | Needed? | Why / workaround |
| --- | --- | --- |
| livepeer/node | No | No async or status route at `406b248` or `a4b68ed`; the single-shot proxy is bound to the request, so the engine's worker holds the connection. Work cut by a restart is `uncertain`, as today. Not vendorable (server) |
| livepeer-python-gateway | No | No submit/poll API to use; the engine owns the paid call (D53) |
| clearinghouse-batteries | No | Payment is per dispatch; nothing new is reserved at submit |

## Consumer impact

**Builder console:** the M4 matrix's "asynchronous with stored results" (submit, poll, fetch; show
`queued`). **ChatLPT:** none required; the FakeEngine fix breaks any test that relied on `mode="async"`
running immediately. **inference-gateway:** none.

## Alternatives considered

- **A broker (Redis, RabbitMQ) or a separate worker process.** Rejected for M4: the contract allows one
  process per database (T22 relies on it), SQLite has one writer, and embedded mode must run async jobs
  with no extra process (core belief 12). A separate worker needs leases with fencing tokens on every
  write and, per the DI review's "reverse this if", a second composition root. The claim is already a
  compare-and-set, so a later multi-process step adds a lease epoch, not a redesign.
- **Webhooks.** Deferred: they need an outbox written with the finish, at-least-once delivery with event
  ids, signing, retries with a dead-letter path and a callback-URL allowlist. The reserved events feed
  (`job.finished`) is the push path when it lands; webhooks would deliver it.
- **Results in an object store now.** Deferred to #73's `MediaStore`; one database keeps result and
  state in one transaction.
- **Re-dispatching interrupted work.** Rejected: the pinned node cannot say whether it ran.

## Questions locked

Locked by Mike on 13 November 2026.

- **Q1, a repeated async submit:** it returns the existing job in any state, with 200 in place of 202 and
  `Idempotent-Replayed: true`. A different body under the same reference is still 409 `OperationConflict`.
- **Q2, interrupted jobs after a restart:** only a `running` job with no attempt row goes back to
  `queued`, and after three such claims it fails `interrupted`. A job with any attempt is never run again
  by the engine. The caller can repeat the submit, and the ISS-14 rule creates a new queued job with
  `retry_of` when no attempt reached a runner.
- **Q3, cancel:** `POST /v1/jobs/{id}/cancel` for queued jobs only. One conditional write: `queued`
  becomes `failed`, reason `cancelled`, nothing paid, cost class `none`. A `running` job answers 409. The
  events feed reports it as `job.finished` with reason `cancelled`.
- **Q4, a paid reply the engine could not store:** cost class `failed_after_payment`. The attempt keeps
  outcome `succeeded`, and the cost rule reads the job's failure kind (`result_not_stored`).
- **Q5, stored inputs and results:** not encrypted by the engine in M4. The operator protects the
  database, and the docs say what is stored, for how long, and that it is not encrypted.
- **Q6, defaults:** 4 workers, 1000 queued, a 1 hour queue time limit, 16 MiB per stored result, results
  kept 7 days. The docs state the worst-case storage: about 16 GiB of queued inputs at the 16 MiB request
  limit.
- **Q7, learning that a job finished:** polling only. No `wait_s`. A consumer that wants a signal uses the
  events feed.
- **Q8, an access key revoked while its job is queued:** the job runs. It was authorized at submit and
  belongs to the actor, not the key. The allocation check at dispatch still stops spending.

**Other decisions that touch this guide.**

- **Migration numbers.** The guide names migration `0009`. #114 and #115 each add a migration first, so
  this one is numbered when it is written.
- **A second `start()`** on a running engine is refused (#113), in a standalone pull request before this
  ticket adds workers to `start()`.
- **The router split** (#25) lands before this ticket, so the cancel and result routes go in their own
  router file.
- **Worker leases** are where a second engine on one database is detected (#112 leaves it here).

### Where the runner standard plugs in later

Runner contract v1 (ISS-47) is not vetted yet. This ticket therefore routes by app ID, as today, and reads
no v1 metadata. ISS-48 adopts the standard as the last feature of M4, and it may move to M5.

- **Working assumption for ISS-48 (12 November):** app IDs stay the routing key; v1 metadata describes a
  runner and never routes; missing, old or unreadable metadata is ignored and logged; model and profile
  are filters; no schema check.
- **Criterion moved to ISS-48 on 13 November:** recovery follows the runner's registry entry: a completion
  record the runner writes last, or a provider request id that lets a late result be collected.
- **The seam to keep:** the job record and `ResultRef` leave room for a provider request id, so a late
  result can be collected without a new table.

## Amended on 18 November 2026

The payment clearinghouse (D82 to D86, the [ISS-31 guide](iss-31-payment-configurations.md)) changes names
here and nothing else.

- **Where it runs.** Async jobs belong to the work half of the engine, so they run in the enterprise's own
  engine. A payment clearinghouse runs no jobs.
- **Names.** `application_id` in the job's actor snapshot becomes `tenant_id` (#163).
- **`not_provisioned`** still fails a job before payment. With a clearinghouse it means the actor has no
  allocation there. Its recovery value is `admin`, as ISS-28 now names it.

## Delivery slices

1. **Tidy:** extract `Dispatcher`, no behavior change; close the FakeEngine parity gaps with tests.
2. **Queue:** migration, submit, claim, worker, restart rules, admission, TTL, health, REST 202.
3. **Results** (`lpb_job_results`, `/result`, size limit, retention), then idempotency rows, docs, console.
