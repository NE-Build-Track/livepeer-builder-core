# Reliability

## Configuration

Settings come from `LPB_<FIELD>` environment variables (`EngineSettings.from_env`). Defaults favor
safety: a durable SQLite file and admission limits on. Payments are always `per_actor`: no setting
chooses an allocation mode or holds a payment key (D82). Three variables were renamed on 18 November
2026 (#163), and an old one is ignored, not read as the new one: rename them before starting the new
release ([what changes](contracts/index.md#upgrading-to-the-domain-language-names)).

| Variable | Default | Meaning |
| --- | --- | --- |
| `LPB_DATABASE_URL` | `sqlite+aiosqlite:///./livepeer-builder.db` | Or `postgresql+asyncpg://…` (with the `[postgres]` extra) |
| `LPB_AUTO_MIGRATE` | `true` | Migrate on start. Production may set `false` and run `lpb migrate`. With `false`, `start()` compares the database's revision with the package's and refuses to start on any mismatch, behind or ahead. The message names both revisions and the database that was checked, says to run `lpb migrate` against that database when it is behind, and says to start the release that migrated it when its revision is one this package does not know. It checks at start only, so replicas already running through a rolling deploy keep running |
| `LPB_SIGNER_URL` | unset | The remote signer, for payments only. Refused at startup with userinfo, a query or a fragment: the engine logs this URL (D58) |
| `LPB_DISCOVERY_URL` | unset; required to start | `GET` this URL for livepeer/node's `/discovery` contract ([contract](contracts/discovery.md)). The engine does not start without it: settings without it are read, and `start()` refuses, naming the variable, before it polls or writes anything (#154). `BuilderEngine.create` has by then opened the database, and migrated it when `LPB_AUTO_MIGRATE` is `true`. So `lpb serve` and `build_app` refuse before they open it, and an application that starts its own engine calls `settings.require_discovery_url()` before `create` ([embedded mode](guides/embedded-mode.md#1-construct-and-start-the-engine-once)). An empty value is unset. No credential is sent. Must be an `http://` or `https://` URL with a host (a bare `host:port` is refused), with no userinfo, query or fragment; one that is not is refused when settings are read. Redirects are not followed: a 3xx is a failed refresh. Each poll has 5 s in all. A failed poll keeps the last good snapshot. A record the reader cannot use is skipped (`discovery.inventory_skipped`); an answer with records and no readable runner is a failed poll (#114, C28) |
| `LPB_BATTERIES_URL`, `LPB_BATTERIES_TOKEN` | unset | Management API and the least-privilege credential (`grants.fund`, not `grants.*`) |
| `LPB_BATTERIES_GRANTS_TOKEN` | unset | `grants.read`, `grants.create`, `grants.fund`, `grants.status`, `allocations.read` and, for the wallet report (#167), `escrow.read`. Optional: without it the tenant desk is off (`payments.tenants` is `None`; onboarding, the tenant list, a grant's read, a tenant's status and the wallet report are absent) |
| `LPB_BATTERIES_TIMEOUT_S` | `30` | Each Batteries HTTP request |
| `LPB_SECRET_KEY` | unset | Fernet key for stored payment keys (`lpb gen-secret-key`, or `livepeer_builder.generate_secret_key()` in code). Required with a payment provider when the built-in secret store is used: without it `lpb serve`, `lpb smoke` and an engine an application starts refuse to start ([payments.md](contracts/payments.md#allotment)). `lpb purge` and `lpb bootstrap-admin` never allot, so they do not need it (#106 review) |
| `LPB_OPERATION_STALE_S` | `120` | How long an admin command may stay `started` in the operation ledger before a repeat resumes it rather than answering 409 `operation_in_progress` (D57, Q3). At least four times `LPB_BATTERIES_TIMEOUT_S`, or the engine refuses to start: a resume must not overlap a call the earlier run may still have in flight |
| `LPB_ENGINE_ID` | `livepeer-builder` | Namespaces this engine's idempotency keys at the payment provider; at least one character |
| `LPB_MAX_PAYMENT_CHALLENGE_RETRIES` | `3` | Maximum retries per runner call for payments that answer HTTP 402 challenges (≥ 0). The first challenge payment plus retries allows up to 4 challenge signatures at the default. Metered live sessions sign renewal payments separately, outside this cap. The call has a separate budget of the same size for unsigned requests, including the initial request and requests for fresh challenges after a signer HTTP 480. A runner HTTP 402 after the last permitted challenge payment ends the attempt with `http` (402). A signer HTTP 480 that exhausts the unsigned-request budget ends the attempt with `payment_refused`. |
| `LPB_DEFAULT_CURRENCY` | `USD` | Grant and allocation currency (`USD` or `ETH`) |
| `LPB_MAX_CONCURRENT_JOBS`, `LPB_MAX_CONCURRENT_JOBS_PER_APP` | `64`, unset | Admission limits (503 + `Retry-After`, before payment) |
| `LPB_MAX_PAID_ATTEMPTS` | `2` | Paid attempts a job may fail over from (1 to 10; `1` turns paid failover off). The operator pays for them, so the caller's `max_attempts` cannot raise it (D55) |
| `LPB_DISCOVERY_INTERVAL_S`, `LPB_COST_SYNC_INTERVAL_S` | `15`, `30` | Background loops |
| `LPB_RATE_MAX_AGE_S` | `600` | An orchestrator-stamped rate older than this makes its offering `stale` ([engine.md](contracts/engine.md#rates-iss-10)). Catalog policy (D62 (3)). The default is two of livepeer/node `livepeer-discovery`'s default 5-minute refreshes |
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
| `LPB_JWT_ACTOR_CLAIM`, `LPB_JWT_TENANT_CLAIM`, `LPB_JWT_TENANT_ID` | `sub`, unset, unset | Which claims become `actor_id` and `tenant_id`. With `jwt` in `LPB_AUTH`, set exactly one of `LPB_JWT_TENANT_CLAIM` (the claim that names each token's tenant) and `LPB_JWT_TENANT_ID` (the one tenant every token is in): neither has a default, and with neither or both the engine refuses to start (#163 review) |
| `LPB_JWT_SCOPE_CLAIM`, `LPB_JWT_SCOPE_PREFIX`, `LPB_JWT_ADMIN_SCOPE` | `scope`, empty, unset | Scopes named after operations become permissions; the admin scope makes an engine-wide admin credential, so it may not be an operation or a standard OpenID scope |
| `LPB_JWT_NAME_CLAIM` | `preferred_username` | The person's display name, recorded on first sign-in for the operator's actor list (D48); empty turns it off |
| `LPB_JWT_ADMIN_READ_SCOPE` | unset | Makes an auditor, a read-only admin, for example a dashboard's service account; the admin scope wins when both are present. Like the admin scope, it may not be an operation, a standard OpenID scope, or the admin scope itself |
| `LPB_JWT_LEEWAY_S` | `30` | Clock skew allowed on `exp` and `nbf` |
| `LPB_HTTP_HOST`, `LPB_HTTP_PORT`, `LPB_LOG_JSON` | `127.0.0.1`, `8080`, `true` | Service and logging. With a private listener, this address is the public listener |
| `LPB_CLEARINGHOUSE_URL`, `LPB_CLEARINGHOUSE_TOKEN`, `LPB_CLEARINGHOUSE_TENANT_ID` | unset | An engine that uses a payment clearinghouse (D84, #168): the address of its public listener (a scheme, a host and an optional port, and nothing after them), the tenant admin key (a secret) and the tenant's id. Set together or not at all, and never with a Batteries setting, which is refused at startup. Allotment, the allowance and usage then reach the clearinghouse, and `LPB_DISCOVERY_URL` defaults to its runner list ([payments.md](contracts/payments.md#the-remote-adapters)). `LPB_SIGNER_URL` names the clearinghouse's published signer, where the engine signs itself; unset, the engine does not pay. `LPB_SECRET_KEY` is still required: the engine keeps the key each allotment and rotation hands over |
| `LPB_CLEARINGHOUSE_TIMEOUT_S` | `20` | Each request to the clearinghouse. `LPB_OPERATION_STALE_S` must be at least six times it: a command is cancelled at half the stale window, and the longest one makes three calls to the clearinghouse ([payments.md](contracts/payments.md#the-remote-adapters)) |
| `LPB_HTTP_PRIVATE_PORT` | unset | Turns the private listener on (D84, #167). Unset, the engine serves one listener with every route, as before. Set (1 to 65535), `lpb serve` serves two: the public one without the routes only a clearinghouse admin calls, where a clearinghouse admin credential is 403, and the private one with every route, for a clearinghouse admin only ([access.md](contracts/access.md#listeners)). Refused at startup when it is the public listener's port on the same host |
| `LPB_HTTP_PRIVATE_HOST` | `127.0.0.1` | Where the private listener binds. Read only with `LPB_HTTP_PRIVATE_PORT`. Loopback unless set: publish the public listener, never this one |
| `LPB_HTTP_MAX_REQUEST_BYTES` | `16777216` (16 MiB) | A `/v1/` request body over it is refused with 413 `request_too_large` before it is read (D59); at least 1024. It fits an 8 MiB `raw_body` as base64 |

## Operations

`lpb migrate`, `revision`, `backup` and `restore` read only `LPB_DATABASE_URL`, so another setting
that would not load does not stop them. `lpb purge`, `bootstrap-admin` and `smoke` load every setting but
do not need `LPB_DISCOVERY_URL`. `purge` and `bootstrap-admin` build an engine and never start it, and
only an engine that starts needs the address (#154). `smoke` starts one: without the variable it uses an
address nothing listens on, so its one poll is refused. `lpb serve`, and any engine an application
starts, still require it (#112, C23): `serve` exits with code 1 before it opens the database, and the
error names the variable. With a payment provider, `purge` and
`bootstrap-admin` also do not need `LPB_SECRET_KEY`, since they allot nothing; `smoke` and `serve` do.
`gen-secret-key`, `verify` and `gen-docs` read no setting.

| Command | When |
| --- | --- |
| `lpb migrate` / `lpb revision` | Before starting a new version; `revision` exits 2 when behind |
| `lpb bootstrap-admin` | Once per deployment; prints the first admin key once |
| `lpb gen-secret-key` | Once; store the key in your secret manager, never in the database |
| `lpb serve` | Run the REST service (`[server]` extra) |
| `lpb payments check` | Read-only: every request is a GET, no engine is built and no database is opened (D87, #168). A self-hosted engine probes the signer and Batteries; an engine that uses a clearinghouse probes its grant, its usage and the runner list there, and the clearinghouse's signer at `LPB_SIGNER_URL`. Exit 0 when every probe passed, 1 when one was refused or failed, 2 when the settings are refused. It prints setting names, never a credential or an address ([payments.md](contracts/payments.md#lpb-payments-check)) |
| `lpb smoke` | Import, migrate, start, health, stop; used by CI and after an install. It ignores `LPB_DATABASE_URL` and runs on a throwaway SQLite file, so it never touches a live database: starting an engine there resolves its running jobs as interrupted. To check the configured database, run `lpb revision`, which only reads |
| `lpb backup FILE` / `lpb verify FILE` / `lpb restore FILE --yes` | SQLite backup and restore (below) |
| `lpb purge` | Apply retention now |

**Two listeners in one process (#167).** With `LPB_HTTP_PRIVATE_PORT` set, `lpb serve` binds both
addresses, and listens on both, before the engine starts. An address that is taken is logged, the one
opened before it is released, and the process exits with the status it exits with for one listener; the
engine has not started. That holds for a wildcard host and a specific one on the same port (`0.0.0.0`
and the private listener's default of `127.0.0.1`), which the system refuses only when the second one
listens. The engine starts once, before either listener accepts a request, and stops once, after both
have ended. Neither serves without the other: SIGINT or SIGTERM ends both in order, each waiting for its
open connections (a second Ctrl-C does not wait for them), and a listener that ends for a reason of its
own ends the other. That other one does not wait: it hangs up on its open connections, as after a
second Ctrl-C. A request still running then is cancelled, and `serve()` waits for it before it stops
the engine, so a paid job in flight ends as a cancelled job does (`uncertain`, its paid attempt kept)
and is not left `running` under an engine that has stopped. The app's shutdown handlers run after
that, once, as they do when the listener ends in order; the engine still stops in order. The process then ends, where it used to stay up for as long as one
connection was held open, with a listener gone and nothing to restart it. `lpb serve` then
exits 0. An application that runs `serve()` in a task of its own stops it by cancelling that task, and
off the main thread, where no signal arrives, that is the only way. A cancel does not wait for requests
in flight, with one listener or two: each listener is closed and hangs up on its open connections, the
requests still running are cancelled and waited for, the app's shutdown handlers run, the engine stops
in order, and the task ends with its cancellation, within a moment and not when the last request would
have ended. Nothing of the server outlives `serve()`. A handler that does not end when it is cancelled,
or a shutdown handler that never returns, holds `serve()`. A cancel that arrives after a first signal, or a
second cancel, does the same. Whichever way `serve()` ends, every listener has ended, its port is free
again, and the process has the signal handlers it had before. Both listeners answer `/livez` and `/readyz`, from the one
engine, so a health check may use either. The log line `http.listeners_bound` names both addresses.

Health is reported by `GET /readyz` (and `engine.health()`): discovery freshness and errors,
the last cost sync, its error and the count of usage rows it has set aside (`cost_sync_set_aside`),
running jobs and the version. It no longer reports an `allocation_mode`: `per_actor` is the only one
(D82). Logs are JSON lines; every line has an `event` field (for example `jobs.recovered` or
`costs.sync_failed`).

`/readyz` needs no credential, so `discovery_error` and `cost_sync_error` are short fixed codes, never
an exception's text (#113, C7). The text, which can quote a URL, a provider's words or a store error's
SQL and parameters, goes to the log with the code, in `discovery.failed` and `costs.sync_failed`:

| Code | Meaning |
| --- | --- |
| `unavailable` | The discovery source or the usage source gave no answer (a timeout, a refused connection), answered with a status that is not 2xx (an error, or a redirect), or raised anything not listed below |
| `invalid_answer` | It answered 2xx with something the engine could not read: a content encoding that does not decode, not JSON, not the expected shape, a value it cannot convert, or a discovery document with records and no readable runner |
| `store_error` | Cost sync failed after the usage source answered: writing or matching the usage in the engine's store |

The codes are a closed set in the contract (`SourceFailureCode` for `discovery_error`, `FailureCode` for
`cost_sync_error`), so the OpenAPI schema lists them and nothing else can be written there. The shipped
adapters decide the code where they read the answer; a supplied source's failure is read by its
exception type (a `ValueError`, `KeyError`, `TypeError` or `ArithmeticError` is `invalid_answer`).

`scripts/live_probe.py` checks a running stack over REST: access, revocation and one repeated top-up
(D61). Name the stack with `LPB_PROBE_URL` and `LPB_PROBE_ADMIN_KEY`; it spends only with `--yes` and
`LPB_PROBE_FUND_ACTOR`, at most a few cents. See [m3-traceability](product-specs/m3-traceability.md#live-probe).

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
| Batteries unreachable | Cost sync records `cost_sync_error` (`unavailable`); costs stay `pending`; jobs still run |
| The usage cursor is rejected | Resync from the start; known rows are skipped (no double application) |
| Batteries answers a 2xx the engine cannot read | Not a JSON object, a list page whose `items` is not a list of objects or whose `next_cursor` is not a string, a list page whose `next_cursor` is the cursor it was asked with, or a grant, a key or an allocation with no `id` or with a field that is not text: a provider error. An admin command answers 502 and its `operation_ref` stays held (an unknown outcome, resumed after `LPB_OPERATION_STALE_S`); cost sync fails that pass with `cost_sync_error` `invalid_answer` and asks for the page again. Never read as an empty answer (#114, C21). A redirect (3xx) is the same provider error whatever its body, and never the call's answer; as `cost_sync_error` it is `unavailable` |
| Batteries delivers a usage row the engine cannot read or store | The row is set aside and the sync goes on: an unknown status, no id or timestamp, a fee that is not a finite number, or a text the store's column cannot keep (too long, a NUL, a lone surrogate). Its id, the reason and its fees are one row of `lpb_usage_set_aside`; a WARNING `costs.usage_set_aside` carries its content, once. `cost_sync_set_aside` in `/readyz` counts the rows left out in all, and `rows_set_aside` in the sync report the new ones. No job's cost includes the row, so reconciliation may not balance until it is accounted for (#114, C13; [payments.md](contracts/payments.md#cost-classification)) |
| Two cost syncs run at once (the background loop and `POST /v1/costs/sync`) | Both end. Each usage row is stored once and each set-aside row recorded once, by whichever sync writes it first; the row is not new to the other, which counts and logs nothing for it. Before, the slower sync failed on the row's primary key and its pass ended before its cursor moved (review of #144) |
| An orchestrator's payment challenge has a manifest id the store cannot keep | Refused before signing (`payment_refused`): nothing stored or paid, and the job fails over (#114, C13) |
| Batteries delivers a known usage row id again with another fee | The stored copy is kept: it is not a correction, and only a new row id changes a job's cost. A WARNING `costs.usage_redelivered` carries the row id and both fees (D61) |
| Discovery fails | The last good snapshot is kept; `discovery_error` is set to a code; it goes stale after three intervals |
| Discovery answers with records and no readable runner | A failed refresh, not an empty inventory: the last good snapshot is kept and goes stale after three intervals; `discovery_error` is `invalid_answer`; `discovery.inventory_skipped` and `discovery.failed` carry the counts of skipped entries and runners. An empty answer (`[]`) still empties the catalog (#114, C28) |
| livepeer/node's `livepeer-discovery` fails a refresh | A TLS failure, a non-200, or a document `Decode` rejects keeps that source's last successful snapshot until `MaxFailedRefreshes` consecutive failures (default 6). The dev stack leaves that default and refreshes every 30s. A source that has never succeeded stays out. A runner with no `observed_at`, or one more than 30s ahead, is skipped inside `Decode` and does not drop the source when another runner in the document is valid. From v1.0.7 the same holds for a runner with a wrongly typed optional field, a repeated key or a key in another letter case (node #82). The engine keeps its own last good snapshot ([contract](contracts/discovery.md)) |
| Every runner for an app advertises zero capacity | A job that would sign no payment (unpaid, or the runners are free or have no price the signer pays) tries them anyway, in selection order: the advertisement can lag. If each refuses, the job fails `refused`, 503 + `Retry-After: 5`. A job that would pay them is not tried: it fails `no_capacity` before any attempt, 503 + `Retry-After: 5`, with nothing signed, because a full runner's refusal can cost a ticket: livepeer/node before v1.0.7 asks for payment before it checks capacity (#114, C17; [contract](contracts/discovery.md#advertised-capacity)) |
| Admission limit reached | 503 + `Retry-After` before any payment |
| A streaming client disconnects | The stream ends (`abandoned`, or `succeeded` if the runner closes within `LPB_STREAM_SETTLE_S`) and its slot is released |
| The store fails while a stream ends | The slot is released; the job stays `running` until the next start resolves it (`jobs.stream_finish_failed` in the log) |
| `start()` fails (a store error, or a cancellation) | The engine stops itself and closes what `create` opened before the error reaches the caller; create a new engine |
| `start()` again on a running engine | Refused with `RuntimeError`; the running engine and its one set of background loops carry on (#113, C27). The REST app's lifespan (`create_app(engine)`) on a running engine is refused the same way and does not stop it |
| The engine stops with open streams | They get `LPB_STREAM_DRAIN_TIMEOUT_S` to finish, then end `abandoned`; `stop()` returns only after every end is written |

There is no HA, exactly-once or SLA. See
[contracts/engine.md](contracts/engine.md#deployment-performance-and-recovery-assumptions).
