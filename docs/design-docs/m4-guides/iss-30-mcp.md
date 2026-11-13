# ISS-30: The same authorized journey through MCP

> **Questions locked on 12 and 13 November 2026.** Mike answered every open question; see [Questions
> locked](#questions-locked). Where an answer differs from the proposal below, the answer wins, and that
> section says which part it replaces. Decision numbers are assigned in the M4 decision record.

**Status:** proposal for discussion, 29 October 2026. Nothing here is decided until it is locked; locked
rules become decisions, numbered when they are locked.\
**Tracker:** ISS-30 ([Network-Engineering-SPE#44](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/44)).\
**Owner:** John (engine). **Reviewers:** Mike, John.\
**Verified against:** builder-core `releases/m3` at `d3df806`; livepeer/console (`livepeer-console`) at
`009a703` for prior art. MCP specification claims are from memory of revisions `2025-06-18` and
`2025-11-25` and are marked **(verify)**: check them against the revision the pinned client speaks.

## Done when

- MCP tools for **discovery, rates, invocation, status/results and usage/cost**, using the configured
  authentication adapter and the **same core authorization and semantics as REST**.
- Interoperability shown with the **pinned client and access configuration chosen in ISS-19**. No universal
  OAuth or API-key path is imposed on enterprises; **MCP progress is not inference streaming**.

**Depends on:** ISS-29 (REST coverage and the shared-behavior contract checks), which itself follows ISS-18,
ISS-20 and ISS-28 (and ISS-46 for events). External: none.

## Current state (verified)

| Area | At `d3df806` |
| --- | --- |
| MCP code | **None.** No `mcp` package in the lock or the venv; `milestones.md:53` says "Not started" |
| Intent in the code | `contracts/common.py:10`: contracts are "the same class … in-process and as the REST/MCP schema". `di-consumers-and-plan-2026-10-23.md:94`: MCP is "a second transport adapter over the same `BuilderEngine`, like `http/`", sharing one composition root |
| Access contract | `access.md#modes` (line 110): "MCP OAuth (PKCE public clients) … engine verifies with JWT bearer and publishes RFC 9728 protected-resource metadata — Defined, with MCP (M3/M4)"; line 248 lists RFC 9728 as "phase 2"; appendix lines 330-332 (PKCE, RFC 8414 on the issuer, RFC 8707 resource). D13 (`m2-decisions.md`): authorization-server flows belong to the enterprise IdP |
| JWT resource server | Built (D45, D47-D50): `adapters/jwt.py`; one `LPB_JWT_AUDIENCE` string (`config.py:77`); challenge is `Bearer realm="…"` with **no `resource_metadata`** (`jwt.py:318-319`, `auth.py:39-40`); the chain joins challenges (`auth.py:56-58`) and REST sends them on 401 (`http/app.py:415-418`). No `/.well-known/oauth-protected-resource` route |
| Authorization | In the services' `AccessPolicy` checks, not in routes (`http/app.py:1-6`). So any adapter that calls the facade with the same `ActorContext` gets the same decisions |
| Error mapping | HTTP-only, in `_install_error_handlers` (`http/app.py:408-462`): `JobFailed` by failure kind, 409s, `NoCapacity` 503, `NotImplementedInMilestone` 501, simple statuses |
| Composition | `http/server.py`: `Extend = Callable[[FastAPI, EngineApi], None]` (line 18) and `_app` (lines 62-66); `lpb serve` in `cli.py:147-152` |
| Process rule | `engine.md:309`: one engine process per database (no HA) |
| Boundaries | `tests/structure/test_layers.py` `ALLOWED`/`CONFINED`; `test_import_weight.py` `HEAVY` |

**Prior art, livepeer/console** (upstream product, not a builder-core consumer). `/api/mcp`
(`app/api/mcp/route.ts`) is Streamable HTTP through `@modelcontextprotocol/sdk` `^1.30.0`, **stateless**
(`sessionIdGenerator: undefined`) with SSE responses so progress flushes during long polls
(`lib/mcp/mcp-http.ts`). It is **its own authorization server** (CIMD, DCR, PKCE; Auth0 login; PymtHouse
JWTs) and serves RFC 9728 metadata at `/.well-known/oauth-protected-resource/api/mcp` with
`WWW-Authenticate: Bearer realm=…, resource_metadata=…` (`lib/mcp/oauth.ts`). Tools: `list_capabilities`,
`describe_capability`, `get_pricing`, `run_capability` (blocks up to 13 minutes), `get_cost_report`, `me`,
`me_usage`, asset tools. Client notes in `docs/mcp-connectors.md`: Codex treats a `200` on `GET /api/mcp`
as protected-resource metadata, and Codex, ChatGPT and Hermes prefer CIMD. **What to reuse:** stateless
Streamable HTTP, the 401 + `resource_metadata` shape, the snake_case tool naming. **What not to:** an
authorization server inside the engine (D13 keeps the engine a resource server).

## Decision proposed

**An MCP adapter beside `http/`, mounted by `lpb serve` in the same process, whose tools call the same
`EngineApi` methods with the `ActorContext` the configured `Authenticator` chain produced.** Parity with REST
is then by construction; tests prove it.

### Transport and packaging

- **Streamable HTTP, stateless** (no `Mcp-Session-Id`): one `POST /mcp` per JSON-RPC message, a JSON
  response or an SSE response when the tool sends progress. `GET /mcp` answers 405 (no server-initiated
  stream) (verify; Q5). Validate `Origin`: a request carrying an `Origin` outside `LPB_MCP_ALLOWED_ORIGINS`
  (empty by default) is 403, as the spec requires against DNS rebinding (verify).
- **Package** `src/livepeer_builder/mcp_server/` (named so it is never confused with the `mcp` distribution):
  `app.py` (`create_mcp_app(engine, settings) -> ASGI app`), `auth.py` (ASGI middleware), `tools.py` (the
  tool table), `metadata.py` (RFC 9728 document). Uses the official Python SDK (`mcp`, FastMCP or the
  low-level `Server`; pin an exact 1.x after a spike).
- **Extra** `[mcp] = ["livepeer-builder[server]", "mcp==<pinned>"]`, separate from `[server]` because the SDK
  brings its own dependencies (`sse-starlette`, `httpx-sse`, `pydantic-settings`, …; verify for the pin).
- **`lpb serve` mounts it** at `LPB_MCP_PATH` (default `/mcp`) when `LPB_MCP=true`, and says which extra to
  install when it is missing (as `_serve` does for `[server]`). **No separate `lpb mcp` command**: a second
  process would be a second engine on the same database (`engine.md:309`). Embedders mount
  `create_mcp_app(engine)` in their own ASGI app, or pass it through `Extend`.
- **Lifespan:** the SDK's Streamable HTTP session manager must run inside the parent app's lifespan; a
  mounted sub-app's lifespan does not run under FastAPI (verify for the pin). `create_app` runs it.
- **Decide with #25** (core/server split): the adapter belongs with `http/` in the server package.

### Authentication and authorization

- **Middleware** builds `AuthRequest(method, path, headers, client_address)` from the ASGI scope exactly as
  `_authenticated` does (`http/app.py:168-178`), calls `engine.authenticator.authenticate`, and puts the
  `ActorContext` on the request state for the tools (read through the SDK's request context; verify the
  accessor for the pin). Unauthenticated or `NotAuthenticated` → **HTTP 401** before JSON-RPC, with the
  chain's challenge plus `resource_metadata="…"` when metadata is served. Any authenticator works: `lpk_`
  keys sent as a static header, JWTs, the trusted backend, an enterprise's own. Nothing is imposed.
- **Do not use the SDK's built-in auth** (`TokenVerifier`/`AuthSettings`): it sees only the bearer string,
  so header- or mTLS-based enterprise authenticators could not run, and it would duplicate `adapters/jwt.py`.
- **Protected-resource metadata (RFC 9728)**, served when JWT is configured, at
  `/.well-known/oauth-protected-resource{LPB_MCP_PATH}`: `resource` = `LPB_MCP_RESOURCE`,
  `authorization_servers` = [`LPB_JWT_ISSUER`], `scopes_supported` = `LPB_JWT_SCOPE_PREFIX` + `discover`,
  `jobs.run`, `jobs.read`, `costs.read`, `bearer_methods_supported` = `["header"]`. The issuer publishes RFC
  8414/OIDC metadata itself (D13). Clients send RFC 8707 `resource` = the MCP URL (verify), so the token's
  `aud` is that URL: `LPB_JWT_AUDIENCE` becomes a list so one engine accepts its REST and MCP resource
  identifiers (Q2). The engine never forwards the token (`access.md`, appendix), which also satisfies the
  spec's no-passthrough rule.
- **Authorization** is the services' `AccessPolicy`: the tools add no checks of their own, and an MCP admin
  surface is out of scope (Q1).

### Tools (M4)

| Tool | Facade call | Permission (checked by the service) |
| --- | --- | --- |
| `list_offerings(app?)` | `discovery.offerings(app=…)` | none in REST (D51); MCP is authenticated at the transport |
| `get_rates(app)` | `discovery.runners(actor, app)` → per-runner `price_info`, read with `payable_rate` (source and age: D62) | `discover` |
| `run_job(…JobRequest fields)` | `jobs.run(actor, JobRequest)`; `mode="async"` stays 501 until ISS-26 | `jobs.run` |
| `get_job(job_id)`, `list_jobs(limit, before)` | `jobs.get`, `jobs.list_jobs` | `jobs.read` |
| `get_job_result(job_id)` | `jobs.result` (501 today: results are not retained) | `jobs.read` |
| `get_job_cost(job_id)`, `get_cost_summary(start, end)` | `costs.for_job`, `costs.summary` | `costs.read` |
| `get_allowance()` | `payments.allowance(actor)` | (as REST) |

- **Schemas** come from the contracts (`JobRequest`, `Job`, `JobCost`, …); `outputSchema` and
  `structuredContent` where the client's revision supports them (verify).
- **`run_job` results:** a JSON body goes in `structuredContent` (and as text); `text/*` as text;
  `image/*` and `audio/*` as image or audio content; other bytes as an embedded blob. Always the `Job`
  id and `ResultRef`. Above `LPB_MCP_MAX_RESULT_BYTES` the bytes are dropped with a note, and they cannot be
  fetched again until ISS-26 stores results (Q3).
- **Progress** during `run_job` is liveness only (`running`, attempt n) when the client sent a
  `progressToken`; never runner bytes. There is no streaming tool and no session tool: a streamed runner
  is run buffered (`jobs.run`), and sockets (ISS-27) stay on REST. This is the tracker's "progress is not
  inference streaming".
- **Errors:** engine errors become tool results with `isError: true` and a body `{error, status, code, job?,
  milestone?}` with the **same status and code as REST**. Move the error→status mapping out of
  `http/app.py` into a pure `errors.describe(exc)` used by both adapters, so they cannot drift (ISS-29's
  contract checks then cover MCP too).

### Docs

`access.md`: the MCP row becomes Implemented, phase 2 metadata is described, the 401 `resource_metadata`
rule, audiences. New `docs/contracts/mcp.md` (tools, schemas, errors, progress rule, settings). `RELIABILITY.md`
settings: `LPB_MCP`, `LPB_MCP_PATH`, `LPB_MCP_RESOURCE`, `LPB_MCP_ALLOWED_ORIGINS`, `LPB_MCP_MAX_RESULT_BYTES`.
`milestones.md` row 53; `ARCHITECTURE.md` adds the adapter.

## Code areas

| File | Change |
| --- | --- |
| `mcp_server/` (new) | `app.py`, `auth.py`, `tools.py`, `metadata.py` |
| `errors.py`, `http/app.py` | `describe(exc)`; HTTP handlers use it (no behavior change) |
| `http/server.py`, `http/app.py` | Mount when `LPB_MCP=true`; run the MCP lifespan; root PRM route if REST metadata is wanted |
| `adapters/jwt.py`, `config.py` | Audience list; `realm` challenge may carry `resource_metadata` (REST too, Q2); the `LPB_MCP_*` settings |
| `cli.py` | `_serve` hint for the `[mcp]` extra |
| `pyproject.toml`, `uv.lock` | `[mcp]` extra with an exact SDK pin |
| `tests/structure/test_layers.py` | `ALLOWED["mcp_server"] = {contracts, errors, api, config}`; `http` may import `mcp_server`; `CONFINED["mcp"] = ("mcp_server/",)` |
| `tests/structure/test_import_weight.py` | `mcp` in `HEAVY`; `import livepeer_builder` still loads none |
| `tests/unit/test_mcp*.py` (new) | Below |

## Test plan (test-driven)

**Levels.** (1) Tool tests through the SDK's in-memory client/server pair (verify the helper for the pin)
over a real engine from `livepeer_builder.testing` with `ScriptedTransport`, the actor supplied by a seam.
(2) Transport tests with the SDK's Streamable HTTP client against the mounted ASGI app, through
`httpx.ASGITransport` if the client accepts an httpx factory (verify), else uvicorn on a loopback port as
`tests/unit/livepeer_net.py` does. (3) A **parity table** that drives the same cases through REST (httpx)
and MCP and compares status and code. (4) JWT and metadata with locally signed tokens, as `test_jwt.py`
does. (5) Manual interop evidence with the ISS-19 client.

**Seams.** The actor provider; `ScriptedTransport` outcomes; the JWKS fetcher; `FixedClock`.

**Starter test:** an unauthenticated `tools/list` is HTTP 401 with the chain's `WWW-Authenticate`.

```text
Transport and auth
[ ] unauthenticated POST /mcp: 401 + the chain's challenge; no JSON-RPC handled
[ ] lpk_ key in Authorization: tools/list returns exactly the M4 tool names
[ ] revoked key and expired JWT: 401
[ ] an enterprise Authenticator reading another header is honored (no bearer needed)
[ ] Origin outside LPB_MCP_ALLOWED_ORIGINS: 403; no Origin: allowed            (verify)
[ ] GET /mcp: 405
[ ] JWT on: PRM document fields; 401 carries resource_metadata
[ ] token for the MCP resource accepted; token for another audience refused
Tools
[ ] list_offerings matches GET /v1/offerings
[ ] get_rates without discover: tool error 403, as REST
[ ] run_job JSON result: structuredContent equals the runner body; job id present
[ ] run_job image result: image content, same bytes
[ ] run_job over LPB_MCP_MAX_RESULT_BYTES: no bytes, size, sha256 and a note that names async
[ ] run_job unprovisioned: isError, status 402, code not_provisioned, job included
[ ] run_job repeated operation_ref: same 409 code as REST (ISS-14)
[ ] run_job mode=async: isError 501 with milestone M4
[ ] progress notifications carry no runner bytes; none without a progressToken
[ ] get_job of another actor's job: 404 (not 403), as REST
[ ] get_job_cost, get_cost_summary, get_allowance match their REST answers
Parity and structure
[ ] parity table: every REST case for these operations gives the same status and code over MCP
[ ] errors.describe drives both adapters (HTTP handlers unchanged in behavior)
[ ] mcp imported only under mcp_server/; import livepeer_builder loads no mcp
[ ] LPB_MCP unset: /mcp is 404; LPB_MCP=true without the extra: lpb serve exits 2 with the hint
```

**Gears.** Obvious Implementation throughout; Triangulate the content mapping across content types.

**Definition of done.** List empty or answered; `AGENTS.md` checks green (`uv run pytest`, coverage gates,
`diff-cover`, `ruff`, `mypy` strict, `scripts/check_docs.py`, `lpb gen-docs`, `lpb smoke`); docs above
updated; an evidence page in `docs/references/evidence/` with the pinned client: an `lpk_` key session and an
OAuth session against the dev stack's identity provider, each running the representative job and reading
its cost.

## Upstream dependencies

| Upstream | Needed? | Why |
| --- | --- | --- |
| livepeer/node | No | MCP sits above the engine's facade |
| livepeer-python-gateway | No | Same |
| clearinghouse-batteries | No | Same |

The only new third-party dependency is the MCP Python SDK (`modelcontextprotocol/python-sdk`), confined to
`mcp_server/`. livepeer/console is prior art only.

## Consumer impact

Builder console: none required; a "connect an MCP client" help page is optional. ChatLPT and
inference-gateway embed the engine and are unaffected. Operators: a new extra and
settings, off by default.

## Alternatives considered

- **A separate MCP process calling REST** (for example in #73's `livepeer-client` wheel): passing the user's
  token on to REST is the token passthrough the spec forbids (verify), and parity becomes a copy.
- **The SDK's built-in auth:** see above; it cannot run header-based enterprise authenticators.
- **An authorization server in the engine** (as livepeer/console has): against D13.
- **Hand-written JSON-RPC:** the spec changes between revisions; the SDK tracks it.

## Questions locked

Locked by Mike on 13 November 2026. The spike of 4 November (SDK 2.3.0, paid jobs run from Claude Code) is
the source of the measured facts.

- **Q1, the tool set:** the table above, plus `whoami` (ISS-29) and `cancel_job` for queued jobs (ISS-26).
  `list_offerings` returns short rows by default and takes a search or capability filter, and a second
  call describes one offering in full: the spike measured the full catalog at 96 offerings and 55 KB. No
  `report_usage`, no admin tools, no session or event tools.
- **Q2, audiences:** `LPB_JWT_AUDIENCE` becomes a list. Proposed for the pull request, not separately
  decided: the MCP endpoint accepts only the MCP audience (`LPB_MCP_RESOURCE`) and REST accepts only the
  others.
- **Q3, large results:** over `LPB_MCP_MAX_RESULT_BYTES` the tool result carries the job id, the size, the
  SHA-256 and a note, and no bytes. The note and the tool's description say to use `mode="async"`, whose
  stored result is read with `get_job_result` or over REST. The cap's default is set in the pull request.
- **Q4, stdio:** HTTP only. No `lpb mcp` command.
- **Q5, `GET /mcp`:** 405. With SDK 2.3.0 a stateless server that accepts the `GET` holds an idle stream
  open.
- **Q6, long jobs:** immediate `run_job` keeps the 300 second default and accepts `timeout_s`. The tool
  sends liveness progress to a client that asked for it. The description says to use `mode="async"` and
  poll for work expected to take more than about a minute. Measured: Claude Code moved a tool call to the
  background after 120 seconds.
- **Q7, the pinned client:** the SDK's own client in CI, and Claude Code as the pinned host for the
  evidence page, with its version recorded.

**Other decisions that touch this guide.**

- **The SDK is 2.3.0**, not 1.x: the server class is `MCPServer`, and the client uses `httpx2`.
- **Tool errors** reuse the ISS-29 code table. REST problems are RFC 9457, so `errors.describe(exc)`
  returns the fields both adapters render.
- **Admin from a token claim** (ISS-33) changes the JWT settings this guide lists.
- **Not verified:** how the pinned client and the identity provider send the resource; whether Claude Code
  can sign in to the Keycloak realm with OAuth; which clients extend their timeout on progress.
- **Noted, not decided:** rates arrive mostly in wei with no USD figure, so a model cannot compare a price
  with the USD allowance.

### Where the runner standard plugs in later

Runner contract v1 (ISS-47) is not vetted yet. This ticket therefore routes by app ID, as today, and reads
no v1 metadata. ISS-48 adopts the standard as the last feature of M4, and it may move to M5.

- **Working assumption for ISS-48 (12 November):** app IDs stay the routing key; v1 metadata describes a
  runner and never routes; missing, old or unreadable metadata is ignored and logged; model and profile
  are filters; no schema check.
- **Criterion moved to ISS-48 on 13 November:** the tools are `search_capabilities`, `describe_capability`
  and a checked `run_job`; tool descriptions come only from the registry, never from runner metadata.
- **The seam to keep:** the compact, filterable `list_offerings` and the describe-one-offering call of Q1.
  They are where registry text replaces runner text.

## Delivery slices

1. `errors.describe` and the HTTP refactor (no behavior change), plus the parity harness on REST alone.
2. `mcp_server/` with `lpk_` auth, the tool table, the `[mcp]` extra and mounting; structure tests.
3. JWT audience list, RFC 9728 metadata, `resource_metadata` on 401; docs; interop evidence.
