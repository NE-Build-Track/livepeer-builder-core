# Reliability

## Configuration

Settings come from `LPB_<FIELD>` environment variables (`EngineSettings.from_env`). Defaults favor
safety: a durable SQLite file, `per_actor` payments, and admission limits on.

| Variable | Default | Meaning |
| --- | --- | --- |
| `LPB_DATABASE_URL` | `sqlite+aiosqlite:///./livepeer-builder.db` | Or `postgresql+asyncpg://…` (with the `[postgres]` extra) |
| `LPB_AUTO_MIGRATE` | `true` | Migrate on start. Production may set `false` and run `lpb migrate` |
| `LPB_SIGNER_URL` | unset | The remote signer, for payments only |
| `LPB_DISCOVERY_URL` | unset | A livepeer-node `/discovery` inventory to read runners from: the discovery service (`lpb discovery`, [contract](contracts/discovery.md)), livepeer-node's own, or one orchestrator. Unset → empty static discovery. No credential is sent |
| `LPB_ALLOCATION_MODE` | `per_actor` | Or `per_application` with `LPB_SHARED_PAYMENT_KEY` (and `LPB_SHARED_ALLOCATION_ID` for reconciliation) |
| `LPB_BATTERIES_URL`, `LPB_BATTERIES_TOKEN` | unset | Management API and the least-privilege credential (`grants.fund`, not `grants.*`) |
| `LPB_BATTERIES_OPERATOR_TOKEN` | unset | `grants.read` and `grants.create`. Optional: without it the grant desk is off (`payments.grants` is `None`; the grant list and create routes are absent) |
| `LPB_BATTERIES_TIMEOUT_S` | `30` | Each Batteries HTTP request |
| `LPB_SECRET_KEY` | unset | Fernet key for stored payment keys (`lpb gen-secret-key`, or `livepeer_builder.generate_secret_key()` in code). Required to provision in `per_actor` mode |
| `LPB_ENGINE_ID` | `livepeer-builder` | Namespaces this engine's idempotency keys at the payment provider; at least one character |
| `LPB_MAX_PAYMENT_CHALLENGE_RETRIES` | `3` | How many times one runner call may retry a 402 payment challenge (≥ 0) |
| `LPB_DEFAULT_CURRENCY` | `USD` | Grant and allocation currency (`USD` or `ETH`) |
| `LPB_MAX_CONCURRENT_JOBS`, `LPB_MAX_CONCURRENT_JOBS_PER_APP` | `64`, unset | Admission limits (503 + `Retry-After`, before payment) |
| `LPB_DISCOVERY_INTERVAL_S`, `LPB_COST_SYNC_INTERVAL_S` | `15`, `30` | Background loops |
| `LPB_STREAM_FINALIZE_TIMEOUT_S` | `10` | The most a stream's closing writes may take; the slot is released regardless (D34) |
| `LPB_STREAM_DRAIN_TIMEOUT_S` | `30` | On stop, how long open streams may finish before they are abandoned (D36) |
| `LPB_STREAM_SETTLE_S` | `1` | After a disconnect or cancel, how long the runner may take to end the stream and count as succeeded; `0` turns it off (D38) |
| `LPB_STREAM_OPEN_GRACE_S` | `5` | How far past its first-byte deadline a transport's open may run before the engine stops it |
| `LPB_RETENTION_JOBS_DAYS`, `LPB_RETENTION_USAGE_DAYS` | unset | See [contracts/persistence.md](contracts/persistence.md#retention) |
| `LPB_RETENTION_INTERVAL_S` | `21600` (6 h) | How often the retention purge runs when a retention setting is on |
| `LPB_AUTH` | `api_keys` | `api_keys`, `jwt`, or `jwt,api_keys`: which credentials the REST service accepts ([access.md](contracts/access.md#jwt-bearer)). `jwt` alone turns engine API keys off |
| `LPB_JWT_ISSUER`, `LPB_JWT_AUDIENCE` | unset | Required with `jwt`: the exact `iss` (`https`), and the value `aud` must contain |
| `LPB_JWT_JWKS_URL` | unset | The issuer's keys (`https`); unset, found through the issuer's OpenID or RFC 8414 metadata |
| `LPB_JWT_ALLOW_HTTP` | `false` | Development only: allows an `http` issuer and key URL, such as a local Keycloak |
| `LPB_JWT_TOKEN_TYPES` | `at+jwt,application/at+jwt` | Accepted `typ` headers (RFC 9068). Adding `jwt` accepts plain JWTs and drops the `client_id` and `jti` requirements: weaker |
| `LPB_JWT_MAX_LIFETIME_S` | `3600` | A token whose `exp - iat` is longer is refused |
| `LPB_JWT_ALGORITHMS` | `RS256,ES256` | Allowed signature algorithms, asymmetric only |
| `LPB_JWT_ACTOR_CLAIM`, `LPB_JWT_APPLICATION_CLAIM`, `LPB_JWT_APPLICATION_ID` | `sub`, unset, `default` | Which claims become `actor_id` and `application_id` |
| `LPB_JWT_SCOPE_CLAIM`, `LPB_JWT_SCOPE_PREFIX`, `LPB_JWT_ADMIN_SCOPE` | `scope`, empty, unset | Scopes named after operations become permissions; the admin scope makes an engine-wide admin credential, so it may not be an operation or a standard OpenID scope |
| `LPB_JWT_NAME_CLAIM` | `preferred_username` | The person's display name, recorded on first sign-in for the operator's actor list (D48); empty turns it off |
| `LPB_JWT_ADMIN_READ_SCOPE` | unset | Makes an auditor, a read-only admin, for example a dashboard's service account; the admin scope wins when both are present. Like the admin scope, it may not be an operation, a standard OpenID scope, or the admin scope itself |
| `LPB_JWT_LEEWAY_S` | `30` | Clock skew allowed on `exp` and `nbf` |
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
| The discovery service loses an orchestrator | Its last inventory is served until `LPD_MAX_FAILED_REFRESHES` consecutive failed probes, then evicted. A failed directory load (an RPC outage, for the on-chain directory) keeps the previous directory ([contract](contracts/discovery.md#probing-and-caching)) |
| Admission limit reached | 503 + `Retry-After` before any payment |
| A streaming client disconnects | The stream ends (`abandoned`, or `succeeded` if the runner closes within `LPB_STREAM_SETTLE_S`) and its slot is released |
| The store fails while a stream ends | The slot is released; the job stays `running` until the next start resolves it (`jobs.stream_finish_failed` in the log) |
| `start()` fails (a store error, or a cancellation) | The engine stops itself and closes what `create` opened before the error reaches the caller; create a new engine |
| The engine stops with open streams | They get `LPB_STREAM_DRAIN_TIMEOUT_S` to finish, then end `abandoned`; `stop()` returns only after every end is written |

There is no HA, exactly-once or SLA. See
[contracts/engine.md](contracts/engine.md#deployment-performance-and-recovery-assumptions).
