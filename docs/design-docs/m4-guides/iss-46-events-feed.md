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
| `job.created` | `create_job` (`store/sql.py:115`) | `services/operations.py:79,81`, including a D57 re-dispatch (`retry_of`), which supersedes the failed job in the same transaction. The `Job` contract has no `retry_of` or `superseded_by`, so the payload cannot name the failed job, and the failed job gets no event; a consumer links the two by their shared `operation_ref`. `GET /v1/jobs` has the same gap |
| `job.finished` | `finish_job` (`store/sql.py:215`) | `JobService._finish` (`services/jobs.py:460`); streams ending in their pump task (`services/streams.py:395,416`); `recover_interrupted` (`services/failover.py:78`) |
| `usage.reported` | `set_usage` (`store/sql.py:232`) | `report_usage` only (`services/jobs.py:230`); usage is always `app_reported` today |
| `cost.observed`, `cost.corrected` | `save_projection` (`store/sql.py:322`) | `CostService._refresh_projection` (`services/costs.py:183`). The service picks `observed` or `corrected` (`costs.py:170-176`), but the store's guarded update can still write nothing when a newer evidence version is already stored |

**Facts the design must handle.**

- `finish_job` takes ids and fields, not the `Job`, so a `job.finished` payload has to be read back in the
  same transaction. It also updates **unconditionally** (`store/sql.py:229-230`). If a cancel lands
  after a `succeeded` finish has committed but before `run` returns (`services/jobs.py:366`), the
  `BaseException` branch (`jobs.py:131-134`) calls `_close_after_error`, which finishes the job again as
  `failed` or `uncertain` (`jobs.py:438-447`). The row ends `failed` after a success. This happens today,
  without events. ISS-28 (G9, [iss-28-failure-recovery.md](iss-28-failure-recovery.md)) already proposes the
  guard: `finish_job` updates only a job that has not ended (`running`, or ISS-26's `queued`).
- `save_projection` receives only a `JobCost`. A cost event's correlation fields (`application_id`,
  `actor_id`, `operation_ref`) come from the job, so the store reads them inside the transaction.
- **Backup and restore** are supported (`store/backup.py`, `lpb restore` for SQLite; `pg_restore` for
  PostgreSQL). A restore brings back an older log, so its sequence numbers fall below cursors consumers
  already hold.
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
Indexes on `(application_id, sequence)` and `(application_id, actor_id, sequence)`. `job_id` has **no
foreign key**: the schema's convention is `ondelete="CASCADE"` (`store/schema.py:105,205`), which would
let the job purge delete events below no floor. Payloads are snapshots, so an event outlives its job.

Two one-row tables, seeded by the migration (an `UPDATE` of a missing row matches nothing, and would
silently write no events):
- `lpb_event_log`: `next_sequence` (1) and `epoch` (a random id; see Restore below).
- `lpb_event_floor`: `floor` (0), the highest sequence retention has deleted. Keeping it apart from the
  allocator's row means a purge never makes a writer wait.

`occurred_at` is the time of the change the event records: `created_at` for `job.created`,
`completed_at` for `job.finished`, `reported_at` for `usage.reported`, and the projection's
`updated_at` for cost events. It is for consumers; ordering is by `sequence` alone, and the two need not
agree.

**Sequence order equals commit order.** This is the property a cursor depends on, and an identity column
does not give it on PostgreSQL. Two transactions take 10 and 11; 11 commits first; a reader returns 11
and moves its cursor to 11; then 10 commits, and that reader never sees it. So the store allocates the
sequence by `UPDATE lpb_event_log SET next_sequence = next_sequence + 1 RETURNING next_sequence - 1`,
then inserts the event row with that value, the id before the increment, as its `sequence`. These are
the **last statements before commit**. The first event is sequence 1, and the head (the highest
sequence assigned) is always `next_sequence - 1`, which is 0 on an empty log. The row lock is held
until commit, so a second writer waits, and sequences become visible in order. SQLite has one writer
already. The cost is that event-writing transactions serialize on one row, for the length of a
commit. The engine runs one process per database (T22) at job rates, not request rates, so this is
small; revisit if the lock wait shows in `finish_job` latency (Q3).

