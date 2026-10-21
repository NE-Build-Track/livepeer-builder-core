# Engine contract: discovery, jobs, results, usage

**Ticket:** ISS-02 (streaming: M3, [plan](../exec-plans/active/streaming-jobs.md)) · **Code:** `contracts/discovery.py`,
`contracts/jobs.py`, `services/discovery.py`, `services/jobs.py` · **Status:** immediate execution and streaming
implemented; async and persistent reserved.

Every model is a frozen pydantic v2 class. The same class is used in-process and as the REST
schema ([generated/openapi.json](../generated/openapi.json)), so the two integration modes cannot
drift. Times are UTC. Money is `Decimal`. Engine IDs are UUIDs.

## Discovery

`DiscoveryService` keeps a snapshot of the runners its `DiscoverySource` advertises. It is refreshed
every `discovery_interval_s` and counts as stale after three missed intervals. The default source
is the engine's own orchestrator list when `LPB_ORCHESTRATOR_LIST` is set: the engine calls
`GET /discovery` on every listed orchestrator and merges the ones that answer, and the signer only
pays (D42). Otherwise it is `LPB_DISCOVERY_URL` or the signer. Discovery and paid calls verify TLS
with the process trust store. An orchestrator whose certificate that store does not accept is left
out of the snapshot and is never called; a refresh fails, keeping the last good snapshot, only when
the list cannot be read or every orchestrator fails.

| Model | Meaning |
| --- | --- |
| `Runner` | One advertised runner: `url`, `app`, `mode` (`single-shot` or `persistent`), capacity, `rate`, and what the runner registered: `version`, `metadata` (up to 1 KB of app text, often JSON) and `gpu`. The engine never interprets `metadata` |
| `NetworkRate` | Wholesale network rate as advertised (`amount`, `currency`, `unit`). Not a retail price |
| `Offering` | One runner app: its modes, the cheapest and dearest comparable rate, runner and ready counts, `observed_at` |

Capability → app resolution (descriptor or configuration table) is a later contract. In M2 the caller
names the runner `app`; `capability` and `model` are labels only.

## Jobs and attempts

