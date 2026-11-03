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
  de-duplication semantics. A tenant application reads its own application's events, and never
  another's, without a Batteries management credential. Within the application, who reads what is Q2.
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
  see every actor). An end-user JWT given `events.read` would see every other user's jobs. The contract
  also says an admin reads every application, while `jobs.list` and `costs.recent` keep an admin inside
  its own `application_id`. See Q2.
- `report_usage` checks "already reported" on the job it read, then `set_usage` updates unconditionally.
  Two concurrent reports both pass, and the second overwrites the first. The usage event needs the
  guard anyway (below), and it fixes this.
- `save_projection` can rewrite a job's cost without a new evidence version. It overwrites an equal
  version (`<=`), and `jobs_needing_projection` (`store/matching.py:68`) re-projects `pending` rows,
  rows with no attempts and rows stored before the job ended, all at the same version. Both SQLite and
  PostgreSQL report a matched row as updated even when no value changed.

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

**Stable ids, written once.** The id is deterministic, and the insert skips a row whose id exists, so
a retried write never emits twice:

| Type | `id` | Written when |
| --- | --- | --- |
| `job.created` | `job:{job_id}:created` | The job row is inserted |
| `job.finished` | `job:{job_id}:finished` | The job reaches `succeeded`, `failed` or `uncertain` |
| `usage.reported` | `job:{job_id}:usage` | `set_usage` stores the first report. It gains `WHERE usage IS NULL`; a second report updates nothing, emits nothing and answers `InvalidRequest` |
| `cost.observed` | `cost:{job_id}:r{revision}` | The first stored projection with status `observed` |
| `cost.corrected` | `cost:{job_id}:r{revision}` | A later projection whose fees or attempt classes differ from the stored one (Q8) |

**A cost event is a change in what the cost says, not a write.** The evidence version cannot key it,
because the same version is rewritten (above), and neither can the update's row count. So
`save_projection` reads the stored row, including its `revision` (a new column), and compares the two
costs' **content**: status, the job's fees, and for each attempt its cost class and its fees. Money is
compared as `Decimal` (the columns are text), and `updated_at` and `first_observed_at` are not content.
Fees are compared per attempt as well as in total because `billable` comes from the `delivered`
attempts: a rematch can move a fee between attempts and leave the total and the classes unchanged.

- **Unchanged:** the row may still be rewritten (`updated_at`), but `revision` stays and no event is
  written. This covers a re-sync, and `_refresh_projection` keeping the `corrected` label on unchanged
  fees (`costs.py:172`).
- **Changed:** the write is a compare-and-set on the revision it read:
  `UPDATE ... SET ..., revision = :read + 1 WHERE job_id = :id AND revision = :read AND
  evidence_version <= :version`. One row updated: the event is written in the same transaction, with id
  `cost:{job_id}:r{read + 1}`. The first such event for a job is `cost.observed`; every later one is
  `cost.corrected`. A repair that fills in missing attempts is therefore a correction, never a frozen
  first payload.
- **Zero rows updated:** another save got there first (the background sync and an operator's
  `costs/sync` can run at once), or newer evidence is stored. No event. The save reads again and
  compares again; it stops once newer evidence is stored. Two saves can never both write `r1`.
- `pending` and `none` projections are not events, and they leave `revision` at 0. A job's first
  `observed` projection is always `r1`, whether or not a `pending` row came first.

**Payloads are snapshots.** `job.*` and `usage.reported` carry the `Job` as stored after the change,
attempts included, read back inside the transaction. `cost.*` carry the `JobCost`. `schema_version` is 1.
The feed holds no secret: requests are never stored, `Failure.body` carries no signer text (D58), and
`headers` reject credentials already.

### Reading

- `StoreEventFeed.after(actor, cursor, limit)` replaces `ReservedEventFeed`. The permission is checked
  first (D44): `events.read`, or 403.
- **Scope** (Q2, recommended): an ordinary actor reads its own actor's events. An auditor or an admin
  reads every actor's events in its own application. That is the rule `jobs.list` and `costs.recent`
  follow: both query `actor.application_id` for every credential (`services/jobs.py:205`,
  `services/costs.py:199`). `events.md` says an admin reads every application; Q2 decides which rule
  the feed takes.