**Stable ids, written once.** The id is deterministic, and the insert skips a row whose id exists, so
a retried write never emits twice:

| Type | `id` | Written when |
| --- | --- | --- |
| `job.created` | `job:{job_id}:created` | The job row is inserted |
| `job.finished` | `job:{job_id}:finished` | The job reaches `succeeded`, `failed` or `uncertain`. `finish_job` gains ISS-28's guard (G9: only `running`, or ISS-26's `queued`) and emits only when it updates one row, so a second finish changes nothing and emits nothing |
| `usage.reported` | `job:{job_id}:usage` | `set_usage` stores the first report. It gains `WHERE usage IS NULL`; a second report updates nothing, emits nothing and answers `InvalidRequest` |
| `cost.observed` | `cost:{job_id}:r{revision}` | The first stored projection with status `observed` |
| `cost.corrected` | `cost:{job_id}:r{revision}` | A later final projection whose content differs from the last cost event (Q8) |

**A cost event is a change in what a final cost says, not a write.** The evidence version cannot key it,
because the same version is rewritten (above), and neither can the update's row count. A cost's
**content** is the job's fees and each attempt's cost class and fees. Each `Fees` compares `fee_eth` and
`fee_usd` as `Decimal` (the columns are text), plus `row_count`. A `fee_usd` of `None` (a row with no USD
price) is never equal to zero. **`status` is not content:** `observed` and `corrected` are the label this
write assigns, not an input to it. `classify` always returns `observed` for a final cost
(`services/costs.py:363-371`), so comparing status with a `corrected` event would mint a new
correction on every save. `updated_at` and `first_observed_at` are not content either. Fees are compared
per attempt as well as in total, because `billable` comes from the `delivered` attempts: a rematch can
move a fee between attempts and leave the total and the classes unchanged.

**The stored row is not always the last thing published.** `jobs_needing_projection` rewrites every ended
`pending` projection on each sync, and its matched fees grow while a sibling attempt waits (`classify`,
`services/costs.py:304`). A session recorded after the job was `observed` bumps its evidence
(`record_payment_evidence`, `store/sql.py:194-202`), and the next compute is `pending` again. So a final
save compares its content with the **last cost event**, `cost:{job_id}:r{revision}`, read by its unique id.
It does not compare with the stored row, which may hold an interim `pending` compute.

`save_projection` reads the row, including `revision` (a new column, 0 until the first event). The
migration sets `revision` to 1 on rows already `observed` or `corrected`, with no event. Those costs
were final before the feed existed, so their next change is a correction, not a first observation. Every
write is conditional on that read: `WHERE job_id = :id AND revision = :read AND evidence_version <=
:version`. Then one of four branches, by the new projection:

| New projection | Write | Event | `revision` | Stored `status` |
| --- | --- | --- | --- | --- |
| Not final: `pending` or `none` | Fees, attempts and status in place | None | Unchanged: a first insert stays 0, and a row at `r1` or later stays there | As computed (`pending` or `none`) |
| First final: `observed`, `revision` 0 | The content, including the insert when no row exists | `cost.observed`, `cost:{job_id}:r1` | 1 | `observed` |
| Later final, content differs from the baseline | The content | `cost.corrected`, `cost:{job_id}:r{read + 1}` | `read + 1` | `corrected` |
| Later final, content equals the baseline | The content, so a row an interim `pending` save changed is restored | None | Unchanged | The baseline's label |

**The baseline** for a later final save is the last cost event, `cost:{job_id}:r{revision}`. When that
event does not exist (a row from before the migration, or an event retention has deleted), the baseline
is the stored row if the stored row is final. When neither exists (no event, and the row holds an
interim `pending` compute), the content counts as changed. That can repeat the same content; it never
loses a change.

- **The label comes from the same rule as the event.** `_refresh_projection`'s own rule
  (`services/costs.py:170-176`) flips the label only when the job's total fees differ, so it goes.
  Otherwise a fee moved between attempts would emit `cost.corrected` with a payload whose `status` still
  says `observed`, and a consumer that trusts `status` would keep the old `billable`.
- **Not final is never labelled `corrected`,** so `jobs_needing_projection` keeps selecting a `pending`
  row until it is final.
