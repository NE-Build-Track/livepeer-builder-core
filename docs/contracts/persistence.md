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
- **Payment keys** are not stored. `lpb_actors.secret_ref` holds a `SecretRef`. The
  encrypted-database `SecretStore` keeps Fernet ciphertext in `lpb_secrets`, with the key outside the
  database.
- **Default URL:** `sqlite+aiosqlite:///./livepeer-builder.db`. There is no silent in-memory store.

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
- **Drift:** `tests/store/test_durability.py::test_migrations_match_the_schema` fails if `schema.py`
  and the migrations disagree.
- **Retained records:** `test_upgrade_preserves_records_written_at_each_revision` writes rows at
  revision 0001 and upgrades.

## Retention

| Setting | Effect | Default |
| --- | --- | --- |
| `LPB_RETENTION_JOBS_DAYS` | Deletes `succeeded`/`failed` jobs (attempts, payment sessions, projections) completed before the cutoff. **`uncertain` jobs are kept** until resolved | keep forever |
| `LPB_RETENTION_USAGE_DAYS` | Deletes cached Batteries usage rows older than the cutoff | keep forever |

The purge runs every `LPB_RETENTION_INTERVAL_S` (default 21600 s, 6 h) when a retention setting is on,
or on demand with `lpb purge` (it prints a `PurgeReport`). It runs through the store's `maintenance`
capability, which `SqlStore` provides; a store without one purges nothing. Batteries stays the
authority for network accounting. Purging the engine cache never changes provider balances.

## Backup and restore

See [RELIABILITY.md](../RELIABILITY.md#backup-and-restore). SQLite: `lpb backup`, `lpb verify` and
`lpb restore` (online backup API, integrity check, schema revision check). PostgreSQL:
`pg_dump`/`pg_restore`. **Back up the `SecretStore` key separately**: the encrypted-database store
is unreadable without `LPB_SECRET_KEY`.
