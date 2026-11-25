# Persistence contract

**Ticket:** ISS-06 · **Decision:** D5 · **Code:** `store/` · **Schema:**
[generated/db-schema.md](../generated/db-schema.md)

## Store

`EngineStore` (in `protocols/`) is the only persistence interface services see. Its optional
`maintenance` (`StoreMaintenance`: `purge`, `schema_revision`) is the store's housekeeping; `None`
means no purge and no reported schema revision. `SqlStore`
implements it with SQLAlchemy Core (async) for **SQLite (default)** and **PostgreSQL (optional,
`[postgres]` extra)**. Both run the same store test suite in CI.

- **Tables** are prefixed `lpb_`, so they can share a database with application tables.
  Applications never write them.
- **Money and fees** are exact decimal strings, summed in Python.
- **Timestamps** are UTC. Lists that are queried (payment sessions) live in child tables.
- **Payment keys** are not stored. `lpb_actors.secret_ref` holds a `SecretRef`: opaque text of any
  length, in an unbounded `text` column (migration 0013, #115; it was `varchar(200)`, which PostgreSQL
  enforced after the provider key was funded), so a supplied secret store may return a long path. The
  encrypted-database `SecretStore` keeps Fernet ciphertext in `lpb_secrets`, with the key outside the
  database.
