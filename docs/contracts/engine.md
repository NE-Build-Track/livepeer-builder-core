# Engine contract: discovery, jobs, results, usage

**Ticket:** ISS-02 · **Code:** `contracts/discovery.py`, `contracts/jobs.py`, `services/discovery.py`,
`services/jobs.py` · **Status:** immediate execution implemented; async and persistent reserved.

Every model is a frozen pydantic v2 class. The same class is used in-process and as the REST
schema ([generated/openapi.json](../generated/openapi.json)), so the two integration modes cannot
drift. Times are UTC. Money is `Decimal`. Engine IDs are UUIDs.

## Discovery

`DiscoveryService` keeps a snapshot of the runners the signer advertises (`DiscoverySource`). It is
refreshed every `discovery_interval_s` and counts as stale after three missed intervals.

| Model | Meaning |
| --- | --- |
| `Runner` | One advertised runner: `url`, `app`, `mode` (`single-shot` or `persistent`), capacity, `rate` |
| `NetworkRate` | Wholesale network rate as advertised (`amount`, `currency`, `unit`). Not a retail price |
| `Offering` | One runner app: its modes, the cheapest and dearest comparable rate, runner and ready counts, `observed_at` |

Capability → app resolution (descriptor or configuration table) is a later contract. In M2 the caller
names the runner `app`; `capability` and `model` are labels only.

## Jobs and attempts

`JobRequest`: `app`, `path`, `method`, `payload`, `operation_ref`, `mode`, `max_attempts`,
`timeout_s`, plus the labels `capability` and `model`.

| State | Meaning |
| --- | --- |
| `queued` | Reserved for async jobs (M4) |
| `running` | Dispatching. A restart resolves leftover `running` jobs on start |
| `succeeded` | A runner answered 2xx |
| `failed` | It ended, and nothing went out that might have run the work |
| `uncertain` | Payment went out (or may have) and the outcome is unknown. Never retried automatically |

An `Attempt` is one runner call. It records `allocation_id` (the allocation whose key paid),
`sessions` (one entry per session the signer signed, in order, each with a `manifest_id` and a
match of `unmatched`, `matched` or `ambiguous`), `payment_sent`, `outcome` (`in_flight`,
`succeeded`, `refused`, `unreachable`, `payment_refused`, `payment`, `timeout`, `http`), the status
code and timestamps. The cost key is the matched `payment_session_id`, not the manifest.

### Behavior the contract fixes

1. **Failover only before payment.** A job moves to another runner only while `payment_sent` is false
   and the outcome is `refused`, `unreachable` or `payment_refused`. After payment, the job is bound to
   that runner.
   - **`payment_refused`:** the signer refused to pay *this orchestrator* before anything was sent. The
     signer statuses are 400 (incomplete or expired payment params) and 481 (price above the session ceiling). Another orchestrator may succeed.
   - **`payment`:** caller- or signer-side refusals, such as 402 (allowance exhausted), 403, 502
     (webhook) and 503 (outbox). These do **not** fail over, because every orchestrator would fail the
     same way.
2. **At most one dispatch per operation.** One `(application_id, actor_id, operation_ref)` dispatches at
   most once (see [idempotency](#idempotency)).
3. **The attempt row is written before the call** (`in_flight`) and updated after it. If the engine dies
   mid-call, `recover_interrupted()` marks the job `uncertain` when anything may have paid, and
   `failed` otherwise.
4. **Admission and provisioning are checked before any payment.** The admission limits
   (`max_concurrent_jobs`, `max_concurrent_jobs_per_app`) refuse with 503 and `Retry-After`. An
   unprovisioned actor fails with `not_provisioned`.
5. **A cost that hasn't arrived is `pending`.** Only a job that never reached payment has cost status
   `none`.

### Idempotency

The semantics are owned by John's PR (decision D11). Until it lands:

| Case | Response |
| --- | --- |
| Same `operation_ref`, same request (`app`, `path`, `method`, `payload`, `capability`, `model`) | 409 `OperationExists` with the original job. Nothing is dispatched |
| Same `operation_ref`, different request | 409 `OperationConflict` |
| No `operation_ref` | Every call dispatches |

Limits such as `timeout_s` may differ between retries. The store enforces uniqueness with a partial
unique index, so concurrent duplicates are also refused.

### Failures

| `Failure.kind` | Payment possible? | REST status |
| --- | --- | --- |
| `no_offering` | no | 404 |
| `no_capacity` | no | 503 + `Retry-After` |
| `not_provisioned` | no | 402 |
| `refused`, `unreachable` (all runners) | no | 503 + `Retry-After` |
| `payment` (includes every orchestrator refused by the signer) | maybe | 503 + `Retry-After` |
| `timeout` | yes → job `uncertain` | 504 |
| `runner_rejected` (4xx) | yes | the runner's own 4xx |
| `runner_error` (5xx) | yes | 502 |
| `interrupted` (restart) | maybe | 502 |

## Results

Immediate results are returned inline and **not retained**. The job records a `ResultRef`
(`storage="inline"`, content type, size, sha256) so the result can be checked later.
`storage="stored"` (a retrievable `location`) is reserved for async results (M4). Until then,
`GET /v1/jobs/{id}/result` answers 501.

## Measured usage

Usage is the work done, in units the capability defines. The default in M2 is **app-reported**:
`report_usage(actor, job_id, quantities)`. It is accepted once per job, only from the job's owner,
and only for a succeeded job. The value keeps its source (`app_reported`). Usage is never confused
with cost. Engine-side meters (`engine_meter`) are a later extension.

## Async jobs (reserved, M4 · ISS-26)

`JobRequest.mode="async"` → `jobs.submit` returns a `queued` job at once. A worker runs the same
single-shot paid call, the result is stored (`ResultRef.storage="stored"`), and the caller polls
`GET /v1/jobs/{id}` and `/result`. A client disconnect never cancels or re-runs the job.
Runner-level recovery handles (a runner's own submit/poll routes, declared per capability) are an
optional extension. Today `mode="async"` answers 501.

## Persistent sessions

Reserved for M4 (ISS-27). `sessions.open(actor, SessionRequest)` reserves a runner session through
the SDK (`reserve_session`), and the SDK funds it with time-based payments until it is closed. **The
engine proxies all traffic**: HTTP, SSE and WebSocket go through `Session.proxy_path`, with the actor
authenticated on every request. Clients never receive the runner's `app_url`. Each session is a job
whose cost grows while it is open (the same payment session, many signings), so its cost reaches
`corrected`. Limits: `max_duration_s`, `idle_timeout_s`, and concurrent sessions per actor.
Funding lives in the engine process, so an engine restart lets the session lapse. Persistent
sessions are distinct from continuous media streaming, which is stretch scope. Today
`POST /v1/sessions` answers 501.

## Deployment, performance and recovery assumptions

These are the explicit decisions for ISS-02.

- **No HA.** One engine process per database. Neither SQLite nor PostgreSQL implies multi-instance
  scheduling.
- **No exactly-once.** At-most-once dispatch per `operation_ref`. Paid-but-unknown is `uncertain`;
  the engine never retries it automatically.
- **No SLA, refunds or financial recourse.** Failures and charges are reported and classified (see
  [payments.md](payments.md#cost-classification)). The enterprise decides what to bill.
- **Performance:** admission limits bound concurrent dispatch. Discovery and cost sync run in the
  background (default 15 s and 30 s). There are no latency guarantees in M2.
- **No continuous streaming.** Incremental text and live media are stretch scope.
