# ISS-32: Prove package embedding, service consumption, and enterprise extensions

**Status:** proposal for discussion, 29 October 2026. Nothing here is decided until it is locked; locked
rules become decisions (numbered then).\
**Tracker:** ISS-32 ([Network-Engineering-SPE#46](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/46)).\
**Owner:** John (engine). **Reviewers:** Mike, John.\
**Verified against:** builder-core `releases/m3` at `d3df806`; ChatLPT `b6f8eb1`, inference-gateway `6e86de3`,
builder console `938db3e`, enterprise desk (`enterprise-ui`) `ccbdbd9`.

## Done when

- **One versioned core candidate** is used by an embedded example and a service example. Standalone
  authentication is replaced by an **enterprise adapter**, ownership checks still hold, and no user needs
  an extra engine key.
- **An additional endpoint and tool** and a **minimal mock-commerce/reporting integration** work through
  public interfaces, including repeat and event handling, with **no core patch and no direct table or
  ledger write**. Standalone use does not need the fixture.
- **Depends on** ISS-07 (CI builds, done), ISS-29 (REST coverage) and ISS-30 (MCP: the "tool").

## What "proved" means (proposed)

1. **An installable candidate.** A `v*` tag on the M4 release branch (for example `v0.2.0rc1`); CI builds
   its wheel and the examples install **that wheel** into a clean environment, never the source tree.
2. **Examples run in CI from the wheel**, import only the public API (the same check the CLI and the
   stock server pass), and their documented code is the tested code (`check_docs.py` regions).
3. **Consumers pin the candidate**: ChatLPT and inference-gateway (embedded) and the console's engine
   image (service) name the same tag, recorded in one table in `docs/references/`.

## Current state (verified)

**The DI refactor is done** (all seven steps and both consumers, in the progress log of
[dependency-injection-refactor.md](../../exec-plans/active/dependency-injection-refactor.md); its
Status block still says "planned … no code has changed", which is stale):

| Piece | Where |
| --- | --- |
| Supply, decorate, assemble; keys and grant desk as optional parts | `runtime/assembly.py` (`EngineSupply`, `EngineParts`, `assemble`) |
| Service composition root with `supply`, `decorate`, `extend` | `http/server.py` (`build_app`, `serve`, `Extend`); `actor_dependency` for extra routes |
| Role facades and a shipped fake | `api.py` (`ActorApi`, `AdminApi`, `EngineApi`); `testing/fake_engine.py`, `testing/engine.py` (`make_engine`) |
| Light import, public API | `tests/structure/test_import_weight.py` (no DB, HTTP, crypto, SDK or server module on `import livepeer_builder`); `tests/structure/test_public_api.py` |
| Extension examples, tested | `tests/unit/test_extension_examples.py` (SSO header authenticator, team read policy, audited vault, cheapest-ready selection, allow-listed discovery); `tests/unit/test_service_mode_example.py` (the same, served, plus `GET /v1/corp/whoami`); `tests/discovery/test_directory_example.py` |
| Built-in enterprise adapter | `JwtBearerAuthenticator` by configuration (`LPB_AUTH=jwt`, D45, `adapters/jwt.py`) |

**Gaps against the criteria:**

| # | Gap | Evidence |
| --- | --- | --- |
| E1 | **Examples are repository tests, not consumers of a built package.** They run against the source tree; CI installs the wheel only for `lpb smoke` and `lpb revision` (`ci.yml`, job `build-install`) | `tests/unit/test_extension_examples.py` |
| E2 | **Examples use non-public names.** `DefaultPolicy` from `livepeer_builder.services.policy` (line 33, used as `TeamReadPolicy`'s fallback, line 83) and `StaticDiscoverySource` from `livepeer_builder.adapters.static` (line 18). Neither module is in `PUBLIC_MODULES` (`test_public_api.py`), and the examples are not in its `APPLICATIONS`. `extending-the-engine.md` tells implementers to delegate to `DefaultPolicy` | `test_public_api.py`, root `__all__` |
| E3 | **No single versioned candidate.** Only `v0.1.0` (19 October) is tagged; `d3df806` is `v0.1.0-147`. Consumers pin four different commits: ChatLPT `2134e29` (`pyproject.toml`), inference-gateway `0d0b532`, the console's images `d3df806`, the enterprise desk's recorded pin `93c8910` (`src/lib/pins.ts`, M2) | `git describe`; consumer files |
| E4 | **The trusted-backend authenticator is not built.** A `service` credential with `actors.assert` can be issued (`services/keys.py:157`), but nothing honors `Livepeer-Actor` (not found in `src/`). Defined in [access.md](../../contracts/access.md#service-mode), tech debt T7 | `grep` |
| E5 | **No mock-commerce or reporting fixture.** Not found in `src/`, `tests/` or `docs/` | `grep -i commerce` |
| E6 | **Event handling has no feed.** `GET /v1/events` answers 501; the feed is held (ISS-18) | `runtime/assembly.py:258` |
| E7 | **No "tool" extension point.** MCP (ISS-30) has no code; `extend` covers REST routes only. The [ISS-30 guide](iss-30-mcp.md) mounts `create_mcp_app(engine)` beside `http/` but defines no hook for an enterprise's own tool | `http/server.py` |
| E8 | **A host app cannot mount part of the REST service.** Every route is a closure in `create_app`; error handlers are private (`_install_error_handlers`) | #25, John's 28 October comment |
| E9 | **The enterprise desk hands users engine keys.** It signs users in with `lpk_` actor keys and can issue them at signup (`enterprise-ui/README.md`), the "extra user-facing engine key" the criterion rules out | consumer README |

**#25 (core/server split), after #41:** the import-weight, layer and public-API tests already deliver
what the `core/` move was for, so John proposes dropping the move and the `http`→`server` rename. Still
open: router factories per route group with a public `install_error_handlers`; `ServerSettings`
(`http_host`, `http_port`, `log_json`) out of `EngineSettings`; the docs generators out of the wheel
(`scripts/gen_docs.py`); docs.

## Decision proposed

### Bounded contexts and extension points

The engine is one context (execution, payment attribution, network cost) published as an **Open Host
Service** (the facade, REST and later MCP) with the contracts as its **Published Language**. Enterprise
applications are downstream contexts that own retail: users, orders, prices, invoices. Each translates
at its edge, as inference-gateway's accounting does: an **Anticorruption Layer** in the consumer, not in
the engine. Identity is a **generic subdomain**: the enterprise buys it (an IdP) and the engine conforms
through the `Authenticator` port. The fixture's job is to prove that boundary: the commerce context
reaches the engine only through the open host service and keeps its own store.

Extension points stay the ports in `protocols/` plus three composition hooks (`supply`, `decorate`,
`extend`); services stay non-replaceable (`extending-the-engine.md`). ISS-32 adds no new port except
the trusted-backend authenticator (a default implementation of an existing port) and, with ISS-30, an
MCP `extend` hook.

### 1. Examples as packages that install the wheel

Move the two example modules out of `tests/unit/` into `examples/`, outside `src/` (never shipped):

| Example | Mode | Shows |
| --- | --- | --- |
| `examples/embedded/` | imported | `BuilderEngine.create(settings, decorate=…)`, its own `ActorContext` per user, typed against `ActorApi`, tested with `FakeEngine` and `make_engine` |
| `examples/service/` | REST | `serve(settings, supply=…, decorate=…, extend=…)`: SSO authenticator with `api_keys=None`, team policy, vault, allow-listed discovery, one extra route; later one extra MCP tool (ISS-30) |
| `examples/commerce/` | REST or imported | the mock-commerce/reporting fixture (part 3) |

Each has its own `pyproject.toml` depending on `livepeer-builder[server]`; a new CI job `examples`
installs `dist/*.whl` and each example into a clean venv and runs its tests. `check_docs.py` example
regions point at `examples/…`. `test_public_api.py` adds the example sources to `APPLICATIONS`, which
fixes E2 by exporting what an implementer needs: `DefaultPolicy` (root `__all__`, as the base a custom
policy delegates to) and `StaticDiscoverySource` (from `livepeer_builder.testing`, or the root).

### 2. Enterprise authentication without a user-facing engine key

- **Service, by configuration:** `LPB_AUTH=jwt` (D45) turns keys off; ISS-33 demonstrates it with the
  console and Keycloak.
- **Service, by code:** `examples/service` supplies its authenticator and `api_keys=None`, so the
  stock key routes and `bootstrap-admin` are absent; admin comes from the SSO's group, delegated to
  `DefaultPolicy`.
- **Backend to engine:** build `ServiceCredentialAuthenticator` (`adapters/auth.py`) as
  [access.md](../../contracts/access.md#service-mode) defines it: a `service` key with `actors.assert`,
  bound to one `application_id`, names the actor in `Livepeer-Actor`; the header is ignored for any other
  credential; the asserted actor gets the service key's other permissions, never admin. This is the
  path for the enterprise desk (E9) and any backend that calls the engine for many users.
- **Ownership holds under each:** the ISS-29 access sweep runs once per authenticator.

### 3. The mock-commerce and reporting fixture (`examples/commerce`)

A small FastAPI app with its **own** SQLite (`orders`, `invoice_lines`), reaching the engine only
through the facade or REST:

- **Order → job.** `POST /orders` runs a job with `operation_ref = "order:" + order_id`. A retried order
  (lost response, crash) gets `OperationExists` (409, `code` from ISS-29) and reads the job instead of
  paying twice: the **repeat handling**.
- **Reporting.** An invoice line carries the retail price (the fixture's own table) beside the network
  cost from `GET /v1/jobs/{id}/cost`, with its class; a `pending` cost stays pending, never zero.
- **Event handling.** Consumes `GET /v1/events`, applies pages, de-duplicates by event `id`, saves the
  cursor. While the feed is held, the same consumer polls `GET /v1/jobs?before=` (ISS-29) behind one
  `EngineChanges` port of the fixture's own, so the switch is one adapter (Q4).
- **Additional endpoint.** `GET /v1/corp/invoice/{order}` added with `extend` on the engine's service,
  authenticated by `actor_dependency`; the MCP tool equivalent arrives with ISS-30.
- **Never writes engine tables:** the fixture opens no engine database URL and imports nothing from
  `livepeer_builder.store` (a test in the example checks both).

### 4. One candidate, pinned

Tag `v0.2.0rc1` when ISS-29 and ISS-30 land; the `examples` job runs against it; ChatLPT and
inference-gateway pin the tag; the console builds its engine image from it; the enterprise desk moves
from `93c8910`. A table in `docs/references/` (consumer, pin, mode, last verified) is the evidence.

### 5. Remaining refactors (from #25 and the DI review)

In this order, each its own PR: (a) router factories per group with a public `install_error_handlers`
(E8), plus a test that mounts only `discovery` and `jobs` into a bare `FastAPI()` and maps a failure to
503 with `Retry-After`; (b) `ServerSettings` out of `EngineSettings`; (c) docs generators to
`scripts/gen_docs.py`; (d) correct the DI plan's Status block. Drop the `core/` move and the rename, and
record why. `CredentialResolver` and `AllowanceReader` stay non-suppliable (interfaces doc, question 5).

## Code areas

| File | Change |
| --- | --- |
| `examples/{embedded,service,commerce}/` (new) | the three examples with their tests and `pyproject.toml` |
| `tests/unit/test_extension_examples.py`, `test_service_mode_example.py` | move to `examples/`; keep thin re-run shims only if needed |
| `tests/structure/test_public_api.py` | example sources join `APPLICATIONS` |
| `src/livepeer_builder/__init__.py`, `testing/__init__.py` | export `DefaultPolicy`, `StaticDiscoverySource` |
| `adapters/auth.py`, `runtime/assembly.py`, `config.py` | `ServiceCredentialAuthenticator`, wired when keys are on |
| `http/app.py` → `http/routes/*.py`, `http/errors.py` | router factories, public `install_error_handlers` (#25) |
| `.github/workflows/ci.yml`, `scripts/check_docs.py` | `examples` job on the wheel; region paths |
| `docs/design-docs/extending-the-engine.md`, `docs/contracts/access.md`, `docs/guides/embedded-mode.md`, `docs/references/` | examples' new home, trusted backend built, the pin table |

## Test plan (test-driven)

**Levels.** Example tests run in a clean venv against the installed wheel (the predictive layer for
"embedding works"); structural tests for public imports and packaging; unit tests for the trusted-backend
authenticator; HTTP tests for the router factories. **Seams:** `FakeEngine` or `make_engine` inside the
embedded and commerce examples, `ScriptedTransport` for outcomes, an injected HMAC secret for SSO.

**Starter test:** add the example modules to `APPLICATIONS` in `test_public_api.py`; it fails today
naming `livepeer_builder.services.policy` and `livepeer_builder.adapters.static` (E2).

```text
Public surface
[ ] the examples import only the public API (red today, E2)
[ ] the wheel contains no examples/ and lpb smoke passes without them
[ ] each example's tests pass in a clean venv with only the wheel installed
Enterprise authentication
[ ] keys off + SSO authenticator: no /v1/admin/keys route, no lpk_ accepted
[ ] Livepeer-Actor on a service key acts as that actor in the key's application
[ ] Livepeer-Actor on an actor, admin or JWT credential is ignored (the caller stays itself)
[ ] a service key cannot assert into another application, nor become admin
[ ] the ISS-29 access sweep passes under SSO, JWT and the trusted backend
Commerce fixture
[ ] an order runs one job whose operation_ref names the order
[ ] the same order retried reads the existing job; the transport is called once
[ ] an invoice line shows retail price and observed network cost side by side
[ ] a pending cost stays pending on the invoice, never 0
[ ] events applied twice change nothing (dedupe by id); the cursor survives a restart
[?] events from /v1/events rather than polling                 (Q4, the ISS-18 hold)
[ ] the fixture opens no engine database and imports nothing from livepeer_builder.store
[ ] the extra endpoint answers 401 without a credential and 200 with one
[?] the extra MCP tool goes through the same authenticator     (ISS-30)
Refactors (#25)
[ ] a bare FastAPI with only the discovery and jobs routers plus install_error_handlers runs a job
[ ] a failure through that host app is 503 with Retry-After and Livepeer-Job-Id
[ ] openapi.json regenerates with no diff after the router split
```

**Definition of done.** Every item `[x]`, `[=]` or `[-]` with a reason; the `AGENTS.md` checks green
(`uv run pytest`, coverage gates, `ruff`, `mypy` strict, `check_docs.py`, `lpb gen-docs --check`), the
new `examples` job green on the tagged candidate, and every consumer's pin recorded.

## Upstream dependencies

| Upstream | Needed? | Why / workaround |
| --- | --- | --- |
| livepeer/node | No | Authentication, examples and the fixture are engine- and app-side |
| livepeer-python-gateway | No for ISS-32 | The core depends on it by git reference (`pyproject.toml`, `2017a80`); a wheel with that dependency installs (CI's `build-install` does it today) but PyPI refuses it (ISS-38, M5). If M5 needs PyPI, vendor the remaining imports (`discovery.discover_runners`, `errors`, `remote_signer`; `adapters/sdk.py:14`, `adapters/runner_call.py:41-54`) as D53 and #61 did, fingerprinted in `tests/unit/test_sdk_contract.py` |
| clearinghouse-batteries | No | The fixture never touches Batteries; the engine does |

## Consumer impact

| Consumer | Mode | Change |
| --- | --- | --- |
| ChatLPT | embedded, its own `ActorContext` per user (`src/chatlpt/adapters/engine.py:60`) | Re-pin to the tag. Already on `create` and `decorate` (DI plan) |
| inference-gateway | embedded, one engine actor for all its users (`backends/livepeer.py:63-67`), retail in its own ledger | Re-pin to the tag; it is the real-world counterpart of the commerce fixture |
| Builder console | service, stock image by configuration | Engine image from the tag; ISS-33 uses the JWT mode |
| Enterprise desk | service, `lpk_` keys for users | Move from `93c8910`; sign users in through its own login and call the engine with a service key and `Livepeer-Actor` (E9) |

## Alternatives considered

- **Keep the examples in `tests/unit/`.** Cheaper, but they never see the built package, so a missing
  export or a packaging gap stays invisible.
- **Publish to PyPI for the candidate.** Blocked by the git dependency and not authorized before ISS-38.
- **Make `ServiceCredentialAuthenticator` an example, not a default.** It guards the `Livepeer-Actor`
  rule that makes the header safe; a copy-pasted version is where that rule gets lost.

## Open questions to lock

- **Q1.** Examples in `examples/` installed from the wheel (recommended) or kept as repository tests?
- **Q2.** Build the trusted-backend authenticator in ISS-32 (recommended: E9 needs it) or leave it defined?
- **Q3.** Candidate as a `v0.2.0rcN` tag (recommended) or a commit SHA?
- **Q4.** Does the fixture wait for the events feed, or poll behind its own port until the hold lifts
  (recommended)?
- **Q5.** Router factories from #25 in ISS-32 (recommended: mounting part of the service is service
  consumption) or later?
- **Q6.** Export `DefaultPolicy` from the root (recommended), or teach only `fallback=parts.policy`?

## Delivery slices

1. Public exports and the public-API check over the examples (E2).
2. `examples/` with the wheel-installed CI job (E1).
3. `ServiceCredentialAuthenticator` and the access sweep under each authenticator (E4).
4. #25 router factories and `install_error_handlers` (E8); `ServerSettings`; docs generators.
5. The commerce fixture (E5, E6), then its MCP tool after ISS-30 (E7).
6. Tag the candidate, re-pin the consumers, record the table (E3, E9).