- **Who holds a payment key** is `lpb_actors.credential_custody` (migration 0015, #166), beside the
  `secret_ref` it describes. `secret_store`: the configured `SecretStore` issued the reference, and
  resolves and deletes it. `work_engine`: a standalone clearinghouse handed the key to the enterprise's
  engine once and kept none, and the reference is a receipt that no store resolves. The column is never
  derived from the reference's text ([payments.md](payments.md#payment-keys)).
- **An async job's request and result** (ISS-26, migration 0016). `lpb_jobs` is the queue: an async
  job waits there `queued`, and `started_at` and `claims` record a worker's claim. `claims` after a
  claim is that claim's number: `claim_job` answers with it, and the writes the worker makes for the
  job afterwards (`start_attempt`, `requeue_unstarted`, `finish_job`, `finish_job_with_result`, each
  with `claim=`) apply only while the job is `running` with that count, in the same statement or
  transaction. A worker that lost the job to a requeue or to another writer's end therefore writes
  nothing, and is told so. A claim given back for a job that only has to wait
  (`requeue_unstarted(..., counted=False)`) takes one off `claims` again, so waiting never uses up the
  three claims a job has ([engine.md](engine.md#running)). A write that names no claim, an immediate
  job's or a stream's, is guarded by the job's state alone, as before. `lpb_job_requests`
  holds what a queued job will run with: its request (a raw body as its bytes) and the actor it was
  authorized for (id, tenant, permissions, credential kind and attributes; never the credential's
  provenance or a display name). The transaction that ends the job deletes the row. `lpb_job_results`
  holds the answer of a succeeded async job, its content type, size and SHA-256, and when it expires,
  written in the transaction that ends the job. **Neither table is encrypted by the engine** (D69
  (5)): a request or an answer may hold anything the caller or the runner put in it, a URL that grants
  access included, so protect the database, its backups and its replicas as you would those requests
  and answers themselves. How long each is kept is under [Retention](#retention); the worst case for
  stored requests is `LPB_ASYNC_MAX_QUEUED` requests at the request limit, about 16 GiB at the defaults.
- **Default URL:** `sqlite+aiosqlite:///./livepeer-builder.db`. There is no silent in-memory store.
- **Optional store protocols.** A store may also implement these; the engine asks with `isinstance`
  and falls back for a supplied store that does not. `SqlStore` implements all of them.

  | Protocol | Adds | Without it |
  | --- | --- | --- |
  | `UsageSetAsideRepo` (#114, C13) | the record of usage rows cost sync left out | the row is left out and logged once per process ([payments.md](payments.md#cost-classification)) |
  | `AllocationCostSnapshot` (#115, C9), on the cost snapshot | `attempt_allocations`: which allocation paid each attempt, so reconciliation counts one allocation at a time | reconciliation counts every attempt of the actor against the current allocation, as before, and knows no earlier allocation ([payments.md](payments.md#cost-classification)) |
  | `UsageOnceRepo` (#115, C20) | `set_usage_once`: a job's usage is written only where it has none and is `succeeded`, in one statement | the engine checks the job it read and calls `set_usage` (unconditional); two usage reports at the same moment can still both succeed ([engine.md](engine.md#measured-usage)) |
  | `ActorCommandRepo` (#115, C4) | `set_actor_status`, `set_actor_key`: each admin command writes only its own actor columns, with its condition in the statement | the engine reads the actor again, checks the condition itself and saves the whole row with `save_actor`; two commands on one actor at the same moment can still overwrite each other ([payments.md](payments.md#allotment)) |

  `ActorCommandRepo` has a third write, `allot_actor`, and its key write takes the key the rotation
  began from (`only_from`). A rotation writes only over that key, and allotment writes its row only
  where the actor has none or still has the revoked row it read. A store has all three writes or is
  treated as having none.

  Since #166 the key write also takes `credential_custody`, a keyword the engine always passes, and
  writes it with `api_key_id` and `secret_ref` in the one statement. Every store keeps
  `ActorAllocation.credential_custody` with the rest of the actor it saves and returns: a store that
  drops it reads every key back as `secret_store`, the field's default.

## Migrations

Alembic migrations are **package data** (`store/migrations/`), so an installed wheel migrates exactly
like a source checkout. Version table: `lpb_alembic_version`.

- **Apply:** `lpb migrate`. Settings `auto_migrate=true` (the default) also migrate on start.
- **Refuse a mismatch:** with `auto_migrate=false`, `BuilderEngine.start()` compares the store's
  revision with the package head before anything else and raises `RuntimeError` on any mismatch,
  behind or ahead (#112, C8). The message names both revisions and the database that was checked, by
  its URL with the password hidden: `lpb migrate` migrates `LPB_DATABASE_URL`, and a supplied SQL
  store may be another database. For a database behind this package it says to run `lpb migrate`
  with `LPB_DATABASE_URL` pointing at that database. For a revision this package does not know, it
  says another release migrated the database and that release should be started: `lpb migrate`
  from this package cannot change it. Otherwise the engine would start
  ready, pay a runner, then fail to record. It checks at start only: a replica already running keeps
  running while another one migrates. Only the packaged migrations are checked: a supplied store whose
  `maintenance` is its own reports revisions of its own schema and is not compared, and neither is one
  with no `maintenance`. A wrapper that keeps the SQL store's `maintenance` is still checked.
- **Check:** `lpb revision` (exit code 2 when behind).
- **An interrupted 0013 on SQLite:** SQLite creates the table that migration 0013 copies the actors
  into (`_alembic_tmp_lpb_actors`) outside the migration's transaction. A run that dies before its
  commit rolls back to the old revision and leaves that empty copy behind. 0013 drops a leftover copy
  when it starts, up or down, and only while `lpb_actors` itself exists, so the next `lpb migrate`
  goes through. Before, it failed with "table _alembic_tmp_lpb_actors already exists" (#115).
- **0014, the domain-language names (#163):** it renames the tenant column to `tenant_id` in the
  five tables that have it, in place; the kind of every stored admin key, to `clearinghouse_admin`,
  with `lpb_access_keys.kind` grown to 32 characters to hold it; three of the ledger's commands; and
  the same names inside the result each ledger record holds. The names are listed under
  [Upgrading to the domain-language names](index.md#upgrading-to-the-domain-language-names). Every row
  is kept, and a downgrade puts the old names back. The run is one transaction on SQLite and on
  PostgreSQL: one that dies before its commit changes nothing
  (`tests/store/test_domain_language_names.py`).
- **0015, credential custody (#166):** it adds `lpb_actors.credential_custody` (16 characters, not
  null) and changes nothing else. Every existing row becomes `secret_store`, whatever its `secret_ref`
  reads: each was written through the configured store, and a supplied store may issue any handle. A
  downgrade drops the column and keeps every row
  (`tests/store/test_credential_custody_migration.py`). No revision creates `lpb_payment_keys`: the
  migration that did was never released, and it is deleted, not undone by a later revision (Mike, 23
  November 2026). A database that ran it is recreated, and the revision check does not find it. The
  withdrawn migration was numbered 0015 too, so a database that stopped there records 0015 as a sound
  one does: `lpb migrate` takes it on to the head and the engine starts without a refusal, yet it has
  `lpb_payment_keys` and no `lpb_actors.credential_custody`. Look at the tables, not the revision: a
  database that has `lpb_payment_keys` is recreated. One that also ran the withdrawn 0016, which
  dropped that table again, records 0016, the number async jobs have now (below): it is the head, so
  the engine starts on it without a refusal and `lpb migrate` changes nothing, yet it has no
  `lpb_actors.credential_custody` and neither of the async jobs' tables. It is recreated too. A
  database at 0014 or earlier has neither the table nor the column, and is migrated as usual.
- **0016, async jobs (ISS-26):** it adds `lpb_jobs.started_at` and `lpb_jobs.claims` (0 for every
  job already stored) and the tables `lpb_job_requests` and `lpb_job_results`, and changes no
  existing row. A downgrade drops both tables and both columns. A job still `queued` then could never
  run, since its request goes with its table and the earlier release has no worker: the downgrade
  ends it `failed`, `interrupted`, so its `operation_ref` may run again. Stored results are deleted;
  the jobs keep their record. The run is one transaction on SQLite and on PostgreSQL, in both
  directions (`tests/store/test_async_migration.py`).
- **Drift:** `tests/store/test_durability.py::test_migrations_match_the_schema` fails if `schema.py`
  and the migrations disagree.
- **Retained records:** `test_upgrade_preserves_records_written_at_each_revision` writes rows at
  revision 0001 and upgrades.

## Retention

| Setting | Effect | Default |
| --- | --- | --- |
| `LPB_RETENTION_JOBS_DAYS` | Deletes `succeeded`/`failed` jobs (attempts, payment sessions, projections, and an async job's stored result and any request left beside it) completed before the cutoff. **`uncertain` jobs are kept** until resolved, and `queued` and `running` ones are never taken | keep forever |
| `LPB_RETENTION_USAGE_DAYS` | Deletes cached Batteries usage rows older than the cutoff | keep forever |
| `LPB_RETENTION_RESULTS_DAYS` | Gives an async job's stored result an `expires_at` that many days after the job ended (ISS-26). The purge deletes the bytes once it has passed, and the result route answers 410 from that moment, purged or not. The job and its `ResultRef` stay until `LPB_RETENTION_JOBS_DAYS` takes the job. In code, `retention_results_days=None` keeps a result as long as its job | 7 days (D69 (6)) |

Neither setting deletes from `lpb_operations` (the operation ledger) or from `lpb_usage_set_aside`,
the record of usage rows cost sync left out (#114, C13; migration 0012;
[payments.md](payments.md#cost-classification)): each row there is an id, a reason and two fees.

The purge runs every `LPB_RETENTION_INTERVAL_S` (default 21600 s, 6 h) when a retention setting is on,
or on demand with `lpb purge` (it prints a `PurgeReport`: `jobs_deleted`, `usage_rows_deleted` and,
since ISS-26, `results_deleted`). With job or usage retention on, the first purge runs at start. With
results retention alone, the default, it runs one interval after start, so an engine with the
default settings writes nothing at start beside its caller's own first writes; an expired result is
never served meanwhile. Every purge deletes the results whose time has passed: a result keeps the
`expires_at` it was given when its job ended, whatever the setting is now, and `lpb purge` deletes
them whatever the settings. It runs through the store's `maintenance`
capability, which `SqlStore` provides; a store without one purges nothing. Batteries stays the
authority for network accounting. Purging the engine cache never changes provider balances.

## Backup and restore

See [RELIABILITY.md](../RELIABILITY.md#backup-and-restore). SQLite: `lpb backup`, `lpb verify` and
`lpb restore` (online backup API, integrity check, schema revision check). PostgreSQL:
`pg_dump`/`pg_restore`. **Back up the `SecretStore` key separately**: the encrypted-database store
is unreadable without `LPB_SECRET_KEY`.
