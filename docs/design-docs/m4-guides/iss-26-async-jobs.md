# ISS-26: Asynchronous jobs and durable recovery handles

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

| Item | Where | Today |
| --- | --- | --- |
| `JobRequest.mode="async"` | `contracts/jobs.py:13,56` | Accepted by the model; `JobService._admit_request` raises `NotImplementedInMilestone` (`services/jobs.py:248`) → REST 501 (`http/app.py` `_reserved`) |
| `jobs.submit` | `services/jobs.py:190` | Checks `jobs.run`, then raises `NotImplementedInMilestone`. **Not on the public `JobsApi`** (`api.py:91`), and no REST route calls it |
| `jobs.result`, `GET /v1/jobs/{id}/result` | `services/jobs.py:195`, `http/app.py:256` | Permission and ownership first (D44), then 501 |
| `queued` state | `contracts/jobs.py:166` | Reserved. `recover_interrupted` reads only `running` (`services/failover.py:59`) |
| `ResultRef.storage="stored"`, `location` | `contracts/jobs.py:169-176` | Reserved; immediate results are `inline`, not retained |
| Request bodies | `engine.md#opaque-request-bodies-and-stream-headers` | **Not stored.** Only a fingerprint (`services/request_identity.py`) |
| Restart, admission | `runtime/engine.py:82`, `services/admission.py` | `recover_interrupted()` runs before any loop starts. Admission is in-memory; `run` admits before `create_job` (`services/jobs.py:121`) |
| Retention | `store/retention.py`, `persistence.md#retention` | Deletes `succeeded`/`failed` jobs (and attempts, sessions, projections) explicitly by table; keeps `uncertain` |
| `finish_job` | `store/sql.py:260` | Unconditional `UPDATE ... WHERE id = :id`, no state guard |
| Deployment | `engine.md#deployment-performance-and-recovery-assumptions` | One engine process per database; T22 relies on it |
| FakeEngine | `testing/fake_engine.py:194,259,304` | **Parity gaps:** records every job `immediate`, runs `mode="async"` as immediate, and `result()` returns immediate results where the engine answers 501 |

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
  `POST /v1/queue/{app}` alias maps onto this later; its `cancel_url` waits for Q3.
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
| `store/schema.py`, migration `0009` | `lpb_job_requests`, `lpb_job_results`; `lpb_jobs.started_at`, `claims` |
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

```text
Tidy first (no behavior change): extract Dispatcher; the whole suite stays green
[ ] submit returns queued, mode async, no transport call, no admission slot held
[ ] run_once dispatches the oldest queued job; it succeeds and the result bytes are stored
[ ] result(): stored bytes and type (sha256 matches); queued/running 409; failed as run; immediate/expired 410
[ ] the claim is a CAS: two claims of one job, one wins (store, both backends)
[ ] the worker never runs a job without an admission slot; immediate and async share the cap
[ ] an app at its per-app cap does not block another app's older job
[ ] queue full: submit is NoCapacity (503, Retry-After); nothing stored
[ ] queued past LPB_ASYNC_QUEUE_TTL_S: failed, no_capacity, no attempt, cost none
[ ] restart with a queued job, or a running one with no attempt row: it runs once after start
[ ] restart with a running job and an in_flight paid attempt: uncertain, never re-dispatched
[ ] a job that reached claims=3 without an attempt: failed, interrupted
[ ] the client going away after submit changes nothing (HTTP: close the client, job still succeeds)
[ ] engine.stop() drains; a dispatch still running after the drain ends uncertain if paid
[ ] repeat submit, same body: the same job, no second dispatch, Idempotent-Replayed   [?] Q1
[ ] same ref, immediate then async: OperationConflict; after a never-reached-a-runner failure: retry_of
[ ] revoked allocation while queued: failed not_provisioned at dispatch, unpaid
[ ] D52 failover and the D55 cap behave as for run (one representative test each)
[ ] reply above LPB_ASYNC_RESULT_MAX_BYTES: failed result_not_stored, ResultRef keeps sha256   [?] Q4
[ ] the finishing transaction is atomic: a failed write leaves the job running, no bytes stored
[ ] retention: result bytes expire, the job stays; the job purge removes both tables; requests go at the end
[ ] cost of a queued job is pending, never none
[ ] REST: 202 + Location + Livepeer-Job-Id; GET carries Retry-After until ended; health counters
[ ] FakeEngine: submit/advance/result match every row above; the three parity gaps closed
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
running immediately. **inference-gateway, enterprise desk:** none.

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

## Open questions to lock

- **Q1.** A repeated async submit returns the existing job (recommended), or 409 `OperationExists`?
- **Q2.** Requeue only `running` jobs with no attempt row (recommended), or also those whose every
  attempt provably never reached a runner (the ISS-14 rule)?
- **Q3.** Cancel a `queued` job (`POST /v1/jobs/{id}/cancel`, nothing paid) in M4, or no cancel at all
  (ISS-28: generic cancellation is not promised)? Recommended: queued only.
- **Q4.** A paid reply the engine could not store: cost `failed_after_payment` (recommended: the builder
  got nothing) or `delivered`, which is what `services/costs.py:367` gives a `succeeded` attempt today?
- **Q5.** Encrypt stored inputs and results with the `SecretStore` key, or rely on database protection
  (recommended for M4)?
- **Q6.** Defaults: 4 workers, 1000 queued, 16 MiB, 7-day results, 1 h queue TTL? **Q7.** Long-polling
  (`GET /v1/jobs/{id}?wait_s=30`) now, or polling only (recommended)?
- **Q8.** An access key revoked while its job is queued: run it (authorized at submit, recommended; the
  allocation check at dispatch still stops spending) or fail it at dispatch?

## Delivery slices

1. **Tidy:** extract `Dispatcher`, no behavior change; close the FakeEngine parity gaps with tests.
2. **Queue:** migration, submit, claim, worker, restart rules, admission, TTL, health, REST 202.
3. **Results** (`lpb_job_results`, `/result`, size limit, retention), then idempotency rows, docs, console.
