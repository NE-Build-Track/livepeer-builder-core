# Dependency-injection refactor

## Status

- **Owners:** Mike. Josh reviews engine code.
- **Ticket:** internal; no Build Track board item. It must land before ISS-32 (M4, from 23 November 2026) builds its embedded and service examples.
- **Branch:** `tasks/mz/dependency-injection-refactor`.
- **State:** planned. The decision log and the design of all seven steps are agreed (2026-10-23). No code has changed. The prerequisite Swagger fix (decision 12) lands before step 5.
- **Analysis:** [di-review-2026-10-23.md](../../design-docs/di-review-2026-10-23.md) and [di-consumers-and-plan-2026-10-23.md](../../design-docs/di-consumers-and-plan-2026-10-23.md).
- **Scope:** strict conformance with the python-dependency-injection guide, v0.1.2. Anything else is listed under [Out of scope](#out-of-scope).
- **Constraints:**
  - **Compatibility is not a goal.** builder-core has no outside users. Break APIs where the design is better for it, and fix ChatLPT and the builder console in the same effort, in their own repositories.
  - **Some repositories are not ours to change.** livepeer-node (`NE-Build-Track/node`), the Python gateway SDK (`livepeer-python-gateway`) and `livepeer-clearinghouse-batteries` are not managed here. No step changes them. Anything that turns out to need a change there is raised with Josh for approval before it is planned.

## Goal

Bring builder-core into strict conformance with the python-dependency-injection guide while staying with hand-written wiring (no container). That means:

- every replaceable part is reachable through public interfaces, so a default can be wrapped or replaced without re-creating its wiring or importing internals;
- construction cleans up after itself when it fails part-way;
- settings are passed explicitly;
- embedders get a typed facade surface and a shipped fake;
- the stock server is a documented composition root;
- tests inject collaborators instead of patching modules;
- importing the package stays light;
- structural tests keep all of this true.

Seven steps, then the consumers. Each step is one reviewable change with its own tests, and the suite and `scripts/check_docs.py` stay green after every step.

## Acceptance

### Step 1: test groundwork (tidy first)

Make the change easy, then make the easy change. Today's tests patch module internals, so this step removes the patching first, with no change in behaviour. The tests that assert private wiring move to step 3, which gives them a public seam (`default_parts` returns `EngineParts`); rewriting them here would mean rewriting them twice.

- [x] **SDK functions are injected:**
  - `SdkRunnerTransport` takes `call` (default `livepeer_gateway.call_runner`; named `call` so it does not shadow the SDK import), and `SignerDiscoverySource` takes `discover` (default `discover_runners`). This is builder-core code only; the SDK is not changed.
  - **Typed with Protocols** `CallRunner` (both `stream=False` and `stream=True` overloads, only the keyword arguments builder-core uses) and `DiscoverRunners`, in `adapters/sdk.py`, adapter-internal.
  - **SDK drift is caught by a test, not by mypy** (see decision 13). `tests/unit/test_sdk_contract.py` records the keyword arguments the adapter really passes on `call()`, `open()` and `fetch()`, and asserts the real SDK functions accept every one. It was shown to fail when the transport passed a keyword the SDK lacks.
  - The 14 `monkeypatch.setattr(sdk.call_runner / sdk.discover_runners, ...)` sites in `tests/unit/test_adapters.py`, `test_adapter_edges.py` and `test_stream_deadlines.py` pass fakes instead. The parametrized deadline test now carries its fake per parameter.
- [x] **Tuning values live in settings** (decision 8):
  - `jobs._OPEN_BACKSTOP_GRACE_S` becomes `stream_open_grace_s` (`LPB_STREAM_OPEN_GRACE_S`, default `5.0`, `> 0`), carried in `StreamLimits`; `test_stream_deadlines.py` stops patching the module constant.
  - The Batteries HTTP timeout (hard-coded `30.0`) becomes `batteries_timeout_s` (`LPB_BATTERIES_TIMEOUT_S`, default `30.0`, `> 0`).
  - `_RETENTION_INTERVAL_S` becomes `retention_interval_s` (`LPB_RETENTION_INTERVAL_S`, default `21600.0`, `> 0`).
  - Defaults equal today's constants, so behaviour is unchanged; `docs/RELIABILITY.md` gains the three rows.
  - The Batteries adapters build their HTTP client through a public `batteries_http_client(base_url, token, timeout_s=...)`, so the timeout is testable without private attributes.
  - Done in a separate tidy commit: `_BODY_LIMIT` is defined once, as `BODY_LIMIT = 4096` in `utils.py`, instead of in `jobs.py`, `streams.py` and `sdk.py`; it stays an internal constant.
  - Cost-sync usage filters stay unset by design (iss-04: the feed is unfiltered so desk-created grants reconcile).
- [x] **Structural test: no environment reads outside `config.py`.** (`tests/structure/test_settings.py`; shown to fail on an injected `os.environ` read.) An AST scan of `src/livepeer_builder/` flags `os.environ`, `os.getenv` and `from os import environ/getenv` anywhere except `config.py`, with a message naming the rule ("settings are values: read the environment only in `EngineSettings.from_env`"). `deploy/` entrypoints are application edges and are not scanned. True today; the test keeps it true.
- [x] **Lands as one commit per item** (SDK injection, settings, body limit, structural test), each leaving the suite green. Tidy-only commits are marked `tidy:` and change no assertions.

### Step 2: construction cleans up on failure

- [x] **Construction builds into a local `AsyncExitStack`.** Every resource is registered the moment it opens (store, Batteries clients, usage source). On success, `pop_all()` hands the stack to the engine as `self._resources`; on failure, everything opened so far is closed.
- [x] **`stop()` keeps today's order:** drain streams, stop the loops, then `await self._resources.aclose()`. Reverse registration order closes the clients before the store. The `closers` list and its `getattr(aclose/close)` duck typing go.
- [x] **Cleanup failures are logged, not raised.** Each close is registered through a wrapper that logs its own failure (`engine.close_failed`, with the resource name), so `stop()` never raises from cleanup. A raising `stop()` would mask the original error in lifespans, `finally` blocks and `async with engine:`.
- [x] **`stop()` is idempotent and valid before `start()`;** the CLI relies on the latter.
- [x] **The engine is single-use.** `start()` after `stop()` raises "engine is stopped; create a new one". Today a restart would run loops against a closed store.
- [x] **A failing-first test** proves a failure part-way closes the store. With `secret_key="not-a-fernet-key"`, construction raises after `SqlStore.connect`; today the store is left open (probe 2026-10-23: `created: 1 closed: 0`). The test observes the store through a recording wrapper on `SqlStore.connect` (a spy, which the guide allows when no injection point exists); in step 3 it becomes a plain test with a supplied store.
- [x] **Tests:** `stop()` twice is safe; `start()` after `stop()` raises. After `stop()`, no task named `lpb-discovery`, `lpb-cost-sync` or `lpb-retention` remains in `asyncio.all_tasks()`, replacing the private `engine._tasks == []` assertion.

### Step 3: construction as data

- [x] **Public `EngineParts`**, a frozen dataclass of every replaceable port:
  - `store`, `clock`, `policy`, `authenticators`, `api_keys`, `discovery_source`, `transport`, `selection`;
  - `allocation_manager`, `grants_manager`, `usage_source`, `secret_store`, `events`, `sessions`.

  Tuning values are not parts. `StreamLimits` and `AdmissionLimiter` are built in `assemble` from settings.
- [x] **Parts hold ports only; services are built in `assemble`, from the final parts, after `decorate`.** A decorated policy therefore reaches every service, including the key service.
- [x] **Layout and exports:** `EngineParts`, `EngineSupply`, `UNSET`, `ApiKeys`, `default_parts` and `assemble` live in `runtime/assembly.py`. The root exports `EngineParts`, `EngineSupply`, `UNSET` and `ApiKeys`; `livepeer_builder.runtime` exports `default_parts` and `assemble`.
- [x] **Port-to-port dependencies come from supplied values.** `EncryptedDbSecretStore` (store, clock) and the default `api_keys` (store) are built from what was supplied. A *decorated* port is not seen by ports built before decoration. The extension guide states the rule, with an example: if other parts depend on it, supply it (Replace); decorate only to wrap behaviour at the point of use.
- [x] **Supplied parts are borrowed.** The engine never closes a resource it was given; only resources `default_parts` opened go on the stack.
- [x] **`decorate` is a plain synchronous function** `(EngineParts) -> EngineParts`. A decoration that needs I/O is a new resource, which the caller opens and supplies.
- [x] **One documented construction path, in two stages** (decision 6):
  - `BuilderEngine.create(settings, *, supply=EngineSupply(...), decorate=fn)`, async. `from_settings` and its keyword overrides are removed.
  - **`settings` is required.** The `settings or EngineSettings.from_env()` fallback goes; `EngineSettings.from_env()` stays an explicit call at the edges (CLI, server root, docs).
  - **Supply (Replace):** `EngineSupply` has every `EngineParts` field, each defaulting to an `UNSET` sentinel. `default_parts(settings, stack, supply)` builds only what was not supplied, in dependency order, and registers resources on `stack`. A supplied part is never built, and dependent defaults (`api_keys`, `EncryptedDbSecretStore`) are built from the supplied store.
  - **`None` is a value, not a missing one.** It means "off" where a part is optional (`api_keys=None` turns keys off).
  - **Decorate:** `decorate: Callable[[EngineParts], EngineParts]` receives the finished parts and returns the parts to use, for wrapping defaults or supplied parts.
  - `assemble(settings, parts, resources)` wires the engine and does no I/O. `default_parts` and `assemble` stay public for tests and unusual cases.
  - The default discovery source keeps the signer bearer header.
- [x] **`BuilderEngine` supports `async with`:** start on enter, stop on exit. The CLI and scripts use it; web applications keep calling `start`/`stop` from their framework's lifespan.
- [x] **API keys are one part** (decision 2, as amended):
  - `ApiKeys(store: KeyRepo, realm: str = "livepeer-builder")` describes the capability.
  - `assemble` builds both the `ApiKeyAuthenticator` and the `KeyService` from it, with the final policy and clock, so acceptance and issuance cannot disagree.
  - `api_keys: ApiKeys | None` defaults to keys on, built from the store.
  - `authenticators: tuple[Authenticator, ...]` holds the deployment's own authenticators, default `()`.
  - The chain is `(*authenticators, <key authenticator>)`.
- [x] **Keys off** (`api_keys=None`):
  - `engine.keys` is `None`, and `create_app` registers no key routes, so they are absent from that deployment's OpenAPI;
  - `lpb bootstrap-admin` exits with "API keys are disabled in this deployment";
  - the engine logs its authentication mode once, in `start()` (not in `create()`, so CLI commands that only build an engine stay quiet);
  - the extension guide states that a replacement authenticator must grant admin permissions to operators.
- [x] **The grant desk is an optional capability** (decision 7):
  - `payments.grants: GrantsApi | None` (list and create grants) is present when `LPB_BATTERIES_OPERATOR_TOKEN` is configured or a grants manager is supplied.
  - When it is `None`, the grant routes are absent from the app and its OpenAPI, and startup logs "grant desk disabled".
  - The startup refusal and the allocation-manager fallback are removed.
  - Funding a grant stays on the engine token (`grants.fund`).
- [x] **Tests:**
  - replacing one part keeps the others;
  - a supplied store means the configured database is never opened, and `api_keys` and the secret store use the supplied store;
  - a decorated default discovery source still sends the signer header;
  - keys off hides the key routes, makes `bootstrap-admin` refuse, and rejects an `lpk_` token;
  - an enterprise authenticator runs before the key authenticator;
  - Batteries without the operator token starts, has no grant routes and still funds grants;
  - `async with` starts and stops.
- [x] **Callers updated:** the CLI, the Railway entrypoint, `docs/guides/embedded-mode.md`, and `tests/unit/test_extension_examples.py`.
- [x] **Private-attribute wiring tests replaced** (moved here from step 1). The tests asserting `engine.discovery._source`, `engine.payments._manager`, `engine.payments._grants`, `engine.costs._worker._source` and `engine.jobs._store` become assertions on the public `EngineParts` that `default_parts` returns, or on behaviour. `test_batteries_without_an_operator_token_does_not_start` is inverted by decision 7.
- [x] `docs/design-docs/extending-the-engine.md` "Wiring it" is rewritten around supply and decorate.
- [x] **The guide cannot drift from its tested example.** The example region in `tests/unit/test_extension_examples.py` is marked `# docs:start wiring` / `# docs:end wiring`, and `scripts/check_docs.py` fails when the guide's fenced block differs from that region.

### Step 4: the facade API

- [x] **Facade Protocols in a new module `livepeer_builder/api.py`.** This module holds what the engine *offers*, kept apart from `protocols/`, which holds what it *needs*. It is re-exported from the package root and imports only contracts and errors.
- [x] **Nested shape, mirroring today's surface:** `EngineApi` has `jobs`, `costs`, `discovery`, `payments`, `keys`, `events`, `sessions` and `authenticator` members, plus `health()`, `start()` and `stop()`.
- [x] **Split by role:**
  - **`JourneyApi`**, what an end-user application needs:
    - `jobs`: `run`, `get`, `list_jobs`, `result`, `report_usage`, and `streaming: StreamingApi | None` (with `stream`);
    - `costs`: `for_job`, `summary`;
    - `discovery`: `offerings`, `runners`, `candidates`;
    - `payments`: `allowance`;
    - `health()`, `start()`, `stop()`.
  - **`AdminApi`**, for operators:
    - `payments`: `provision`, `fund`, `set_status`, `rotate_key`, `revoke`, `fund_grant`, and `grants: GrantsApi | None` (`list_grants`, `create_grant`);
    - `keys: KeysApi | None` (`issue`, `list_keys`, `revoke`);
    - `costs`: `recent`, `reconcile`, `sync_now`.
  - **`EngineApi`** combines both, plus `authenticator` (`authenticate`, `challenge`), `events` (`after`) and `sessions` (`open`). `events` and `sessions` stay engine-only while they are M4 placeholders.
- [x] **Members are read-only properties,** so `EngineApi` can narrow a member it combines (for example `payments`) under mypy's rules.

  Applications type against the narrowest Protocol they use: ChatLPT uses `JourneyApi`, and `http/` uses `EngineApi`.
- [x] **Full coverage.** The Protocols cover every engine member `http/` calls, and `http/` is typed against `EngineApi`, so the server and embedders share one contract. `BuilderEngine` satisfies the Protocols (mypy-checked).
- [x] **Optional capabilities are `X | None` members, decided once at construction from the parts:**
  - `jobs.streaming: StreamingApi | None` is present only when the transport streams. `stream()` moves onto it, and `hasattr(engine.jobs, "stream")` goes from consumers.
  - `keys: KeysApi | None` and `payments.grants: GrantsApi | None` follow the same rule.
  - Future capabilities (async jobs, ISS-26; persistent endpoints, ISS-27) follow the same pattern.
  - Any capability report (in health) is derived from which members are present.
- [x] **Shipped doubles, option (iii):**
  - **`livepeer_builder.testing.make_engine(*, runners=..., supply=..., decorate=..., **settings)`**, for integration-level tests. It is an async context manager, promoted from the `make_engine` test fixture:
    - it builds a real engine with `create` over in-memory SQLite and the shipped port fakes, each of which can be supplied;
    - it takes one discovery snapshot and does not start the loops, so tests are deterministic;
    - it stops on exit.
  - **`livepeer_builder.testing.FakeEngine`**, for application unit tests. It implements `JourneyApi` only and is modelled on ChatLPT's hand-written fake:
    - constructor data: offerings, runners, results per `(app, path)` (a `JobResult` or an exception to raise), cost records, allowance, health;
    - `streaming`: `None` or scripted chunks;
    - recorded calls (`fake.jobs.requests`, `fake.jobs.usage_reports`);
    - real contract values and errors, never mocks.
  - **A contract test** runs the same journey assertions against `FakeEngine` and `make_engine()`:
    - `run` returns a result, and a failed run raises `JobFailed`;
    - `get` and `list_jobs` return the job;
    - `costs.for_job` on an unknown job raises `NotFound`;
    - `discovery.candidates` filters by app;
    - `streaming` is `None` for a non-streaming transport and yields chunks for a streaming one;
    - `health()` reports ready after a snapshot.
  - Admin members stay real-only. Applications that need to script admin flows use `make_engine` or a small fake typed against `AdminApi`.
- [x] **Complete exports.** Every type that appears in a public return value or raised error is exported from the root: at least `Attempt`, `Fees`, `AttemptCost` and `NotFound`. Two structural tests enforce it:
  - a walk over the type hints of every `JourneyApi`, `AdminApi` and `EngineApi` member, including fields of returned models, asserts each builder-core type reached is exported;
  - every `EngineError` subclass in `errors.py` is exported.
- [x] **`create_app(engine: EngineApi)`**, so `http/`, embedders and `FakeEngine` share one contract.
- [x] `docs/guides/embedded-mode.md` shows typing against `JourneyApi`, the capability check, `FakeEngine` and `make_engine`.

### Step 5: the stock server is a composition root

**Prerequisite:** the separate Swagger fix (see [Out of scope](#out-of-scope)) lands first, so Railway needs no OpenAPI patch here.

- [ ] **Two entry points in `livepeer_builder/http/server.py`** (part of the `[server]` extra):
  - **`build_app(settings, *, supply, decorate, extend) -> FastAPI`**, for mounting the engine in another ASGI stack. The app's lifespan starts and stops the engine, and `build_app` does not configure logging.
  - **`serve(settings, *, supply, decorate, extend)`**, which runs the whole service with guaranteed cleanup: `engine = await create(...)`, then `try:` run uvicorn `finally: await engine.stop()` (idempotent, step 2). This closes today's leak when uvicorn fails before its lifespan runs. `serve` calls `configure_logging(json_lines=settings.log_json)`, because it is the reference application's composition root.
  - `lpb serve`, the Railway entrypoint and the docs use `serve`.
  - The `uvicorn` confinement in `tests/structure/test_layers.py` moves from `cli.py` to `http/server.py`.
- [ ] **Extra endpoints get the engine explicitly.** `extend: Callable[[FastAPI, EngineApi], None]` receives the app and the engine; routes are added with `app.include_router(make_router(engine))`. There is no `app.state.engine` lookup. `http.server` exports `actor_dependency(engine)`, the same dependency the built-in routes use, so extra endpoints get the same authentication and policy checks.
- [ ] **No new `lpb serve` flag.** A customized service is a small entrypoint of its own: `asyncio.run(serve(EngineSettings.from_env(), supply=..., decorate=..., extend=...))`. A `--app module:attr` loader can come later if operators ask for it.
- [ ] **A tested service-mode example,** in `tests/unit/test_service_mode_example.py`: an enterprise authenticator supplied, the discovery source decorated, and one endpoint added through `extend` using `actor_dependency`. It is shown in a new "Service mode" section of `extending-the-engine.md` and kept in step by the same docs marker check as step 3.
- [ ] **The Railway entrypoint calls `serve(settings)`**, with settings built as values: `EngineSettings.from_env().model_copy(update={"database_url": asyncpg_url(...)})`. It no longer rewrites `os.environ` for the serve path or patches the spec (the Swagger fix has landed). The migrate path that shells out to `lpb` keeps its env rewrite.

### Step 6: import weight and declared dependencies

- [ ] **Lazy default adapters.** The default adapters and `livepeer_builder.store` are imported inside `default_parts`, not at the top of `runtime/engine.py`, so nothing on the root import path loads them.
- [ ] **A store maintenance capability** replaces `isinstance(self.store, SqlStore)` in `purge()` and `schema_revision()`:
  - `EngineStore` gains `maintenance: StoreMaintenance | None`, where `StoreMaintenance` has `purge(...)` and `schema_revision()`.
  - `SqlStore` provides it. With `None`, the engine reports a zero purge and no revision, as today.
  - This removes the runtime layer's only reference to the concrete store.
- [ ] **`PurgeReport` moves** from `store/retention.py` to `contracts/` (it is returned by `engine.purge()`).
- [ ] **Declared dependencies:**
  - `aiohttp>=3.9` in core dependencies (imported in `adapters/sdk.py`; locked at 3.14.5; the floor is confirmed against the code);
  - `starlette` in the `[server]` extra (imported in `http/`).
  - A structural test maps every third-party top-level import in `src/livepeer_builder` to a distribution declared in `[project] dependencies`, or in the extra that owns the module (`http/` → `[server]`), through a small import-name-to-distribution table.
- [ ] **Import-weight test.** In a fresh interpreter, `import livepeer_builder` loads none of `sqlalchemy`, `alembic`, `aiosqlite`, `asyncpg`, `httpx`, `cryptography`, `livepeer_gateway`, `aiohttp`, `fastapi`, `starlette` or `uvicorn`. Any unavoidable module is named in the test with its reason.
- [ ] **The rule covers only the root and `livepeer_builder.api`.** `livepeer_builder.testing` (`make_engine` needs SQLite), `store` and `http` may load heavy dependencies; they are imported deliberately.

### Step 7: remaining structural guards

- [ ] **The server, CLI and Railway import only the public API.** The public API is the root `__all__` plus the `__all__` of the subpackages documented as public: `api`, `runtime`, `http`, `store` and `testing`. The test covers `cli.py`, `http/server.py` and `deploy/railway/engine/entrypoint.py`. Today's violations are fixed as follows:
  - `adapters.secrets.EncryptedDbSecretStore`, used to generate `LPB_SECRET_KEY`, is replaced by a public `generate_secret_key()` exported from the root;
  - `runtime.logging.configure_logging` is added to `livepeer_builder.runtime.__all__`;
  - `docs_gen` and `docs_diagrams` (`lpb gen-docs`) stay as an allowlist entry in the test, with its reason: maintainers' doc tooling, not operator API.
- [ ] **Guide Rule 1, no DI container:** container packages (`dishka`, `dependency_injector`, `wireup`, `punq`, `lagom`, `kink`, `svcs`, `injector`, `that_depends`) join the third-party confinement test as allowed nowhere. This guards decision 1.
- [ ] **Guide Rule 11, logging setup belongs to the application:**
  - only `runtime/logging.py` may modify the root logger (its handlers, or `basicConfig`);
  - only `cli.py`, `http/server.py` and the Railway entrypoint may call `configure_logging`.
- [ ] **Guide Rule 5's optional module-state check is not added.** The one module-level collection, `_SHIELDED` in `utils.py`, holds references to shielded tasks so they are not garbage-collected. It is a legitimate asyncio pattern.
- [ ] **`EngineSupply` and `EngineParts` have the same fields** (`dataclasses.fields()` names match), with a message naming decision 6.
- [ ] **Layer table** (`ARCHITECTURE.md` and `tests/structure/test_layers.py`):
  - `api.py` sits beside `protocols/` and may import contracts and errors;
  - `runtime/assembly.py` is app wiring;
  - `http/server.py` is UI, may import runtime, api, contracts, errors and config, and is where `uvicorn` is confined;
  - `http/` and `testing/` may import `api`. Services satisfy the Protocols structurally, checked by mypy through `create_app(engine: EngineApi)` and a typing test.

### Then: consumers, in their own repositories

- [ ] **ChatLPT:**
  - re-pin to the refactored core;
  - build the engine with `BuilderEngine.create(settings, decorate=...)`, passing `EngineSettings` into its builder so its tests stop setting `LPB_*` environment variables;
  - decorate `parts.discovery_source` instead of re-implementing discovery;
  - type against `JourneyApi` and test with `FakeEngine`;
  - use `jobs.streaming` instead of `hasattr`;
  - declare `livepeer_gateway` if still imported directly;
  - drop `LPB_BATTERIES_OPERATOR_TOKEN` from its configuration;
  - clean up on partial failure.
- [ ] **Builder console:**
  - re-pin the engine image;
  - re-copy `openapi/` if the default spec changed;
  - confirm sign-in, the grant desk and operator writes still work. The console keeps the operator token.

### Out of scope

- **Swagger bearer scheme in `create_app`.** It is an HTTP usability fix, not DI, so it is a separate small change outside this plan, **landing before step 5** (decision 12):
  - declare an `HTTPBearer(auto_error=False)` dependency on the actor resolver, so every deployment's spec carries the bearer scheme and Swagger shows Authorize;
  - regenerate `docs/generated/openapi.json`;
  - delete `install_swagger_bearer` from the Railway entrypoint.
- **A runtime capability flag for API keys in health** (`api_keys_enabled`). Not needed until a client must discover keys-off at runtime.
- **Changes to livepeer-node, the Python gateway SDK or clearinghouse-batteries.** They need Josh's approval and are not part of this plan.

## Progress

- [x] 2026-10-23: analysis complete; plan drafted; decision log being aligned item by item. Nothing committed: the owner asked to finish analysis and planning first.
- [x] 2026-10-23: python-dependency-injection guide v0.1.2 adds facade Protocols, shipped facade fakes and optional capabilities, so step 4 is guide-backed.
- [x] 2026-10-23: decision log aligned (items 1–10); steps re-sequenced (decision 11).
- [x] 2026-10-23: step 1 design agreed: SDK Protocols, setting names and defaults, `BODY_LIMIT` in `utils.py`, the env-read structural test, one commit per item. Private-attribute tests move to step 3.
- [x] 2026-10-23: step 2 design agreed: stack handed over with `pop_all()`, cleanup failures logged not raised, idempotent `stop()`, a single-use engine, a spy-observed failing-first test, a behavioural task check.
- [x] 2026-10-23: step 3 design agreed:
  - layout and exports;
  - services built after decorate, so `ApiKeys` holds the repository and realm (decision 2 amended);
  - supply versus decorate for port dependencies;
  - supplied parts borrowed;
  - a synchronous `decorate`;
  - mode lines logged in `start()`;
  - the docs drift check.
- [x] 2026-10-23: step 4 design agreed: the role split with exact members, read-only property members, `FakeEngine` modelled on ChatLPT's fake, `make_engine` as an async context manager without loops, the journey contract cases, two export-completeness tests.
- [x] 2026-10-23: step 5 design agreed: `build_app` plus `serve` with guaranteed cleanup, `http/server.py`, logging in `serve` only, an `extend` hook with `actor_dependency` (no `app.state`), no new CLI flag, a tested service-mode example, Railway on `serve`. The Swagger fix lands first (decision 12). 
- [x] 2026-10-23: step 6 design agreed: lazy default adapters, a store maintenance capability, `PurgeReport` in contracts, `aiohttp` and `starlette` declared with a declared-dependencies test, an import-weight test for the root and `api`. 
- [x] 2026-10-23: Swagger fix landed as its own commit (`fix(http): declare the bearer scheme so Swagger shows Authorize`), on branch `tasks/mz/swagger-bearer`; the refactor branch is rebased on it.
- [x] 2026-10-23: **step 1 done**: SDK injection (`3e4ad23`), tuning settings (`eb2b9ba`), body limit (`7f9d7ae`), env-read guard. Every commit leaves the CI gate green.
- [x] 2026-10-23: **step 2 done**: exit stack with logged cleanup, idempotent single-use engine, failing-first partial-failure test (spies on `SqlStore.connect`/`close`), behavioural loop check replacing `engine._tasks`. The close-failure test injects the fault on `SqlStore.close`, since no seam exists for an engine-owned resource.
- [x] 2026-10-23: **step 3 done**, in three commits. Part 1: `runtime/assembly.py` (`EngineParts`, `EngineSupply`, `UNSET`, `ApiKeys`, `default_parts`, `assemble`), `BuilderEngine.create`, `async with`, keys-off and grant-desk capabilities with conditional routes, mode log in `start()`. Each behaviour was shown to fail its test under a deliberate mutation. The spec generator now turns every optional capability on, so `docs/generated/openapi.json` still documents the grant desk. Part 2 landed: every caller (CLI, Railway, the spec generator, tests) uses `create`; `from_settings` and its shim are removed; the private-attribute wiring tests and the post-construction rewiring (`engine.jobs._transport = …`, a monkeypatched `finish_job`) are replaced by supplied parts and assertions on `default_parts`; the unfiltered usage feed is checked through the request it sends. `engine.jobs._open_streams` stays as a test-synchronisation poll. Part 3: `extending-the-engine.md` rewritten around supply and decorate, with its example kept equal to the tested region by a new `check_docs.py` check (shown to fail on a drifted block); tests for borrowed supplied parts and the start-up mode log.
- [x] 2026-10-23: **step 4 done**, in three commits. Part 1:
  - `livepeer_builder/api.py` with the role-split Protocols, nested, as read-only properties;
  - `jobs.streaming` as an optional capability (`Streaming` in `services/streams.py`), and `create_app(engine: EngineApi)`;
  - mypy proves `BuilderEngine` satisfies `JourneyApi`, `AdminApi` and `EngineApi`;
  - an HTTP test for the 400 a non-streaming transport answers (shown to fail without the guard);
  - the facade Protocols exported from the root.

  Part 2 landed:
  - `livepeer_builder.testing.make_engine` (a real engine over in-memory parts, as an async context manager, loops not started), which the repository's own `make_engine` fixture now uses;
  - `FakeEngine` (`JourneyApi`, scripted with `Reply`/`Fails` per `(app, path)`, optional streaming);
  - a contract test running eight journey cases against both, which caught each of four deliberate breaks in the fake.

  The `testing` layer may now import what `runtime` does, because `make_engine` builds a real engine.
  Part 3: complete exports, with two structural tests (types reachable from the facade Protocols, through model fields, and every `EngineError`). They named 22 contracts and 6 errors to add. Ports consumers implement stay in the public `livepeer_builder.protocols`. The embedded guide covers role typing, capabilities, `FakeEngine` and `make_engine`.

  Deviations, recorded here: `api.py` imports `protocols` (it reuses `Authenticator`, `EventFeed` and `SessionManager` for engine members that are those ports), and `streaming` is a plain attribute rather than a property, which keeps `services/jobs.py` under the 500-line limit.
- [x] 2026-10-23: step 7 design agreed: the public-API definition and fixes (`generate_secret_key`, exported `configure_logging`, a docs-tooling allowlist), the guide's Rule 1 and Rule 11 checks (Rule 5 skipped), field parity, the layer table. **The design of all seven steps is agreed.** The consumers are planned in their own repositories after builder-core.

## Decision log

1. **2026-10-23: stay with hand-written wiring; no DI container** in the package or the server. Neither the entry points (CLI, server, Railway, and MCP next) nor the resources (no per-request sessions) meet the threshold, and consumers wire by hand as well. Source: Mike, after [di-review-2026-10-23.md](../../design-docs/di-review-2026-10-23.md) section 1.
2. **2026-10-23: authenticators are configurable, and API keys are one part.**
   - `ApiKeys(authenticator, service) | None`; keys are on by default.
   - Keys off removes the key routes from the app and its OpenAPI, makes `bootstrap-admin` refuse, and is logged at startup.
   - The deployment's own authenticators run first.

   Source: Mike.

   *Amended 2026-10-23 (step 3 design):* `ApiKeys` holds the key repository and realm, not a built `KeyService`. Services are built in `assemble` after `decorate`, so a decorated policy reaches the key service too. Source: Mike.
3. **2026-10-23: the embedding surface is the public API.**
   - Protocols live in `livepeer_builder/api.py`, use the nested shape, and are split by role (`JourneyApi`, `AdminApi`, `EngineApi`). They cover every member `http/` uses, and `http/` is typed against them.
   - Optional capabilities are `X | None` members (`jobs.streaming`, `keys`).
   - The doubles are `make_engine` (real engine, fake ports) plus `FakeEngine` (journey only), with a contract test.

   Source: Mike. The guide v0.1.2 records the generic pattern.
4. **2026-10-23: no board item.** Tracked here only. Source: Mike.
5. **2026-10-23: all seven steps, in order, scoped to strict conformance** with the guide (v0.1.2). The Swagger fix and an `api_keys_enabled` health flag are out of scope; the public-API exports and the two structural tests are in. Source: Mike.
6. **2026-10-23: construction is `BuilderEngine.create(settings, *, supply, decorate)`.**
   - **Supply (Replace)** happens before defaults are built, so replaced parts are never built and dependents use the replacement.
   - **Decorate** happens after, to wrap the configured defaults.
   - `EngineSupply` uses an `UNSET` sentinel so that `None` can mean "off".
   - `from_settings` and its keyword overrides are removed; `build_app` takes the same two stages.
   - `BuilderEngine` supports `async with`.

   This replaces the draft's "build everything, then replace", which would have opened and migrated the configured database even when the store was replaced, and built dependent defaults from the wrong store. Source: Mike.
7. **2026-10-23: the grant desk is an optional capability** (`payments.grants: GrantsApi | None`). The operator token is needed only by deployments that list or create grants; the routes are absent and startup logs the fact when it is off.
   - **Restores least privilege:** ChatLPT stops holding a `grants.create` credential it never uses.
   - **Removes a wrong-credential fallback.**
   - **Reverses #11's start-up refusal.** Josh is told when he reviews the engine code.

   No clearinghouse change is needed; the scopes already exist. Source: Mike.
8. **2026-10-23: settings are the single home for tuning values.** Parts hold ports (behaviour); settings hold values.
   - Three hard-coded tunables move into settings: `stream_open_grace_s`, `batteries_timeout_s`, `retention_interval_s`.
   - Limit objects are built from settings, not supplied as parts.
   - `EngineSettings` is an ordinary value, so embedders tune in code without the environment.
   - This would change only if a limit had to change while the engine runs; it would then become a port.

   Source: Mike.
9. **2026-10-23: compatibility is not a goal.** Break APIs where the design improves; fix ChatLPT and the console in the same effort. Source: Mike.
10. **2026-10-23: livepeer-node, the Python gateway SDK and clearinghouse-batteries are not changed by this plan.** Any change there needs Josh's approval first. Source: Mike.
11. **2026-10-23: steps re-sequenced** after decisions 2 and 6–8 changed their contents:
    1. test groundwork (tidy first);
    2. cleanup on failure;
    3. construction as data (absorbs explicit settings);
    4. the facade API;
    5. the server root;
    6. import weight;
    7. remaining structural guards;
    8. then the consumers.

    The content is unchanged; only grouping and order. Source: Mike.
12. **2026-10-23: the separate Swagger fix lands before step 5,** so step 5's Railway entrypoint calls plain `serve(settings)` with no OpenAPI patch. The fix stays outside this plan's scope; only its timing is fixed. Source: Mike.
13. **2026-10-23: SDK signature drift is caught by a contract test, not by mypy.** Step 1's design assumed mypy would check the real SDK functions against the `CallRunner` and `DiscoverRunners` Protocols. The SDK ships no `py.typed`, and builder-core's mypy settings treat `livepeer_gateway` as untyped, so that check cannot happen. Adding type information to the SDK would need Josh's approval (decision 10). A test that records the keyword arguments the adapter passes and checks them against the real signatures gives the same protection inside builder-core. Source: found during step 1 implementation.