- **Page:** `sequence > cursor`, in the caller's scope, in order, up to `limit`. `next_cursor` is the
  last sequence returned. On an empty page it is the head (the highest committed sequence), so a quiet
  tenant does not rescan everyone else's events.
- **One snapshot.** The page and the head come from **one statement** (a CTE that reads
  `lpb_event_log` and the page together). Under PostgreSQL's READ COMMITTED, two statements are two
  snapshots: an event committed between them would be above the page but below the head, and
  `next_cursor` would skip it. Sequence order equals commit order, so within one snapshot no lower
  sequence can still be pending.
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

A job or usage event commits with its change or not at all, so it cannot be lost and cannot be a
phantom. A cost event is different. `apply_usage` and `match_sessions` commit new usage and raise the
evidence version in their own transactions, and the event waits for the `save_projection` that
follows. A crash between them **delays** the event; it does not lose it. Every sync re-projects every
out-of-date job (`jobs_needing_projection` includes `projection_out_of_date()` for all jobs, not only
the touched ones), and that save emits. Until then, `GET /v1/jobs/{id}/cost` computes the new fee live,
so it can be ahead of the feed by one sync interval. A failure in the feed's read path never affects a
job. Push delivery (Q1) runs in its own
task, with its own cursor per subscription. A failing collector raises lag in health; it never blocks a
job or another tenant's delivery.

### Push delivery (if Q1 brings it into M4)

#86's design, narrowed to a consumer of `lpb_events`:

- `lpb_event_subscriptions` per `application_id`: `target_url`, `event_types`, `auth_secret_ref`
  (a `SecretRef`, never the secret), `cursor_sequence`, and backoff state.
- A subscription delivers every actor's events in its `application_id`: the auditor-scope read, never
  another application's.
- A worker reads each subscription's events through that query, posts structured-mode
  CloudEvents v1.0, advances the cursor on 2xx, and honours `Retry-After`. Delivery is at least once,
  in `sequence` order per subscription. It signs with `Livepeer-Signature` (HMAC over the timestamp
  and body) and sends a bearer token from `SecretStore`.