`JobRequest`: `app`, `path`, `method`, `payload`, `operation_ref`, `mode`, `max_attempts`,
`timeout_s` (the whole job), `first_byte_timeout_s` and `idle_timeout_s` (streams only, 60 s each; see
[Streaming](#deadlines-d35)), plus the labels `capability` and `model`. The SDK transport sends a
missing `payload` as the JSON object `{}` (D41).

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
`succeeded`, `refused`, `unreachable`, `payment_refused`, `payment`, `timeout`, `http`, `abandoned`), the status
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
| `abandoned` (the caller stopped reading a stream) | yes → job `uncertain` | n/a: the response had already started |

## Streaming

`jobs.stream(actor, request)` dispatches the same immediate job as `jobs.run` and hands the runner's
response back **as it arrives**: a `JobStream` with the `Job` (state `running` until the stream
ends), the runner's `status_code` and `content_type`, and an async iterator of `bytes`. The engine
moves bytes; it never interprets them, so server-sent events, ndjson and audio all ride the same
path (D29). Streaming is a delivery choice, not an execution mode: `mode` stays `immediate`.

```python
stream = await engine.jobs.stream(actor, JobRequest(app="example/chat", path="/v1/chat/completions"))
async with stream:  # leaving the block ends the stream, however the block ends
    async for chunk in stream:
        if my_format_says_done(chunk):
            await stream.complete()  # the job succeeded; stop the runner now
            break
        forward(chunk)
```

Everything up to the runner's answer is `run`: admission, idempotency, provisioning, failover. The
transport's `open` pays the 402 challenge before any byte flows, so `payment_sent` and the signed
`manifest_ids` are known when the stream opens and are **written on the attempt before the first
byte** (outcome `in_flight`, D31). A restart mid-stream therefore resolves the job as `uncertain`.
The admission slot is held until the stream ends.

### Deadlines (D35)

| Field | Default | What it bounds |
| --- | --- | --- |
| `timeout_s` | 300 s | The whole job, from the call to `jobs.stream` to the runner's last byte. It also ends a stream that nobody reads. Once the runner has ended the stream it no longer applies: a caller still handling the last chunks reads them and the end, and the job succeeds. |
| `first_byte_timeout_s` | 60 s | From each runner request to its first body chunk: payment, headers and the first chunk. Capped at `timeout_s`; setting it higher is rejected (422). |
| `idle_timeout_s` | 60 s | The longest gap between chunks once data flows. `null` turns it off. |

`first_byte_timeout_s` and `idle_timeout_s` measure the runner's silence, so they count only while
the caller waits for data; a slow reader is not the runner's fault. A timeout never fails over to
another runner, paid or not, exactly as for `run`. The SDK transport enforces the first-byte limit
itself and keeps the sessions signed so far: a timeout after `on_payment_session` has
`payment_sent=True` (job `uncertain`), while a timeout or failure during signing before
`on_payment_session` has `payment_sent=False` (job `failed`, D41). The engine stops a transport that
runs 5 s past the limit and treats the payment as sent, because it cannot know.

### How a stream ends

| Event | Attempt outcome | Job state |
| --- | --- | --- |
| The runner closes the stream normally | `succeeded` (a `ResultRef` for the bytes received) | `succeeded` |
| The caller calls `complete()` (D38) | `succeeded` (a `ResultRef` for the bytes the caller read, not the engine's read-ahead) | `succeeded` |
| The runner fails mid-stream (reset, 5xx trailer) | `http` or `unreachable`, `payment_sent` as known | `uncertain` when paid, else `failed` |
| A deadline above runs out | `timeout` | `uncertain` when paid, else `failed` |
| The caller calls `aclose()`, leaves `async with` before the end, or stops iterating (`break`, `return`, an error in the loop) | `abandoned`, at once | `uncertain` when paid, else `failed` (D30, D40) |
| The caller is cancelled, or calls `disconnected()` because its HTTP client went away | `succeeded` if the runner ends the stream within `LPB_STREAM_SETTLE_S` (the `ResultRef` then includes what it sent in that window), else `abandoned` | as above (D38) |
| `engine.stop()` while the stream is open | `abandoned` after `LPB_STREAM_DRAIN_TIMEOUT_S`; ends already being written finish first (D36) | `uncertain` when paid, else `failed` |
| The engine dies mid-stream | `recover_interrupted()` as today | `uncertain` |

The end is recorded **exactly once, by a task the caller's cancellation cannot stop** (D34), so a
disconnect under Starlette or anyio still writes the job and releases the slot. The writes have
`LPB_STREAM_FINALIZE_TIMEOUT_S`; if the store fails, the slot is released anyway, the job stays
`running`, and the next start resolves it. Reading a stream that has already ended yields nothing
when it succeeded or the caller stopped it, and raises `JobFailed` otherwise.

Session rows are append-only (D39): the end-of-stream write adds manifests signed after the first
byte and never resets a match cost sync made while the stream ran. A stream funded in several rounds
logs every round under one `payment_session_id`, as a buffered job does (20 October evidence: 16
rows for one 55 s stream, matched mid-stream and still matched after the end).

### REST (D37)

`POST /v1/jobs` with `Accept: text/event-stream` or `?stream=true` (not a body field) streams the
runner's body unbuffered, with `Livepeer-Job-Id` in the headers.

- **The headers wait for the runner's first chunk.** Any failure before it is an ordinary error
  response from the failure mapping above (504 for a timeout, 502 for a runner error) with the job.
  An empty body from the runner is a 200 with an empty body.
- **A failure after the first chunk aborts the connection**: the response never sends its closing
  chunk, so clients see a cut-off transfer (`fetch` rejects, curl reports a partial transfer, HTTP/2
  resets the stream). `GET /v1/jobs/{id}` carries the outcome. The engine writes nothing into the
  stream itself, because it does not know the format. uvicorn logs one line for the abort.
- A client disconnect gets the settle window: the REST layer calls `JobStream.disconnected()` when a
  send fails or is cancelled, however the server reports it (D40). An app serving a stream over its
  own HTTP layer should do the same; without it, a closed iterator is the caller stopping.

**Transports:** streaming needs a `StreamingRunnerTransport` (`open` beside `call`). The SDK
transport implements it; an enterprise transport without `open` makes `jobs.stream` answer 400.
`open` must honour `request.first_byte_timeout_s` and raise `RunnerCallError("timeout")` with the
payment evidence so far.

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
- **Streaming is per job, not continuous media.** `jobs.stream` delivers one runner response as it
  arrives (above); live media and persistent sessions remain M4 (ISS-27).