- **No stale write.** Every branch writes only when the row still has the revision it read. A re-sync
  that read revision N and commits after a correction to N+1 writes nothing, so it cannot rewind the row
  and make the next change reuse an id that is already taken. Every branch also moves `evidence_version`
  forward, or `projection_out_of_date` would select the row on every sync.
- **No row yet:** the first save inserts with `ON CONFLICT (job_id) DO NOTHING` (SQLAlchemy's
  `on_conflict_do_nothing`, on both backends). Today's plain insert raises `IntegrityError` when two
  first saves race, and `sync_once` (`services/costs.py:134-137`) then stops before the remaining jobs.
- **Zero rows written** (a lost compare-and-set, a conflicting insert, or newer evidence stored): another
  save got there first; the background sync and an operator's `costs/sync` can run at once. No event,
  and **no second attempt with the same `JobCost`.** Two saves can stamp the same evidence version and
  still hold different fees, because `job_for_projection` reads the version before `_compute` reads
  usage (`services/costs.py:160,166`): a usage commit between the two saves' usage reads is seen by
  one and not the other. Retrying the loser would append a `cost.corrected` that puts the older fees
  back. Stopping is safe: both saves stamp the version read before the usage commit, which bumped
  `jobs.evidence_version` in its own transaction, so whichever save wins, the stored row is behind and
  the next `sync_once` computes again from the newer usage. That emits `cost.corrected` only if the
  winner held the older fees; if it held the newer ones, the content equals the baseline and the sync
  only advances `evidence_version`. The save returns the row now stored.
- **The event is written only when this transaction's insert or compare-and-set wins,** so two saves can
  never both write `r1`.

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
  last sequence returned. **On an empty page it is the cursor, unchanged.** The scoped indexes mean
  the next poll reads only the caller's own events, never anyone else's, so there is no reason to hand
  out the global head. That would also let every tenant watch the whole engine's event volume.
  The cost: a tenant with no events of its own keeps its cursor, so each purge that raises the floor
  past it is one 410, and the tenant resyncs although it missed nothing. That is at most once per purge
  run, and only when event retention is on. A floor kept per scope (the highest deleted sequence per
  actor) would remove it; Q4 asks whether M4 needs that.
- **One statement.** The floor check and the page come from one statement (a CTE over
  `lpb_event_floor`, `lpb_event_log` and the page, returning the floor, the epoch and the scoped head
  even when the page is empty). Under PostgreSQL's READ COMMITTED, two statements are two snapshots: a
  purge committing between a floor check and the page would delete events the page then skips, with no
  410. Sequence order equals commit order, so within one snapshot no lower sequence is still pending.
- **Expired cursor:** `cursor < floor` raises `CursorExpired` → **410** with `code: "cursor_expired"`,
  `floor` and **`resume_after`**: `max(scoped head, floor)`, where the scoped head is the highest
  sequence in the caller's scope (0 when it has none), from the same statement. The scoped head alone
  is not enough: retention deletes a prefix, so a tenant whose events are all gone has a scoped head
  below `floor`, and polling from it would be 410 forever. Polling from `resume_after` is never
  expired, but it does not return the caller's events at or below it, including any still above
  `floor` (events at 150 and 180 with `floor` 100 resume at 180). The resync below is what covers
  them, so a consumer must never just advance to `resume_after` without it. A cursor above the global head (`next_sequence - 1`) is also 410: it can only come
  from before a restore. `after` must be ≥ 0 (the route allows negatives today, `http/app.py:261`).
- **Resync** without a gap: note `resume_after`, rebuild state from the jobs list (`GET /v1/jobs`, paged)
  and each job's cost (`GET /v1/jobs/{id}/cost`), then continue from `resume_after`. Events applied twice
  are dropped by `id`. An event that commits while the rebuild runs is above `resume_after` and is
  applied on top of a rebuild that may already include it. That converges because payloads are
  snapshots: a consumer replaces the job's or cost's state with each payload, never adds it to a total,
  and the later events that follow it in order replace it again. There is no `/v1/costs` route, and `/v1/costs/projections` is capped at 25 with no
  paging (`http/app.py:241-243`), so neither can rebuild a tenant's costs. Job paging is not gap-free
  yet: `before` is a strict `created_at <` with no tiebreaker (`store/sql.py:164`), so jobs sharing a
  timestamp at a page boundary are skipped. The resync is only complete once ISS-29's `(created_at, id)`
  keyset paging lands (its Q3).
