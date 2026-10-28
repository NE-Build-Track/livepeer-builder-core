# Engine contract: discovery, jobs, results, usage

**Ticket:** ISS-02 (streaming: M3, [plan](../exec-plans/active/streaming-jobs.md)) · **Code:** `contracts/discovery.py`,
`contracts/jobs.py`, `services/discovery.py`, `services/jobs.py` · **Status:** immediate execution and streaming
implemented; async and persistent reserved.

Every model is a frozen pydantic v2 class. The same class is used in-process and as the REST
schema ([generated/openapi.json](../generated/openapi.json)), so the two integration modes cannot
drift. Times are UTC. Money is `Decimal`. Engine IDs are UUIDs.

## Discovery

`DiscoveryService` keeps a snapshot of advertised runners from its `DiscoverySource`. It is refreshed every
`discovery_interval_s` and counts as stale after three missed intervals. The default source,
`HttpDiscoverySource`, reads a livepeer-node `/discovery` inventory from `LPB_DISCOVERY_URL`: normally the
discovery service ([discovery.md](discovery.md)), which finds orchestrators on-chain or from a static list.
The signer only signs; it is not a discovery source (D43). With no `LPB_DISCOVERY_URL` the snapshot is
empty unless an embedder supplies a source.

`offerings(app=None)` is public: it takes no actor and checks no permission, and `GET /v1/offerings`
needs no key (D51, proposed, [#5](https://github.com/NE-Build-Track/livepeer-builder-core/issues/5)).
The handler returns the full `Offering`: modes, the rate range, `runner_count` and `ready_count`. It
has no runner URL. `runners(actor, app)` returns runner URLs and still checks `discover`.

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
| `failed` | It ended with a known outcome: nothing went out that might have run the work, or the runner answered with an error. After failing over, the failure is the last runner's. `failure.payment_sent` says whether any attempt was paid |
| `uncertain` | Payment went out (or may have) and the outcome is unknown. Never retried automatically |

An `Attempt` is one runner call. It records `allocation_id` (the allocation whose key paid),
`sessions` (one entry per session the signer signed, in order, each with a `manifest_id` and a
match of `unmatched`, `matched` or `ambiguous`), `payment_sent`, `outcome` (`in_flight`,
`succeeded`, `refused`, `unreachable`, `payment_refused`, `payment`, `timeout`, `http`, `abandoned`,
`payment_unknown`), the status
code and timestamps. The cost key is the matched `payment_session_id`, not the manifest.

### Behavior the contract fixes

1. **Fail over when another runner can do what this one did not (D52).** Runners are tried in
   selection order, at most `max_attempts` of them. A failed attempt decides what happens next
   (`services/failover.py`):

   | Attempt | Outcome | Next |
   | --- | --- | --- |
   | Not paid | `refused`, `unreachable`, `payment_refused`, or `http` 404 or 409 | Fail over |
   | Not paid | Anything else | `failed` |
   | Paid | `refused` (503), `payment_refused`, or `http` 404 or 409 | Fail over to **another orchestrator** |
   | Paid | `timeout`, `unreachable`, or `http` 502 or 504 | `uncertain`: the runner may have run the work |
   | Paid | `http` 500 or any other status, `payment` | `failed`: the runner ran and failed, or the request would fail, and be charged, everywhere |
   | Any | The signer gave no validated answer (`payment_unknown`) | `uncertain`: the signer may have charged, so no one else is paid |

   - **Another orchestrator.** After a paid failover, the engine skips every later runner on the
     orchestrator that took the payment, so a job never pays one orchestrator twice. A runner without
     an `orchestrator_url` is matched by its own URL's origin, since a Live Runner is reached through
     its orchestrator. A skipped runner does not count against `max_attempts`.
   - **The operator caps paid failover (D55).** `LPB_MAX_PAID_ATTEMPTS` (default 2, 1 to 10) is how many
     paid attempts a job may fail over from. Once that many have, no further attempt starts, because
     the next could be paid too: the job is `failed` with the last runner's words, and
     `jobs.paid_attempt_cap` is logged. Each of those payments is `failed_after_payment`, never
     billable, so the operator pays for them; the caller's `max_attempts` cannot raise the cap. Unpaid
     failovers do not count. `1` turns paid failover off.
   - **404, 409 and 503** come from the orchestrator refusing the request before the app ran: the
     runner is gone or stopped its session (livepeer-python-gateway#83), the session already exists, or
     the capacity was taken ([sdk-gateway.md](../references/sdk-gateway.md#runner-session-stop-83)). A
     500 is the app's own error after it ran; a 502 or 504 from the orchestrator's proxy can come while
     the app still runs. Neither runs the work again.
   - **The charge stays attributed.** A paid attempt the job failed over from keeps its sessions and is
     `failed_after_payment`: the allocation paid it, and it is never `billable`
     ([payments.md](payments.md#cost-classification)).
   - **Every runner failed over.** The job is `failed` with the last runner's status code and body. A
     404 or 409 is reported as `refused` (503, retry later), so the caller never reads an orchestrator's
     refusal as its own not-found or conflict.
   - **`failure.payment_sent`** is true when any attempt of the job was paid, not only the last.
   - **A restart or an engine error** leaves the job `uncertain` only when its last attempt was still in
     flight, ended `payment_unknown`, or was paid and did not fail over. Attempts it failed over from ended with a known outcome.
     An attempt still in flight whose stored manifests were all refused, with nothing paid, is the
     exception (D54): the job is `failed`. An attempt in flight with no stored manifest stays
     `uncertain`, because it may predate D54.
   - **A cancel while a refusal is being stored** (the caller went away just after the signer refused)
     ends the job `failed`, its cost `none` and the manifest `unpaid`, which is all correct. The
     job's `failure` still reads `runner_error` ("engine error: CancelledError"), and the attempt stays
     `in_flight`, because the refusal never reached the attempt's own record.
   - **No runner selected.** A `SelectionPolicy` that returns no runner fails the job `no_offering`,
     before any payment.
   - **`payment_refused`:** the signer refused to pay *this orchestrator*. The signer statuses are 400
     (incomplete or expired payment params) and 481 (price above the session ceiling). A
     `SignerRefreshRequired` (HTTP 480) that uses up the payment retry budget is the same refusal:
     unpaid, and not `payment_unknown`. A refresh that still has budget asks the runner for a fresh
     challenge and does not fail the attempt. Another orchestrator may succeed.
   - **`payment`:** caller- or signer-side refusals, such as 402 (allowance exhausted) and 403.
     The stored manifest becomes `unpaid` (D54), so it is never matched or billed, and the job
     ends `failed`. They do **not** fail over, because every orchestrator would fail the same way.
     A failed write of the manifest before signing is also `payment`: nothing was signed. A signer
     502 or 503 is not a refusal: it is `payment_unknown`, the manifest stays `unknown`, and the job
     ends `uncertain`. The cost stays `pending` until usage matches it.
2. **At most one dispatch per operation.** One `(application_id, actor_id, operation_ref)` dispatches at
   most once (see [idempotency](#idempotency)).
3. **The attempt row is written before the call** (`in_flight`) and updated after it. **The manifest is stored before
   signing** (D54): the Live Runner transport calls the recorder's `remember(manifest_id)` with
   `LivePaymentSession.manifest_id`, which stores it on the attempt as a session whose `payment_state` is
   `unknown`, in a write the caller cannot cancel. If that write fails, nothing is signed: the attempt
   ends unpaid (`payment`) and the job is `failed`. A cancel or the open deadline during that write
   signs nothing either: once the write ends, the session is stored `unpaid`, so the job is `failed`
   with no cost. **`on_signed` makes the session `paid`** once the
   signer validates the manifest, before the paid request goes out, and the engine stores
   `payment_sent` then (D42). An expired challenge (a 480 refresh), `SkipPaymentCycle`, and a signer
   400, 401, 402, 403 or 481 make it `unpaid`: it is never matched to usage and costs nothing, so a
   debit on it stays unattributed on the allocation and is never billed. A lost reply, a signer 5xx,
   or a cancel during `get_payment` is not a refusal: the session stays `unknown`, and the job pays no
   other orchestrator. The cost stays `pending` until usage matches that manifest, which confirms it
   `paid`; then the attempt is `uncertain` with the fee. This holds across a crash, because the
   `unknown` session is stored before the signer is asked: a restarted job is `uncertain` with its
   cost `pending`, never `none` at 0.
   A cancel, a disconnect or a crash during
   the paid request, after `on_signed`, keeps the charge attributed to its job. If that write fails, the call goes on, and the write after the call, or the
   close after an error or cancel, stores the same sessions again. A report that arrives after the job
   ended increments the job's evidence version, so its stored cost projection is out of date and the
   next read and sync count it. If the engine dies mid-call, `recover_interrupted()` marks the job
   `uncertain` when anything may have paid, and `failed` otherwise.
4. **Admission and provisioning are checked before any payment.** The admission limits
   (`max_concurrent_jobs`, `max_concurrent_jobs_per_app`) refuse with 503 and `Retry-After`. An
   unprovisioned actor fails with `not_provisioned`.
5. **A cost that hasn't arrived is `pending`.** So is a job still running, which may yet pay. Only a job
   that ended without reaching payment has cost status `none`.

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
| `payment` (402 or 403: the signer refused, and the manifest is `unpaid`; or the manifest could not be stored, so nothing was signed) | no | 503 + `Retry-After` |
| `payment_unknown` (the signer was asked and did not refuse) | maybe | 503, no `Retry-After` |
| `timeout` | maybe → `uncertain` when paid, else `failed` | 504 |
| `runner_rejected` (4xx) | yes | the runner's own 4xx |
| `runner_error` (5xx) | yes | 502 |
| `interrupted` (restart) | maybe | 502 |
| `abandoned` (the caller stopped reading a stream) | maybe → `uncertain` when paid, else `failed` | n/a: the response had already started |

## Streaming

`jobs.streaming.stream(actor, request)` dispatches the same immediate job as `jobs.run` and hands the runner's
response back **as it arrives**: a `JobStream` with the `Job` (state `running` until the stream
ends), the runner's `status_code` and `content_type`, and an async iterator of `bytes`. The engine
moves bytes; it never interprets them, so server-sent events, ndjson and audio all ride the same
path (D29). Streaming is a delivery choice, not an execution mode: `mode` stays `immediate`.
`jobs.streaming` is `None` when the configured transport cannot stream (D33); the REST route then
answers 400.

```python
stream = await engine.jobs.streaming.stream(
    actor, JobRequest(app="example/chat", path="/v1/chat/completions")
)
async with stream:  # leaving the block ends the stream, however the block ends
    async for chunk in stream:
        if my_format_says_done(chunk):
            await stream.complete()  # the job succeeded; stop the runner now
            break
        forward(chunk)
```

Everything up to the runner's answer is `run`: admission, idempotency, provisioning, failover (a paid
refusal at open moves to another orchestrator, as for `run`). The
transport's `open` pays the 402 challenge before any byte flows, so `payment_sent` and the signed
`manifest_ids` are known when the stream opens and are **written on the attempt before the first
byte** (outcome `in_flight`, D31). Those ids are the validated signatures only. Ending the stream
keeps the sessions the recorder already merged, and appends any signed id that is not already there,
instead of replacing that list. A restart mid-stream therefore resolves the job as `uncertain`.
The admission slot is held until the stream ends.

### Deadlines (D35)

| Field | Default | What it bounds |
| --- | --- | --- |
| `timeout_s` | 300 s | The whole job, from the call to `jobs.streaming.stream` to the runner's last byte. It also ends a stream that nobody reads. Once the runner has ended the stream it no longer applies: a caller still handling the last chunks reads them and the end, and the job succeeds. |
| `first_byte_timeout_s` | 60 s | From each runner request to its first body chunk: payment, headers and the first chunk. Capped at `timeout_s`; setting it higher is rejected (422). |
| `idle_timeout_s` | 60 s | The longest gap between chunks once data flows. `null` turns it off. |

`first_byte_timeout_s` and `idle_timeout_s` measure the runner's silence, so they count only while
the caller waits for data; a slow reader is not the runner's fault. A timeout never fails over to
another runner, paid or not, exactly as for `run`. The Live Runner transport enforces the first-byte
limit itself and keeps the sessions signed so far: a timeout after the signer's payment was validated
has `payment_sent=True` (job `uncertain`). A timeout or other non-refusal during signing leaves the
stored manifest `unknown`, and the cost stays `pending` until usage matches it; the job is
`uncertain` (D53, D54). An explicit signer refusal makes it `unpaid`, which costs nothing, and so
does a timeout while the manifest is being stored, before the signer is asked (job `failed`). The engine
stops a transport that
runs `LPB_STREAM_OPEN_GRACE_S` (default 5 s) past the limit and treats the payment as sent, because it
cannot know.

### How a stream ends

| Event | Attempt outcome | Job state |
| --- | --- | --- |
| The runner closes the stream normally | `succeeded` (a `ResultRef` for the bytes received) | `succeeded` |
| The caller calls `complete()` (D38) | `succeeded` (a `ResultRef` for the bytes the caller read, not the engine's read-ahead) | `succeeded` |
| The runner fails mid-stream (reset, 5xx trailer) | `http` or `unreachable`, `payment_sent` as known | `uncertain` when paid, else `failed` |
| A deadline above runs out | `timeout`, or `payment_unknown` when it runs out while signing | `uncertain` when paid or `payment_unknown`, else `failed` |
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
transport implements it. With a transport that has no `open`, `jobs.streaming` is `None` in-process and
the REST streaming route answers 400 (D33).
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
- **Streaming is per job, not continuous media.** `jobs.streaming.stream` delivers one runner response as it
  arrives (above); live media and persistent sessions remain M4 (ISS-27).

## Opaque request bodies and stream headers

`JobRequest` accepts `raw_body: bytes` instead of `payload`, with an explicit `content_type`.
The raw body is limited to 8 MiB. Empty bytes are an explicit empty body, distinct from the
legacy omitted JSON payload. The engine's runner call (`adapters/runner_call.py`, D53) sends
these bytes unchanged, with that content type, on the unpaid request and on the paid retry
after the 402. For example:

```python
request = JobRequest(
    app="inference/runner",
    path="/v1/chat/completions",
    model="served-model",
    raw_body=b' {"model":"served-model","messages":[]} ',
    content_type="application/json",
)
```

In the stock REST JSON request envelope, `raw_body` uses URL-safe base64, as generated in
OpenAPI. Embedded Python callers use bytes directly. Request bodies are not added to job
records. The operation fingerprint includes a raw-body digest/length, content type and
normalized headers; legacy JSON fingerprints are unchanged. A changed body under the same
operation reference remains an operation conflict. No database migration is needed.

`headers` is an immutable tuple of name/value pairs for injected transports. Names are
normalized and sorted; duplicate names, control characters, credentials, Livepeer control
headers and HTTP framing headers are rejected. Limits are 32 fields and 16 KiB of names and
values. Use `content_type` rather than a Content-Type header. The runner call sends these
headers only to the runner, on every request of the call; an `accept` header replaces the
default `*/*`. Signer credentials and the payment headers stay the transport's.

`RunnerStream.headers` and `JobStreamApi.headers` expose response header pairs to embedded
callers, preserving repeated fields as the runner sent them. Custom stream implementations must provide
this property (an empty tuple when no headers exist). The shipped fakes support it. These
headers are untrusted; applications must filter cookies, credentials, hop-by-hop fields and
content framing before forwarding them to another client. The stock REST stream keeps its
existing header policy. Buffered SDK results do not expose a new header guarantee.

`return_http_errors=True` is opt-in and requires `jobs.streaming.stream` (or the REST stream
option). Failover is unchanged (D56): an answer that fails over for a default call (503, 404,
409, under D52 and the `LPB_MAX_PAID_ATTEMPTS` cap) still fails over while another runner can
be tried. The answer the engine stops at, and any other non-402 answer, is returned as
status, headers and original bytes. A completed response with status >= 300 records an HTTP
attempt (`refused` for 503) and a failed job; a paid 502 or 504 leaves the job `uncertain`, as
for a default call, since the app may still be running. The job's failure is recorded as a
raised runner error would be: a 404 or 409 is `refused`, and `payment_sent` is true when any
attempt was paid, including one the job failed over from. It never becomes a successful
inference. A timeout, cancellation or interrupted body retains existing uncertain/abandoned
semantics. Error bodies are not added to durable failure records. Payment challenges and
signer failures remain control flow, not client responses. In this mode the runner call turns
off transparent decompression and asks for `Accept-Encoding: identity`; callers must interpret
any content encoding returned anyway. Redirects are never followed, and no paid request is
ever sent twice: aiohttp's resend of an idempotent request on a dropped connection is off for
every runner request (D56).

The engine sends the runner request itself (D53), so no SDK change is needed: the SDK stays
pinned to upstream `2017a80`. An earlier draft pinned NE-Build-Track SDK commit `cd6ed5e`
([SDK PR #6](https://github.com/NE-Build-Track/livepeer-python-gateway/pull/6)); that PR is
replaced by this one. No streaming uploads or provider-specific request processing is added.
