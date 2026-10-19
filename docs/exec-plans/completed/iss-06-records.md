# ISS-06: Implement SQLite records, migrations, retention, and backup/restore

## Status

Owners: John, Mike. GitHub: #12. **Accepted by Mike on 13 October.** Tests pass on SQLite and
PostgreSQL 16 in CI on `releases/m2`.

## Goal

Persist engine-owned access mappings, jobs and attempts, result references, measured usage and cost
projections behind the storage interface. Exercise restart, migration and restore with
representative records. Make retention configurable and documented.

## Acceptance

- [x] SQLite store behind `EngineStore`; vendor-neutral (Postgres passes the same suite)
- [x] Access mappings, jobs, attempts, result references, measured usage, cost projections persisted
- [x] Restart: `test_records_survive_restart_and_interrupted_jobs_resolve`
- [x] Migration over retained records: `test_upgrade_preserves_records_written_at_each_revision`
- [x] Restore: `test_sqlite_backup_and_restore`; `lpb backup`, `verify` and `restore`
- [x] Retention configurable and documented: [contracts/persistence.md](../../contracts/persistence.md#retention)
- [x] Provider ledger stays authoritative (usage rows are a cache; assumption A1)
- [x] Mike accepted assumptions A1 and A2 (12 October) in [m2-decisions.md](../../design-docs/m2-decisions.md)
- [x] Mike's review (13 October)

## Progress

- [x] 2026-10-12: Store, two migrations, backup, retention, with durability tests.
- [x] 2026-10-13: Coverage work fixed `lpb verify` on a non-SQLite file (now `InvalidRequest`); Mike accepted.

## Decision log

- 2026-10-12: SQLAlchemy Core and Alembic; migrations shipped as package data (D5).
