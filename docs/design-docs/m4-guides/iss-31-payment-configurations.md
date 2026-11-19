# ISS-31: Both payment-operation configurations

> **Rewritten on 18 November 2026.** Mike replaced the design locked on 13 November (D74). The second
> configuration is now a **payment clearinghouse**. The decisions are D82 to D87 in
> [m4-decisions.md](../m4-decisions.md), and the answers are under [Questions locked](#questions-locked).
> The 13 November design (`separately_operated`, `per_application` only) is not built.

**Status:** locked by Mike on 18 November 2026, with the open points settled the same day (D87). The
work is tracked in [the exec plan](../../exec-plans/active/iss-31-payment-clearinghouse.md).\
**Tracker:** ISS-31 ([Network-Engineering-SPE#45](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/45)),
source task P4, amended on 18 November. Its work is eight builder-core issues, #162 to #169 (P1 to P8).\
**Owner:** John (engine). **Reviewers:** Mike, John.\
**Verified against:** builder-core `releases/m4` at `719378f`; Clearinghouse Batteries `fb76150`;
livepeer/node `eee09c4` (v1.0.7); the gateway SDK at `7dc2a07`; console `main` at `274a361`.

## Words used here

| Term | Meaning |
| --- | --- |
| Payment clearinghouse | The hosted service: an engine, Batteries, the signer and the wallet. It does payments only |
| Self-hosted payments | An enterprise running that same stack for itself, with one tenant |
| Enterprise app | Software with its own users that buys network work. ChatLPT is one |
| Embedded engine, engine server | The two ways an enterprise app uses the engine: as a library in its process, or as its own HTTP server |
| Tenant | An enterprise app's account at a clearinghouse |
| Tenant admin | The enterprise app's credential for its tenant. It divides the grant and cannot enlarge it |
| Clearinghouse admin | Runs the clearinghouse: onboards tenants, tops up grants, funds the wallet |
| Grant, allocation | The tenant's prepaid budget, and one actor's share of it |
| Payment key | The key an enterprise's engine presents when it asks for a payment for one actor |
| Work half, clearinghouse half | The two halves of the engine, described below |

## Done when

- A payment clearinghouse serves at least two tenants, one for each enterprise app. Each enterprise app
  runs its jobs in its own engine, as an embedded engine or an engine server, and reaches the
  clearinghouse only for signing, allotment, allowance and usage.
- One tenant's admin cannot read or change another tenant. The attempt is refused.
- Self-hosted payments still work, as one deployment with one tenant.
- The clearinghouse admin onboards a tenant and tops up its grant by hand, on a private listener. The
  arrangement and its limits are documented. Nothing implies an ongoing public service. Funded evidence
  for both configurations is ISS-36, not this ticket.

**Depends on:** ISS-13 (#26). **External:** H2 (Batteries), H3 (signer), H4 (funded environment).

## Current state (verified)

| Item | State | Where |
| --- | --- | --- |
| Self-hosted payments | Done in M3: the console's stack runs the engine, Batteries and the signer on one host | console `docs/references/m3-environment.md` (ISS-13) |
| Allocation modes | `per_actor` is the only mode since P1 (#162). At `719378f` `per_application` was built too: it took one allocation and one key from settings for the whole engine | `services/payments.py`. The settings `allocation_mode`, `shared_payment_key` and `shared_allocation_id` are gone from `config.py` |
| Names | Since P2 (#163) routes, classes, settings and records use the domain language: `tenant_id`, the kind `clearinghouse_admin`, `allot`, `top_up`, `PaymentKey`, `LPB_BATTERIES_GRANTS_TOKEN`, and the `/v1/clearinghouse` prefix. At `719378f` they said `application_id`, `admin`, `provision`, `fund`, `PaymentCredential`, `LPB_BATTERIES_OPERATOR_TOKEN` and `/v1/admin` | `contracts/`, `http/app.py`, `config.py`, migration 0014 |
| Admin authority | Since P4 (#165) there are two admin kinds. `DefaultPolicy.check` still returns at once for `clearinghouse_admin`, an admin of every tenant. A `tenant_admin` is an admin of its own tenant only: it divides its grant and cannot enlarge it, and it changes no admin key. At `719378f` there was one admin kind | `services/policy.py` |
| Writes and the policy | Since P4 (#165) allot, top up, status, revoke, payment key rotation and every key command pass the tenant they act on to the policy, so a tenant admin is confined there and another tenant in a path is 403 before any lookup or Batteries call. At `719378f` they passed no resource, so the path or body chose the tenant | `services/payments.py`, `services/keys.py`, `services/tenants.py` |
| Tenants | Since P4 (#165) a tenant's id is its Batteries grant's id. `POST /v1/clearinghouse/tenants` onboards one (its grant and its first tenant admin key), and its grant, actors, allocations and keys are nested under `/v1/clearinghouse/tenants/{tenant_id}`. `grant_id` is gone from `AllotRequest`, `TopUpRequest` and their bodies. Records written before keep the grant they name: nothing is re-keyed | `services/tenants.py`, `http/clearinghouse.py`; [payments.md](../../contracts/payments.md#tenants) |
| Payment keys | Since P5 (#166) allotting an allocation returns the actor's payment key (`lpp_`), once. Rotating the payment key replaces it, with the Batteries key behind it, and the old one stops at once; revoking the allocation stops it. The clearinghouse keeps the key's hash, bound to the allocation, and finds the actor of a live key (`payments.resolve_payment_key`). As a bearer on any route but the signer's two paths it gets what no credential gets. Before P5 the answer was the allocation alone, and an allocation allotted then has no payment key until it is rotated | `services/issued_payment_keys.py`, migration 0015; [payments.md](../../contracts/payments.md#payment-keys) |
| A tenant's usage | Since P5 (#166) `GET /v1/clearinghouse/tenants/{tenant_id}/usage` returns the tenant's payment records as the `UsageSource` port's own page, filtered by Batteries' `grant_id` filter, and the tenant list and the grant read carry `spent`. Before P5 only cost sync read usage, for every tenant | `services/tenant_usage.py`, `adapters/batteries.py`; [payments.md](../../contracts/payments.md#a-tenants-usage) |
| The runner list | Since P5 (#166) `GET /v1/clearinghouse/discovery` serves the livepeer/node document the clearinghouse's own discovery source last read, byte for byte, to a tenant's admin or auditor. Today's reader reads it once its HTTP client sends a credential, which is P7's to add. Before P5 no engine served a list | `services/runner_list.py`, `adapters/sdk.py`; [discovery.md](../../contracts/discovery.md#the-clearinghouses-runner-list) |
| Signing through a clearinghouse | Since P5 (#166) an engine with a signer serves the signer's two paths at the root, `POST /sign-orchestrator-info` and `POST /generate-live-payment`, each called with a payment key (D87 point 3, as amended on 19 November). It puts the actor's Batteries key in the payment key's place and calls its signer once; the body, the signer's `state` and the signer's answer pass through as they are, so an empty allocation is the signer's 402. A fixed job and a live job are paid through it by the unmodified SDK's transport, against a loopback signer. Before P5 only an engine's own jobs reached a signer | `http/signing.py`, `services/payment_signing.py`, `adapters/signer_forward.py`; [payments.md](../../contracts/payments.md#signing-through-a-clearinghouse) |
| Signer address | One setting, `LPB_SIGNER_URL` | `config.py` |
| Payment ports | Five: signing inside `RunnerTransport`, `CredentialResolver`, `AllocationManager`, `AllowanceReader`, `UsageSource`. A caller can swap each one. Since P3 (#164) a structure test fails when the work half reaches payments another way | `protocols/__init__.py`, `runtime/assembly.py` (`EngineSupply`), `tests/structure/test_two_halves.py` |
| Route sets | Since P3 (#164) the routes are a work set and a clearinghouse set, each mounted by itself. Since P4 the clearinghouse set is two groups that also mount by themselves: a tenant's own routes, and what only a clearinghouse admin does, for the private listener of P6. Since P6 (#167) one table says which listener serves which group (`http/listeners.py`); with one listener the app mounts every group. No role setting yet. Reconciliation, cost sync and an engine's own access keys are work routes, at `/v1/costs/reconciliation`, `/v1/costs/sync` and `/v1/keys`. At `719378f` every route was in one function | `http/work.py`, `http/clearinghouse.py`, `http/app.py` (`shared_app`, `create_app`); `api.py` (`WorkApi`, `ClearinghouseApi`) |
| Listeners | Since P6 (#167) one by default, on `LPB_HTTP_HOST` and `LPB_HTTP_PORT`, with every route for every kind of credential: what self-hosted payments runs. `LPB_HTTP_PRIVATE_PORT` turns on a private listener (`LPB_HTTP_PRIVATE_HOST`, loopback unless set): two apps over one engine in one process. The public listener serves no route of the private group (404) and refuses a `clearinghouse_admin` credential (403) on every route that takes an access credential; the private one serves every route, to a `clearinghouse_admin` only. The signer's two paths are on every listener and take a payment key on each: the rule about kinds does not reach them, and an access key of any kind is 401 there. The rule is applied where every route authenticates, so a route added later is covered. The `clearinghouse` role (P8) will require the private listener. Before P6 there was one listener | `http/listeners.py`, `http/auth.py`, `http/server.py`, `config.py`; [access.md](../../contracts/access.md#listeners) |
| The wallet | Since P6 (#167) `GET /v1/clearinghouse/wallet`, in the private group, for a `clearinghouse_admin`: the deposit and reserve as Batteries' on-chain listener has confirmed them (ETH, with the block it has read to), the total of unspent grants for each currency (each grant's total less what its allocations spent), and whether the signer answers its identity call. Nothing is converted and no margin is computed. The deposit and reserve need Batteries' on-chain listener, which is off by default; "ready" is the signer's identity call, not its `/readyz`. Before P6 nothing reported the wallet | `services/wallet.py`, `adapters/batteries_escrow.py`, `adapters/sdk.py`; [payments.md](../../contracts/payments.md#the-wallet-report) |
| Consumer apps | ChatLPT and the inference gateway embed the engine in `per_application`. The inference gateway refuses any other mode | their `deploy/.env.example`; inference-gateway `runtime.py:121` |

### Batteries at the pin (why the engine draws the line)

- A management credential lists permissions by type of resource, such as `grants.*`, and each one covers
  every grant, allocation and key (`creds.example.toml`). The database has no tenant column.
- Batteries does keep each tenant's money apart. Every allocation belongs to one grant, and the
  allocation, key, session, settlement and usage lists all filter by `grant_id`
  (`internal/app/management.go`).
- Batteries refuses an allocation larger than the grant's unallocated balance
  (`internal/store/manage.go`, "insufficient unallocated grant balance").
- A grant's name, sponsor and metadata are set at creation. Afterwards only its status and total change.

Batteries is a Go server. It cannot be vendored, and no upstream change is proposed.

## Decision

**The second configuration is a payment clearinghouse: an engine in front of Batteries and the signer
that does payments only and is the only thing published.** An enterprise app keeps its own engine, which
runs its jobs and sends them straight to orchestrators. That engine asks the clearinghouse to sign each
payment, to allot allocations, and for its tenant's usage. Batteries and the signer stay private.

This reverses one alternative the 13 November design rejected, a tenant layer shipped in builder-core.
It is still software for someone to run, not a hosted service.

### What each side holds

| | Self-hosted payments | Payment clearinghouse |
| --- | --- | --- |
| Runs Batteries, the signer and the wallet | The enterprise | The clearinghouse |
| Tenants | One | Many, one for each enterprise app |
| Where jobs run | The enterprise's engine | The enterprise's engine |
| The enterprise's engine holds | Batteries tokens and one Batteries key for each actor | The clearinghouse's address, a tenant admin key, and one payment key for each actor |
| Per-actor cap | Batteries, at each payment | Batteries, at each payment |
| What crosses to another party | Nothing | Payment calls only. Never a prompt or a result |
| Who tops up the grant | The enterprise | The clearinghouse admin, by hand in the first version |

### The engine's two halves

The engine stays one program. A role setting decides which half a deployment runs.

- **Work half:** network capabilities, runner choice, the runner call, streaming, job records and the
  cost of each job.
- **Clearinghouse half:** tenants, grants, allocations, payment keys, signing and usage.

| `LPB_ROLE` | Routes served | Payment ports point at | Who runs it |
| --- | --- | --- | --- |
| `both`, the default | Work and clearinghouse | Batteries and the signer | An enterprise with self-hosted payments |
| `work` | Work only | The payment clearinghouse | An enterprise that uses a clearinghouse |
| `clearinghouse` | Clearinghouse only | Batteries and the signer | A payment clearinghouse, which never runs jobs |

The work half reaches payments through five ports and nothing else. Each gets a second adapter that calls
the clearinghouse over HTTP.

| Port | Reaches today | With a payment clearinghouse |
| --- | --- | --- |
| Signing, inside `RunnerTransport` | The signer, with the actor's Batteries key | The clearinghouse, at the signer's two paths, with that actor's payment key |
| `CredentialResolver` | The engine's stored Batteries key | The payment key the clearinghouse issued at allotment |
| `AllocationManager` | Batteries' management API | The tenant routes, with the tenant admin key |
| `AllowanceReader` | Batteries | The actor's allocation at the clearinghouse |
| `UsageSource` | Batteries' usage feed, for every tenant | The usage route, for the caller's tenant only |

### Tenants and money

- **A tenant and its grant share one id.** Onboarding creates the Batteries grant, and that grant's id is
  the tenant's id. Nothing maps one to the other, and no route takes a separate grant id.
- **Each tenant has one grant, for life.** It is prepaid and topped up as needed. A tenant that must stop
  is paused. Its grant is never closed.
- **A tenant admin divides the grant and cannot enlarge it.** Only the clearinghouse admin onboards
  tenants and tops up grants.
- **`per_actor` is the only allocation mode.** An enterprise app that wants one shared budget runs every
  job as a single actor.
- **No key is tied to a grant.** Payment keys belong to allocations and access keys belong to the tenant,
  so a leaked key is rotated and the grant is untouched.
- **Each clearinghouse has one wallet.** Its deposit stays above the total of unspent grants, with a
  margin for the ETH price. The clearinghouse admin watches it and funds it by hand.

### One payment, and live work

1. The enterprise's engine sends the request to a runner, which answers 402 with its price and a payment
   id.
2. The engine asks the clearinghouse to sign, at the signer's own path (`POST /generate-live-payment`),
   presenting that actor's payment key.
3. The clearinghouse swaps in the actor's Batteries key and calls the signer. The signer asks Batteries
   to approve the payment against the allocation, signs, and records an accounting event.
4. The engine sends the signed payment to the orchestrator.

Fixed work pays once, up front. Live work pays in payment cycles: the SDK asks for the next payment every
3 seconds (`PAYMENT_INTERVAL_S`), and each cycle is approved against the allocation. When the allocation
is empty the next payment is refused and the stream stops. The clearinghouse sees the capability, the
orchestrator, the fee and the payment id. It never sees the job, its outcome or its content.

### Routes

Kinds: `clearinghouse_admin` (every tenant, private listener only), `tenant_admin` (one tenant),
`auditor` (one tenant, read-only), `actor` (itself), and a payment key (one actor's allocation).

**Clearinghouse half, public listener.** Another tenant's id in the path is 403, and a
`clearinghouse_admin` credential is 403 on this listener. The signer's two paths take a payment key
and nothing else: an access key of any kind is 401 on them.

| Route | Who may call | What it does |
| --- | --- | --- |
| `GET /v1/me` | Any credential | The caller's kind and tenant. From ISS-29 (D72) |
| `GET /v1/clearinghouse/tenants/{tenant_id}/grant` | That tenant's admin or auditor | The grant's total, unallocated balance and status |
| `GET /v1/clearinghouse/tenants/{tenant_id}/actors` | That tenant's admin or auditor | The tenant's actors and their allocations |
| `GET …/actors/{actor_id}/allocation` | That tenant's admin or auditor | One allocation: allotted, spent, available and status |
| `POST …/actors/{actor_id}/allocation` | That tenant's admin | Allots an allocation from the tenant's grant and returns the actor's payment key once |
| `POST …/allocation/top-up`, `…/status`, `…/revoke`, `…/rotate-payment-key` | That tenant's admin | Adds to, pauses, ends or re-keys one allocation |
| Key routes under `/v1/clearinghouse/tenants/{tenant_id}/keys` | That tenant's admin | Manages the tenant's auditor keys. A tenant admin cannot issue tenant admin keys |
| `GET /v1/clearinghouse/tenants/{tenant_id}/usage` | That tenant's admin or auditor | The tenant's payment records |
| `POST /generate-live-payment` | A payment key | Signs one payment against that actor's allocation. 402 when it is empty. At the root, where the gateway SDK calls a signer |
| `POST /sign-orchestrator-info` | A payment key | The signer's identity, which every payment begins with. No charge. At the root |
| `GET /v1/clearinghouse/discovery` | Any credential of a tenant | The clearinghouse's runner list |

**Clearinghouse half, private listener.** For a `clearinghouse_admin` only. Every public route is also
served here, for any tenant. That includes the signer's two paths, which take a payment key here as
they do on the public listener.

| Route | What it does |
| --- | --- |
| `POST /v1/clearinghouse/tenants` | Onboards a tenant: creates its grant and returns the tenant's id and its first tenant admin key, once |
| `GET /v1/clearinghouse/tenants` | Every tenant, with its grant's balance and payment usage |
| `POST /v1/clearinghouse/tenants/{tenant_id}/grant/top-up` | Adds to the tenant's grant |
| `POST /v1/clearinghouse/tenants/{tenant_id}/status` | Pauses or resumes a tenant by pausing its grant |
| `POST /v1/clearinghouse/tenants/{tenant_id}/keys` with kind `tenant_admin` | Issues another tenant admin key |
| `GET /v1/clearinghouse/wallet` | The deposit and reserve, the total of unspent grants, and the signer's readiness |

**Work half.** The job, offering, runner, cost and allowance routes keep their paths. Reconciliation and
cost sync move here from `/v1/admin`, as `GET /v1/costs/reconciliation` and `POST /v1/costs/sync`. The
access keys of an enterprise's own engine are under `/v1/keys`.

The names follow D83. The paths that began as proposals are locked (D87): payments, usage, discovery,
tenant status, both sets of key routes, reconciliation and cost sync. The payments path is the signer's
two paths at the root, as Mike amended D87 point 3 on 19 November 2026: the gateway SDK calls a signer
at those two paths of the signer address's origin and drops any path in the address, so a clearinghouse
that serves them is reached by the SDK unmodified.

### What builder-core can do without an upstream change

| Need | Without upstream | How |
| --- | --- | --- |
| Separate one tenant from another | Yes | The clearinghouse's engine holds every Batteries credential and checks the tenant itself |
| Per-actor allocations for many tenants | Yes | Each tenant's allocations sit under its own grant, which Batteries already keeps apart |
| A tenant's own usage and nothing more | Yes | The usage route filters by the tenant's grant |
| Pay without reaching the signer | Yes | The clearinghouse serves the signer's two paths and swaps the payment key for the Batteries key |

The 13 November design needed grant-scoped Batteries credentials to allow `per_actor` under a separate
operator. This design does not, because no credential leaves the clearinghouse.

### The arrangement (to document, not a service)

Two Compose projects on one host, as D74 already decided, so the boundary is real: separate networks and
separate credential files.

- **Clearinghouse project:** a proxy, the engine in its `clearinghouse` role with a public and a private
  listener, Batteries, the signer and the wallet keystore. It publishes only the proxy.
- **Enterprise project:** an enterprise app with its engine in its `work` role. It knows the
  clearinghouse's address and its tenant admin key, and nothing about Batteries or the signer.

Limits to write down: one host; the same people run both sides; top-up and wallet funding are manual
steps; one signer for the wallet; no availability promise.

The two Compose projects live in this repository's `deploy/` folder, in place of the planned
`deploy/separate-operator/` (D87). The separate `self-hosted-payments` template stays the installable
reference, which ISS-37 documents.

**`lpb payments check`** is kept from the 13 November design as a read-only command (D87). It prints the
role and probes what that role needs. A self-hosted engine probes the signer and Batteries. An engine
that uses a clearinghouse probes the clearinghouse: who it is, its grant and its usage. It never writes,
and ISS-36's runbook uses it as a precondition.

### Docs to change

`payments.md` (replace "Mode B" and the allocation modes with the two configurations, the tenant rules
and the routes); `access.md` (the kinds and the tenant check on writes); `build-track-baseline.md` (the
Mode B row); `milestones.md` (the ISS-31 row); `RELIABILITY.md` (the configuration table and the role
setting); the tech debt tracker (T5 closes, because `LPB_SHARED_ALLOCATION_ID` goes); console
`m3-environment.md` and `m3-journey.md` (the "second configuration" wording).

P1 (#162) changed `payments.md` (the allocation modes, and "Mode B" as a short "Payment configurations"
section; the tenant rules and the routes follow with their tickets) and the configuration table of
`RELIABILITY.md`, and closed T5.

P2 (#163) renamed what `payments.md`, `access.md`, the other contracts, the guides, `RELIABILITY.md`,
`SECURITY.md`, the glossary and the dev stack's files call things. The old and the new names are in
[the contracts index](../../contracts/index.md#upgrading-to-the-domain-language-names).

P3 (#164) moved three groups of routes in `access.md`, `payments.md`, `RELIABILITY.md`, the
architecture documents and the dev stack's README, and added the two halves to `ARCHITECTURE.md`. The
old and the new paths are in
[the contracts index](../../contracts/index.md#upgrading-to-the-work-routes-and-the-clearinghouse-routes).

P4 (#165) added the tenant rules and the tenant routes to `payments.md`, the kind `tenant_admin` and the
tenant check on writes to `access.md`, the tenant boundary to `SECURITY.md`, the grant calls to
`batteries-api.md`, and the grants credential's permissions to `RELIABILITY.md` and the dev stack. The
old and the new routes and fields are in
[the contracts index](../../contracts/index.md#upgrading-to-tenants-at-the-clearinghouse).

P5 (#166) added payment keys, a tenant's usage and signing through a clearinghouse to `payments.md`, the
runner list to `discovery.md`, the payment key and what it is a credential for to `access.md` and
`SECURITY.md`, the table and its migration to `persistence.md`, and amended D87 point 3. The old and the
new answers and fields are in
[the contracts index](../../contracts/index.md#upgrading-to-payment-keys-a-tenants-usage-and-the-runner-list).

P6 (#167) added the listeners to `access.md`, the wallet report to `payments.md`, the two settings and
how two listeners start and stop to `RELIABILITY.md`, the private listener to `SECURITY.md`, the escrow
report to `batteries-api.md`, the signer's identity call as a readiness probe to `node-signer.md`, and
`escrow.read` to the grants credential in the dev stack. What a deployment does is in
[the contracts index](../../contracts/index.md#upgrading-to-a-public-and-a-private-listener).

## Code areas

| File | Change |
| --- | --- |
| `config.py` | Remove the allocation mode and the shared-key settings; add `role`, `clearinghouse_url`, `clearinghouse_token`; rename the JWT application settings |
| `contracts/`, `store/`, a migration | `application_id` becomes `tenant_id`; `ProvisionedActor` becomes `ActorAllocation`; the kinds |
| `services/policy.py` | The `tenant_admin` kind; writes pass the target tenant to the policy |
| `services/payments.py`, `services/grants.py`, `services/keys.py` | Tenants, onboarding, grant top-up, allotment that returns a payment key |
| `http/app.py`, `http/server.py` | Route groups for the two halves; the public and private listeners |
| `adapters/` (new files) | Remote adapters for the five ports; the wire from a clearinghouse to its signer |
| `runtime/assembly.py` | Wire the role: which routes mount and which adapters are used |
| `cli.py` | `lpb payments check`, read-only, for each role |
| `deploy/` | The two Compose projects |

## Test plan (test-driven)

**Levels.** The policy and the tenant rules are service tests. The routes and the two listeners are HTTP
tests. The remote adapters run one suite shared with the local adapters. The role setting is an assembly
test. The arrangement is run once and recorded. It is ISS-36's input, not its evidence.

**Seams.** `EngineSupply` (inject fakes), `httpx.MockTransport` for the clearinghouse and for Batteries,
and a second engine instance in the `clearinghouse` role for the end-to-end test.

**Starter test:** a tenant admin of one tenant calls a route that names another tenant and gets 403, with
no Batteries call recorded.

```text
[x] per_actor is the only mode; the allocation mode and shared-key settings are gone (P1, #162)
[x] onboarding creates one grant and returns the tenant id and one tenant admin key, once (P4, #165)
[x] the tenant's id is the grant's id; no route takes a separate grant id (P4, #165)
[x] a tenant admin allots, tops up, pauses and revokes allocations in its own tenant (P4, #165)
[x] another tenant in the path is 403, before any lookup or Batteries call (P4, #165)
[x] a tenant admin cannot onboard a tenant, top up its grant or issue a tenant admin key (P4, #165)
[x] an allocation larger than the grant's unallocated balance is refused (P4, #165)
[x] allotment returns a payment key once; rotation replaces it and the old one stops (P5, #166)
[x] the signer's two paths sign for the payment key's own actor only; an empty allocation is 402 (P5, #166)
[x] a fixed job and a live job are paid through the clearinghouse's signer paths (P5, #166: the SDK's
    transport, a clearinghouse app and a loopback signer; the engine around the transport is P7's)
[x] the usage route returns one tenant's records (P5, #166)
[x] onboarding, top-up, tenant status and the wallet are 404 on the public listener (P6, #167)
[x] a clearinghouse_admin credential is 403 on the public listener (P6, #167)
[x] the wallet route reports the deposit, the reserve, unspent grants and signer readiness (P6, #167)
[ ] the shared port suite passes against the local and the remote adapters
[ ] role clearinghouse mounts no job routes; role work mounts no clearinghouse routes
[ ] role work runs a paid job through role clearinghouse with two tenants
[ ] role both still runs a paid job by itself
[ ] lpb payments check probes the signer and Batteries in role both, and the clearinghouse in role work
[ ] lpb payments check never sends a non-GET request; it exits 1 when a probe is refused
[ ] a clearinghouse that is unreachable or not ready fails a job as retry; an unknown outcome as check
```

**Definition of done.** Every item `[x]`, `[=]` or `[-]` with a reason; the checks in `AGENTS.md`
(`uv run pytest`, coverage gates, `ruff`, `mypy` strict, `scripts/check_docs.py`, `lpb gen-docs`,
`lpb smoke`); the arrangement run once with one paid job attributed; the docs above updated.

## Upstream dependencies

| Upstream | Needed? | Why / workaround |
| --- | --- | --- |
| livepeer/node | No | The signer authorizes each request by the key it is given. The clearinghouse swaps in that key |
| livepeer-python-gateway | No | The SDK calls a signer at two paths of the signer address's origin, with a header. A clearinghouse serves those two paths, so the SDK is pointed at it unmodified (tested in #166) |
| clearinghouse-batteries | No | No credential leaves the clearinghouse, so global permissions are enough |

## Consumer impact

| Consumer | Change |
| --- | --- |
| ChatLPT, inference gateway | Move to `per_actor`: allot an allocation for each user, or run as a single actor. Take the renamed API. ChatLPT is the proof, with its embedded engine in the `work` role (#116) |
| Console | Sign-in on every page and engine-only reads. A tenant admin view and a clearinghouse admin view, as stretch scope (builder-core-console #26) |
| The `self-hosted-payments` template | Becomes the reference deployment for both configurations (ISS-37) |

## Alternatives considered

- **Separately operated, `per_application` only** (D74, 13 November). Replaced: it had no per-actor cap,
  its read token could see every tenant's usage, and it needed Batteries and the signer published.
- **A hosted engine that also runs the jobs.** Rejected: the clearinghouse would see every prompt and
  result. An engine server already covers an enterprise app that cannot embed Python.
- **A table binding grants to tenants.** Rejected for one id: with one grant for each tenant there is
  nothing to map.
- **Two separate services.** Rejected: self-hosted payments needs both halves together.
- **A wallet for each tenant.** Rejected: it means a whole stack for each tenant.

## Questions locked

Locked by Mike on 18 November 2026. They replace the four answers of 13 November.

- **The second configuration** is a payment clearinghouse that does payments only (D82). `per_actor` is
  the only allocation mode. Grants are prepaid, one for each tenant, for life.
- **Tenants and names** (D83): one id for a tenant and its grant; `tenant` in the API with the grant
  nested under it; the kinds `clearinghouse_admin` and `tenant_admin`; the `/v1/clearinghouse` prefix.
  Renaming app IDs to capabilities is not part of this and stays with ISS-48.
- **The two halves** (D84): one program with `LPB_ROLE`; remote adapters for the five ports; a public and
  a private listener at the clearinghouse.
- **Access at the clearinghouse** (D85): keys only in the first version; a tenant admin may issue auditor
  keys; identity providers stay at the enterprise's own engine.
- **Operation and scope** (D86): the console runs twice at a clearinghouse and top-up is manual; the work
  is inside M4.

**The open points, settled by Mike on 18 November (D87).** They were first left for John.

- **The key that signs for an actor:** a payment key for each actor, issued at allotment. The engine
  already stores one key for each actor and presents it this way.
- **The runner list:** the clearinghouse's list is the default, and an enterprise may use its own
  discovery. Nothing is enforced at signing in the first version.
- **The route paths** are as written under Routes. The payments path was amended by Mike on 19 November
  2026: the signer's two paths at the root, in place of `POST /v1/clearinghouse/payments`.
- **The two Compose projects** live in this repository's `deploy/` folder.
- **`lpb payments check`** is kept as a read-only check for each role.
- **The Keycloak realm's `operator` role** is renamed and maps to `clearinghouse_admin`. A second role
  maps to `tenant_admin` (ISS-33).
- **A clearinghouse failure** is classified like a signer failure: unreachable or not ready is `retry`,
  an unknown outcome is `check`, and a refusal keeps its reason (ISS-28).

**Not in the first version.** Automated top-ups; federation through the clearinghouse's own sign-in
service and support for several issuers; wallet alerts and automatic funding; refusing payments to
orchestrators that are not on the clearinghouse's list; events from the clearinghouse; `lpb` commands for
the clearinghouse admin.

**Other decisions that touch this guide.**

- **`GET /v1/me`** is ISS-29's (D72). It returns the tenant, and both halves serve it.
- **The grant funding route** that ISS-29 renamed is replaced by the nested top-up route above.
- **The recovery value `operator`** becomes `admin` (ISS-28).
- **The events feed** (ISS-46) belongs to the work half.
- **The Batteries pin is `v0.0.3`** (#108, #117). This guide was verified at `fb76150`.

## Delivery slices

One builder-core issue each, all sub-issues of ISS-31.

1. **P1 (#162).** Make `per_actor` the only allocation mode. Built: the three settings and the code
   that read them are removed, with `SettingsSecretStore` and `allocation_mode` in `GET /readyz`.
2. **P2 (#163).** Rename routes, classes and settings to the domain language. Built
   ([#177](https://github.com/NE-Build-Track/livepeer-builder-core/pull/177)): tenant for
   application, `clearinghouse_admin` for the kind `admin`, allot for provision, top up for fund, the
   payment key, the grants token and the `/v1/clearinghouse` prefix, with migration 0014 for the records.
   A route whose final shape needs more than a rename kept its shape under the new prefix, for P3 and
   P4: the key routes, the actor list, the grant list and creation, the grant top-up, reconciliation
   and cost sync.
3. **P3 (#164).** Separate the work routes from the clearinghouse routes. Built
   ([#178](https://github.com/NE-Build-Track/livepeer-builder-core/pull/178)): the routes are two sets,
   each mounted by itself (`http/work.py`, `http/clearinghouse.py`), and one app still mounts both.
   Reconciliation, cost sync and an engine's own access keys moved to the work set, at
   `GET /v1/costs/reconciliation`, `POST /v1/costs/sync` and `/v1/keys`, with no alias at the old
   paths. The facade follows the halves: `WorkApi` and `ClearinghouseApi` replace `AdminApi`. A
   structure test (`tests/structure/test_two_halves.py`) holds the work half to the five payment
   ports. The routes that P4 nests under a tenant stay where P2 left them: the actor list, the grant
   list and creation, and the grant top-up.
4. **P4 (#165).** Tenants at the clearinghouse. Built
   ([#179](https://github.com/NE-Build-Track/livepeer-builder-core/pull/179)): the kind `tenant_admin`, confined by
   the access policy, to which every write now passes its tenant; onboarding
   (`POST /v1/clearinghouse/tenants`), the tenant list, and a tenant's grant, status, actors,
   allocations and access keys under `/v1/clearinghouse/tenants/{tenant_id}`. A tenant's id is its
   grant's id: `grant_id` left the allot and top-up requests, and the actor list, the grant list and
   creation and the grant top-up left their old paths with no alias. No migration, and no existing
   record is re-keyed: an allocation allotted before stays on the grant its record names. Payment usage
   on the tenant list, and the payment key that allotment returns, are P5's. The routes only a
   clearinghouse admin calls are one group, which P6 puts on the private listener.
5. **P5 (#166).** The routes an enterprise's engine calls: payments, usage and discovery. Built
   ([#180](https://github.com/NE-Build-Track/livepeer-builder-core/pull/180)): a payment key for each
   actor (`lpp_`), issued at allotment, replaced by rotation and stopped by revocation, with migration
   0015 for its hash; the signer's two paths at the root (`POST /sign-orchestrator-info`,
   `POST /generate-live-payment`), each called with a payment key and forwarded once to the signer with
   the actor's Batteries key; a tenant's usage (`GET /v1/clearinghouse/tenants/{tenant_id}/usage`), as
   the `UsageSource` port's own page, with `spent` on the tenant list; and the runner list
   (`GET /v1/clearinghouse/discovery`), livepeer/node's document as the clearinghouse read it. What an
   engine does with them, and reading `signer_not_reached` as a retry, is P7's.
6. **P6 (#167).** A public and a private listener. Built: `LPB_HTTP_PRIVATE_PORT` turns the private
   listener on, and without it one listener serves everything, as before, so self-hosted payments is
   unchanged. With it, `lpb serve` runs two servers over one engine in one process: the engine starts
   once and stops once, an address that cannot be bound stops both, and neither serves without the
   other. One table (`http/listeners.py`) says which group of routes each listener serves and which
   credentials it takes: the private group is 404 on the public listener, a `clearinghouse_admin`
   credential is 403 there on every route, and the private listener takes a `clearinghouse_admin`
   only. The wallet report (`GET /v1/clearinghouse/wallet`) is in the private group, through two new
   optional protocols (`EscrowSource`, `SignerProbe`). The signer's two paths of P5 are a group in
   that table, served on every listener to a payment key. Requiring the private listener for the
   `clearinghouse` role is P8's.
7. **P7 (#168).** Remote adapters for the five payment ports.
8. **P8 (#169).** The engine's role setting.
