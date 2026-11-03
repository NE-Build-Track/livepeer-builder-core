# ISS-46: The versioned integration events feed

**Status:** proposal for discussion, 2 November 2026. Nothing here is decided until it is locked; locked
rules become a decision in the M4 decision record.\
**Tracker:** ISS-46 ([Network-Engineering-SPE#51](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/51)),
moved out of ISS-18 ([#31](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/31)) when ISS-18
closed for M3 on 2 November.\
**Owner:** John (engine). **Reviewers:** Mike, John.\
**Verified against:** builder-core `releases/m3` at `1514212`; the delivery proposal
[builder-core#86](https://github.com/NE-Build-Track/livepeer-builder-core/issues/86) as of 2 November.

## Done when

- The engine serves a versioned feed of job, usage and cost events, with stable ids and replay and
  de-duplication semantics, scoped per application. A tenant application can read its own usage this
  way without a Batteries management credential.
- A reporting or delivery failure leaves a job's state and its result as they were. Network cost,
  settlement and retail billing stay distinguishable.
- **Depends on** ISS-18 (cost reports, [iss-18-cost-reports.md](../m3-guides/iss-18-cost-reports.md)).
  **External:** none. ISS-26, ISS-29 and ISS-32 each have a row that waits for this feed.

## Current state (verified)

| Item | Where | Today |
| --- | --- | --- |
| Contract | `docs/contracts/events.md` (D10) | `GET /v1/events?after=&limit=` returns `{events, next_cursor}`; five types; a retention window and **410** on an expired cursor; no replay from zero |
| `EngineEvent`, `EventPage` | `contracts/reserved.py:16-34` | Public and exported. `sequence`, `id`, `type`, `schema_version`, correlation fields, `payload` (the `Job`, or the `JobCost`) |
| `EventFeed` | `protocols/__init__.py:441` | `after(actor, cursor, limit)`. Only on `EngineApi` (`api.py:240`), not `ActorApi` |
| Placeholder | `services/reserved.py:9` | `ReservedEventFeed` raises `NotImplementedInMilestone(..., "held", "ISS-18")` → 501. **Names ISS-18, which is closed** |
| Route | `http/app.py:259` | `GET /v1/events`, `after` defaults to 0, `limit` 1–1000 (default 500) |
| Assembly | `runtime/assembly.py:261` | Falls back to `ReservedEventFeed` unless a caller supplies `events` |
| Permission | `contracts/access.py:19`, `services/policy.py:9-18`, `adapters/jwt.py:38` | `events.read` exists, the auditor holds it, and a JWT scope may grant it |
| `410` | `errors.py` | No error type for it (ISS-29 adds `code`; it wants `cursor_expired`) |
| FakeEngine | `testing/fake_engine.py:396` | Implements `ActorApi`; **has no `events` at all** |
| `store/sql.py` | 464 lines | The structure test caps a source file at 500 (`tests/structure/test_taste.py:9`) |

**Where each event type would be written.** Every path that changes a job already goes through one of
four store methods, so emitting there covers the services, streams and restart recovery alike:

| Event | Store method | Callers |
| --- | --- | --- |
| `job.created` | `create_job` (`store/sql.py:115`) | `services/operations.py:79,81`, including a D57 re-dispatch (`retry_of`), which supersedes the failed job in the same transaction |
| `job.finished` | `finish_job` (`store/sql.py:215`) | `JobService._finish` (`services/jobs.py:460`); streams ending in their pump task (`services/streams.py:395,416`); `recover_interrupted` (`services/failover.py:78`) |
| `usage.reported` | `set_usage` (`store/sql.py:232`) | `report_usage` only (`services/jobs.py:230`); usage is always `app_reported` today |
| `cost.observed`, `cost.corrected` | `save_projection` (`store/sql.py:322`) | `CostService._refresh_projection` (`services/costs.py:183`). The service picks `observed` or `corrected` (`costs.py:168-176`), but the store's guarded update can still write nothing when a newer evidence version is already stored |

**Two facts the design must handle.**

- `finish_job` takes ids and fields, not the `Job`, so a `job.finished` payload has to be read back in the
  same transaction.
- The events contract says an actor with `events.read` reads **its whole application's** events. D44
  says an ordinary actor reads only its own records (`services/ownership.py:23`: only admin and auditor
  see every actor). An end-user JWT given `events.read` would see every other user's jobs. See Q2.

## Decision proposed

**One append-only table, `lpb_events`, written in the same transaction as the change it records, and
read by `GET /v1/events`.** That is the pull feed the contract defines, and it meets the acceptance.
Push delivery (#86's CloudEvents worker) is a consumer of the same table, in its own slice (Q1).

### The event log

`lpb_events` (migration `0012`): `sequence` (integer, primary key), `id` (unique), `type`,
`schema_version`, `occurred_at`, `application_id`, `actor_id`, `job_id`, `operation_ref`, `payload` (JSON).
Indexes on `(application_id, sequence)` and `(application_id, actor_id, sequence)`. A one-row
`lpb_event_log` holds `next_sequence` and `floor` (the highest sequence retention has deleted).

**Sequence order equals commit order.** This is the property a cursor depends on, and an identity column
does not give it on PostgreSQL. Two transactions take 10 and 11; 11 commits first; a reader returns 11
and moves its cursor to 11; then 10 commits, and that reader never sees it. So the store allocates the
sequence by `UPDATE lpb_event_log SET next_sequence = next_sequence + 1 RETURNING ...` as the **last
statement before commit**. The row lock is held until commit, so a second writer waits, and sequences
become visible in order. SQLite has one writer already. The cost is that event-writing transactions
serialize on one row, for the length of a commit. The engine runs one process per database (T22) at
job rates, not request rates, so this is small; revisit if the lock wait shows in `finish_job` latency
(Q3).

**Stable ids, written once.** The id is deterministic, and the insert skips a row whose id exists:

| Type | `id` | Written when |
| --- | --- | --- |
| `job.created` | `job:{job_id}:created` | The job row is inserted |
| `job.finished` | `job:{job_id}:finished` | The job reaches `succeeded`, `failed` or `uncertain` |
| `usage.reported` | `job:{job_id}:usage` | `set_usage` stores the report (once per job) |
| `cost.observed` | `cost:{job_id}:v{evidence_version}` | The first stored projection with status `observed` |
| `cost.corrected` | `cost:{job_id}:v{evidence_version}` | A later projection whose fees or attempt classes differ from the stored one (Q8) |

`save_projection` emits only when its write changed the row: the guarded update or the insert. A
projection rewritten with the same content emits nothing, and a slower sync that loses to newer
evidence emits nothing. `pending` and `none` projections are not events.

**Payloads are snapshots.** `job.*` and `usage.reported` carry the `Job` as stored after the change,
attempts included, read back inside the transaction. `cost.*` carry the `JobCost`. `schema_version` is 1.
The feed holds no secret: requests are never stored, `Failure.body` carries no signer text (D58), and
`headers` reject credentials already.

### Reading

- `StoreEventFeed.after(actor, cursor, limit)` replaces `ReservedEventFeed`. The permission is checked
  first (D44): `events.read`, or 403.
- **Scope** (Q2, recommended): an ordinary actor reads its own actor's events. An auditor reads its whole
  application, and an admin reads every application. This is the rule `jobs.list` and `costs.recent`
  already follow.
- **Page:** `sequence > cursor`, in the caller's scope, in order, up to `limit`. `next_cursor` is the
  last sequence returned. On an empty page it is the highest sequence committed when the query ran, so
  a quiet tenant does not rescan everyone else's events. That is safe only because sequence order equals
  commit order.
- **Expired cursor:** `cursor < floor` raises `CursorExpired` → **410** with `code: "cursor_expired"`,
  `floor` and `head`. The contract's resync then works without a gap: read `head`, resync from
  `GET /v1/jobs` and `/v1/costs`, and continue from `head`. Events applied twice are dropped by `id`.
- A cursor of 0 on a log that has never been purged reads from the start. The feed starts at the
  migration, and older jobs get no backfilled events (Q7).

### Retention

`LPB_RETENTION_EVENTS_DAYS` (default: keep everything, like the other retention settings) deletes events
by `occurred_at` and raises `floor` in the same transaction. The job purge does not touch events,
because payloads are snapshots; an event can outlive its job, and the 410 path covers the reverse.
Q4 asks whether event retention must be at least job retention.

### Failure isolation

The event row commits with the change or not at all, so a lost event cannot happen and a phantom one
cannot either. A failure in the feed's read path never affects a job. Push delivery (Q1) runs in its own
task, with its own cursor per subscription. A failing collector raises lag in health; it never blocks a
job or another tenant's delivery.

### Push delivery (if Q1 brings it into M4)

#86's design, narrowed to a consumer of `lpb_events`:

- `lpb_event_subscriptions` per `application_id`: `target_url`, `event_types`, `auth_secret_ref`
  (a `SecretRef`, never the secret), `cursor_sequence`, and backoff state.
- A worker reads each subscription's events through the same scoped query, posts structured-mode
  CloudEvents v1.0, advances the cursor on 2xx, and honours `Retry-After`. Delivery is at least once,
  in `sequence` order per subscription. It signs with `Livepeer-Signature` (HMAC over the timestamp
  and body) and sends a bearer token from `SecretStore`.
- **Added to #86:**
  - **Target URLs:** HTTPS only, and DNS-resolved to a public address unless `LPB_EVENTS_ALLOW_PRIVATE`
    is set. Without this the engine can be made to call internal services (SSRF).
  - **Behind the floor:** a subscription whose cursor falls behind `floor` is `paused` and alerts. It
    never skips ahead silently.
  - **Repeated failure:** `paused` after N attempts, with the error kept on the subscription.
- **Corrections in #86:** the method is `save_projection`, not `save_projections`. And in #86 a
  `cost.corrected` payload carries the previous fees as well as the `JobCost`, which changes the
  contract's `payload` (Q5).
- **Out of scope for ISS-46:**
  - #86 task 6, decoupling the console from `CONSOLE_BATTERIES_*` (the console's ISS-34 work);
  - exposing grant balances on `GET /v1/admin/grants` (ISS-31's question).

## Code areas

| File | Change |
| --- | --- |
| `store/sql.py` | **Tidy first:** move the job and attempt writes into `store/jobs.py` (no behavior change); the file is at 464 of 500 lines |
| `store/schema.py`, migration `0012` | `lpb_events`, `lpb_event_log` (`lpb_event_subscriptions` in the push slice) |
| `store/events.py` (new) | `append(conn, ...)` (sequence last, skip on existing id), `events_after(scope, cursor, limit)`, `floor_and_head()`, `purge_before(cutoff)` |
| `store/jobs.py`, `store/sql.py` | `create_job`, `finish_job` (reads the job back), `set_usage`, `save_projection` append in their transaction |
| `store/retention.py`, `config.py` | `LPB_RETENTION_EVENTS_DAYS`; `PurgeReport.events_deleted` |
| `protocols/__init__.py` | `EventRepo` (`events_after`, `floor_and_head`); the `EventFeed` docstring loses "(M3)" |
| `contracts/reserved.py` → `contracts/events.py` | Move `EngineEvent` and `EventPage` out of "reserved", keeping the exports; `EventPage` unchanged |
| `services/events.py` (new) | `StoreEventFeed(store, policy)`; `services/reserved.py` keeps only sessions |
| `errors.py`, `http/app.py` | `CursorExpired` → 410 with `floor` and `head` |
| `runtime/assembly.py` | Build `StoreEventFeed` by default |
| `testing/fake_engine.py` | `events`, recording the same events with the same ids from the jobs it runs |
| `services/event_push.py` (push slice) | `EventPushWorker`: subscriptions, signing, backoff, health |
| Docs | `events.md` (scope, ids, 410 body, ordering, retention), `persistence.md#retention`, `RELIABILITY.md`, D10's row, the tech-debt row T7, generated schema and OpenAPI |

## Test plan (test-driven)

**Levels.**
- Store contract tests on **SQLite and PostgreSQL** (`tests/store/`, `LPB_TEST_POSTGRES_URL`) for
  append, ids, ordering, the floor and purge.
- Service tests through `make_engine` with the real `SqlStore` and the scripted transport
  (`tests/unit/test_events_feed.py`).
- One HTTP test per answer.
- FakeEngine parity in `test_fake_engine_contract.py`.

**Seams.**
- `FixedClock` for retention.
- A store wrapper that fails the write after the event append, to prove the rollback.
- Two PostgreSQL connections with a held transaction, to prove commit-order visibility.
- No mocks of contracts or the store.

**Starter test:** after one immediate job succeeds, the feed returns `job.created` then `job.finished`,
in order, with ids `job:{id}:created` and `job:{id}:finished`.

```text
Tidy first (no behavior change): job writes move to store/jobs.py; the whole suite stays green
[ ] one job: created then finished, in sequence order, stable ids, payload is the stored Job
[ ] a stream that ends in its pump task writes job.finished; so does recover_interrupted
[ ] a D57 re-dispatch: job.created for the new job; nothing new for the superseded one
[ ] report_usage writes usage.reported once; a refused second report writes nothing
[ ] first observed projection: cost.observed; a re-sync with the same content: nothing
[ ] newer evidence with different fees: cost.corrected; a slower sync losing to newer evidence: nothing
[ ] pending and none projections write no event
[ ] a failed write after the append rolls the event back with the change (both backends)
[ ] PostgreSQL: a later transaction cannot commit an earlier-visible sequence (two connections)
[ ] the same id twice writes one row
[ ] scope: an actor sees only its own events; an auditor its application; an admin all   [?] Q2
[ ] without events.read: 403 before any lookup
[ ] paging: limit honoured; next_cursor is the last sequence; an empty page returns head
[ ] retention: events older than the window go, floor rises; a job purge leaves events alone
[ ] cursor below floor: 410 cursor_expired with floor and head; the resync recipe loses nothing
[ ] FakeEngine: the same events, ids and scope as the real engine
[ ] push slice: delivered in order, cursor moves on 2xx only, Retry-After honoured, HMAC verifies  [?] Q1
[ ] push slice: private or non-HTTPS target refused; behind floor pauses; one tenant's failure delays no other
```

**Definition of done.**
- Every item is `[x]`, `[=]` or `[-]` with a reason, and every `[?]` is answered.
- The `AGENTS.md` checks are green: pytest, the coverage gates, diff-cover ≥ 90, ruff, mypy strict,
  `check_docs.py`, `gen-docs` and `lpb smoke`.
- `tests/store` is green on PostgreSQL.
- The docs above are updated, and the 501 no longer names ISS-18.

## Upstream dependencies

| Upstream | Needed? | Why |
| --- | --- | --- |
| livepeer/node | No | Every event is the engine's own record |
| livepeer-python-gateway | No | Not involved |
| clearinghouse-batteries | No | Cost events come from the engine's projections, filled by the existing usage sync. Batteries' global tokens (D23) are the reason a tenant needs this feed, not a dependency of it |

## Consumer impact

- **Builder console:** it can show a tenant's usage from the feed instead of Batteries. That is ISS-34
  work and optional for ISS-46.
- **ISS-32's fixture:** consumes `GET /v1/events` (its Q4).
- **ISS-29:** documents `EventPage` and the 410.
- **ChatLPT, inference-gateway:** none. Neither reads events. The new migration means a re-pin before
  either moves to a later builder-core.

## Alternatives considered

- **An identity column, read with a lag window.** Rejected: any fixed lag can still miss a slow commit,
  and it delays every reader.
- **PostgreSQL `xid8` snapshot visibility.** It works, but only on PostgreSQL; SQLite needs another path,
  and the code is harder to test. Keep it as the revisit if the counter row ever contends.
- **CDC on the internal tables.** Rejected, as #86 rejects it: it couples consumers to private schema and
  needs extra infrastructure per deployment.
- **Event-sourced costs** (projections derived from the log). Rejected in the ISS-35 guide. The log
  records changes; it is not the system of record.
- **Writing events from the services** after the store call. Rejected: it is a dual write, and a crash
  between the two loses the event.

## Open questions to lock

- **Q1.** M4 delivers the pull feed as its acceptance (recommended), with push as a separate slice that
  may follow. Or both pull and push in M4?
- **Q2.** An ordinary actor reads only its own events (recommended, D44), or its whole application as
  `events.md` says today?
- **Q3.** Allocate sequences with the counter row (recommended), or with a PostgreSQL advisory lock taken
  at the same point?
- **Q4.** Must `LPB_RETENTION_EVENTS_DAYS` be at least `LPB_RETENTION_JOBS_DAYS`, enforced at startup
  like `operation_stale_s`? Recommended: yes.
- **Q5.** A `cost.corrected` payload is the new `JobCost` only (recommended; consumers keep the previous
  one), or the `JobCost` plus the previous fees, as #86 proposes for append-only meters?
- **Q6.** ISS-26's async jobs: `job.created` at submit and `job.finished` at the end, with no new type
  (recommended), or add `job.started` (a `schema_version` question, not a breaking one)?
- **Q7.** No backfill for jobs that existed before the migration (recommended), or synthesize their
  events once?
- **Q8.** Does a change in attempt cost classes with the same fees count as `cost.corrected`?
  Recommended: yes. Today `_refresh_projection` marks `corrected` only when the fees change.

## Delivery slices

1. **Tidy:** split `store/sql.py`; move the event contracts out of `reserved`; no behavior change.
2. **Log:** migration `0012`, `store/events.py`, emission in the four write paths, ordering on both
   backends.
3. **Feed:** `StoreEventFeed`, scope, paging, retention and the 410; FakeEngine; docs. **ISS-46's
   acceptance is met here.**
4. **Push** (Q1): subscriptions, the worker, signing, the SSRF guard, health and the admin/CLI views.
