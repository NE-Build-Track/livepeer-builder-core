# ISS-35: Isolation, recovery and accounting replay

**Status:** proposal for discussion, 29 October 2026. Nothing here is decided until it is locked.\
**Tracker:** ISS-35 ([Network-Engineering-SPE#49](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/49)),
source task V3. Extends ISS-23 ([guide](../m3-guides/iss-23-verification.md)).\
**Owner:** John (engine). **Reviewers:** Mike, John.\
**Verified against:** builder-core `releases/m3` at `d3df806`; Clearinghouse Batteries `ca09e47`.

## Done when

- Checks cover the required **interfaces and auth configurations**, **restart and disconnect**,
  **uncertain final saves**, **late, unmatched and corrected cost observations**, and **replay without
  double application**.
- **SQLite migration and restore** are exercised with integrated records and the supported recovery
  handles. Injected failures are kept apart from observed upstream behavior, and unresolved external
  evidence limits are recorded.

**Depends on:** ISS-06 (#12), ISS-23 (#36), ISS-28 (#42), ISS-29 (#43), ISS-30 (#44), ISS-31 (#45),
ISS-32 (#46). **External:** H1, H2, H3.

## Current state (verified)

The base is strong; ISS-23 maps it by category. What exists for this ticket:

| Area | Existing checks (`d3df806`) | Gap |
| --- | --- | --- |
| Isolation | Cross-actor and revoked checks listed in the ISS-23 guide; auditor reads own application only (`services/policy.py:9-36`, D46) | Only REST and the facade exist; MCP (ISS-30) and enterprise auth (ISS-32/33) add surfaces. No sweep over *every* operation × principal |
| Restart | `test_records_survive_restart_and_interrupted_jobs_resolve` (`tests/store/test_durability.py:155`); `test_jobs.py::test_restart_recovery_marks_maybe_paid_jobs_uncertain`, `…after_an_unknown_payment_stays_uncertain`, `…between_attempts_after_a_paid_failover_is_failed`; `test_streaming.py::test_restart_resolves_an_open_stream_as_uncertain` | Async jobs and sessions (ISS-26/27) have no recovery yet |
| Uncertain final save | RELIABILITY "The store fails while a stream ends": the job stays `running` until the next start; `test_a_crash_while_signing_leaves_the_cost_pending` (`test_durability.py:252`) | No test that fails `finish_job` for a buffered job and restarts |
| Late / unmatched / corrected | `test_costs.py`: `…marks_corrections` (222), `test_rows_that_arrive_before_the_attempt_match_on_the_next_sync` (316), `…manifest_shared_by_two_sessions…` (342), `test_a_row_committed_while_a_sync_classifies_the_job_is_never_lost` (676); `test_contract.py` evidence-version tests (311–413), refusals (487–580) | — |
| No double application | `apply_usage` skips known row ids in one transaction with the cursor (`store/sql.py:294-316`); `test_cursor_mismatch_restarts_from_the_beginning` (`test_costs.py:405`); properties `test_fees_add_up_however_the_rows_are_split`, `test_every_attributed_charge_is_counted_exactly_once` (`test_properties.py:62,80`) | No **rebuild** check: derived state recomputed from the source must equal the incremental result |
| Migration | `test_upgrade_preserves_records_written_at_each_revision`, `test_migrations_match_the_schema` | — |
| Backup / restore | `test_sqlite_backup_and_restore` (one job), `test_backup_guards` | Not with sessions, projections, cursor, secrets, keys; not followed by a sync; not with a missing `LPB_SECRET_KEY` |
| Injected vs observed | Every check is fixture-based (ISS-23 finding) | No labelling; no live recovery evidence since M2 |

### What replay means here (system of record vs derived data)

| Data | Authority | In the engine |
| --- | --- | --- |
| Usage rows, allocations, spent | **Batteries** | `lpb_usage_rows` is a cache; retention may delete it (`store/retention.py`) |
| Jobs, attempts, session log (`manifest_id`, `payment_state`) | **The engine** | `lpb_jobs`, `lpb_attempts`, `lpb_payment_sessions` |
| Matches (`match_status`, `payment_session_id`), projections, cursor | **Derived** | recomputable from the two above |

A projection is a deterministic function of the engine's records and Batteries' rows, **except three
path-dependent fields**: `status = corrected` (set when fees differ from the previous projection,
`services/costs.py:165-183`), `first_observed_at` and `updated_at` (from the clock). A rebuild therefore
yields `observed` where history said `corrected`. Equivalence must be defined on the rest.

### Candidate defects found by reading (to pin with a test first, not yet reproduced)

- **R1. Usage retention can flip an ambiguous match.** Every sync re-checks `ambiguous` keys
  (`store/matching.py:110-127`), and `_match_key` counts sessions only among cached rows (`:146-156`).
  If `LPB_RETENTION_USAGE_DAYS` purges the older session's rows while the newer remain, and job retention
  removed the older entry, the key sees one session and one entry and turns `matched`.
- **R2. Job retention moves delivered fees to `unattributed`.** Purging a job deletes its session log
  (`store/retention.py:31-34`); its cached rows then match no `matched` entry, so `unattributed_usage`
  (`store/cost_reads.py:123-130`) counts them. Reconciliation still balances, with a different split.

## Decision proposed

**Four check families, each test row tagged `injected` or `observed`, published in the traceability
spec ISS-23 creates; plus one rebuild-equivalence check that recomputes every derived table from the
systems of record and must reproduce the incremental answer.**

### 1. Isolation sweep (injected)

One table-driven test per surface: the FastAPI route table, the facade (`api.py`), and the MCP tool
list (ISS-30). Principals: owner actor, other actor same application, actor of another application,
auditor same application, auditor other application, admin, revoked key, expired JWT, no credential.
Expected outcome per (operation kind, principal) from one table (200 / 403 / 404 per D44 / 401), run
under `LPB_AUTH=api_keys`, `jwt` and `jwt,api_keys`. A new route or tool is covered without editing the
test; one with no row fails. Includes job results, session proxy paths and async job reads.

### 2. Recovery (injected, with one observed run)

A store decorator (`decorate=` on `make_engine`, `testing/engine.py:30`) fails a named store call once.
For each mode (immediate buffered, streamed, async, session): crash before dispatch, after payment
intent, after `on_signed`, at `finish_job`; then a fresh engine on the same database starts. Rules
checked: nothing re-runs a job that may have reached a runner (engine.md "No exactly-once"); maybe-paid
ends `uncertain`; never-dispatched async work runs once; a session lapses `failed` with its cost kept;
the cost stays `pending` until usage settles it, never `none`.

### 3. Accounting replay (injected)

- **Idempotent re-feed.** Re-deliver pages, duplicate rows, `duplicate`/`ignored` statuses, a cursor
  mismatch, and two concurrent syncs: projections and reconciliation unchanged.
- **Crash between sync's three commits** (rows, matches, projections): the next sync converges
  (out-of-date jobs are recomputed, `jobs_needing_projection`, `store/matching.py:68-79`).
- **Rebuild equivalence.** New `store/replay.py`: on a **copy** of the database, delete
  `lpb_usage_rows`, `lpb_cost_cursor`, `lpb_cost_projections`; reset `match_status='unmatched'`,
  `payment_session_id=NULL` on sessions (keep `payment_state` and `payment_sent`, which are the engine's
  records). Sync to the end from a `UsageSource` (Batteries, or a recorded feed), project every ended
  job, then compare per job: `fees`, `billable`, per-attempt `(number, cost_class, fees)`, each
  session's match; and per allocation the reconciliation. `observed` and `corrected` compare equal;
  timestamps are ignored.
- **Property test.** Hypothesis generates jobs with sessions (collisions, refusals, unknowns), a feed
  with late and duplicate rows, page splits, duplicated pages and crash points. The final derived state
  equals a single clean pass. This is the main guard; the examples above are its readable cases.

### 4. Migration and restore (injected and observed)

An **integrated fixture database**: actors with secrets, access keys (active, revoked, rotated), jobs in
every state and mode, attempts, sessions in every `payment_state` and `match_status`, projections,
usage rows, the cursor, and ISS-11's operation ledger. Checks: upgrade from each older revision keeps
every record; backup while a sync runs → restore → start → every read equal; restore then sync re-reads
rows newer than the backup and applies each once; rebuild equivalence holds after restore; a restore
without the right `LPB_SECRET_KEY` fails on the first paid job with a clear error, not a wrong charge.
Async recovery handles (ISS-26) survive restore.

### Observed checks (live, recorded, not in CI)

On the dev stack, once: kill the engine during a paid job; stop Batteries during a sync; restart the
signer with an undelivered outbox. Record states, costs and reconciliation before and after. Batteries
`upstream/main` `d47d1ff` pauses webhook authorization on Kafka lag (#40); the pin `ca09e47` does not.

### External evidence limits to record

T6 (usage rows assumed immutable), T13/T16 (32-bit manifests; collisions), no void event for rejected
tickets (D21), no time filter on `/v1/usage` (console G6), signer outbox delivery is at-least-once.

### `lpb costs verify`

Expose rebuild equivalence as a read-only operator command: copy the database (the backup API), rebuild
in the copy, print the differences, exit 1 on any. It is "trust, but verify" for a deployment, and the
observed evidence for this ticket.

## Code areas

| File | Change |
| --- | --- |
| `store/replay.py` (new) | `reset_derived(copy_url)`, `compare(live, rebuilt)` |
| `cli.py` | `lpb costs verify` |
| `testing/` | `FailingStore` decorator; `RecordedUsageSource` (JSONL feed); integrated fixture builder |
| `tests/unit/test_isolation_sweep.py` (new) | family 1 |
| `tests/unit/test_recovery.py` (new) | family 2 |
| `tests/unit/test_properties.py`, `tests/store/test_replay.py` (new) | family 3 |
| `tests/store/test_durability.py` | family 4 |
| `docs/product-specs/m4-traceability.md` (new, or ISS-23's file extended) | rows with `injected`/`observed` |
| `docs/RELIABILITY.md`, `persistence.md`, `payments.md` | replay definition, R1/R2 outcome, `lpb costs verify` |

## Test plan (test-driven)

**Levels.** Unit with real `SqlStore` on in-memory SQLite and fakes; store contract tests on SQLite and
PostgreSQL (`LPB_TEST_POSTGRES_URL`); property tests (Hypothesis, already used); HTTP and MCP route
tests; observed runs outside CI.

**Seams.** `decorate` for fault injection, `FixedClock`, `FakeUsageSource`/recorded feed, the loopback
network (`tests/unit/livepeer_net.py`) for signer behavior. Never mock the store's SQL.

**Starter test:** rebuild equivalence on the smallest case: one delivered job, one row. It forces the
`replay.py` interface.

```text
Replay
[ ] rebuild of one delivered job equals the incremental projection
[ ] rebuild equals incremental for failover, ambiguous, refused, unknown-confirmed, late-row jobs
[ ] corrected rebuilds as observed; the comparison treats them as equal               [?] Q2
[ ] re-feeding every page twice changes nothing
[ ] a crash after apply_usage, then a sync: same result as no crash
[ ] property: any split, duplication, crash point → the clean-pass state
[ ] R1 pinned: usage purge with an ambiguous key does not turn it matched            [?] Q3
[ ] R2 pinned: job purge moves fees to unattributed; reconciliation still balances  [?] Q3
Recovery
[ ] finish_job fails once → restart → uncertain (maybe paid) or failed; no second dispatch
[ ] each mode × crash point leaves cost pending, never none, when a session may have paid
[ ] queued async job survives restart and runs once                                  [?] ISS-26
[ ] open session at restart ends failed; its cost is kept                            [?] ISS-27
Isolation
[ ] sweep over every route × principal × auth mode matches the table
[ ] the same sweep over MCP tools and facade methods                                  [?] ISS-30
[ ] admin of application A is global; auditor of A cannot read B                    [=] D46
Restore
[ ] integrated fixture survives upgrade from every revision
[ ] backup during a sync, restore, start: reads equal
[ ] restore then sync applies newer rows once
[ ] wrong LPB_SECRET_KEY: the first paid job fails clearly, nothing is signed
Observed (recorded once)
[ ] engine killed mid paid job; Batteries down during sync; lpb costs verify clean afterwards
```

**Definition of done.** Every item `[x]`, `[=]` or `[-]` with a reason; the traceability spec lists every
row with its evidence tag; the checks in `AGENTS.md` (including `tests/store` on PostgreSQL); the
observed runs recorded under `docs/references/evidence/`; R1 and R2 either fixed or accepted in a
decision.

## Upstream dependencies

| Upstream | Needed? | Why / workaround |
| --- | --- | --- |
| livepeer/node | No | Recovery never re-signs; outbox redelivery is absorbed by Batteries' de-duplication |
| livepeer-python-gateway | No | The runner call is the engine's (D53) |
| clearinghouse-batteries | No | Rebuild reads `/v1/usage` from the start, which Batteries keeps. Immutability of rows (T6) is an assumption to record, not a change to ask for |

## Alternatives considered

- **Event-sourced costs** (projections only from an append-only event log). Rejected: the engine's
  tables already separate records from derived data; the rebuild check gives the same guarantee.
- **Replay on the live database.** Rejected: destructive and races the sync loop; always a copy.

## Open questions to lock

- **Q1.** Ship `lpb costs verify` (recommended) or keep rebuild as a test only?
- **Q2.** Keep `corrected` path-dependent (recommended; the events feed will carry history), or store
  the first observed fees so a rebuild can reproduce it?
- **Q3.** R1/R2: forbid usage retention shorter than job retention and re-check only within an attempt
  window (T16's exit), or document the effect?

## Delivery slices

1. Replay: `store/replay.py`, the property test, R1/R2 pinned.
2. Restore with the integrated fixture; `lpb costs verify`.
3. Recovery and isolation sweeps (after ISS-26, ISS-27, ISS-30 merge).
4. Observed runs and the traceability spec.