- **Added to #86:**
  - **Target URLs (SSRF):** a tenant-supplied URL must not reach internal services. Checking the
    name once at registration is not enough: it can resolve elsewhere at delivery (DNS rebinding), and
    an HTTPS answer can redirect to `http://169.254.169.254/`. So the worker:
    - requires HTTPS;
    - resolves at each delivery and judges **every** address returned with discovery's
      `public_address` (`discovery/probe.py:47`). It already refuses what a list of ranges misses:
      IPv4 carried in IPv6 (mapped, NAT64 `64:ff9b::/96`, 6to4, IPv4-compatible), site-local
      `fec0::/10`, and shared CGNAT space, which holds a cloud metadata address (`100.100.100.200`).
      Python's `is_global` alone accepts `64:ff9b::a9fe:a9fe`, which is `169.254.169.254`;
    - refuses the target if any address fails, rather than dialing the ones that pass;
    - connects to the address it checked (pinned), not to the name again;
    - does not follow redirects: a 3xx is a failed delivery. Following them would need the same check
      on every hop.

    **Private targets** (a collector inside a local compose stack) are allowed only by the operator's
    deployment config, as an exact list (`LPB_EVENTS_PRIVATE_TARGETS`, scheme, host and port). They
    can never be set through the API, by an admin credential or anyone else.
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
| `store/schema.py`, migration `0012` | `lpb_events`, `lpb_event_log`, `lpb_cost_projections.revision` (`lpb_event_subscriptions` in the push slice) |
| `store/events.py` (new) | `append(conn, ...)` (sequence last, skip on existing id), `events_after(scope, cursor, limit)`, `floor_and_head()`, `purge_before(cutoff)` |
| `store/jobs.py`, `store/sql.py` | `create_job`, `finish_job` (reads the job back), `set_usage` (guarded on `usage IS NULL`, returns whether it stored), `save_projection` (compares content, compare-and-set on `revision`) append in their transaction |
| `store/retention.py`, `config.py` | `LPB_RETENTION_EVENTS_DAYS`; `PurgeReport.events_deleted` |
| `protocols/__init__.py` | `EventRepo` (`events_after`, `floor_and_head`); the `EventFeed` docstring loses "(M3)" |
| `contracts/reserved.py` → `contracts/events.py` | Move `EngineEvent` and `EventPage` out of "reserved", keeping the exports; `EventPage` unchanged |
| `services/events.py` (new) | `StoreEventFeed(store, policy)`; `services/reserved.py` keeps only sessions |
| `services/jobs.py` | `report_usage`: `set_usage` now returns whether it stored the report, and `report_usage` raises `InvalidRequest` when it did not, instead of returning the caller's copy |
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
[ ] two concurrent usage reports: one stored, one InvalidRequest, one event (fixes today's overwrite)
[ ] first observed projection: cost.observed r1, also after a pending row (r0); a re-sync: nothing
[ ] two concurrent saves of one change: one writes r1 and its event, the other re-reads and writes nothing
[ ] a fee moved between attempts, same total and classes: cost.corrected
[ ] a corrected label kept on unchanged fees: nothing
[ ] newer evidence with different fees: cost.corrected r2; a slower sync losing to newer evidence: nothing
[ ] same evidence version, attempts filled in by the repair: cost.corrected with the new attempts
[ ] crash after apply_usage, before save_projection: the next sync emits the cost event
[ ] pending and none projections write no event
[ ] a failed write after the append rolls the event back with the change (both backends)
[ ] PostgreSQL: a later transaction cannot commit an earlier-visible sequence (two connections)
[ ] the same id twice writes one row
[ ] scope: an actor sees only its own events; an auditor and an admin their application   [?] Q2
[ ] PostgreSQL: an event committed while a page is read is never skipped by next_cursor (one snapshot)
[ ] without events.read: 403 before any lookup
[ ] paging: limit honoured; next_cursor is the last sequence; an empty page returns head
[ ] retention: events older than the window go, floor rises; a job purge leaves events alone
[ ] cursor below floor: 410 cursor_expired with floor and head; the resync recipe loses nothing
[ ] FakeEngine: the same events, ids and scope as the real engine
[ ] push slice: delivered in order, cursor moves on 2xx only, Retry-After honoured, HMAC verifies  [?] Q1
[ ] push slice: non-HTTPS, private-resolving, rebinding and redirecting targets refused (pinned address)
[ ] push slice: NAT64, IPv4-compatible, site-local and CGNAT targets refused; one bad address refuses the name
[ ] push slice: a private target works only when listed in LPB_EVENTS_PRIVATE_TARGETS; no API sets it
[ ] push slice: behind floor pauses; one tenant's failure delays no other
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
- **Q2.** Scope. (a) An ordinary actor reads only its own events (recommended, D44), or its whole
  application, as `events.md` says? (b) An admin reads its own application, as the job and cost lists
  do (recommended), or every application, as `events.md` says? An operator-wide view would then need
  its own parameter, whose absence keeps the admin in its own application. That is the reverse of
  `GET /v1/admin/actors`, where an admin's default is every application and `application_id` narrows
  it (`services/payments.py:203`). The admin credential may act on any application (`DefaultPolicy`),
  so this default has to come from the feed's own query, as it does in `list_jobs`.
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
  Recommended: yes, and `_refresh_projection` labels it `corrected` too, so the projection and the
  event agree. Today it marks `corrected` only when the fees change.

## Delivery slices

1. **Tidy:** split `store/sql.py`; move the event contracts out of `reserved`; no behavior change.
2. **Log:** migration `0012`, `store/events.py`, emission in the four write paths, ordering on both
   backends.
3. **Feed:** `StoreEventFeed`, scope, paging, retention and the 410; FakeEngine; docs. **ISS-46's
   acceptance is met here.**
4. **Push** (Q1): subscriptions, the worker, signing, the SSRF guard, health and the admin/CLI views.
