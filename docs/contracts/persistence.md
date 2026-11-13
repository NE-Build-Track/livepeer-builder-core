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
- **Optional store protocols.** A store may also implement these; the engine asks with `isinstance`
  and falls back for a supplied store that does not. `SqlStore` implements all of them.

  | Protocol | Adds | Without it |
  | --- | --- | --- |
  | `UsageSetAsideRepo` (#114, C13) | the record of usage rows cost sync left out | the row is left out and logged once per process ([payments.md](payments.md#cost-classification)) |
  | `AllocationCostSnapshot` (#115, C9), on the cost snapshot | `attempt_allocations`: which allocation paid each attempt, so reconciliation counts one allocation at a time | reconciliation counts every attempt of the actor against the current allocation, as before, and knows no earlier allocation ([payments.md](payments.md#cost-classification)) |
  | `UsageOnceRepo` (#115, C20) | `set_usage_once`: a job's usage is written only where it has none and is `succeeded`, in one statement | the engine checks the job it read and calls `set_usage` (unconditional); two usage reports at the same moment can still both succeed ([engine.md](engine.md#measured-usage)) |
  | `ActorCommandRepo` (#115, C4) | `set_actor_status`, `set_actor_key`: each admin command writes only its own actor columns, with its condition in the statement | the engine reads the actor again, checks the condition itself and saves the whole row with `save_actor`; two commands on one actor at the same moment can still overwrite each other ([payments.md](payments.md#provisioning)) |

## Migrations

Alembic migrations are **package data** (`store/migrations/`), so an installed wheel migrates exactly
like a source checkout. Version table: `lpb_alembic_version`.

- **Apply:** `lpb migrate`. Settings `auto_migrate=true` (the default) also migrate on start.
- **Refuse a mismatch:** with `auto_migrate=false`, `BuilderEngine.start()` compares the store's
  revision with the package head before anything else and raises `RuntimeError` on any mismatch,
  behind or ahead, naming both revisions and `lpb migrate` (#112, C8). Otherwise the engine would start
  ready, pay a runner, then fail to record. It checks at start only: a replica already running keeps
  running while another one migrates. A supplied store with no `maintenance` is not checked.
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

Neither setting deletes from `lpb_operations` (the operation ledger) or from `lpb_usage_set_aside`,
the record of usage rows cost sync left out (#114, C13; migration 0012;
[payments.md](payments.md#cost-classification)): each row there is an id, a reason and two fees.

The purge runs every `LPB_RETENTION_INTERVAL_S` (default 21600 s, 6 h) when a retention setting is on,
or on demand with `lpb purge` (it prints a `PurgeReport`). It runs through the store's `maintenance`
capability, which `SqlStore` provides; a store without one purges nothing. Batteries stays the
authority for network accounting. Purging the engine cache never changes provider balances.

## Backup and restore

See [RELIABILITY.md](../RELIABILITY.md#backup-and-restore). SQLite: `lpb backup`, `lpb verify` and
`lpb restore` (online backup API, integrity check, schema revision check). PostgreSQL:
`pg_dump`/`pg_restore`. **Back up the `SecretStore` key separately**: the encrypted-database store
is unreadable without `LPB_SECRET_KEY`.
