# ISS-29: Complete REST coverage across the required execution modes

**Status:** proposal for discussion, 29 October 2026. Nothing here is decided until it is locked; locked
rules become decisions (numbered then).\
**Tracker:** ISS-29 ([Network-Engineering-SPE#43](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/43)).\
**Owner:** John (engine). **Reviewers:** Mike, John.\
**Verified against:** builder-core `releases/m3` at `d3df806`; builder console `main` at `938db3e`.

## Done when

- Extend and verify the initial REST contract for **all required modes** (immediate, streamed, async,
  persistent), durable status and results, shared errors and ownership rules.
- Contract checks show that **every REST operation uses shared behavior** and that the initial (M3)
  journey stays compatible. Closes the rest of I1 after ISS-20.
- **Depends on** ISS-18 (events feed, held), ISS-20 (M3 REST journey), ISS-28 (failure classification),
  and so transitively on ISS-26 (async) and ISS-27 (persistent).

## Current state (verified)

`http/app.py` (469 lines) defines 31 operations on 27 paths plus the hidden `/livez`; the committed
`docs/generated/openapi.json` lists the same 27 paths (OpenAPI 3.1.0). Each route authenticates through
`actor_dependency(engine)` and makes one facade call; authorization and ownership live in the services
(`services/jobs.py` `_owned`, `services/ownership.py`).

| Area | Operations today | State | Gap for M4 |
| --- | --- | --- | --- |
| Health, catalog | `GET /readyz`, `GET /v1/offerings` (public), `GET /v1/runners` | built | rate fields from the ISS-10 guide |
| Immediate | `POST /v1/jobs` | built | `code` on problems (below) |
| Streamed | `POST /v1/jobs?stream=true` or `Accept: text/event-stream` (D37) | built; 400 without a streaming transport | none |
| Status | `GET /v1/jobs` (`limit` only), `GET /v1/jobs/{id}` | built | **no paging**: the service and facade take `before` (`jobs.py` `list_jobs`, `api.py:94-96`), REST does not |
| Async (ISS-26) | `mode="async"` on `POST /v1/jobs` | **501** (`jobs.py:249`) | no `submit` on the facade `JobsApi` (`api.py:91-100`) although `JobService.submit` exists (`jobs.py:190-193`); no 202 answer defined |
| Results | `GET /v1/jobs/{id}/result` | **501** after permission and ownership (`jobs.py:195-197`) | stored results (ISS-26) |
| Persistent (ISS-27) | `POST /v1/sessions` | **501** (`ReservedSessions`, `services/reserved.py`) | no close or read route, though `SessionManager.close` exists (`protocols/__init__.py:383-386`); no proxy route for `Session.proxy_path`; the 501 runs before any permission check |
| Usage, cost | `POST /v1/jobs/{id}/usage`, `GET /v1/jobs/{id}/cost`, `GET /v1/costs/projections` (max 25), `GET /v1/costs/summary`, `GET /v1/allowance` | built | projections have no paging (console gap G3); `by_app` from the ISS-18 guide; admin reads scoped to the key's application (G9) |
| Events (ISS-18) | `GET /v1/events?after&limit` | **501**: `assemble` falls back to `ReservedEventFeed` (`runtime/assembly.py:258`) | held by the 21 October M3 decision (item 4); no error type for the contract's **410** on an expired cursor (`errors.py` has none) |
| Credentials | `/v1/admin/keys` (issue, list, get, rotate, revoke; only when `engine.keys`) | built | `operation_ref` on issue and rotate (idempotency guide) |
| Allocations, grants | provision, read, fund, status, revoke, rotate-key; grants list and create (only with a grant desk); `POST /v1/admin/grant/fund` | built | `operation_ref` on provision and create grant; route naming (`grant/fund` has no id, `interfaces-and-http-servers.md` question 3); the created grant comes back incomplete (G12) |
| Admin reads | `GET /v1/admin/actors`, `GET /v1/admin/reconciliation`, `POST /v1/admin/costs/sync` | built | no application-wide reconciliation in `per_actor` (G10) |
| Caller identity | none | not found | no "who am I" (console G13) |

### Error model today

- Every engine error is `{"error": <message>, ...extra}` as `application/json` (`app.py:410-413`);
  extras are `job` (409 and `JobFailed`), `milestone` and `ticket` (501). There is **no stable `code`**.
- FastAPI's own 422 is a different shape, `{"detail": [...]}` (no `RequestValidationError` handler).
- `ProviderError` passes Batteries 400, 404, 409 and 422 through as the engine's own status
  (`app.py:459-462`), so a client cannot tell them apart (`interfaces-and-http-servers.md` question 2).
- `CursorMismatch`, `UnsupportedForBackend` and `RunnerCallError` have no handler, and there is no
  catch-all for `EngineError`, so one that escapes is a bare 500.
- The OpenAPI spec declares **no error responses** (only 200, 201, 204 and 422), so generated clients
  have no type for a problem.
- Consumers parse messages: the console detects an unprovisioned builder with
  `detail.startsWith("no allocation")` (`builder-core-mgmt-ui/web/src/services/builder.ts:177`).

### Versioning and drift today

- All routes are under `/v1`; the app's `version` is the engine's (`app.py:191`); the committed spec
  says `"generated"` (`docs_gen.py:24`). Events carry `schema_version` (`events.md`). **No written REST
  compatibility rule exists.** Releases are `v*` tags read by hatch-vcs; only `v0.1.0` exists.
- **Drift check exists:** CI runs `uv run lpb gen-docs --check` (`.github/workflows/ci.yml`), with every
  optional capability on so the spec documents the whole API (`docs_gen.py:13-20`). Nothing checks for
  **breaking** changes against a release.
- The console keeps a copy (`openapi/engine.openapi.json`, identical to `d3df806`'s), generates types
  with `openapi-typescript` (`web/package.json` `gen:api`) and checks only two features of it
  (`web/tests/structure/pins.test.ts`).

### Docs out of step with the code

`interfaces-and-http-servers.md` (Routes) omits `GET /v1/admin/actors` and key get/rotate, and says
events answer 501 "until M4"; `docs/contracts/index.md` still lists JWT as "defined (M4)" although D45
built it; D51 (public catalog) is still under "Proposed" in `m3-decisions.md` while `GET /v1/offerings`
is public in code (`app.py:206-209`); `ReservedEventFeed` names the hold (`milestone: "held"`, `ticket: "ISS-18"`, D60).

## Decision proposed

**1. A REST contract document and a coverage table.** Add `docs/contracts/rest.md`: one row per
operation with its facade call, permission, modes, success status and problem codes, and one row per
facade member that has **no** route with the reason (`discovery.candidates`, `keys.bootstrap_admin`,
`start`/`stop`: embedded only). A structural test keeps the table equal to `app.openapi()` and to the
facade Protocols, so a new route or facade member without a row fails CI.

**2. Mode routes** (each lands with its ticket and follows its guide, [iss-26-async-jobs.md](iss-26-async-jobs.md)
and [iss-27-persistent-endpoints.md](iss-27-persistent-endpoints.md); ISS-29 owns the shared error
model, the coverage table and the contract checks):

| Mode | Route | Answer |
| --- | --- | --- |
| Async (ISS-26) | `POST /v1/jobs` with `mode="async"` | `202`, `Location: /v1/jobs/{id}`, `Livepeer-Job-Id`, body `Job` (`queued`); facade gains `jobs.submit` |
| Async result | `GET /v1/jobs/{id}/result` | the stored body and content type; `409 code=job_not_finished` with the job and `Retry-After` while queued or running; `410 code=result_expired` after retention and for an immediate job (never retained) |
| Persistent (ISS-27) | `POST /v1/sessions`, `GET /v1/sessions[/{id}]`, `POST /v1/sessions/{id}/close`, `ANY` and WebSocket on `/v1/sessions/{id}/app/{path}` | `201 Session`, `Session[]`/`Session`, `Session`, the runner's answer (as the ISS-27 guide proposes) |
| Events (if the hold lifts) | `GET /v1/events` | `EventPage`; `410 code=cursor_expired` |

The aliases proposed in #73 (`/v1/run/{app}`, `/v1/queue/{app}`, `wss` routes) translate into the same
`JobRequest`/`SessionRequest` and add no behavior; they are in scope only if ISS-27 adopts #73.
Reserved routes check permission before answering 501 (`ReservedSessions` and `ReservedEventFeed` take
the policy), matching `JobService.result`.

**3. One error body, with a stable `code`.** Keep `error` (the message) for compatibility and add
`code` to every problem. Codes per error type: `not_authenticated`, `access_denied`, `not_found`,
`invalid_request`, `validation_failed` (the 422, which also gets `{"error", "code", "detail"}`),
`not_provisioned`, `operation_exists`, `operation_in_progress`, `operation_conflict` (idempotency guide),
`no_capacity`, `provider_unavailable`, `provider_rejected` (a passed-through Batteries 4xx gains a code
that says so), `job_failed` (with `kind` = `Failure.kind` and `reason` from the ISS-12 guide),
`cursor_expired`, `not_implemented`, `internal` (a new catch-all `EngineError` handler, generic message).
An `ErrorBody` model is declared app-wide through FastAPI's `responses=`, so the spec and the console's
generated types carry it. Codes are an open set: clients must tolerate unknown codes and fields.
MCP tool errors reuse the same table ([iss-30-mcp.md](iss-30-mcp.md) puts `code` in its error results).

**4. Paging, one convention.** List routes keep returning arrays (no envelope, so no break) and gain
keyset paging: `before` (an ISO time, as the ISS-15 guide proposes) on `GET /v1/jobs`,
`GET /v1/costs/projections` and `GET /v1/admin/actors`, plus an RFC 8288 `Link: <…>; rel="next"` header.
The store orders by `(created_at, id)`, and the **cursor carries both**: ordering alone does not stop
a skip, because `before` with a time only cannot tell which of two jobs with an equal `created_at` the
last page already returned (the M3 limit D59 documents). So the next page is `created_at < t OR
(created_at = t AND id < last_id)`, with the pair in the `Link` header or a `before_id` parameter (Q3).

**5. Versioning rule** (in `rest.md`): within `/v1` changes are additive only (new routes, new optional
fields, new codes and enum values, which clients must tolerate). Until the M5 release (ISS-38) a breaking
change needs a decision record and a consumer re-pin; after it, a changed resource gets a `/v2` path.
`scripts/check_api_compat.py` compares the generated spec with the spec at the newest `v*` tag
(`git show <tag>:docs/generated/openapi.json`) and fails on a removed operation, a removed response
field or a new required request field, unless `rest.md` lists the break with its decision.

**6. Contract checks that all REST operations use shared behavior:**
- **A REST side for the facade contract.** `tests/unit/test_fake_engine_contract.py` runs each journey
  case against `FakeEngine` and a real engine (`Side`). Add a third side: a test-only `ActorApi` client
  over `httpx.ASGITransport` and `create_app`, which maps problems back to `EngineError`s by `code`. The
  same cases then prove REST, facade and fake agree, and the `code` table must be complete and
  invertible to pass.
- **A generated access sweep.** Parametrize over every operation in `app.openapi()`: no credential is
  401 with `WWW-Authenticate`; a credential without the permission is 403; another actor's resource is
  404; an auditor's write is 403. New routes join the sweep automatically (it extends the ISS-23 guide's
  route sweep).
- **Journey compatibility.** An HTTP test pins the seven M3 journey calls and their answers
  (`builder-core-mgmt-ui/docs/product-specs/m3-journey.md`), and `check_api_compat.py` guards them
  against the release spec.

**7. Bring the M3 guides' REST changes in through this contract**, each with its row in `rest.md`:
`code`, `operation_ref` on admin bodies, `Retry-After` on in-progress and `Idempotent-Replayed`
([iss-11-14-idempotency.md](../m3-guides/iss-11-14-idempotency.md)); `Failure.reason` and status by
reason ([iss-12-payment-failures.md](../m3-guides/iss-12-payment-failures.md)); `before` on jobs
([iss-15-history-and-results.md](../m3-guides/iss-15-history-and-results.md)); `by_app`
([iss-18-cost-reports.md](../m3-guides/iss-18-cost-reports.md)); rate fields
([iss-10-rates.md](../m3-guides/iss-10-rates.md)). Add `GET /v1/me` (G13: actor, application, kind,
permissions; any authenticated caller) and an optional `application_id` on the admin money reads (G9).

## Code areas

| File | Change |
| --- | --- |
| `http/app.py` | `ErrorBody`, `code` in `problem()`, 422 and catch-all handlers, `before` + `Link`, 202 for async, session routes, `/v1/me`; split per #25's router factories if it lands first |
| `errors.py` | `code` class attribute on each `EngineError`; `CursorExpired` (410), `ResultExpired`, `JobNotFinished` |
| `api.py` | `JobsApi.submit`; `SessionManager` read; `whoami` on `ActorApi` (or a REST-only route, Q4) |
| `services/reserved.py` | policy check before the 501; correct milestone text for the held feed |
| `store/sql.py` | `(created_at, id)` keyset for jobs, projections, actors |
| `testing/fake_engine.py` | the same codes and paging |
| `scripts/check_api_compat.py`, `.github/workflows/ci.yml` | release-compatibility check |
| `tests/unit/rest_client.py`, `test_fake_engine_contract.py`, `tests/structure/test_rest_coverage.py`, `tests/unit/test_http_routes.py` | REST side, coverage table, access sweep, journey pin |
| `docs/contracts/rest.md` (new), `engine.md`, `events.md`, `contracts/index.md`, `interfaces-and-http-servers.md` | contract, codes, versioning, the stale rows above |

## Test plan (test-driven)

**Levels.** HTTP through `httpx.ASGITransport` and `create_app` on an in-memory SQLite engine (the
existing `test_http_routes.py` style); the facade contract with three sides; one structural test over
`app.openapi()`; the compatibility script tested on two small fixture specs. **Seams:** `FixedClock` for
paging times, `ScriptedTransport` and `ScriptedStreamTransport` for outcomes. No mocks of FastAPI.

**Starter test:** a 404 for another actor's job carries `code: "not_found"` beside `error` (red today:
there is no `code`).

```text
Error model
[ ] every EngineError type maps to exactly one code (table test over EngineError subclasses)
[ ] a 422 body is {"error", "code": "validation_failed", "detail"}
[ ] an unmapped EngineError is 500 code=internal with a generic message, no exception text
[ ] JobFailed carries code=job_failed and kind; not_provisioned stays 402
[ ] a passed-through Batteries 409 says code=provider_rejected
[ ] the OpenAPI spec declares ErrorBody on every operation
Coverage and shared behavior
[ ] every OpenAPI operation has a rest.md row and every facade member a row or a reason
[ ] the facade contract cases pass on the REST side (journey, failures, streams, repeats)
[ ] access sweep: 401, 403, 404, auditor-write 403 for every operation
[ ] the seven M3 journey calls answer as pinned
Paging
[ ] GET /v1/jobs?before= returns the next page, newest first, own jobs only
[ ] two jobs with the same created_at are neither skipped nor repeated across pages
[ ] Link rel=next is present until the last page
[ ] projections and actors page the same way
Modes (land with ISS-26, ISS-27)
[ ] async submit is 202 with Location and a queued Job
[ ] result while running is 409 job_not_finished; after success the stored bytes
[ ] session open, read, list, close round trip; another actor's session is 404
[?] events: EventPage and 410 cursor_expired                  (only if the hold lifts, Q1)
Compatibility
[ ] check_api_compat fails on a removed operation and on a new required field
[ ] check_api_compat passes on an added optional field
[ ] GET /v1/me returns the caller's actor, application, kind and permissions
```

**Gears.** Obvious Implementation for the codes and handlers; Triangulate the keyset paging across ties.

**Definition of done.** Every item `[x]`, `[=]` or `[-]` with a reason; the `AGENTS.md` checks green
(`uv run pytest`, coverage gates, `ruff`, `mypy` strict, `scripts/check_docs.py`, `lpb gen-docs --check`),
`tests/store` on PostgreSQL, the new compatibility check in CI, and the console re-pinned to the spec.

## Upstream dependencies

| Upstream | Needed? | Why |
| --- | --- | --- |
| livepeer/node | No | REST shape is engine-only. Async and session runtime needs belong to ISS-26/27 |
| livepeer-python-gateway | No | No REST behavior comes from the SDK; ISS-27's `reserve_session` problem (#73) is that ticket's |
| clearinghouse-batteries | No | `provider_rejected` only labels statuses Batteries already sends |

## Consumer impact

| Consumer | Change |
| --- | --- |
| Builder console | Re-copy the spec and regenerate types; branch on `code` (replace `builder.ts:177`); use `/v1/me` and `before`; record the spec's sha256 in `pins.ts` so `pins.test.ts` proves the copy matches the pin. It exercises 25 of the 31 operations (`web/src/domain/catalog.ts`); not `GET …/allocation`, `/usage`, `/result`, `/v1/events`, `/v1/sessions` or `costs/sync` |
| Enterprise desk | Probes the reserved 501s (its ISS-04 page); update when they go live |
| ChatLPT, inference-gateway | None: embedded, no REST |

## Alternatives considered

- **RFC 9457 `application/problem+json`** (`type`, `title`, `status`, `detail`). Rejected for M4: it renames
  `error`, which every consumer reads; `code` gives the same branching. Revisit at `/v2`.
- **A page envelope `{items, next}`.** Rejected: it breaks every list consumer; `Link` is additive.
- **oasdiff in CI.** A Go tool for a check of three rules; the script is smaller and testable here.

## Open questions to lock

- **Q1.** Does ISS-29 wait for the events hold to lift, or close with `/v1/events` documented as held
  (recommended: close with the hold recorded, as the ISS-18 guide does)?
- **Q2.** `code` beside `error` (recommended) or RFC 9457 now?
- **Q3.** Keyset paging on `(created_at, id)`, with the cursor carrying both (`before` plus `before_id`,
  recommended), or an opaque cursor? Either way a time-only `before` keeps D59's equal-time skip.
- **Q4.** `/v1/me` as a facade member (`whoami`, recommended so MCP gets it too) or REST only?
- **Q5.** Rename `POST /v1/admin/grant/fund` to `POST /v1/admin/grants/{id}/fund` before the M5 release,
  keeping the old path as an alias until then?

## Delivery slices

1. Error model: `code`, 422 and catch-all handlers, `ErrorBody` in the spec (unblocks the idempotency
   and ISS-12 work and the console).
2. `rest.md`, the coverage test, the access sweep and the journey pin; fix the stale docs.
3. Paging and `/v1/me`.
4. The REST side of the facade contract.
5. `check_api_compat.py` in CI.
6. Mode routes, each with ISS-26 and ISS-27.
