# Reliability

## Configuration

Settings come from `LPB_<FIELD>` environment variables (`EngineSettings.from_env`). Defaults favor
safety: a durable SQLite file, `per_actor` payments, and admission limits on.

| Variable | Default | Meaning |
| --- | --- | --- |
| `LPB_DATABASE_URL` | `sqlite+aiosqlite:///./livepeer-builder.db` | Or `postgresql+asyncpg://…` (with the `[postgres]` extra) |
| `LPB_AUTO_MIGRATE` | `true` | Migrate on start. Production may set `false` and run `lpb migrate` |
| `LPB_SIGNER_URL`, `LPB_DISCOVERY_URL` | unset | Signer (discovery and payment), optional discovery override. Both unset → empty static discovery |
| `LPB_ALLOCATION_MODE` | `per_actor` | Or `per_application` with `LPB_SHARED_PAYMENT_KEY` (and `LPB_SHARED_ALLOCATION_ID` for reconciliation) |
| `LPB_BATTERIES_URL`, `LPB_BATTERIES_TOKEN` | unset | Management API and the least-privilege credential (`grants.fund`, not `grants.*`) |
| `LPB_BATTERIES_OPERATOR_TOKEN` | unset | `grants.read` and `grants.create`. Required when `LPB_BATTERIES_URL` is set |
| `LPB_SECRET_KEY` | unset | Fernet key for stored payment keys (`lpb gen-secret-key`). Required to provision in `per_actor` mode |
| `LPB_DEFAULT_CURRENCY` | `USD` | Grant and allocation currency (`USD` or `ETH`) |
| `LPB_MAX_CONCURRENT_JOBS`, `LPB_MAX_CONCURRENT_JOBS_PER_APP` | `64`, unset | Admission limits (503 + `Retry-After`, before payment) |
| `LPB_DISCOVERY_INTERVAL_S`, `LPB_COST_SYNC_INTERVAL_S` | `15`, `30` | Background loops |
| `LPB_RETENTION_JOBS_DAYS`, `LPB_RETENTION_USAGE_DAYS` | unset | See [contracts/persistence.md](contracts/persistence.md#retention) |
| `LPB_HTTP_HOST`, `LPB_HTTP_PORT`, `LPB_LOG_JSON` | `127.0.0.1`, `8080`, `true` | Service and logging |

## Operations

| Command | When |
| --- | --- |
| `lpb migrate` / `lpb revision` | Before starting a new version; `revision` exits 2 when behind |
| `lpb bootstrap-admin` | Once per deployment; prints the first admin key once |
| `lpb gen-secret-key` | Once; store the key in your secret manager, never in the database |
| `lpb serve` | Run the REST service (`[server]` extra) |
| `lpb smoke` | Import, migrate, start, health, stop; used by CI and after an install |
| `lpb backup FILE` / `lpb verify FILE` / `lpb restore FILE --yes` | SQLite backup and restore (below) |
| `lpb purge` | Apply retention now |

Health is reported by `GET /readyz` (and `engine.health()`): discovery freshness and errors, the last
cost sync and its error, running jobs, the allocation mode and the version. Logs are JSON lines; every
line has an `event` field (for example `jobs.recovered` or `costs.sync_failed`).

## Backup and restore

- **SQLite:** `lpb backup FILE` uses SQLite's online backup API, so the copy is consistent while the
  engine runs, and is integrity-checked. `lpb verify FILE` prints the schema revision.
  `lpb restore FILE --yes` verifies the backup, then writes it over the live database. **Stop the
  engine first.** The tested drill is `tests/store/test_durability.py::test_sqlite_backup_and_restore`.
- **PostgreSQL:** use `pg_dump` and `pg_restore`. The `lpb` commands refuse non-SQLite URLs.
- **Keys:** back up `LPB_SECRET_KEY` separately. Without it, stored payment keys can't be decrypted,
  so they would have to be rotated.

## Failure behavior

| Situation | Behavior |
| --- | --- |
| The engine dies mid-job | On start, `running` jobs become `uncertain` (may have paid) or `failed`. Never re-run |
| The signer or runner times out after payment | Job `uncertain`; cost classified `uncertain` |
| Batteries unreachable | Cost sync records `cost_sync_error`; costs stay `pending`; jobs still run |
| The usage cursor is rejected | Resync from the start; known rows are skipped (no double application) |
| Discovery fails | The last good snapshot is kept; `discovery_error` is set; it goes stale after three intervals |
| Admission limit reached | 503 + `Retry-After` before any payment |

There is no HA, exactly-once or SLA. See
[contracts/engine.md](contracts/engine.md#deployment-performance-and-recovery-assumptions).
