# Streaming jobs: incremental runner responses through the engine

## Status

Owner: Mike. Ticket: Mike is filing it (new M3 ticket; touches ISS-14 immediate execution, ISS-20 REST,
ISS-28 failure classification). Branch `tasks/m3/mz/stream-support` off `releases/m3`. **Implemented and
shown live on 19 October; a review on 20 October found two blocking lifecycle bugs, fixed on 20 October
(D34–D39). Merged to `releases/m3` (PR #28, 21 October). Awaiting Mike's and Josh's review of the
engine code.** Since the dependency-injection refactor (PR #41), the entry point is the optional
capability `jobs.streaming.stream(...)` (`None` when the transport has no `open`, D33), and the stream
code lives in `services/streams.py`; the items below record the API as first built.
Requested by the first enterprise reference
app (ChatLPT, `NE-Build-Track/chatlpt-sample-enterprise-app`), whose design note is the origin of
this plan: `docs/design-docs/streaming-through-the-engine.md` in that repository.

## Goal

Let a caller receive a runner's response bytes as they arrive, in both integration modes, while the
engine keeps every rule the job contract fixes: failover only before payment, the attempt row written
before the call, accurate `payment_sent` and `manifest_ids`, idempotency by `operation_ref`, and cost
classification. The engine moves bytes with a content type; it never interprets the stream, so an
LLM's server-sent events, synthesized audio and ndjson progress all ride the same path.

## Acceptance

- [x] `JobService.stream(actor, request)` returns a `JobStream` (the `Job`, `status_code`,
      `content_type`, an async iterator of `bytes`, `aclose()`); `run` is unchanged.
- [x] `RunnerTransport.open(runner, request, credential)` returns a `RunnerStream` with
      `payment_sent` and `manifest_ids` (growing as the SDK signs further sessions); the SDK transport
      implements it over `call_runner(..., stream=True)`.
- [x] Failover happens only when `open` fails before payment with `refused`, `unreachable` or
      `payment_refused`; once `open` returns, the job is bound to that runner.
- [x] The attempt row is written before `open` and finished when the stream ends; `ResultRef`
      (`inline`, size, sha256) is computed from the bytes as they pass.
- [x] New outcomes and states behave per the table below, including the new attempt outcome
      `abandoned`; no migration is needed (`lpb_attempts.outcome` is a string column); `recover_interrupted()`
      treats a job left `running` with a stream as `uncertain`.
- [x] `JobRequest.idle_timeout_s` (gap between chunks) beside `timeout_s` (total). Default changed from
      none to 60 s on 20 October (D35).
- [x] REST: `POST /v1/jobs` with `Accept: text/event-stream` or `?stream=true` (a query parameter, not a
      body field: `JobRequest` rejects unknown fields) streams the body unbuffered with `Livepeer-Job-Id`
      in the headers. Superseded on 20 October by D37: headers wait for the first chunk, and a failure
      after it aborts the connection.
- [x] Cost sync attributes a streamed job's rounds exactly as today (many signings, one manifest);
      reconciliation balances in a live run; evidence page written
      ([2026-10-19-streaming-live](../../references/evidence/2026-10-19-streaming-live.md)).
- [x] `Runner` gains `version`, `gpu` and `metadata` from the discovery entry (pass-through; the
      engine never interprets `metadata`). ChatLPT showed on 19 October that the chat runner publishes
      `{"model_id": "Qwen3.8-27B", "max_context": 131072}` there.
- [x] `docs/contracts/engine.md` gains a "Streaming" section and the outcome table; `lpb gen-docs`
      regenerated; `docs/design-docs/m2-decisions.md` untouched, the decisions recorded in
      [m3-decisions.md](../../design-docs/m3-decisions.md) (D29–D33).
- [x] Unit tests with a scripted streaming transport that yields chunks and fails at chosen points;
      structure tests unchanged; coverage floors hold.
- [ ] Mike's review; Josh's review of the engine code.

### Review fixes (20 October, D34–D39)

- [x] **D34** Finishing a stream runs in its own task, so a client disconnect under Starlette/anyio, or any
      repeated cancellation, still writes the job and releases the slot. `LPB_STREAM_FINALIZE_TIMEOUT_S`
      (10 s) bounds the writes; a store failure releases the slot and leaves the job to `recover_interrupted`.
- [x] **D35** `timeout_s` counts from the job's start and covers opening; new `first_byte_timeout_s`
      (60 s, capped at `timeout_s`); `idle_timeout_s` defaults to 60 s. The SDK transport enforces the
      first-byte limit and keeps signed sessions; the engine's backstop treats a transport that ignores it
      as paid. A timeout never fails over. SDK issue drafted for the unbounded first read.
- [x] **D36** `JobStream` is an async context manager; `engine.stop()` drains open streams for
      `LPB_STREAM_DRAIN_TIMEOUT_S` (30 s), then abandons the rest; a stream nobody reads still ends at
      `timeout_s`; a failed write right after `open` closes the stream and keeps its manifests.
- [x] **D37** REST: headers wait for the first chunk (earlier failures are 504/502 with the job); a
      failure after it aborts the connection.
- [x] **D38** `JobStream.complete()` records success when the caller has what it needs; a cancelled
      reader or disconnected client gets `LPB_STREAM_SETTLE_S` (1 s) for the runner to end the stream;
      `aclose()` stops at once.
- [x] **D39** Session rows are append-only, so the end-of-stream write never resets a match.
- [x] `JobStream` exported from `livepeer_builder`; docs and generated files updated; 502 tests pass,
      including an ASGI disconnect test on both the cancel-scope (2.3) and send-raises (2.4) paths, which
      fail when finishing runs in the caller's task.
- [x] Live rerun on ChatLPT's payment stack: 16 funding rows under one session in a 55 s stream, a match
      made mid-stream kept by the end write (D39), a client disconnect through uvicorn releasing the slot
      after one funding row (D34), and `complete()` recorded as `delivered` (D38)
      ([evidence](../../references/evidence/2026-10-20-streaming-fixes-live.md)). Spend about $0.0021.
- [x] PR #28 review fixes: the result counts only bytes handed to the caller, never the read-ahead
      (`9e61cde`); `timeout_s` stops once the runner has ended the stream (`9e61cde`); closing or
      dropping the iterator abandons at once and a disconnect is said with `JobStream.disconnected()`
      (**D40**); `engine.stop()` waits for ends already being written. 508 tests pass.

## Design

### Contract

```python
class JobStream(Protocol):
    job: Job  # state "running" until the stream ends
    status_code: int
    content_type: str

    def __aiter__(self) -> AsyncIterator[bytes]: ...
    async def aclose(self) -> None: ...  # the caller gave up; see "abandoned"


class JobService:
    async def run(self, actor, request) -> JobResult: ...  # unchanged
    async def stream(self, actor, request) -> JobStream: ...  # new
```

Streaming is a delivery choice, not an execution mode: `mode` stays `immediate`. `async` (M4) is
untouched.

### Transport

```python
class RunnerTransport(Protocol):
    async def call(self, runner, request, credential) -> RunnerReply: ...  # unchanged
    async def open(self, runner, request, credential) -> RunnerStream: ...  # new


class RunnerStream(Protocol):
    status_code: int
    content_type: str
    manifest_ids: tuple[str, ...]  # every session signed so far, in order
    payment_sent: bool

    def __aiter__(self) -> AsyncIterator[bytes]: ...
    async def aclose(self) -> None: ...
```

`SdkRunnerTransport.open` wraps `call_runner(..., stream=True)`, which pays the 402 challenge before
any byte flows and keeps funding the session while the stream is open. `payment_sent` is therefore
known when `open` returns. `manifest_ids` must keep growing as the SDK signs further sessions; see
open question 1.

### Rules kept

| Rule | How |
| --- | --- |
| Failover only before payment | `open` raising `refused`, `unreachable` or `payment_refused` with `payment_sent=False` moves to the next runner; after `open` returns, bound |
| Attempt row before the call | written `in_flight` before `open`; finished when the stream ends |
| Result not retained | size and sha256 accumulate per chunk; `ResultRef(storage="inline")` at the end |
| Idempotency | unchanged; `operation_ref` dispatches once |
| Cost classification | unchanged; the session log already resolves many signings under one manifest (14 October evidence) |

### New rules

| Event | Attempt outcome | Job state |
| --- | --- | --- |
| Runner closes the stream normally | `succeeded` | `succeeded` |
| Runner errors mid-stream (reset, 5xx trailer) | `http` or `unreachable`, `payment_sent=True` | `uncertain` |
| No chunk within `idle_timeout_s` | `timeout`, `payment_sent=True` | `uncertain` |
| Total `timeout_s` exceeded | `timeout`, `payment_sent=True` | `uncertain` |
| Caller calls `aclose()` before the end | **`abandoned`** (new), `payment_sent=True` | `uncertain` |
| Engine dies mid-stream | `recover_interrupted()` as today | `uncertain` |

`abandoned` is honest: the work was paid for and probably ran; the caller stopped reading. An
application that wants the paid result even when its own client leaves (ChatLPT does) keeps reading
and never calls `aclose()`.

### REST

`POST /v1/jobs` with `Accept: text/event-stream` or `?stream=true` answers with the
runner's status and content type, `Livepeer-Job-Id` in the headers, and the body streamed unbuffered.
`GET /v1/jobs/{id}` shows the running job and, afterwards, the outcome.

### Files

| Area | Change |
| --- | --- |
| `contracts/jobs.py` | `AttemptOutcome` and `FailureKind` + `abandoned`; `JobRequest.idle_timeout_s`. `RunnerStream` lives in `protocols/`; `JobStream` is a service class |
| `protocols/__init__.py` | `RunnerStream`; `StreamingRunnerTransport` (optional `open`, D33) |
| `services/jobs.py` | `stream()`; shared dispatch with `run()` up to the call; a `_StreamingAttempt` that finishes the attempt and job when iteration ends, errors or is closed |
| `adapters/sdk.py` | `open` over `call_runner(stream=True)`, mapping `LiveRunnerCallStream` and keeping `payment_sent` / `manifest_ids` current |
| `http/app.py` | the streaming branch of `POST /v1/jobs` (`StreamingResponse`) |
| store | no migration: the outcome column is a plain string |
| `testing/fakes.py` | `ScriptedStreamTransport` |
| `docs/contracts/engine.md`, `docs/generated/` | streaming section; regenerated OpenAPI and schema |

### Open questions, resolved 19 October

All four were answered: 1 by reading the SDK (no change needed), 2–4 by Mike accepting the proposed defaults.

### The original questions

1. Does `LiveRunnerCallStream` expose the sessions it signs after the first, so `manifest_ids` stays
   complete for cost attribution? If not, the SDK needs `on_payment_session` to fire for ongoing
   funding (an SDK PR before this lands).
2. Should `abandoned` also be a `Failure.kind`, so REST clients see it on `GET /v1/jobs/{id}`?
3. Idle timeout default: none (today's behavior) or 60 s?
4. Should `Runner.metadata` be exposed as a parsed `dict` when it is JSON, or left as the string the
   node echoes? The plan says string; apps parse.

## Progress

- [x] 2026-10-19: plan drafted from ChatLPT's design note and the 14 and 19 October evidence.
- [x] 2026-10-19: Mike approved the design; defaults accepted: `abandoned` is also a failure kind, idle timeout
      defaults to none, `metadata` stays a string. Q1 settled by reading the SDK: ongoing funding re-signs the
      same payment session (one manifest per stream), so no SDK change is needed.
- [x] 2026-10-19: contracts, protocols (`RunnerStream`, optional `StreamingRunnerTransport`), `JobService.stream`
      and `JobStream`, the SDK transport's `open`, the REST streaming branch, runner `version`/`metadata`/`gpu`,
      scripted stream fakes and the outcome-table tests. No migration: `lpb_attempts.outcome` is a string column.
      472 tests, coverage 97.7%, floors held. Commit `ebedff1`.
- [x] 2026-10-19: live on the network: a stream read to the end (`succeeded`, 12 chunks in 1.3 s) and one
      abandoned (`uncertain`/`abandoned`), both costs attributed and matched, reconciliation balanced
      ([evidence](../../references/evidence/2026-10-19-streaming-live.md)). Spend about $0.00022.
- [x] 2026-10-20: review of `ebedff1`/`973ac57` (two blocking bugs: a disconnect left the job `running`
      and the slot held; nothing bounded the wait for the first byte). Mike locked seven changes one by
      one; implemented as D34–D39 with tests, docs and regenerated files.
- [x] 2026-10-20: live rerun of the fixes on ChatLPT's payment stack
      ([evidence](../../references/evidence/2026-10-20-streaming-fixes-live.md)).
- [x] 2026-10-20: automated review of `a89216d` (PR #28): `complete()` counted the read-ahead, and
      `timeout_s` could fail a runner that had finished. Fixed in `9e61cde`.
- [x] 2026-10-21: automated review of `9e61cde`: leaving the loop went through the settle window (true
      only without `async with`: inside it, `__aexit__` abandons first), and shutdown could close the
      store under a final write. Fixed with D40 and a shutdown wait.
- [x] 2026-10-21: SDK re-pinned to NE `dev/bytestreaming` `3238731` (payment session callback from
      [livepeer-python-gateway#81](https://github.com/livepeer/livepeer-python-gateway/pull/81) and
      streamable runner bodies on upstream `main`). The engine does not use streamable bodies yet.
      Adapter aligned with #81 (D41): `on_payment_session` is the sole source of `payment_sent` (no
      callback = `payment_sent=False` and job `failed`), and a missing payload is sent as `{}`.
- [x] 2026-10-22: SDK re-pinned from NE `dev/bytestreaming` `3238731` to livepeer/livepeer-python-gateway
      `main` `76a1a1c`. #80 and #81 are on `main`. D41 is unchanged.
- [x] 2026-10-27: SDK re-pinned to `main` `2017a80`. It adds the runner-side session stop
      ([livepeer-python-gateway#83](https://github.com/livepeer/livepeer-python-gateway/pull/83)), which
      the engine does not call. `call_runner(stream=True)` and its payment path are unchanged, so D41
      and T14 still hold.
- [x] 2026-10-27: the engine owns the paid runner call (D53). `SdkRunnerTransport.open` now answers the
      402 itself and signs through the SDK's signer client; the stream keeps its metered funding until it
      closes. T14 is closed.

## Decision log

- 2026-10-19: Streaming is designed generically (bytes plus a content type), not as an LLM feature.
  Source: the enterprise app owner's requirement that other applications need streaming too.
- 2026-10-19: `mode` stays `immediate`; streaming is a delivery choice. Source: keeps M4's async
  contract untouched and the idempotency and admission rules identical.
- 2026-10-20: The fixes stay generic (D29): the engine never learns a stream's format, so "the caller
  is done" comes from the caller (`complete()`) or from the runner closing within the settle window,
  never from parsing `[DONE]`. Source: Mike, review walkthrough.
- 2026-10-20: Defaults over required choices: every new deadline and setting has a default, so a builder
  only touches them when they need to. Source: Mike, review walkthrough.
- 2026-10-21: With the `3238731` SDK ([livepeer-python-gateway#81](https://github.com/livepeer/livepeer-python-gateway/pull/81)),
  signing runs before the paid runner request and `on_payment_session` fires only once a valid payment
  session is obtained, so no callback means `payment_sent=False` and the job ends `failed`; a missing
  payload keeps the `{}` body (D41). Source: PR #31.
- 2026-10-22: Drop the NE fork pin. Upstream `main` `76a1a1c` contains
  [#80](https://github.com/livepeer/livepeer-python-gateway/pull/80) and
  [#81](https://github.com/livepeer/livepeer-python-gateway/pull/81), so the engine pins that commit.
  Source: the fork and upstream `main` now carry the same callback and streamable-body behavior.