- A cursor of 0 on a log that has never been purged reads from the start. The feed starts at the
  migration, and older jobs get no backfilled events (Q7). They do get events from then on: a restart
  can finish one, usage can be reported, and its cost can change. So a consumer can see `job.finished`
  or `cost.corrected` for a job it never saw created, and has to handle that.

### Restore

After `lpb restore` (SQLite) or `pg_restore` (PostgreSQL), the log's head is below cursors consumers
hold. New events would reuse sequence numbers those consumers have passed, and a job that was
`running` in the backup is finished again by `recover_interrupted` under an id they already hold.
So `lpb restore` writes a new `epoch`, and after `pg_restore` the operator runs `lpb events new-epoch`
(documented in `RELIABILITY.md`). `EventPage` and the 410 body carry `epoch`, an additive field. A
consumer sends it back (`?epoch=`), and a mismatch is 410, so every consumer resyncs once, and the
resync replaces the stale state the reused ids would have hidden. A consumer that sends no epoch skips
the check, and the contract says that is at its own risk (Q9).

### Retention

`LPB_RETENTION_EVENTS_DAYS` (default: keep everything, like the other retention settings).
- **What goes:** a prefix of the log. The purge finds the highest sequence whose `occurred_at` is older
  than the window, deletes every event at or below it, and sets `floor` to it (never lowering it). The
  log above `floor` therefore has no holes, even though `occurred_at` does not follow `sequence`.
- **Its own transaction**, apart from the job and usage purge (`store/retention.py:24` runs those in one).
  It touches only `lpb_events` and `lpb_event_floor`, so it never holds a lock a writer needs. In
  particular, it cannot deadlock with `create_job(retry_of=...)`, which holds a failed job's row
  (`store/operation_refs.py:33`) while it allocates a sequence.
- **Wiring:** `_retention_loop` returns early when job and usage retention are both unset
  (`runtime/engine.py:152`), and `purge()` takes only those two settings (`engine.py:142`,
  `store/maintenance.py:16`, `protocols/__init__.py:421`). All of these change, or an events-only
  setting would never run.
- The job purge does not touch events, and the 410 path covers the reverse. Q4 asks whether event
  retention must be at least job retention.

### Failure isolation

A job or usage event commits with its change or not at all, so it cannot be lost and cannot be a
phantom. A cost event is different. `apply_usage` and `match_sessions` commit new usage and raise the
evidence version in their own transactions, and the event waits for the `save_projection` that
follows. A crash between them **delays** the event; it does not lose it. Every sync re-projects every
out-of-date job (`jobs_needing_projection` includes `projection_out_of_date()` for all jobs, not only
the touched ones), and that save emits. Until then, `GET /v1/jobs/{id}/cost` computes the new fee live,
so it can be ahead of the feed by one sync interval. A failure in the feed's read path never affects a
job. Push delivery (Q1) runs in its own task, with its own cursor per subscription. A failing
collector raises lag in health; it never blocks a job or another tenant's delivery.

### Push delivery (if Q1 brings it into M4)

#86's design, narrowed to a consumer of `lpb_events`:

- `lpb_event_subscriptions` per `application_id`: `target_url`, `event_types`, `signing_secret_ref`,
  an optional `bearer_secret_ref` (both `SecretRef`s, never the secrets), `cursor_sequence`, and backoff
  state. The two secrets are separate. The bearer token travels in every request, so a signing key
  sent the same way would let anyone who sees one request forge signatures.
- `event_types` filters the scoped query. The cursor moves to the last event delivered, and an empty
  filtered page leaves it unchanged, as a pull read does.
- A subscription delivers every actor's events in its `application_id`: the auditor-scope read, never
  another application's.
- A worker reads each subscription's events through that query, posts structured-mode
  CloudEvents v1.0, advances the cursor on 2xx, and honours `Retry-After`. Delivery is at least once,
  in `sequence` order per subscription. It signs with `Livepeer-Signature` (HMAC over the timestamp
  and body, with the signing key, which is never sent) and sends the bearer token when one is set.
