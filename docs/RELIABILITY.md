# Reliability

## Configuration

Settings come from `LPB_<FIELD>` environment variables (`EngineSettings.from_env`). Defaults favor
safety: a durable SQLite file and admission limits on. Payments are always `per_actor`: no setting
chooses an allocation mode or holds a payment key (D82). Three variables were renamed on 18 November
2026 (#163), and an old one is ignored, not read as the new one: rename them before starting the new
release ([what changes](contracts/index.md#upgrading-to-the-domain-language-names)).

| Variable | Default | Meaning |
| --- | --- | --- |
| `LPB_ROLE` | `both` | Which half of the engine this deployment runs (D84, #169): `both`, `work` or `clearinghouse`. It chooses the routes that are mounted, what the payment ports reach and what the engine starts ([Roles](#roles)). Any other value is refused at startup, and so is a setting that contradicts the role: the refusal names settings and echoes no value |
| `LPB_DATABASE_URL` | `sqlite+aiosqlite:///./livepeer-builder.db` | Or `postgresql+asyncpg://…` (with the `[postgres]` extra) |
| `LPB_AUTO_MIGRATE` | `true` | Migrate on start. Production may set `false` and run `lpb migrate`. With `false`, `start()` compares the database's revision with the package's and refuses to start on any mismatch, behind or ahead. The message names both revisions and the database that was checked, says to run `lpb migrate` against that database when it is behind, and says to start the release that migrated it when its revision is one this package does not know. It checks at start only, so replicas already running through a rolling deploy keep running |
| `LPB_SIGNER_URL` | unset | The remote signer, for payments only. Refused at startup with userinfo, a query or a fragment: the engine logs this URL (D58) |
| `LPB_DISCOVERY_URL` | unset; required to start | `GET` this URL for livepeer/node's `/discovery` contract ([contract](contracts/discovery.md)). The engine does not start without it: settings without it are read, and `start()` refuses, naming the variable, before it polls or writes anything (#154). `BuilderEngine.create` has by then opened the database, and migrated it when `LPB_AUTO_MIGRATE` is `true`. So `lpb serve` and `build_app` refuse before they open it, and an application that starts its own engine calls `settings.require_discovery_url()` before `create` ([embedded mode](guides/embedded-mode.md#1-construct-and-start-the-engine-once)). An empty value is unset. No credential is sent. Must be an `http://` or `https://` URL with a host (a bare `host:port` is refused), with no userinfo, query or fragment; one that is not is refused when settings are read. Redirects are not followed: a 3xx is a failed refresh. Each poll has 5 s in all. A failed poll keeps the last good snapshot. A record the reader cannot use is skipped (`discovery.inventory_skipped`); an answer with records and no readable runner is a failed poll (#114, C28) |
| `LPB_BATTERIES_URL`, `LPB_BATTERIES_TOKEN` | unset | Management API and the least-privilege credential (`grants.fund`, not `grants.*`) |
| `LPB_BATTERIES_GRANTS_TOKEN` | unset | `grants.read`, `grants.create`, `grants.fund`, `grants.status`, `allocations.read` and, for the wallet report (#167), `escrow.read`. Optional: without it the tenant desk is off (`payments.tenants` is `None`; onboarding, the tenant list, a grant's read, a tenant's status and the wallet report are absent) |
| `LPB_BATTERIES_TIMEOUT_S` | `30` | Each Batteries HTTP request |
| `LPB_SECRET_KEY` | unset | Fernet key for stored payment keys (`lpb gen-secret-key`, or `livepeer_builder.generate_secret_key()` in code). Required with a payment provider when the built-in secret store is used, by an engine that stores allocation keys, which is one in its `both` or its `work` role: without it `lpb serve`, `lpb smoke` and an engine an application starts refuse to start ([payments.md](contracts/payments.md#allotment)). An engine in its `clearinghouse` role stores no allocation's key and needs none ([Roles](#roles)). `lpb purge` and `lpb bootstrap-admin` never allot, so they do not need it (#106 review) |
| `LPB_OPERATION_STALE_S` | `120` | How long an admin command may stay `started` in the operation ledger before a repeat resumes it rather than answering 409 `operation_in_progress` (D57, Q3). At least four times `LPB_BATTERIES_TIMEOUT_S`, or the engine refuses to start: a resume must not overlap a call the earlier run may still have in flight |
| `LPB_ENGINE_ID` | `livepeer-builder` | Namespaces this engine's idempotency keys at the payment provider; at least one character |
| `LPB_MAX_PAYMENT_CHALLENGE_RETRIES` | `3` | Maximum retries per runner call for payments that answer HTTP 402 challenges (≥ 0). The first challenge payment plus retries allows up to 4 challenge signatures at the default. Metered live sessions sign renewal payments separately, outside this cap. The call has a separate budget of the same size for unsigned requests, including the initial request and requests for fresh challenges after a signer HTTP 480. A runner HTTP 402 after the last permitted challenge payment ends the attempt with `http` (402). A signer HTTP 480 that exhausts the unsigned-request budget ends the attempt with `payment_refused`. |
| `LPB_DEFAULT_CURRENCY` | `USD` | Grant and allocation currency (`USD` or `ETH`) |
| `LPB_MAX_CONCURRENT_JOBS`, `LPB_MAX_CONCURRENT_JOBS_PER_APP` | `64`, unset | Admission limits (503 + `Retry-After`, before payment) |
| `LPB_MAX_PAID_ATTEMPTS` | `2` | Paid attempts a job may fail over from (1 to 10; `1` turns paid failover off). The operator pays for them, so the caller's `max_attempts` cannot raise it (D55) |
| `LPB_ASYNC_WORKERS` | `4` | Async jobs dispatched at once (at least 1), inside the admission limits above, which immediate jobs and streams share ([async jobs](contracts/engine.md#async-jobs), D69). A job keeps its worker and its admission slot until its end is written, so one whose end the database refused holds them until the database answers |
| `LPB_ASYNC_MAX_QUEUED` | `1000` | Async jobs that may wait at once, a hard limit: it holds for submits at the same moment. A submit beyond it is 503 + `Retry-After`, with nothing stored. The stored requests of a full queue are at most this many times the request limit: about 16 GiB at the defaults (1000 jobs, 16 MiB) |
| `LPB_ASYNC_QUEUE_TTL_S` | `3600` (1 h) | An async job still queued this long after its submit ends `failed`, `no_capacity`, with nothing paid. It is how long a job waits for a worker, and for a runner to be listed or to have room, or its actor's credential to be readable: none of those fails a queued job by itself |
| `LPB_ASYNC_RESULT_MAX_BYTES` | `16777216` (16 MiB) | The largest answer the engine stores for an async job. A larger answer is not stored: the job ends `failed`, `result_not_stored`, and its payment is `failed_after_payment`. It also bounds the memory one answer takes in a worker: the answer is read in pieces, and one that grows past the limit is measured to its end and not kept ([engine.md](contracts/engine.md#results-of-an-async-job)). The answers the workers hold are then bounded by `LPB_ASYNC_WORKERS` times this, and twice that while kept answers are being stored |
| `LPB_ASYNC_POLL_S` | `1` | How often the async worker looks at the queue while jobs wait. A submit wakes it at once, and an engine with nothing queued does not look. It is also how long the worker backs off after a step that failed, how long a job that was given back or whose claim failed is left alone (doubling each time, up to 32 intervals), and how often an end the store refused is written again |
| `LPB_ASYNC_DRAIN_TIMEOUT_S` | `30` | On stop, how long running async jobs may take to end before they are cancelled: one stopped before its first attempt goes back to the queue (on the third claim that counts it ends `failed`, `interrupted`; a claim given back by a job that only had to wait does not count), and any other ends `uncertain` when it may have paid |
| `LPB_DISCOVERY_INTERVAL_S`, `LPB_COST_SYNC_INTERVAL_S` | `15`, `30` | Background loops |
| `LPB_RATE_MAX_AGE_S` | `600` | An orchestrator-stamped rate older than this makes its offering `stale` ([engine.md](contracts/engine.md#rates-iss-10)). Catalog policy (D62 (3)). The default is two of livepeer/node `livepeer-discovery`'s default 5-minute refreshes |
| `LPB_STREAM_FINALIZE_TIMEOUT_S` | `10` | The most a stream's closing writes may take; the slot is released regardless (D34) |
| `LPB_STREAM_DRAIN_TIMEOUT_S` | `30` | On stop, how long open streams may finish before they are abandoned (D36) |
| `LPB_STREAM_SETTLE_S` | `1` | After a disconnect or cancel, how long the runner may take to end the stream and count as succeeded; `0` turns it off (D38) |
| `LPB_STREAM_OPEN_GRACE_S` | `5` | How far past its first-byte deadline a transport's open may run before the engine stops it |
| `LPB_RETENTION_JOBS_DAYS`, `LPB_RETENTION_USAGE_DAYS` | unset | See [contracts/persistence.md](contracts/persistence.md#retention) |
| `LPB_RETENTION_RESULTS_DAYS` | `7` | How long an async job's stored result is kept after the job ended (at least 1). In code, `EngineSettings(retention_results_days=None)` keeps it as long as its job. The job keeps its record ([persistence.md](contracts/persistence.md#retention)) |
| `LPB_RETENTION_INTERVAL_S` | `21600` (6 h) | How often the retention purge runs, in every engine that runs jobs. With job or usage retention on, the first purge runs at start; otherwise one interval after it, so the default settings add no write at start. With every retention setting off it still runs: a result stored while results had a time keeps its `expires_at` and is deleted when that time comes |
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
| `LPB_CLEARINGHOUSE_URL`, `LPB_CLEARINGHOUSE_TOKEN`, `LPB_CLEARINGHOUSE_TENANT_ID` | unset | The payment clearinghouse of an engine in its `work` role (D84, #168, #169): the address of its public listener (a scheme, a host and an optional port, and nothing after them), the tenant admin key (a secret) and the tenant's id. Required with `LPB_ROLE=work`, set together, and refused at startup with any other role or beside a Batteries setting. Allotment, the allowance and usage then reach the clearinghouse, and `LPB_DISCOVERY_URL` defaults to its runner list ([payments.md](contracts/payments.md#the-remote-adapters)). `LPB_SIGNER_URL` names the clearinghouse's published signer, where the engine signs itself; unset, the engine does not pay. `LPB_SECRET_KEY` is still required: the engine keeps the key each allotment and rotation hands over |
| `LPB_CLEARINGHOUSE_TIMEOUT_S` | `20` | Each request to the clearinghouse. `LPB_OPERATION_STALE_S` must be at least six times it: a command is cancelled at half the stale window, and the longest one makes three calls to the clearinghouse ([payments.md](contracts/payments.md#the-remote-adapters)) |
| `LPB_HTTP_PRIVATE_PORT` | unset | Turns the private listener on (D84, #167). Unset, the engine serves one listener with every route, as before. Set (1 to 65535), `lpb serve` serves two: the public one without the routes only a clearinghouse admin calls, where a clearinghouse admin credential is 403, and the private one with every route but a tenant's own routes, which name no tenant, for a clearinghouse admin only ([access.md](contracts/access.md#listeners)). Required with `LPB_ROLE=clearinghouse` (#169). Refused at startup when it is the public listener's port on the same host |
| `LPB_HTTP_PRIVATE_HOST` | `127.0.0.1` | Where the private listener binds. Read only with `LPB_HTTP_PRIVATE_PORT`. Loopback unless set: publish the public listener, never this one |
| `LPB_HTTP_MAX_REQUEST_BYTES` | `16777216` (16 MiB) | A `/v1/` request body over it is refused with 413 `request_too_large` before it is read (D59); at least 1024. It fits an 8 MiB `raw_body` as base64 |

### Roles

One program covers three deployments (D84, #169). `LPB_ROLE` says which one this is.

| | `both`, the default | `work` | `clearinghouse` |
| --- | --- | --- | --- |
| Who runs it | An enterprise with self-hosted payments | An enterprise that uses a payment clearinghouse, as an engine server or an embedded engine | A payment clearinghouse |
| Routes | The work routes and the clearinghouse routes | The work routes. Every clearinghouse route is 404 | The clearinghouse routes. Every work route is 404: jobs, the catalog (`GET /v1/offerings`), runners, costs, the allowance read and `/v1/keys` |
| Allotment and rotation | The engine stores each allocation's key for its own jobs and answers none (`deliver_allocation_keys=False`) | No route allots. The engine's own commands store the key its clearinghouse hands over | Each key is handed to the caller once, and none is stored (`deliver_allocation_keys=True`) |
| Payment ports | Batteries and its signer | The clearinghouse, for allocations, the allowance and usage; its own secret store for keys | Batteries |
| Signs | At its signer, `LPB_SIGNER_URL` | At the clearinghouse's published signer, `LPB_SIGNER_URL` | Nothing: it never runs a job. Its transport can only ask the signer's identity, for the wallet report |
| Listeners | One, or two with `LPB_HTTP_PRIVATE_PORT` | One, or two with `LPB_HTTP_PRIVATE_PORT` | Two, always |
| Needs, or it does not start | With Batteries, `LPB_SECRET_KEY` | `LPB_CLEARINGHOUSE_URL`, `LPB_CLEARINGHOUSE_TOKEN`, `LPB_CLEARINGHOUSE_TENANT_ID` and `LPB_SECRET_KEY` | `LPB_HTTP_PRIVATE_PORT`, `LPB_BATTERIES_URL`, `LPB_BATTERIES_TOKEN` and `LPB_BATTERIES_GRANTS_TOKEN`. No `LPB_SECRET_KEY`: it stores no allocation's key |
| `LPB_SIGNER_URL` unset | It starts, and runs its jobs unpaid | It starts, and runs its jobs unpaid | It starts, and its wallet report says the signer is `not_configured` |
| Refuses | The `LPB_CLEARINGHOUSE_*` settings | Any Batteries setting | The `LPB_CLEARINGHOUSE_*` settings: a clearinghouse is no tenant of another |
| Starts | Resolving interrupted jobs, discovery, cost sync, retention, the async worker | The same | Discovery alone, for the runner list it publishes. It holds no job: nothing is resolved, synced, purged or queued, and `LPB_COST_SYNC_INTERVAL_S`, the retention settings and the `LPB_ASYNC_*` settings do nothing |
| Runs jobs | Yes | Yes | No. `jobs.run` refuses an embedding caller too: `InvalidRequest`, from admission, before the job's reference is claimed or anything is recorded. `jobs.streaming` is `None`: the role's default transport opens no stream. With a transport a caller supplies that does, `jobs.streaming.stream` is refused the same way. So are `jobs.submit`, which queues nothing, and a step of the async worker, whose loop never starts (ISS-26) |
| Serves the wallet report and the runner list | Yes, with Batteries | No: it is nobody's clearinghouse | Yes |

- **One place decides who keeps the keys.** `delivers_allocation_keys(role)` in `http/listeners.py`.
  `create_app` asks it for every app, on one listener and on two, and nothing else chooses.
- **The secret key is needed where the engine stores allocation keys:** in its `both` role, the keys
  its Batteries mints, and in its `work` role, the keys its clearinghouse hands over. Without it such an
  engine does not start (#112, C2). An engine in its `clearinghouse` role stores none and is not asked.
- **No role is refused for a missing signer address.** `lpb payments check` fails for it in every role,
  and says what follows: a `both` or a `work` engine runs its jobs unpaid, and a clearinghouse's wallet
  report cannot say whether the signer answers.
- **A clearinghouse runs the signer and signs nothing.** Its `LPB_SIGNER_URL` is the address its own
  engine reaches the signer at, where the wallet report reads the signer's readiness. Enterprise
  engines reach the signer at its published address, which may be another one: neither the wallet
  report nor the clearinghouse's `lpb payments check` tests that address. An enterprise's
  `lpb payments check` does, from its own side. The signer keeps its Batteries authorization webhook,
  which is the only check at signing ([SECURITY.md](SECURITY.md)).
- **What a `clearinghouse` engine needs is judged on the parts it will run with.** Batteries is an
  allocation manager and a grants manager, so a part supplied through `EngineSupply` counts. Without
  one the engine does not start, and the error names the settings.
- **An engine in its `work` role must name an engine in its `clearinghouse` role.** Pointed at an
  engine that keeps the keys itself, a `both` engine, its allotment is answered without a key: the
  command fails with a `ProviderError` that says which role `LPB_CLEARINGHOUSE_URL` must name, and the
  allocation exists there.
- **`/livez` and `/readyz` are served in every role, on every listener.** `/readyz` names the role.
- **`create_app(engine)` mounts the routes of the engine's role.** For an engine in its `clearinghouse`
  role it takes `listener="public"` or `listener="private"`; the app of one listener is refused.
- **An engine server in its `work` role serves no route that allots.** The allocation routes are the
  clearinghouse half's. An embedding app allots through `engine.payments.allot`, which calls the
  clearinghouse and stores the key it hands over (tech debt T41).
- **The two Compose projects and a proof runbook** for a clearinghouse and an enterprise on one host
  are in [deploy/README.md](../deploy/README.md).

## Operations

`lpb migrate`, `revision`, `backup` and `restore` read only `LPB_DATABASE_URL`, so another setting
that would not load does not stop them. `lpb purge`, `bootstrap-admin` and `smoke` load every setting but
do not need `LPB_DISCOVERY_URL`. `purge` and `bootstrap-admin` build an engine and never start it, and
only an engine that starts needs the address (#154). `smoke` starts one: without the variable it uses an
address nothing listens on, so its one poll is refused. `lpb serve`, and any engine an application
starts, still require it (#112, C23): `serve` exits with code 1 before it opens the database, and the
error names the variable. With a payment provider, `purge` and
`bootstrap-admin` also do not need `LPB_SECRET_KEY`, since they allot nothing. `smoke` and `serve` need it
in the roles that store allocation keys, `both` and `work`, and not in the `clearinghouse` role, which
stores none and needs it for no command ([Roles](#roles)). `gen-secret-key`, `verify` and `gen-docs` read
no setting.

| Command | When |
| --- | --- |
| `lpb migrate` / `lpb revision` | Before starting a new version; `revision` exits 2 when behind |
| `lpb bootstrap-admin` | Once per deployment; prints the first admin key once |
| `lpb gen-secret-key` | Once; store the key in your secret manager, never in the database |
| `lpb serve` | Run the REST service (`[server]` extra) |
| `lpb payments check` | Read-only: every request is a GET, no engine is built and no database is opened (D87, #168). It prints the role and probes what that role needs (#169): every role probes the signer at `LPB_SIGNER_URL`; an engine in its `both` or its `clearinghouse` role also probes Batteries, and its grants credential: when one is set, and always in the `clearinghouse` role, where the check fails without it; one in its `work` role probes its grant and its usage at the clearinghouse, and the runner list when that is its discovery address. Exit 0 when every probe passed, 1 when one was refused or failed, 2 when the settings are refused. It prints setting names, never a credential or an address ([payments.md](contracts/payments.md#lpb-payments-check)) |
| `lpb smoke` | Import, migrate, start, health, stop; used by CI and after an install. It ignores `LPB_DATABASE_URL` and runs on a throwaway SQLite file, so it never touches a live database: starting an engine there resolves its running jobs as interrupted. To check the configured database, run `lpb revision`, which only reads. The engine is built in the role `LPB_ROLE` names, from the deployment's settings, and the output names the role (#169): settings that contradict the role, or a `clearinghouse` engine without Batteries, fail here |
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

Health is reported by `GET /readyz` (and `engine.health()`): the engine's role (#169), discovery
freshness and errors, the last cost sync, its error and the count of usage rows it has set aside
(`cost_sync_set_aside`), running jobs, the async jobs waiting (`queued_jobs`) and how long the oldest
has waited in seconds (`oldest_queued_s`, `null` when none waits; ISS-26), and the version. Alert on
`oldest_queued_s`, not on the depth of the queue: a deep queue that drains is healthy, and one old
job means nothing is being claimed. Both numbers are the worker's last count, read without a database
call: a count under way when a job is queued is not kept, so for a moment they may say more than the
queue holds, and never less. An engine in its `clearinghouse` role syncs no
cost, so its `cost_sync_at` stays `null`. It no longer reports an `allocation_mode`: `per_actor` is the only one
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

`cost_sync_error` is the outcome of the last cost sync, whoever started it: the background loop, or an
admin with `POST /v1/costs/sync`. A sync that fails sets the code and writes `costs.sync_failed`, and
the next one that works clears it. Before #158 a failed `POST /v1/costs/sync` answered its error (502
or 503) and left the code as it was, so `/readyz` showed no error until the loop's next pass failed too.
A request refused before any sync (403, or 503 from an engine with no usage source) sets nothing.

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
| The engine dies mid-job | On start, `running` jobs become `uncertain` (may have paid) or `failed`. Never re-run. An async job is the exception before its first attempt: nothing went out, so it goes back to the queue, three claims at most, then `failed`, `interrupted`. A `queued` one stays queued ([restart and stop](contracts/engine.md#restart-and-stop)) |
| The async queue is full (`LPB_ASYNC_MAX_QUEUED`) | A submit is 503 + `Retry-After`; nothing is stored. Jobs already queued run as workers come free |
| An async job waits past `LPB_ASYNC_QUEUE_TTL_S` | `failed`, `no_capacity`, nothing paid, cost `none`. `oldest_queued_s` in `/readyz` shows a queue that is not being drained |
| No runner is listed for a queued job's app, every runner it would pay advertises no room, or its actor's credential cannot be read | The job stays `queued`; other apps' jobs are not held up. One job of such an app is claimed each `LPB_ASYNC_POLL_S` and given back if it has to wait, so the wait costs one claim an app in an interval, whatever the queue's length. The job runs when the cause has passed, or ends `no_capacity` at `LPB_ASYNC_QUEUE_TTL_S`. A discovery read that failed at start therefore fails no queued job ([engine.md](contracts/engine.md#running)) |
| An actor's allocation is revoked while its job is queued | The job fails `not_provisioned`, unpaid, when the worker reaches it, also while its app has no runner or no room: a queued job is asked about its actor first |
| A queued job does not pay (its credential has no key) and every priced runner is full | It runs on a runner that advertises no room, as an immediate job does: only a job that would pay for the refusal waits |
| The database is away when an async job ends | The worker keeps the job and its slot and writes the end again every `LPB_ASYNC_POLL_S` (`jobs.async_end_failed`), with no restart. A paid answer that could not be stored is dropped: the job ends `uncertain`, `runner_error`, and the payment is `failed_after_payment` |
| The async worker's step fails (the database is away, a claim raises) | Logged (`jobs.async_step_failed`) and tried again after `LPB_ASYNC_POLL_S`, not sooner. A job whose claim raises is set aside and the jobs behind it are claimed |
| A job's `timeout_s` runs out between two attempts, or while an attempt's row is being written | No further runner is called or paid: `failed`, `timeout`. An attempt whose row was written and whose runner was then not called is recorded `refused`, unpaid. `timeout_s` is one deadline for all of a job's attempts, from its start (an async job's from its claim) ([engine.md](contracts/engine.md#behavior-the-contract-fixes), 6) |
| A paid async answer is larger than `LPB_ASYNC_RESULT_MAX_BYTES`, or the write of a job's end fails | No bytes are stored and the job is never `succeeded`: `result_not_stored`, or `runner_error`, `uncertain` when paid. Either way the payment is `failed_after_payment`, never billed as delivered |
| The signer or runner times out after payment | Job `uncertain`; cost classified `uncertain` |
| Batteries unreachable | Cost sync records `cost_sync_error` (`unavailable`); costs stay `pending`; jobs still run |
| The usage cursor is rejected | Resync from the start; known rows are skipped (no double application) |
| Batteries answers a 2xx the engine cannot read | A content encoding that does not decode, not a JSON object, a JSON object that names one of its own keys twice, a list page whose `items` is not a list of objects or whose `next_cursor` is not a string, a list page whose `next_cursor` is the cursor it was asked with or one the list was at earlier (A, B, A), a grant, a key or an allocation with no `id` or with a field that is not text, an id or a key's secret with a lone surrogate (#158), or an allocation amount stated as `NaN` or `Infinity`: a provider error. An admin command answers 502 and its `operation_ref` stays held (an unknown outcome, resumed after `LPB_OPERATION_STALE_S`); cost sync fails that pass with `cost_sync_error` `invalid_answer` and asks for the page again. Never read as an empty answer (#114, C21). A redirect (3xx) is the same provider error whatever its body, and never the call's answer; as `cost_sync_error` it is `unavailable` |
| The payment provider answers a new key or a new grant with an id the engine cannot keep | An `allocation_id`, a key `id` or a grant `id` of more than 200 characters, or with a NUL or a lone surrogate, from Batteries, a payment clearinghouse or a supplied allocation manager: a provider error, on SQLite and on PostgreSQL. So is a grant `id` that is not one path segment (a `/`, `..`, white space around it): it would be a tenant whose grant no route can name. The allotment, the rotation or the onboarding answers 502 and stores nothing (no secret, no actor, no tenant admin key, nothing noted in its record), and its `operation_ref` stays held (an unknown outcome, resumed after `LPB_OPERATION_STALE_S` and refused again while the provider answers the same id). The provider applied the command: the allocation, the key or the grant exists there and nothing in the engine names it. No error, log line or record repeats such an id. A payment clearinghouse hands a key over once, so no repeat of that reference can finish there: the tenant's admin revokes the allocation at the clearinghouse and the engine allots again under a new reference, or, for a rotation, rotates again under a new one ([the key an enterprise's engine is handed](contracts/payments.md#the-key-an-enterprises-engine-is-handed), #158) |
| A supplied secret store returns a reference with a NUL or a lone surrogate for a new payment key | No database keeps such text in every column the reference goes to. The engine deletes the copy from the store again and the allotment or the rotation fails: HTTP 500, nothing noted, no actor written or changed, and the `operation_ref` stays held, since the provider minted the key. Once the store returns text, the repeat (after `LPB_OPERATION_STALE_S`) finishes: Batteries replays the key; a payment clearinghouse answers it as a key that never arrived. A reference of any length is kept (#115, #158) |
| Batteries delivers a usage row the engine cannot read or store | The row is set aside and the sync goes on: an unknown status, no id (or a blank one) or timestamp, a fee that is not a plain decimal string as Batteries writes one (an exponent, a JSON number, `NaN`), a text the store's column cannot keep (too long, a NUL, a lone surrogate), a fee whose digits written out are longer than its column (`9E+999999` from a usage source other than Batteries; #158), or a row the JSON decoder's limits refuse (an integer of more than 4300 digits, nesting past its depth; #158). Its id, the reason and its fees are one row of `lpb_usage_set_aside`; a WARNING `costs.usage_set_aside` carries its content, once. `cost_sync_set_aside` in `/readyz` counts the rows left out in all, and `rows_set_aside` in the sync report the new ones. No job's cost includes the row, so reconciliation may not balance until it is accounted for (#114, C13; [payments.md](contracts/payments.md#cost-classification)) |
| Two cost syncs run at once (the background loop and `POST /v1/costs/sync`) | Both end. Each usage row is stored once and each set-aside row recorded once, by whichever sync writes it first; the row is not new to the other, which counts and logs nothing for it. A job both of them project has one projection, from the newer evidence of the two (#158). Before, the slower sync failed on the row's primary key and its pass ended before its cursor moved (review of #144) |
| A runner answers an error, or a 402 payment challenge, with a very long body | The engine reads 16 KiB of an error and keeps its first 4096 characters, as before. It reads a challenge up to 64 KiB; a longer one is refused unsigned (`http`, 402), as a malformed one is. The rest of the body is not read and the connection is closed ([failures](contracts/engine.md#failures)) |
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
| The engine stops with async jobs running | It claims no more from the first moment of `stop()`, before it waits for open streams, and starts none it had claimed and not yet started: that job goes back to the queue. It gives running jobs `LPB_ASYNC_DRAIN_TIMEOUT_S` to end, then cancels the rest: one with no attempt goes back to the queue, any other ends as a cancelled immediate job does. A step of the worker still under way after that time is cancelled too, also one driven with `run_once()`, and what it claimed goes back to the queue. `stop()` returns only after every end is written and no step is under way. An exit of `serve()` that does not wait for callers (a cancel, or a second Ctrl-C with two listeners) still stops the engine this way: the drain time is not skipped (`tests/unit/test_async_forced_exit.py`) |
| A client goes away after an async submit | Nothing changes: the job belongs to the worker, and its owner reads it back by id |

There is no HA, exactly-once or SLA. See
[contracts/engine.md](contracts/engine.md#deployment-performance-and-recovery-assumptions).
