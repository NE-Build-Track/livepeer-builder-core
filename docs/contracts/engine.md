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
   - **`payment`:** caller- or signer-side refusals: 401 or 403 (`not_authorized`) and 402
     (`allowance_exhausted`). The stored manifest becomes `unpaid` (D54), so it is never matched or
     billed, and the job ends `failed`. They do **not** fail over, because every orchestrator would
     fail the same way. A signer that cannot be reached, or fails, before anything is signed is
     `payment` too (`signer_unavailable`, D58): a refused connection or a DNS failure on any signer
     call, since such a request never reached the signer, or a 5xx, timeout or dropped connection at
     `/sign-orchestrator-info`, which signs nothing. Every runner shares the signer, so it does not
     fail over either. A failed write of the manifest before signing is also `payment`: nothing was
     signed. A signer 5xx, timeout or lost reply **while signing** is not a refusal: it is
     `payment_unknown` (`signer_unknown`), the manifest stays `unknown`, and the job ends
     `uncertain`. The cost stays `pending` until usage matches it.
2. **At most one dispatch per operation.** One `(application_id, actor_id, operation_ref)` dispatches at
   most one job that reaches a runner. A job that failed before any runner accepted the work may be
   dispatched again under the same reference (see [idempotency](#idempotency)).
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
   debit on it stays unattributed on the allocation and is never billed. So does a connection the
   signer refused, or a signer name that did not resolve (D58, Q3): aiohttp fails those before a
   byte of the request is written. A lost reply, a timeout, a dropped connection, a signer 5xx,
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

Final rules (D57; they replace D11's interim 409). A repeat is identified by
`(application_id, actor_id, operation_ref)`. The request's fingerprint covers `app`, `path`, `method`,
`payload` (or the raw body), `capability`, `model`, `content_type`, `headers` and `return_http_errors`.
Limits such as `timeout_s` and `max_attempts` may differ between repeats.

| The reference names a job that… | Answer | Dispatched? |
| --- | --- | --- |
| does not exist | Runs it | yes |
| exists with a different fingerprint | 409 `OperationConflict`, `code: operation_conflict` | no |
| is `running` | 409 `OperationInProgress`, `code: operation_in_progress`, `Retry-After: 1`, the job in the body | no |
| `succeeded`, or ended `uncertain` | 409 `OperationExists`, `code: operation_exists`, the job in the body | no |
| `failed`, and never reached a runner (below) | Runs a **new job** under the same reference | yes |
| `failed` in any other way | 409 `OperationExists`, `code: operation_exists`, the job in the body | no |
| no reference given | Runs it | yes |

Every 409 for a job carries the job's id in `Livepeer-Job-Id`. Admin commands (provision, fund,
grants, rotations, keys) use the same codes, with the command in the body instead of a job: see
[payments.md](payments.md#repeat-safe-commands). `OperationInProgress` is an `OperationExists`, so
a caller that catches `OperationExists` keeps working; branch on `code` to tell them apart.

**Never reached a runner** is a closed list. The job failed before dispatch (`no_offering`,
`no_capacity`, `not_provisioned`), or was `interrupted` by a restart before its first attempt (an attempt
is written before any runner is called), or every attempt was refused before the app ran: `refused` (the
orchestrator answered 503), an orchestrator 404 or 409, or a signer refusal (`payment`, or
`payment_refused` for this orchestrator). No attempt has `payment_sent`. `unreachable` and an unpaid
`timeout` are **not** on the list: a disconnect after the request went out is `unreachable`, and a free
runner may have run the work.

**Why a job that ran answers 409, not its result.** Immediate output is returned inline and not
retained ([results](#results)), so the engine cannot replay the first answer, and a 2xx with another
body would break a client that parsed the first. Read the job in the body (or `GET /v1/jobs/{id}`)
instead.

**Streaming** follows the same table. A repeat never streams again: while the stream is open the job
is `running`, and once it ended the job ran.

**How the store keeps it.** A partial unique index holds one job per reference among the jobs no retry
superseded, so concurrent duplicates are refused where the job is written, and a duplicate with
another body is `OperationConflict` there too. A re-dispatch is one transaction: a compare-and-set
marks the failed job `superseded_by` the new one (only if it is still `failed` and not superseded),
then inserts the new job with `retry_of` the old one. The old job keeps its reference for history. When
two repeats race, one wins and the other is answered from the winner (normally
`OperationInProgress`).

**Lifetime.** A reference lives as long as its job. `LPB_RETENTION_JOBS_DAYS` purges `succeeded` and
`failed` jobs, which frees their references; `uncertain` jobs are kept. A reference older than the
retention window may dispatch again, so use UUIDs or ULIDs.

### Failures

`Failure` has `kind`, `message`, `status_code`, `body`, `payment_sent` and, for a payment failure,
`reason` (D58). **Branch on `kind` for behavior** (it decides failover and cost) and on `reason` for
what to tell the builder. `reason` is set only on `payment` and `payment_unknown`; jobs stored before
D58 have none. For a payment failure `message` is the engine's own words ("the signer refused payment:
allowance exhausted (HTTP 402)") and `status_code` is the signer's status, when it answered; the
signer's URL and its own text go to the engine log only, and `body` is never the signer's
([payments.md](payments.md#who-answers-what)).

| `Failure.kind` | `reason` | Payment possible? | REST status | What it means · what to do |
| --- | --- | --- | --- | --- |
| `no_offering` | | no | 404 | No runner serves the app. Check the name against `GET /v1/offerings`; nothing ran, so the same `operation_ref` runs again |
| `no_capacity` | | no | 503 + `Retry-After` | The engine's admission limits are full. Retry after the header, with the same `operation_ref` |
| `not_provisioned` | | no | 402 | The actor has no allocation (`per_actor` mode). An operator provisions it; retrying alone does not help. Then the same `operation_ref` runs again |
| `refused`, `unreachable` (all runners) | | no | 503 + `Retry-After` | Every runner tried refused or could not be reached. Retry after the header. After `refused` nothing ran, so the same `operation_ref` runs again; after `unreachable` the request may have reached a free runner, so a repeat under the same reference is 409: read the job and send a new reference |
| `payment` | `not_authorized` | no | **403**, no `Retry-After` | The signer refused the payer: Batteries answered 401 (invalid or revoked key) or 403 (grant or allocation inactive, paused included, or a session binding mismatch). The manifest is `unpaid`. An operator checks the actor's allocation and key; retrying does not help |
| `payment` | `allowance_exhausted` | no | **402**, no `Retry-After` | The signer refused because the allocation is spent (Batteries 402). The manifest is `unpaid`. An operator funds the allocation; then the same `operation_ref` runs again ([idempotency](#idempotency)) |
| `payment` | `signer_unavailable` | no | 503 + `Retry-After: 10` | The signer could not be reached, or failed, before anything was signed: a refused connection or a DNS failure on any signer call (Q3), or a 5xx, a timeout or a dropped connection at `/sign-orchestrator-info`. A manifest already stored becomes `unpaid`. No other runner is tried, because they all share the signer. Retry after the header, with the same `operation_ref`; an operator checks the signer if it lasts |
| `payment` | none | no | 503 + `Retry-After: 10` | Any other payment failure: the manifest could not be stored before signing (nothing was signed), every runner was `payment_refused` (a signer 400 or 481, or a refresh past its budget), the signer refused with another status, or its identity reply was malformed. Retry after the header, with the same `operation_ref` |
| `payment_unknown` | `signer_unknown` | maybe | 503, no `Retry-After` | The signer was asked and gave no validated answer while signing (a 5xx, a timeout, a dropped or malformed reply): it may have charged. The job is `uncertain` and the cost `pending` until usage settles it. Do not retry blindly: a repeat under the same `operation_ref` is 409, and a new one could pay twice |
| `timeout` | | maybe → `uncertain` when paid, else `failed` | 504 | The runner did not answer in time; it may still have run the work. Read the job: when `uncertain`, wait for its cost before running it again. A repeat under the same `operation_ref` is 409, so a new attempt needs a new reference |
| `runner_rejected` (4xx) | | yes | the runner's own 4xx | The runner ran and refused the request; `failure.body` holds its words. Fix the request and send it with a new `operation_ref`. A paid attempt is `failed_after_payment`, never billable ([payments.md](payments.md#cost-classification)) |
| `runner_error` (5xx) | | yes | 502 | The runner failed after it ran, or the engine did (`engine error: …`). Not run again automatically: try later with a new `operation_ref`. A paid attempt is `failed_after_payment`, never billable |
| `interrupted` (restart) | | maybe | 502 | The engine stopped while the job ran; the next start resolves it `uncertain` when anything may have paid, else `failed`. Read the job: one with no attempt never reached a runner and runs again under the same `operation_ref`; otherwise treat it as `timeout` |
| `abandoned` (the caller stopped reading a stream) | | maybe → `uncertain` when paid, else `failed` | n/a: the response had already started | The caller left the stream before its end ([how a stream ends](#how-a-stream-ends)); the bytes it did not read are gone. Read the job for the outcome and cost. To get the result, run it again with a new `operation_ref` |

`not_provisioned` and `allowance_exhausted` are both 402: `kind` tells them apart. A 403 from
`POST /v1/jobs` is either the access policy (`AccessDenied`, no job in the body) or `not_authorized`
(the job in the body).

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

## Inputs and results in M3

What a builder sends, what comes back, and what the engine keeps (D59). The engine moves bytes between
the caller and a runner: it hosts no media and keeps no result in M3. Stored results are M4 (ISS-26).
The REST path through this section is [guides/rest-builder.md](../guides/rest-builder.md).

### Inputs

- A JSON `payload` (an object), or a `raw_body` of up to 8 MiB with its `content_type`
  ([opaque request bodies](#opaque-request-bodies-and-stream-headers)). **The whole request is
  limited** over REST: a `/v1/` body over `LPB_HTTP_MAX_REQUEST_BYTES` (default 16 MiB, which fits an
  8 MiB `raw_body` as base64) is refused with 413, `code: request_too_large` and `limit_bytes`, before
  the engine reads it into memory or checks the credential, whether it declares its `Content-Length` or
  streams without one. Routes outside `/v1/` (an operator's own, a persistent endpoint's proxy) are not
  limited by the engine. Embedded callers pass a `JobRequest` and are not limited.
- **A URL inside a payload is passed to the runner unchanged, and the runner fetches it.** The engine
  does not fetch, host, validate or rewrite media. The URL must be reachable from the runner, and stay
  so until the runner has read it (a signed URL's expiry included).
- Neither is kept: the job records a fingerprint of the request for [idempotency](#idempotency), not
  its body.

### Results

Immediate results are returned inline, **once**, and **not retained**. The job records a `ResultRef`
(`storage="inline"`, `content_type`, `size_bytes`, and the `sha256` of the bytes returned), so a
builder can check a copy it saved: hash the saved bytes and compare. A stream's `ResultRef` covers the
bytes the caller received ([how a stream ends](#how-a-stream-ends)). Save the result when the job
returns; the engine cannot give it again.

`storage="stored"` (a retrievable `location`) is reserved for async results (M4). Until then,
`GET /v1/jobs/{id}/result` checks the permission (403) and the owner (404 for another actor's job or a
missing one), then answers 501 with `milestone: "M4"` and `ticket: "ISS-26"`.

### Provider-hosted assets

Some runners answer with a URL to a file the model provider hosts, not the file itself: the FLUX
schnell runner returns `output.images[0].url`, a temporary fal URL. That URL belongs to the provider.
It expires on the provider's schedule, and the engine cannot reissue it: the engine never fetched the
file, and the `ResultRef` hashes the runner's answer (the JSON holding the URL), not the image. Once
the URL has expired:

- the job still reads `succeeded`, and its cost stands (the attempt is `delivered`);
- fetching the URL returns the provider's own error, not the engine's;
- nothing in the engine can reproduce the file.

**Save the asset when the job returns, or ask the runner for inline output where it offers one.** For
FLUX schnell, `sync_mode: true` in the payload returns the image as a data URI inside the answer, so
the `ResultRef` covers it and it lasts as long as your copy. An application that shows such a URL
should handle a failed load ("the provider no longer serves this file") instead of a broken image.

### Job history

`GET /v1/jobs` (`jobs.list_jobs` in-process) lists jobs **newest first**: an actor's own; for an admin
or an auditor, every actor's in its application ([access.md](access.md#answers)). `limit` is 1 to 500
(default 50). To page, pass `before`: the `created_at` of the last job on the previous page, as an
ISO-8601 time with its offset (`Z` or `+00:00`; percent-encode `+` in a URL). A time without an offset
is 422. Jobs created in the same microsecond share a `created_at`, so a page that ends between two of
them skips the second. `GET /v1/jobs/{id}` returns one job; another actor's job, or a missing one, is
404.

A job records its `state`, `failure`, `result` (a `ResultRef`, on success), reported `usage`, its
labels and its times. Each attempt records the runner and orchestrator it called, the allocation that
paid, the sessions the signer signed, `payment_sent`, its `outcome`, the status code and its times
([jobs and attempts](#jobs-and-attempts)). Neither records the request body or the result's bytes. The
cost is read beside the job (`GET /v1/jobs/{id}/cost`). `LPB_RETENTION_JOBS_DAYS` purges `succeeded`
and `failed` jobs; `uncertain` ones are kept ([persistence.md](persistence.md#retention)).

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
- **No exactly-once.** At most one job per `operation_ref` reaches a runner (a job that never did may
  be dispatched again, see [idempotency](#idempotency)). Paid-but-unknown is `uncertain`;
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
