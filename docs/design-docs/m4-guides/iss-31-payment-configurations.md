# ISS-31: Both payment-operation configurations

**Status:** proposal for discussion, 29 October 2026. Nothing here is decided until it is locked.\
**Tracker:** ISS-31 ([Network-Engineering-SPE#45](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/45)),
source task P4. Replaces the "Mode B" gap of D23.\
**Owner:** John (engine). **Reviewers:** Mike, John.\
**Verified against:** builder-core `releases/m3` at `d3df806`; Clearinghouse Batteries `ca09e47` (the pin)
and `livepeer/clearinghouse-batteries` `main` at `d47d1ff` (27 October); console `main` at `938db3e`.

## Done when

- The payment-operation configuration **not covered by ISS-13** is added and verified, so that both
  **self-operated** and **separately operated** payment access have usable setup, funding, credentials
  and reporting prerequisites.
- The actual test/operator arrangement and its limits are documented. Nothing implies an ongoing
  public service. Funded evidence for both configurations is ISS-36, not this ticket.

**Depends on:** ISS-13 (#26). **External:** H2 (Batteries), H3 (signer), H4 (funded environment).

## Current state (verified)

**The two configurations are the P4 pair, not the two allocation modes.** P4 in the task breakdown
reads "an operator running/funding Batteries and the signer, and an engine using a separately operated
payment service". builder-core calls them Mode A and Mode B
([build-track-baseline.md](../../product-specs/build-track-baseline.md#payment-operation-modes)). The
allocation modes (`per_actor`, `per_application`) are a separate axis, and both are already built.

| Item | State | Where |
| --- | --- | --- |
| Configuration 1 (self-operated) | Done in M3: the console's stack runs engine, Batteries and signer on one host; the operator funds the payer and grants | console `docs/references/m3-environment.md` (ISS-13) |
| Allocation modes | `per_actor` (default) and `per_application` built and tested | `services/payments.py:102-123`; `test_payments.py::test_per_application_mode_uses_the_configured_key` |
| Mode B | **Recorded as a gap** (D23): "Batteries management credentials are global", three options listed | [payments.md#mode-b](../../contracts/payments.md#mode-b) |
| Usage filter | `BatteriesUsageSource(filters=...)` and `CostSyncWorker(filters=...)` accept filters, and the checkpoint stores them; **assembly passes none** | `adapters/batteries.py:133-171`, `services/costs.py:73-106`, `runtime/assembly.py:222-231` |
| `per_application` allocation id | Set by hand (`LPB_SHARED_ALLOCATION_ID`) | tech debt T5 |
| Signer URL | `LPB_SIGNER_URL` (`config.py:43`); any reachable URL works | |
| Startup check of payment credentials | **Not found.** A credential that lacks a permission shows up as a Batteries 403 on first use | |

**Contradiction to fix.** The console says the second configuration is `per_application`
(`m3-environment.md` "Limits", `m3-journey.md` M4 table), and builder-core's `milestones.md` says "Mode A
implemented; Mode B defined as a gap". `per_application` is already implemented; what ISS-31 adds is
separate operation. The two statements meet in the design below, because Mode B runs `per_application`.

### Batteries at the pin (why Mode B is hard)

- Management permissions are `resource.action` and **"A permission covers all resources of that type"**
  (README "Management permissions" at `ca09e47`; the list is `internal/serviceauth/registry.go:18-27`).
  No credential can be limited to one grant. `upstream/main` `d47d1ff` has the same list.
- So a credential with `api_keys.create`, `allocations.create` and `allocations.fund` (what `per_actor`
  provisioning needs) can create allocations under, and fund from, **any tenant's grant**. A credential
  with `usage.read` reads **every tenant's usage**, whatever filter it sends.
- The webhook and the signer are already multi-tenant: the signer authorizes each request by the `lpg_`
  key it is given, and Batteries debits that key's allocation (payments.md "Payment path"). The engine
  never needs a management credential to *pay*. It needs one to *provision* and to *read usage*.

Batteries is a Go server. It cannot be vendored, and no upstream PR is proposed.

## Decision proposed

**Configuration 2 is "separately operated, operator-provisioned": the engine runs `per_application`
with an `lpg_` key and a read-only credential that the payment operator issues, filters its usage
sync to that one allocation, and refuses every money-moving command.** The operator owns funding,
allocations and keys. Per-actor allocations under a separate operator stay out until Batteries offers
grant-scoped credentials.

### What each side holds

| | Configuration 1: self-operated (ISS-13) | Configuration 2: separately operated (ISS-31) |
| --- | --- | --- |
| Runs Batteries and the signer | the engine's operator | the payment operator |
| Funds the payer (TicketBroker) | the engine's operator | the payment operator |
| Allocation mode | `per_actor` (or `per_application`) | `per_application` only |
| Engine holds | management token (`allocations.*`, `api_keys.*`, `usage.read`, `grants.fund`), optional grant-desk token | one `lpg_` key, its allocation id, a token with `allocations.read` and `usage.read` only |
| Grants, allocation, funding | engine admin routes | the operator's own tools (Batteries CLI or management API) |
| Per-user cap | the signer, per allocation | none upstream; the enterprise's own (admission limits help) |
| Per-user spend | exact (`spent_*` per allocation) | the engine's session log only |
| Usage visibility limit | the operator's own data | the read token can technically read every tenant (documented limit) |

### Engine changes

1. **New setting** `LPB_PAYMENT_OPERATION = self_operated | separately_operated` (default
   `self_operated`, so nothing changes for current deployments). `EngineSettings` validates
   `separately_operated` at load:
   - `LPB_ALLOCATION_MODE` must be `per_application`;
   - `LPB_SHARED_PAYMENT_KEY`, `LPB_SHARED_ALLOCATION_ID`, `LPB_SIGNER_URL`, `LPB_BATTERIES_URL`,
     `LPB_BATTERIES_TOKEN` are required;
   - `LPB_BATTERIES_OPERATOR_TOKEN` is refused (no grant desk against someone else's Batteries).
2. **Usage sync filtered to the allocation** in `runtime/assembly.py`: when separately operated, pass
   `filters={"allocation_id": shared_allocation_id}` to both `BatteriesUsageSource` and `CostSyncWorker`.
   The checkpoint already records filters, so the first sync after a change restarts from the start and
   skips known rows (`services/costs.py:85`). Batteries keeps session-less rows out of filtered lists
   (README, "Management HTTP API"); they never count anyway (only `applied` rows do).
3. **Money-moving commands refused before any provider call.** Today only `provision` checks the
   mode (`services/payments.py:134`); `fund`, `set_status`, `revoke` and `rotate_key` fail later with
   `NotFound` because no actor is provisioned in `per_application`, and `fund_grant`
   (`services/payments.py:230`) has no mode check at all, so it would reach Batteries and get a 403.
   When separately operated, all six raise `InvalidRequest("funding and keys are managed by the payment
   operator")` first. The grant routes stay unmounted, as without an operator token today.
4. **`lpb payments check`** (new CLI command, read-only): prints the configuration and probes what it
   needs: signer `/readyz`; `GET /v1/allocations/{id}` (proves `allocations.read` and that the key's
   allocation exists); `GET /v1/usage?allocation_id=…&limit=1` (proves `usage.read`). It never writes.
   This is the "usable setup … prerequisites" check for both configurations, and the operator's first
   step after receiving credentials.
5. **Health** (`/readyz`) reports `payment_operation` beside `allocation_mode`
   (`contracts/health.py:17`), so the console and an
   auditor can tell the configurations apart.

No contract type, store table or migration changes. `FakeEngine` is unaffected (it has no payment
configuration).

### What builder-core can do without an upstream change, and what it cannot

| Need | Without upstream | Limit |
| --- | --- | --- |
| Pay without a management credential | Yes: `lpg_` key + signer URL | |
| Read only its own usage | Filter by allocation (above) | The token *can* read others; trust, not enforcement |
| Per-actor allocations under a separate operator | **No** safe way: the create/fund permissions span every grant | Needs grant-scoped credentials upstream, or an operator-run tenant layer |
| Exact per-user spend | No (one allocation) | Session-log attribution only, as `per_application` today |

**The operator-run tenant layer** (option 3 of payments.md) is the operator's code, not builder-core's.
builder-core documents what such a layer must enforce so that a later configuration can lift the
`per_application` restriction: every `POST /v1/api-keys` names the tenant's `grant_id`; every
`/v1/allocations/{id}/*` and `/v1/api-keys/{id}/*` is checked against the allocation's grant; every
`GET /v1/usage`, `/v1/sessions`, `/v1/allocations` is forced to the tenant's `grant_id`; `/v1/grants`
is denied except `GET` and `POST /fund` on the tenant's own grant. That table goes into payments.md.

**Longer term (#68):** an x402-style payment-provider boundary would make the hosted operator a
facilitator that takes a caller-owned intent id and returns usage keyed by it. It removes the
management credential from the engine entirely and closes T13. It is research, not M4.

### The test/operator arrangement (to document, not a service)

Recommended: **two Compose projects on one host**, so the boundary is real (separate networks, separate
credential files) and reproducible without Railway.

- **Operator project** (Batteries + signer, the payer keystore): creates a grant, an allocation and one
  `lpg_` key with the Batteries CLI, writes a credential file with a `tenant-engine` entry allowing
  `allocations.read` and `usage.read`, and publishes only the signer port and the management port.
- **Engine project** (engine + console): knows only the two URLs, the `lpg_` key, the allocation id and
  the read token, with `LPB_PAYMENT_OPERATION=separately_operated`.

Limits to write down: one host; the same people operate both sides; the read token is not
tenant-scoped; one signer per payer (it shares the payer with ChatLPT and the console's own stack, so
only one runs at a time); funding is a manual operator step; no availability promise.

### Docs to change

`payments.md` (replace "Mode B" with "Payment operation configurations": the table above, the tenant-layer
requirements, the limits); `build-track-baseline.md` (Mode B row); `m2-decisions.md` D23 → new decision;
`milestones.md` ISS-31 row; `RELIABILITY.md` configuration table; a new `deploy/separate-operator/README.md`
for the arrangement; console `m3-environment.md` and `m3-journey.md` (the "second configuration" wording).

## Code areas

| File | Change |
| --- | --- |
| `config.py` | `payment_operation` setting and its validator |
| `runtime/assembly.py` | allocation filter on usage source and worker; pass the configuration to `PaymentsService` |
| `services/payments.py` | one guard for the six admin money commands when separately operated |
| `cli.py` | `lpb payments check` |
| `contracts/health.py`, `services/` health | `payment_operation` on `EngineHealth` |
| `deploy/separate-operator/` (new) | two compose files, credential template, README |
| `tests/unit/test_adapters.py`, `test_assembly.py`, `test_payments.py`, `test_cli.py`, `tests/structure/test_deploy.py` | below |

## Test plan (test-driven)

**Levels.** Settings validation and assembly wiring are unit tests with `FakeUsageSource` and
`FakeAllocationManager` (`testing/fakes.py`); the refusal is a service test plus one HTTP route test; the
check command runs against `httpx.MockTransport` for the adapter calls. The deploy files are checked by a
structure test (as `test_deploy.py` checks the Railway credential list). The live check is the operator
arrangement, run once and recorded (it is ISS-36's input, not its evidence).

**Seams.** Settings (pure), `EngineSupply` (inject the fakes), `MockTransport` for Batteries reads.

**Starter test:** `separately_operated` with `per_actor` fails settings validation with a message
naming `LPB_ALLOCATION_MODE`.

```text
[ ] separately_operated requires per_application
[ ] separately_operated requires the key, allocation id, signer, Batteries URL and token
[ ] separately_operated refuses an operator token
[ ] self_operated is the default and changes no wiring (existing tests stay green)
[ ] the usage source receives allocation_id=<shared allocation>
[ ] the checkpoint records the filter; changing it restarts the cursor and applies no row twice
[ ] fund_grant is refused before any provider call (FakeAllocationManager records no call)
[ ] provision, fund, set_status, revoke, rotate_key answer the operator message, not NotFound
[ ] the grant routes are absent; POST /v1/admin/grant/fund answers 400 with the operator message
[ ] reconciliation balances on the shared allocation with filtered usage
[ ] a paid job attributes through the filtered feed (loopback signer, FakeUsageSource)
[ ] lpb payments check reports signer, allocation read and usage read; exit 1 on a 403
[ ] lpb payments check never sends a non-GET request
[ ] readyz reports payment_operation
[ ] the operator credential template allows only allocations.read and usage.read (structure)
[?] the check also reads GET /v1/api-keys/{id} to close T5                        Q3
```

**Definition of done.** Every item `[x]`, `[=]` or `[-]` with a reason; the checks in `AGENTS.md`
(`uv run pytest`, coverage gates, `ruff`, `mypy` strict, `scripts/check_docs.py`, `lpb gen-docs`,
`lpb smoke`); the arrangement run once with `lpb payments check` green and one paid job attributed; docs
above updated.

## Upstream dependencies

| Upstream | Needed? | Why / workaround |
| --- | --- | --- |
| livepeer/node | No | The signer authorizes per `lpg_` key through the webhook; a separate operator changes nothing in it |
| livepeer-python-gateway | No | Payment goes through the engine's own runner call (D53) with the configured key |
| clearinghouse-batteries | **Not for M4** | Grant-scoped credentials would unlock `per_actor` under a separate operator. It is a server; not vendorable. M4 works around it with `per_application` and a filtered read; a tenant layer is the operator's choice. Re-check permission scoping on each pin bump |

## Consumer impact

| Consumer | Change |
| --- | --- |
| Console | Reads `payment_operation` from health; in configuration 2 hides Operator money actions, and its direct Batteries pages (Money, Users) need the operator's read token or show "not available" |
| ChatLPT, inference-gateway | None (they use `per_application` today or embed their own settings) |
| Enterprise desk | None |

## Alternatives considered

- **Per-actor under a separate operator with a global credential.** Rejected: one engine could drain
  every tenant's grant.
- **Ship a tenant proxy in builder-core.** Rejected for M4: it is the operator's product, not the
  engine's, and would imply a hosted service.
- **Railway as the separate operator.** Possible (Batteries and signer already deploy there), but their
  ports are on Railway's private network today (`deploy/railway/README.md`) and it shares the payer.

## Open questions to lock

- **Q1.** Is `per_application` only acceptable for configuration 2 in M4 (recommended), or must per-actor
  caps work under a separate operator (then a Batteries change or a tenant layer is a prerequisite)?
- **Q2.** Arrangement: two Compose projects on one host (recommended), or Railway's Batteries and signer
  made public?
- **Q3.** Close T5 by reading the key's allocation at start (needs `api_keys.read`, a wider grant), or
  keep `LPB_SHARED_ALLOCATION_ID` by hand (recommended)?
- **Q4.** Setting name: `LPB_PAYMENT_OPERATION` with `self_operated | separately_operated`?

## Delivery slices

1. Setting, validation, filtered usage, refusals, health field (engine PR).
2. `lpb payments check` and the `deploy/separate-operator/` arrangement, run once.
3. Docs (payments.md, baseline, decisions) and the console wording and health display.