- **Isolation between tenants:** each subscription delivers in its own task, up to
  `LPB_EVENTS_PUSH_CONCURRENCY` at once, and each delivery has its own timeout. One worker looping over
  subscriptions in turn would let one slow collector delay every other tenant.
- **Added to #86:**
  - **Target URLs (SSRF):** a tenant-supplied URL must not reach internal services. Checking the
    name once at registration is not enough: it can resolve elsewhere at delivery (DNS rebinding), and
    an HTTPS answer can redirect to `http://169.254.169.254/`. So the worker:
    - requires HTTPS;
    - resolves at each delivery and judges **every** address returned as `lpb discovery`'s
      `public_address` did (`discovery/probe.py:47`; D63 removed that package, so `probe.py` here means
      `src/livepeer_builder/discovery/probe.py` at `c3d615f`, the last commit that has it). It already refuses what a list of ranges misses:
      IPv4 carried in IPv6 (mapped, NAT64 `64:ff9b::/96`, 6to4, IPv4-compatible), site-local
      `fec0::/10`, and shared CGNAT space, which holds a cloud metadata address (`100.100.100.200`).
      Python's `is_global` alone accepts `64:ff9b::a9fe:a9fe`, which is `169.254.169.254`;
    - refuses the target if any address fails, rather than dialing the ones that pass;
    - connects to the address it checked (pinned), with the TLS identity of the name, as `Prober`
      does (`discovery/probe.py:149-167`). The URL carries the literal address, `Host` is the
      host and port (`Prober` copies `parts.netloc`, which would include any `user:pass@`; the worker
      refuses a target URL with user info instead), and `sni_hostname` is the name, so the
      certificate is verified against the name. Verification is never turned off;
    - tries each passed address in order, with a short connect budget on all but the last (`_fetch`,
      `probe.py:132-147`), so a dead AAAA record does not pause a subscription whose A record works.
      An untrusted certificate is an answer, not a dead address. The worker should share discovery's
      pinned-connect code rather than copy it;
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
| `store/schema.py`, migration `0012` | `lpb_events` (no foreign key on `job_id`), `lpb_event_log` and `lpb_event_floor` with their seed rows, `lpb_cost_projections.revision` (1 on rows already final) (`lpb_event_subscriptions` in the push slice) |
| `store/events.py` (new) | `append(conn, ...)` (sequence last, skip on existing id), `last_cost_event(conn, job_id, revision)` (by id), `events_after(scope, cursor, limit, epoch)` (floor, epoch, scoped head and page in one statement), `purge_before(cutoff)` (own transaction, prefix only), `new_epoch()` |
| `store/jobs.py`, `store/sql.py` | `create_job`; `finish_job` (ISS-28's G9 guard: `running` or `queued` only; reads the job back; returns whether it finished); `set_usage` (guarded on `usage IS NULL`, returns whether it stored); `save_projection` (reads the job's correlation fields, compares with the baseline, compare-and-set on `revision`). Each appends in its own transaction |
| `store/retention.py`, `store/maintenance.py`, `runtime/engine.py`, `config.py` | `LPB_RETENTION_EVENTS_DAYS`; `purge()` takes it; `_retention_loop` runs when any retention is set; `PurgeReport.events_deleted` |
| `store/backup.py`, `cli.py` | `lpb restore` writes a new epoch; `lpb events new-epoch` for `pg_restore` |
| `protocols/__init__.py` | `EventRepo` (`events_after`, `new_epoch`); `JobRepo.set_usage` and `finish_job` return `bool`; the `EventFeed` docstring loses "(M3)" |
| `api.py` | `events` moves from `EngineApi` to `ActorApi`: it is an actor read (`events.read`), and the FakeEngine parity tests run over `ActorApi` |
| `services/costs.py` | `_refresh_projection`'s label rule (`costs.py:170-176`) goes; the store labels |
| `contracts/reserved.py` → `contracts/events.py` | Move `EngineEvent` and `EventPage` out of "reserved", keeping the exports; `EventPage` unchanged |
| `services/events.py` (new) | `StoreEventFeed(store, policy)`; `services/reserved.py` keeps only sessions |
| `services/jobs.py` | `report_usage`: `set_usage` now returns whether it stored the report, and `report_usage` raises `InvalidRequest` when it did not, instead of returning the caller's copy. `_finish` reads `finish_job`'s answer |
| `errors.py`, `http/app.py`, `contracts/events.py` | `CursorExpired` → 410 with `floor`, `resume_after` and `epoch`; `EventPage.epoch`; `after` ≥ 0; `?epoch=` |
| `runtime/assembly.py` | Build `StoreEventFeed` by default |
| `testing/fake_engine.py` | `events`: `job.*` and `usage.reported` with the engine's ids and scope. It has no cost sync, so cost events come only from a test hook (`record_cost(job_cost)`). Its `report_usage` today neither stores the usage nor refuses a second report (`fake_engine.py:315-322`); it must do both |
| `services/event_push.py` (push slice) | `EventPushWorker`: subscriptions, signing, backoff, health |
| Docs | `events.md` (scope, ids, 410 body, ordering, retention), `persistence.md#retention`, `RELIABILITY.md`, D10's row, the tech-debt row T7, generated schema and OpenAPI |

