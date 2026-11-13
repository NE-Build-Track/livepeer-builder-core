# ISS-28: Failure classification and recovery across baseline modes

> **Questions locked on 12 and 13 November 2026.** Mike answered every open question; see [Questions
> locked](#questions-locked). Where an answer differs from the proposal below, the answer wins, and that
> section says which part it replaces. Decision numbers are assigned in the M4 decision record.

**Status:** proposal for discussion, 29 October 2026. Nothing here is decided until it is locked; locked
rules become a decision in the M4 decision record.\
**Tracker:** ISS-28 ([Network-Engineering-SPE#42](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/42)).\
**Owner:** John (engine). **Reviewers:** Mike, John.\
**Verified against:** builder-core `releases/m3` at `d3df806`; livepeer/node `406b248`;
livepeer-python-gateway `2017a80`; consumers ChatLPT and inference-gateway at their working trees on
29 October.

## Done when

- Validation, access, capacity, execution, payment and reporting failures are distinguishable in every
  baseline mode (immediate, streamed, async, persistent). Evidence is kept for a timeout, a restart, an
  uncertain outcome and a failed final save.
- Recovery checks existing work before any supported retry and never infers success from payment alone.
  Generic cancellation, automatic failover and exactly-once execution are **not promised**.
- **Depends on** ISS-15, ISS-26 ([iss-26-async-jobs.md](iss-26-async-jobs.md)) and ISS-27 (persistent
  sessions; #73 is its design). Builds on the M3 guides for ISS-12
  ([iss-12-payment-failures.md](../m3-guides/iss-12-payment-failures.md), `Failure.reason`), ISS-14
  ([iss-11-14-idempotency.md](../m3-guides/iss-11-14-idempotency.md), the retryable rule). #72's engine funding loop is not
  coming (closed as not planned; upstream as livepeer-python-gateway#87), so T17 reads the SDK's
  `run_payments()` result.

"Not promised" does not mean "not done": D52 failover stays, best effort, inside one dispatch.

## Current state (verified)

**Failures with no job record** (refused at the boundary, nothing written, nothing paid):

| Category | Error | REST | Where |
| --- | --- | --- | --- |
| Validation | pydantic validation, `InvalidRequest` | 422, 400 | contracts, `http/app.py` `simple` map |
| Access | `NotAuthenticated`, `AccessDenied`, `NotFound` (ownership, D44) | 401, 403, 404 | `services/ownership.py` |
| Capacity | `NoCapacity` (admission) | 503 + `Retry-After` | `services/admission.py`; `run` admits before `create_job` (`services/jobs.py:121`) |
| Idempotency | `OperationExists`, `OperationConflict` | 409 | `services/jobs.py:257` |

**Failures on a job** (`Failure.kind`, `contracts/jobs.py:142`; REST map `http/app.py:67-79`):
`no_offering`, `no_capacity`, `not_provisioned`, `refused`, `unreachable`, `payment`, `payment_unknown`,
`timeout`, `runner_rejected`, `runner_error`, `interrupted`, `abandoned`. Each attempt has an
`AttemptOutcome` (`contracts/jobs.py:93`); `services/failover.py` turns it into a verdict (`failover`,
`failed`, `uncertain`) and a kind (`failure_kind`, `failure_of`). `engine.md#failures` tabulates them.

**Recovery today.** `recover_interrupted` (`services/failover.py:59`) runs once at start: every `running`
job becomes `uncertain` when `payment_unresolved` (the last attempt in flight, `payment_unknown`, or paid
and not a failover), else `failed`, kind `interrupted`. The engine never retries a job by itself; the
caller retries through `operation_ref` (interim 409 until ISS-14 lands).

**Streaming end states** (D34–D40, `engine.md#how-a-stream-ends`): runner end or `complete()` →
`succeeded`; runner error, deadline, `aclose()`/break, disconnect past `LPB_STREAM_SETTLE_S`,
`engine.stop()` past the drain → `uncertain` when paid, else `failed`; a crash → `recover_interrupted`.
A failed finishing write leaves the job `running` for the next start (D34).

### Gaps

| # | Gap | Evidence |
| --- | --- | --- |
| G1 | **No category or recovery on the record.** Consumers rebuild them by hand, and disagree | ChatLPT `_UNAVAILABLE_KINDS` (`adapters/engine.py:56`) and treats any `timeout` as uncertain, paid or not (`:370`); inference-gateway repeats the set inline (`backends/livepeer.py:252`) |
| G2 | **Three payment refusals read the same** (401, 402, 403 → `payment`, 503) | ISS-12 guide; `reason` not built yet |
| G3 | **A failed final save becomes `interrupted` with no trace of why.** If `finish_job` fails after a 2xx, `run` raises, the job stays `running`, and the restart makes it `uncertain`/`interrupted`. The `succeeded` attempt row is the only evidence, and nothing points at it | `services/jobs.py:121-131`, `_close_after_error`; `services/failover.py:41-50` |
| G4 | **That same job's cost is `delivered`** (billable) although the caller got an error: the class follows the attempt outcome, not the job | `services/costs.py:367-368`; `payments.md#cost-classification` |
| G5 | **T17:** a metered stream the orchestrator released can end `succeeded` | tech-debt T17; the SDK's `run_payments()` already returns `True` on a release, and the engine drops it |
| G6 | **Unpaid `timeout` and `unreachable` cannot be told apart from "never reached a runner"**, so neither is retryable under ISS-14 (its Q2) | `services/failover.py:18-19`; `network_kind` in `adapters/runner_call.py` |
| G7 | **Reporting failures have no job-level signal.** Cost sync errors are only `EngineHealth.cost_sync_error`; `quarantined` usage rows and `ambiguous` matches (T13, T16) show only as `pending`/`uncertain` cost | `contracts/health.py`, `contracts/costs.py:17,54` |
| G8 | **Async and persistent rows do not exist** | ISS-26 reserved; `services/reserved.py` `ReservedSessions` |
| G9 | `finish_job` overwrites any state (`store/sql.py:260`); a late writer could reopen an ended job. A concrete case (found for ISS-46, at `1514212`, where the update is `store/sql.py:229-230`): a cancel that lands after a `succeeded` finish has committed but before `run` returns reaches `_close_after_error`, which finishes the job again as `failed` or `uncertain` | No guard on `state` |

## Decision proposed

**Keep `kind` for behavior, add two derived fields for meaning.** `kind` (and ISS-12's `reason`) stays
the stored fact. A pure function maps the stored facts (state, failure, attempts) to:

- `category`: `validation`, `access`, `capacity`, `execution`, `payment`, `reporting`;
- `recovery`: what is safe next.

| `recovery` | Meaning | Who acts |
| --- | --- | --- |
| `retry` | Nothing reached a runner and nothing was paid. Repeating with the **same** `operation_ref` re-dispatches (ISS-14) | Caller, after `Retry-After` |
| `resume` | The engine continues by itself (an async job requeued after a restart) | Nobody |
| `check` | It may have run or been paid. Read the job and its cost; do not resubmit blindly. A new reference is new paid work | Caller, then operator if cost stays `uncertain` |
| `fix_request` | The request is wrong for this runner or the engine | Caller |
| `operator` | An operator must act (fund, re-authorize, provision, fix configuration) | Operator |
| `final` | It ran and failed. A new reference is new paid work, at the caller's choice | Caller |

Both are **computed, never stored** (a pydantic `computed_field` on `Job`), so they appear in REST and
OpenAPI, need no migration, and can be corrected later without rewriting history. Errors with no job
record get the same two words in the problem body.

### The classification

| Kind (`reason`) | Category | Job | `recovery` | Modes |
| --- | --- | --- | --- | --- |
| boundary 400/422 | validation | none | `fix_request` | all |
| 401, 403, ownership 404 | access | none | `fix_request` (401: re-authenticate) | all |
| admission `NoCapacity`, queue full | capacity | none | `retry` | immediate, streamed, async submit |
| `no_offering` | capacity | failed | `retry` | all |
| `no_capacity` (all busy; async `queue_expired`) | capacity | failed | `retry` | all |
| `refused` (every runner, nothing paid) | capacity | failed | `retry` | all |
| `refused` after a paid failover | capacity | failed | `final` | all |
| `unreachable`, unpaid | execution | failed | `check` (G6; `retry` once split, Q2) | all |
| `timeout`, unpaid / paid | execution | failed / uncertain | `check` | all |
| `runner_rejected` (runner 4xx) | validation | failed | `fix_request` | all |
| `runner_error` (5xx) | execution | failed | `final` | all |
| `abandoned` | execution | failed / uncertain | `check` when paid, else `final` | streamed, persistent |
| `abandoned` (`released`, T17) | execution | uncertain | `check` | streamed, persistent |
| `interrupted` (`restart`) | execution | failed / uncertain | `check` when paid, else `final` | immediate, streamed, persistent |
| `interrupted` (`restart`), async, no attempt | execution | queued | `resume` | async |
| `interrupted` (`final_save_failed`) | reporting | uncertain | `check` | immediate, streamed, async |
| `result_not_stored` (ISS-26) | reporting | failed | `check` | async |
| `not_provisioned` | payment | failed | `operator` | all |
| `payment` (`not_authorized`, `allowance_exhausted`) | payment | failed | `operator` | all |
| `payment` (`signer_unavailable`) | payment | failed | `retry` | all |
| `payment_unknown` (`signer_unknown`) | payment | uncertain | `check` | all |
| cost `pending` past a threshold, `ambiguous`, `quarantined` | reporting | unchanged | `operator` (reconcile) | all |

`reason` generalizes ISS-12's `PaymentReason` into `FailureReason`, adding `restart`, `final_save_failed`,
`released`, `queue_expired` and (if Q3 of ISS-26 says yes) `cancelled`.

### Evidence the engine must keep

- **Timeout:** the attempt keeps outcome `timeout`, `payment_sent`, sessions (D35, D54). Exists.
- **Restart:** the last attempt stays `in_flight` with its sessions; the failure says `restart`.
- **Failed final save (G3):** `recover_interrupted` gives `final_save_failed` when the last attempt is
  `succeeded`: the runner answered, the engine lost the record. State `uncertain`, never `succeeded`
  (the caller may never have received it). The cost class follows the job (G4, Q1).
- **Uncertain:** never retried by the engine; usage that later confirms a payment (D54 (2)) changes the
  cost, **never the job state**. A test pins this ("success is not inferred from payment").
- **Every transition is guarded** (G9): `finish_job` updates only `running` (and ISS-26's `queued`)
  rows; a guarded miss is logged, not raised. ISS-46 writes `job.finished` in the same transaction, only
  when the guarded update wins ([iss-46-events-feed.md](iss-46-events-feed.md)).

### Recovery checks existing work first

The engine retries nothing on its own beyond D52's failover inside a dispatch and ISS-26's requeue of a
job that never started. A caller's retry goes through `operation_ref`: the engine reads the existing job
first and re-dispatches **only** when `recovery == "retry"`, which is exactly ISS-14's
"never reached a runner" rule, now one function (`services/recovery.py`) shared by the classifier, the
re-dispatch, ISS-26's requeue and the FakeEngine.

### Persistent sessions (ISS-27)

From #73's session end table: signer refused before reserve → `payment`, `operator` or `retry` by
reason; reserve refused after signing → D52 failover, then `refused`; client closed or limit reached →
`succeeded`; runner released → `succeeded` if frames flowed, else `failed`, `final`; connection lost
after signing → `uncertain`, `check`; engine restart → the session lapses, `interrupted`/`restart`,
`check`. **Never `resume`:** funding lives in the engine process (`engine.md#persistent-sessions`).

## Code areas

| File | Change |
| --- | --- |
| `contracts/jobs.py` | `FailureCategory`, `Recovery`, `FailureReason`; `Failure.reason`; computed `Job.category`, `Job.recovery` |
| `services/recovery.py` (new, pure) | `classify(job) -> (category, recovery)`; `never_reached_runner(attempts)` (from ISS-14) |
| `services/failover.py` | `recover_interrupted` sets `reason` (`restart`, `final_save_failed`) and leaves ISS-26 requeue to its caller |
| `services/streams.py`, `adapters/runner_call.py` | T17: keep the funding task's `run_payments()` result; a stream that ends after a release (`True`) ends `uncertain`, `abandoned`/`released` |
| `services/costs.py` | Cost class of a `succeeded` attempt on a job that did not succeed: per Q1 |
| `store/sql.py` | `finish_job` state guard |
| `http/app.py` | `category` and `recovery` in every problem body; a table test keeps REST status and `Retry-After` consistent with `recovery` (today `unreachable` sends `Retry-After: 5`; it stays only if Q2 makes the connect phase `retry`) |
| `errors.py` | Boundary errors carry `category` and `recovery` |
| `testing/fake_engine.py` | Same classification through `services/recovery.py` |
| `docs/contracts/engine.md` | One "Failures and recovery" table replacing the Failures table, per mode |
| `docs/exec-plans/tech-debt-tracker.md` | Close T17 |

## Test plan (test-driven)

**Levels.** The classifier is a pure function, tested as a table (one row per line of the
classification above), plus a property test with Hypothesis (already used in `test_properties.py`):
any job with a paid attempt is never `retry`, any `uncertain` job is `check`. Job-level tests produce
each kind through the scripted transport and assert the computed fields; restart tests reuse
`test_durability.py`; one HTTP test per category; FakeEngine parity.

**Seams.** The scripted transport and loopback signer (`tests/unit/livepeer_net.py`); a store wrapper
that fails `finish_job` once (failed final save); `FixedClock`; the SDK's `PAYMENT_INTERVAL_S` (monkeypatched).

**Starter test:** an unpaid `no_offering` job is `capacity` / `retry`.

```text
[ ] no_offering → capacity / retry
[ ] every runner refused, nothing paid → capacity / retry; after a paid failover → final
[ ] runner 4xx → validation / fix_request; runner 5xx → execution / final
[ ] paid timeout → uncertain / check; unpaid timeout after send → failed / check; connect-phase → retry (Q2)
[ ] signer 402 and 403 → payment / operator; signer 5xx before signing → payment / retry (ISS-12)
[ ] payment_unknown → uncertain / check
[ ] property: a job with any paid attempt is never retry; every uncertain job is check
[ ] restart with an in_flight paid attempt → interrupted/restart, uncertain, check
[ ] failed final save after a 2xx: restart gives interrupted/final_save_failed, uncertain, attempt kept succeeded
[ ] failed final save: cost class uncertain (Q1)
[ ] usage confirming an unknown session leaves an uncertain job uncertain (no success from payment)
[ ] finish_job on an ended job changes nothing (store, both backends)
[ ] stream: each D34–D40 end gives the documented category and recovery
[ ] T17: a metered stream released by the orchestrator, then ended cleanly → uncertain / released
[ ] async: queued job across a restart → resume; result_not_stored → reporting / check   (ISS-26)
[ ] persistent: each #73 session end → the row above                            [?] blocked on ISS-27
[ ] a same-ref retry re-dispatches only when recovery == retry (ISS-14)
[ ] boundary errors: 400/401/403/422/503 bodies carry category and recovery
[ ] REST: every recovery == retry answer has Retry-After; no check or operator answer has one
[ ] FakeEngine classifies every row identically
[ ] engine.md's table lists every kind and reason (doc test)
```

**Definition of done.** List empty, `[?]` answered; `AGENTS.md` checks green (pytest, coverage gates,
diff-cover, ruff, mypy strict, `check_docs.py`, `gen-docs`) and `tests/store` on PostgreSQL; `engine.md`,
`payments.md`, OpenAPI, T17 closed; ChatLPT and inference-gateway switched to `recovery`.

## Upstream dependencies

| Upstream | Needed? | Why / workaround |
| --- | --- | --- |
| livepeer/node | No | The 404/409/503 refusal semantics at `406b248` are what D52 already uses. Not vendorable (server) |
| livepeer-python-gateway | No (vendored) | T17 needs only `run_payments()`'s return value (`True` = released), which the SDK already gives. Q2's connect-phase split is in the engine's own runner call (D53) |
| clearinghouse-batteries | No | Reporting categories read what cost sync already stores |

## Consumer impact

| Consumer | Change |
| --- | --- |
| Builder console | Show category and the recovery hint on the job view; "check" jobs link to the cost |
| ChatLPT | Replace `_UNAVAILABLE_KINDS` and the timeout rule with `recovery` (fixes unpaid timeout shown as uncertain) |
| inference-gateway | Map `retry` → unavailable, `check` → error with job id; drop the inline kind set |

## Alternatives considered

- **More `kind` values instead of `category`/`recovery`.** Rejected: `kind` drives failover and cost; the
  advice is a view over it and should be fixable without a migration.
- **Store `recovery` on the record.** Rejected: a later rule change would leave old rows wrong.
- **Engine-side automatic retries of `check` jobs.** Rejected: paid, maybe ran; exactly-once is not
  promised, and the pinned node gives no handle to ask.

## Questions locked

Locked by Mike on 13 November 2026.

- **Q1, the cost of a job whose runner answered but which did not succeed:** the cost class follows the
  job. `final_save_failed` is `uncertain`; `result_not_stored` is `failed_after_payment` (ISS-26). One
  rule: a `succeeded` attempt is `delivered` only when its job succeeded.
- **Q2, connect-phase failures:** split in M4. A failure in the connect phase (DNS, TCP, TLS or a connect
  timeout) with nothing paid is `retry`, and a repeat under the same `operation_ref` dispatches again. A
  failure after the request went out stays `check`. A closed list: only an error the engine positively
  identifies as a connect failure is `retry`. This adds one entry to D57's "never reached a runner" list.
- **Q3, a runner's own 4xx:** category `validation`, recovery `fix_request`. **To be revisited when the
  runner standard is complete** (Mike: "We need clear and concise error reporting"). Known weak spot until
  then: a runner 401, 403 or 429 after payment also reads `fix_request`. The revisit belongs with ISS-48.
- **Q4, a cost that stays `pending` too long:** health gains a count of ended jobs whose cost has been
  `pending` longer than a threshold, and the age of the oldest. Reconciliation lists them as `reporting` /
  `operator`. The job's state and cost class do not change. The threshold is a setting, proposed default
  one hour after the job ended.

**Other decisions that touch this guide.**

- **`cancelled`** is a reason, because ISS-26 adds a cancel for queued jobs.
- **Zero-capacity runners** (#114): a job refused by every runner fails `refused`, and `no_capacity` means
  only the engine's own admission limits. The classification table reads accordingly.
- **A streamed answer's header send** moves inside the guarded block (#113), so a disconnect before the
  headers settles like any other streaming end.
- **The set-aside usage rows** of #114 add a count to health, beside the pending-cost count of Q4.
- **REST problem bodies** are RFC 9457 (ISS-29), and they carry `category` and `recovery` as extension
  fields.
- **Parts of this guide are already built:** `Failure.reason` (D58) and the ISS-14 repeat rules (D57). The
  current-state section predates both.

### Where the runner standard plugs in later

Runner contract v1 (ISS-47) is not vetted yet. This ticket therefore routes by app ID, as today, and reads
no v1 metadata. ISS-48 adopts the standard as the last feature of M4, and it may move to M5.

- **Working assumption for ISS-48 (12 November):** app IDs stay the routing key; v1 metadata describes a
  runner and never routes; missing, old or unreadable metadata is ignored and logged; model and profile
  are filters; no schema check.
- **Criterion moved to ISS-48 on 13 November:** runner failures map to the standard's base error kinds
  through the registry entry; retries follow each kind's rule, only where the application opts in, and
  never when the entry says the caller owns attempts; streamed results are judged by the entry's success
  rule.
- **The seam to keep:** `services/recovery.py` is the one classifier, so the standard's error kinds map
  into it in one place. That is also where the category of a runner's 4xx (Q3) is revisited.

## Delivery slices

1. `services/recovery.py`, the computed fields, the table and property tests (immediate and streamed).
2. Evidence fixes: `finish_job` guard, `final_save_failed`, Q1's cost class.
3. T17 from the SDK loop's result; the stream rows.
4. Async rows with ISS-26; persistent rows with ISS-27; docs and consumers.
