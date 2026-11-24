# ISS-26: asynchronous jobs and durable recovery handles

## Status

- **Owners:** Mike decides and reviews; John owns the engine design.
- **Ticket:** ISS-26
  ([Network-Engineering-SPE#40](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/40)), amended
  on 13 November 2026. The first M4 engine ticket after the payment clearinghouse (ISS-31).
- **Guide:** [iss-26-async-jobs.md](../../design-docs/m4-guides/iss-26-async-jobs.md), with its current
  state brought up to date and its test plan marked.
- **Decisions:** D69 in [m4-decisions.md](../../design-docs/m4-decisions.md), and the guide's amendment of
  18 November 2026 (async jobs are the work half's; `tenant_id`).
- **Branch:** `tasks/m4/iss-26-async-jobs`, from P8's branch (#169) at `699d759`. One pull request.
- **Progress:** built and in review.

## Goal

A job outlives the request that created it. An async job is queued in the engine's own database, a
worker in the engine's process runs it through the same paid call as an immediate job, and its owner
reads its state and its stored result back by id. A disconnect or a restart keeps the job's identity and
recovery references and never starts a replacement execution.

## Acceptance

From ISS-26, as amended on 13 November 2026.

- [ ] A job outlives its original request and exposes durable status and an eventual result or
  failure, through the supported handle: the engine's own job id, `GET /v1/jobs/{id}` and
  `GET /v1/jobs/{id}/result` ([engine.md](../../contracts/engine.md#async-jobs)).
- [ ] A disconnect and a restart preserve the job's identity and recovery references without submitting
  a replacement execution ([restart and stop](../../contracts/engine.md#restart-and-stop)).

## Progress

- [x] **13 November 2026.** Q1 to Q8 are locked (D69).
- [x] **19 November 2026.** The tidy slice: the paid call, the job writes, the FakeEngine's modules and
  the cost class each move to a module of their own, with no behavior change.
- [x] **19 November 2026.** The queue and the results: `jobs.submit`, the worker, restart and stop, the
  queue's limits, stored results, the result route, the cancel of a queued job, health, migration 0016,
  and the FakeEngine's parity.
- [x] **19 November 2026.** Retention of stored results; the clearinghouse role refuses async jobs; a
  queued job on both sets of payment adapters; the client going away; the documents.
- [x] **20 November 2026, review fixes.** A submit stores its job and tells the worker in one task the
  request's cancellation cannot stop, so a job stored just before its request was cancelled is not left
  waiting for the next submit. A worker's step notes each claim as it is made, so a job claimed before a
  later claim failed still runs and gives its admission slot back. `LPB_ASYNC_RESULT_MAX_BYTES` bounds
  what a worker holds of a reply, not only what is stored: the reply is read in pieces through the new
  optional `BoundedRunnerTransport`.
- [x] **20 November 2026, review fixes, second pass.** A count of the queue is kept only if nothing newer
  is known when its answer arrives, so one that was being read while a job was queued no longer stores an
  empty queue over that job. A woken loop looks at the queue once whatever the count says: a submit and
  the end of a job are each a reason by themselves. A step cancelled while its claim was being written
  resolves that job at once (back to the queue, or `failed` after its third claim) instead of leaving it
  `running` until a restart; the admission slot was never lost there. A runner's error answer and its
  402 challenge are no longer read whole: 16 KiB of an error (its first 4096 characters are kept, as
  before) and 64 KiB of a challenge, on the call path immediate jobs and streams share.
- [x] **23 November 2026, carried forward.** The branch is on the direct-signer design and the roles
  (#183, #185). Migration 0016 follows the new 0015, credential custody. Async jobs stay the work
  half's: their routes are in the work group of the listener table, and an engine in its `clearinghouse`
  role queues none, starts no worker and serves none of them, with the transport that role is given
  too. An engine in its `work` role signs for a queued job at the published signer itself, at dispatch.
  An exit of `serve()` that does not wait for callers still stops the engine, so a running async job is
  drained or cancelled and its end written before the database closes
  (`tests/unit/test_async_forced_exit.py`).
- [ ] **ISS-26.** Built and in review: [#184](https://github.com/NE-Build-Track/livepeer-builder-core/pull/184).

## Decision log

- **13 November 2026, D69.** Q1 to Q8: a repeated submit returns the existing job; only a `running` job
  with no attempt is requeued, three times at most; a cancel for queued jobs only; a paid answer that
  could not be stored is `failed_after_payment`; stored inputs and results are not encrypted; the
  defaults; polling only; a job whose access key is revoked while it waits still runs. Source: Mike.
- **18 November 2026.** Async jobs are the work half's, and the actor's snapshot says `tenant_id`.
  Source: the guide's amendment (D82 to D86).
- **19 November 2026, where the guide is silent.** A cancelled job is `failed` with kind `abandoned`
  and reason `cancelled`, needs `jobs.run`, and answers 200 with itself when cancelled again; a repeat
  after a cancel or a queue expiry queues a new job (D57's closed list); an actor with no allocation is
  failed `not_provisioned` at submit, as `run` fails it; a job stopped before its first attempt goes
  back to the queue; the queue's limit is counted before the insert (T45); results retention alone
  purges first one interval after start, so the default settings add no write at start; `start()`
  counts the queue before the worker's loop runs. Source: the guide's rules, applied; for Mike's review.
- **20 November 2026, review of #184.** The result limit is applied while the reply is read. The bound
  reaches the transport through an optional protocol, `BoundedRunnerTransport` (`call_bounded`), as
  streaming reached it through `open` (D33): `RunnerTransport` and `call` are unchanged, a supplied
  transport without it is called as before, and an immediate job is read whole. `RunnerReply` gains
  `size_bytes` and `sha256`, optional. A reply over the limit is still read to its end, so the job
  keeps the true size and hash (D69 (4)). The submit's shield adds no idle query. Source: the review,
  applied; for Mike's review.
- **19 November 2026, not built.** No worker lease (T44): #112 left the detection of a second engine to
  ISS-26's leases, and the locked guide builds none. No events feed (ISS-46) and no provider request id
  (ISS-48); their seams are kept. Source: the guide and D64.