## Test plan (test-driven)

**Levels.**
- Store contract tests on **SQLite and PostgreSQL** (`tests/store/`, `LPB_TEST_POSTGRES_URL`) for
  append, ids, ordering, the floor and purge. **Every race test lives here**, on a SQLite file and on
  PostgreSQL. `make_engine` uses in-memory SQLite with `StaticPool` (`testing/engine.py:26`,
  `store/sql.py:57`), one shared connection, so concurrent transactions there interleave instead of
  racing.
- Service tests through `make_engine` with the real `SqlStore` and the scripted transport
  (`tests/unit/test_events_feed.py`), for everything that is not a race.
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
[ ] a second finish after a committed success (cancel before run returns): the job stays succeeded, one event
[ ] a stream that ends in its pump task writes job.finished; so does recover_interrupted
[ ] a D57 re-dispatch: job.created for the new job; nothing new for the superseded one
[ ] report_usage writes usage.reported once; a refused second report writes nothing
[ ] two concurrent usage reports: one stored, one InvalidRequest, one event (fixes today's overwrite)
[ ] first observed projection: cost.observed r1, also after a pending row (r0); a re-sync: nothing
[ ] two concurrent saves of one change: one writes r1 and its event, the other writes nothing and returns the stored row
[ ] two first saves of one job: one inserts, the other conflicts and writes nothing, and sync_once goes on to the next job
[ ] two saves of one evidence version with different fees (usage committed between their reads), the
    order fixed by the test, not a race: the loser writes no event and no row. Older fees win: the next
    sync writes cost.corrected with the newer fees. Newer fees win: the next sync writes no event and
    only advances evidence_version
[ ] floor 100, caller events at 150 and 180: resume_after is 180; the resync's rebuild holds both
[ ] a stale re-sync committing after a correction: the row stays at r2, and the next change is r3
[ ] an unchanged save with newer evidence advances evidence_version and is not re-selected next sync
[ ] a fee moved between attempts, same total and classes: cost.corrected, and the payload's status is corrected
[ ] fees growing while a sibling attempt is still pending: no event, revision stays 0, status stays pending
[ ] a session recorded after the first observed (r1): the pending recompute keeps r1 and emits nothing;
    the next final save emits cost.corrected r2 if it differs from r1, and restores the row with no event if not
[ ] the last cost event deleted by retention: the baseline is the stored final row; equal content emits nothing
[ ] a row final before the migration (revision 1, no event): its next change is cost.corrected r2, never cost.observed
[ ] a pre-migration row with no attempts, repaired: cost.corrected r2 with the new attempts
[ ] a corrected label kept on unchanged fees: nothing (the input says observed, the last event says corrected)
[ ] fee_usd None against a last event with fee_usd 0: content differs
[ ] newer evidence with different fees: cost.corrected r2; a slower sync losing to newer evidence: nothing
[ ] crash after apply_usage, before save_projection: the next sync emits the cost event
[ ] pending and none projections write no event
[ ] a failed write after the append rolls the event back with the change (both backends)
[ ] PostgreSQL: a later transaction cannot commit an earlier-visible sequence (two connections)
[ ] the same id twice writes one row
[ ] scope: an actor sees only its own events; an auditor and an admin their application   [?] Q2
[ ] PostgreSQL: an event committed while a page is read is never skipped by next_cursor (one snapshot)
[ ] without events.read: 403 before any lookup
[ ] paging: limit honoured; next_cursor is the last sequence; an empty page returns the cursor unchanged
[ ] an empty log: the poll returns next_cursor 0; one job finishes; polling from 0 returns job.created (sequence 1)
[ ] a quiet tenant's empty page reveals nothing about other tenants' volume
[ ] retention: a prefix older than the window goes, floor rises; a job purge leaves events alone
[ ] retention with only LPB_RETENTION_EVENTS_DAYS set: the loop runs and purges
[ ] a purge committing between two reads cannot skip events: floor and page come from one statement
[ ] a purge never blocks an event writer (PostgreSQL: purge held open, finish_job still commits)
[ ] cursor below floor: 410 cursor_expired with floor and resume_after; after < 0: 422
[ ] floor raised past every event of the caller's: resume_after is floor; polling it is an empty page, not 410
[ ] cursor above the global head: 410; after a restore, an old epoch: 410; the resync then matches the store
[ ] resync: every job and its cost rebuilt from /v1/jobs and /v1/jobs/{id}/cost (after ISS-29's keyset paging)
[ ] FakeEngine: job and usage events, ids and scope match the engine; a second usage report is refused; cost events via the hook
[ ] push slice: delivered in order, cursor moves on 2xx only, Retry-After honoured, HMAC verifies  [?] Q1
[ ] push slice: non-HTTPS, private-resolving, rebinding and redirecting targets refused (pinned address)
[ ] push slice: NAT64, IPv4-compatible, site-local and CGNAT targets refused; one bad address refuses the name
[ ] push slice: a private target works only when listed in LPB_EVENTS_PRIVATE_TARGETS; no API sets it
[ ] push slice: behind floor pauses; one tenant's slow collector delays no other (concurrent deliveries)
[ ] push slice: the signing key is never sent; a request's headers cannot forge another's signature
[ ] push slice: event_types filters; the cursor moves to the last delivered event; a URL with user info is refused
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
  like `operation_stale_s`? Recommended: yes. And does M4 keep the floor per scope, so a tenant with no
  events of its own is never sent a 410 it does not need? Recommended: no, a global floor for M4; the
  repeat is one resync per purge run, and only with event retention on.
- **Q5.** A `cost.corrected` payload is the new `JobCost` only (recommended; consumers keep the previous
  one), or the `JobCost` plus the previous fees, as #86 proposes for append-only meters?
- **Q6.** ISS-26's async jobs: `job.created` at submit and `job.finished` at the end, with no new type
  (recommended), or add `job.started` (a `schema_version` question, not a breaking one)?
- **Q7.** No backfill for jobs that existed before the migration (recommended), or synthesize their
  events once? Either way, older jobs emit events from the migration on, so consumers must accept a
  `job.finished` or `cost.corrected` for a job they never saw created.
- **Q8.** Does any change in content with the same job total count as `cost.corrected`, such as an
  attempt's class changing or a fee moving between attempts? Recommended: yes, with the label written
  by `save_projection` from the same comparison, so the projection and the event agree. Today the label
  changes only when the total changes.
- **Q9.** Restore: an `epoch` on `EventPage` and the 410, with `lpb restore` and `lpb events new-epoch`
  changing it (recommended), or document that every consumer must resync by hand after a restore?

## Delivery slices

1. **Tidy:** split `store/sql.py`; move the event contracts out of `reserved`; no behavior change.
2. **Log:** migration `0012`, `store/events.py`, emission in the four write paths, ordering on both
   backends.
3. **Feed:** `StoreEventFeed`, scope, paging, retention and the 410; FakeEngine; docs. **ISS-46's
   acceptance is met here.**
4. **Push** (Q1): subscriptions, the worker, signing, the SSRF guard, health and the admin/CLI views.
