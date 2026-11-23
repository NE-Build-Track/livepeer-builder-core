# ISS-31: Both payment-operation configurations

> **Rewritten on 18 November 2026.** Mike replaced the design locked on 13 November (D74). The second
> configuration is now a **payment clearinghouse**. The decisions are D82 to D87 in
> [m4-decisions.md](../m4-decisions.md), and the answers are under [Questions locked](#questions-locked).
> The 13 November design (`separately_operated`, `per_application` only) is not built.

**Status:** locked by Mike on 18 November 2026, with the open points settled the same day (D87). Mike
amended D85 point 1 and D87 points 1 and 3 on 19 and 23 November 2026, after review of #180 and #186: an
enterprise's engine signs at the signer itself. The work is tracked in
[the exec plan](../../exec-plans/active/iss-31-payment-clearinghouse.md).\
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
| Payment key | An allocation's own Batteries key (`lpg_`): what an engine presents to the signer to pay for one actor |
| Work half, clearinghouse half | The two halves of the engine, described below |

## Done when

- A payment clearinghouse serves at least two tenants, one for each enterprise app. Each enterprise app
  runs its jobs in its own engine, as an embedded engine or an engine server. That engine reaches the
  clearinghouse for allotment, the allowance, usage and the runner list, and signs at the clearinghouse's
  signer itself.
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
| Payment keys | Since P5 (#166) allotment and payment key rotation at a standalone clearinghouse hand the allocation's own Batteries key (`lpg_`) to the caller, once, and the clearinghouse keeps none (`allot_for_delivery`, `rotate_for_delivery`; the routes with `deliver_allocation_keys`). Each allocation records who holds its key (`credential_custody`: `secret_store` or `work_engine`). A repeat of a finished command answers without the key, and a resumed one hands over the key Batteries returns again. The combined app (`create_app`) stores the key and answers none, as every engine did before P5 | `services/payments.py`, `services/payment_keys.py`, `services/allocation_delivery.py`, `http/delivered.py`, migration 0015; [payments.md](../../contracts/payments.md#payment-keys) |
| A tenant's usage | Since P5 (#166) `GET /v1/clearinghouse/tenants/{tenant_id}/usage` returns the tenant's payment records as the `UsageSource` port's own page, filtered by Batteries' `grant_id` filter, and the tenant list and the grant read carry `spent`. Before P5 only cost sync read usage, for every tenant | `services/tenant_usage.py`, `adapters/batteries.py`; [payments.md](../../contracts/payments.md#a-tenants-usage) |
| The runner list | Since P5 (#166) `GET /v1/clearinghouse/discovery` serves the livepeer/node document the clearinghouse's own discovery source last read, byte for byte, to a tenant's admin or auditor. Since P7 (#168) the reader of an engine that uses a clearinghouse carries the tenant admin key there, and to no other address, and that list is its default discovery address. Before P5 no engine served a list | `services/runner_list.py`, `adapters/sdk.py`, `config.py`; [discovery.md](../../contracts/discovery.md#the-clearinghouses-runner-list) |
| Signing | An engine signs at its signer itself, inside `RunnerTransport`, with the actor's payment key. No engine serves a signer path or signs for another engine. Since P7 (#168) an enterprise's engine that uses a clearinghouse reaches the clearinghouse's signer the same way, with the key it was handed | `adapters/sdk.py`, `adapters/runner_call.py`; [payments.md](../../contracts/payments.md#payment-path) |
| Signer address | One setting, `LPB_SIGNER_URL`. An engine that uses a clearinghouse sets it to the clearinghouse's published signer | `config.py` |
| The role | Since P8 (#169). `LPB_ROLE` is `both` (the default), `work` or `clearinghouse`, and it is the one switch: it chooses the routes that are mounted, who keeps each allocation's key, what is behind the payment ports and what the engine starts. `work` needs the three clearinghouse settings and refuses a Batteries setting; `both` and `clearinghouse` refuse the clearinghouse settings; `clearinghouse` needs the private listener, Batteries and the grants credential. A contradiction is refused at startup by the names of the settings. An engine in its `clearinghouse` role mounts no work route, hands each allocation's key to the caller and stores none, resolves no interrupted job, runs no cost sync or retention purge, refuses to run a job for an embedding caller, and is given no transport that could pay: it asks the signer's identity for its wallet report and signs nothing. It still reads discovery, for its runner list. `/livez` and `/readyz` are served in every role, and `/readyz`, `lpb payments check` and `lpb smoke` name the role. `GET /v1/offerings` is a work route: a clearinghouse publishes its runner list, not a catalog. Before P8 an app mounted every group, whatever the settings | `config.py`, `http/listeners.py`, `runtime/assembly.py`, `runtime/engine.py`, `runtime/payment_defaults.py`; [RELIABILITY.md](../../RELIABILITY.md#roles) |
| The two Compose projects | Since P8 (#169). `deploy/clearinghouse` (the engine in its `clearinghouse` role, Batteries with its on-chain listener, the signer and discovery; the engine's public listener and the signer are published, Batteries is not, and the private listener is on the engine's loopback) and `deploy/enterprise` (the engine in its `work` role, with the clearinghouse's address and its signer's, and nothing of Batteries), with a proof runbook. The paid proofs on the direct-signer path are the runbook's, and are to be run | [deploy/README.md](../../../deploy/README.md) |
| An engine that uses a clearinghouse | Since P7 (#168); since P8 (#169) it is an engine in its `work` role, and the three settings below are refused without `LPB_ROLE=work`. `LPB_CLEARINGHOUSE_URL` (a scheme, a host and a port, nothing more), `LPB_CLEARINGHOUSE_TOKEN` and `LPB_CLEARINGHOUSE_TENANT_ID`, set together and never beside a Batteries setting, point three payment ports at the clearinghouse through remote adapters: `AllocationManager`, `AllowanceReader` and `UsageSource`. The other two stay the engine's own: it stores the key each allotment and rotation hands over in its own secret store, and signs with it at `LPB_SIGNER_URL`. The allowance is read from the allocation route, so no work route of the clearinghouse is called. One suite runs against the local and the remote adapters, a paid fixed job and a paid live job included. A failure of an admin call or a read at the clearinghouse is `ProviderUnavailable`, `ProviderOutcomeUnknown` or a `ProviderError` that keeps its status. The standalone clearinghouse it calls is an engine in its `clearinghouse` role (P8) | `adapters/clearinghouse.py`, `adapters/clearinghouse_wire.py`, `runtime/payment_defaults.py`, `config.py`; [payments.md](../../contracts/payments.md#the-remote-adapters) |
| Payment ports | Five: signing inside `RunnerTransport`, `CredentialResolver`, `AllocationManager`, `AllowanceReader`, `UsageSource`. A caller can swap each one: since P7 (#168) `EngineSupply` has `credentials` and `allowances` too. Since P3 (#164) a structure test fails when the work half reaches payments another way | `protocols/__init__.py`, `runtime/assembly.py` (`EngineSupply`), `tests/structure/test_two_halves.py` |
| Route sets | Since P3 (#164) the routes are a work set and a clearinghouse set, each mounted by itself. Since P4 the clearinghouse set is two groups that also mount by themselves: a tenant's own routes, and what only a clearinghouse admin does, for the private listener of P6. Since P6 (#167) one table says which listener serves which group (`http/listeners.py`); with one listener the app mounts every group. Since P8 (#169) the same table says which half each group belongs to, and an app mounts the groups of the engine's role: both halves, the work half alone, or the clearinghouse half alone. Reconciliation, cost sync and an engine's own access keys are work routes, at `/v1/costs/reconciliation`, `/v1/costs/sync` and `/v1/keys`. At `719378f` every route was in one function | `http/work.py`, `http/clearinghouse.py`, `http/app.py` (`shared_app`, `create_app`); `api.py` (`WorkApi`, `ClearinghouseApi`) |
| Listeners | Since P6 (#167) one by default, on `LPB_HTTP_HOST` and `LPB_HTTP_PORT`, with every route for every kind of credential: what self-hosted payments runs. `LPB_HTTP_PRIVATE_PORT` turns on a private listener (`LPB_HTTP_PRIVATE_HOST`, loopback unless set): two apps over one engine in one process. The public listener serves no route of the private group (404) and refuses a `clearinghouse_admin` credential (403) on every route; the private one serves every route, to a `clearinghouse_admin` only. The rule is applied where every route authenticates, so a route added later is covered. In the `both` role each listener's app stores an allocation's key and answers none (`deliver_allocation_keys=False`): the engine behind it runs jobs. Since P8 (#169) an engine in its `clearinghouse` role requires the private listener (its settings do not load without `LPB_HTTP_PRIVATE_PORT`, and no app of one listener is built for it), and both of its listeners hand each key to the caller and store none. Before P6 there was one listener | `http/listeners.py`, `http/auth.py`, `http/server.py`, `config.py`; [access.md](../../contracts/access.md#listeners) |
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

**The second configuration is a payment clearinghouse: an engine in front of Batteries that does
payments only, with a signer beside it that enterprise engines reach directly.** An enterprise app keeps
its own engine, which runs its jobs and sends them straight to orchestrators. That engine asks the
clearinghouse to allot allocations, and for its tenant's allowance and usage. Allotment hands it each
actor's payment key once, and it signs each payment at the clearinghouse's signer itself, with that key
(D85 point 1 and D87 point 1, as Mike amended them on 19 and 23 November 2026). The clearinghouse's
engine and its signer are published. Batteries stays private.

This reverses one alternative the 13 November design rejected, a tenant layer shipped in builder-core.
It is still software for someone to run, not a hosted service.

### What each side holds

| | Self-hosted payments | Payment clearinghouse |
| --- | --- | --- |
| Runs Batteries, the signer and the wallet | The enterprise | The clearinghouse |
| Tenants | One | Many, one for each enterprise app |
| Where jobs run | The enterprise's engine | The enterprise's engine |
| The enterprise's engine holds | Batteries tokens and one Batteries key for each actor | The clearinghouse's address, the signer's address, a tenant admin key, and one Batteries key for each actor, handed over once by allotment or rotation |
| The clearinghouse's engine holds | | Batteries tokens. No actor's Batteries key: it keeps a receipt for each one it handed over |
| Per-actor cap | Batteries, at each payment | Batteries, at each payment |
| What crosses to another party | Nothing | Admin calls to the clearinghouse and payment calls to its signer. Never a prompt or a result |
| Who tops up the grant | The enterprise | The clearinghouse admin, by hand in the first version |

### The engine's two halves

The engine stays one program. A role setting decides which half a deployment runs.

- **Work half:** network capabilities, runner choice, the runner call, streaming, job records and the
  cost of each job.
- **Clearinghouse half:** tenants, grants, allocations, payment keys, usage and the runner list. It
  mints each payment key and hands it over; it signs nothing.

| `LPB_ROLE` | Routes served | Payment ports point at | Who runs it |
| --- | --- | --- | --- |
| `both`, the default | Work and clearinghouse | Batteries and the signer | An enterprise with self-hosted payments |
| `work` | Work only | The payment clearinghouse, and its signer for signing | An enterprise that uses a clearinghouse |
| `clearinghouse` | Clearinghouse only | Batteries | A payment clearinghouse, which never runs jobs and signs nothing |

The work half reaches payments through five ports and nothing else. With a payment clearinghouse,
`AllocationManager`, `AllowanceReader` and `UsageSource` get a second adapter that calls the
clearinghouse over HTTP. Signing and `CredentialResolver` stay with the enterprise's engine: it stores
the keys it is handed and signs at the clearinghouse's signer.

| Port | Reaches today | With a payment clearinghouse |
| --- | --- | --- |
| Signing, inside `RunnerTransport` | The signer, with the actor's Batteries key | The clearinghouse's signer, directly, with the actor's Batteries key |
| `CredentialResolver` | The engine's stored Batteries key | The engine's stored Batteries key: the one the clearinghouse handed over at allotment or rotation |
| `AllocationManager` | Batteries' management API | The tenant routes, with the tenant admin key. Allotment and rotation answer the actor's key, once |
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
2. The engine asks the clearinghouse's signer to sign (`POST /generate-live-payment`), presenting that
   actor's payment key, which it stored when the clearinghouse handed it over.
3. The signer asks Batteries to approve the payment against the allocation, signs, and records an
   accounting event. The clearinghouse's engine is not in this path.
4. The engine sends the signed payment to the orchestrator.

Fixed work pays once, up front. Live work pays in payment cycles: the SDK asks for the next payment every
3 seconds (`PAYMENT_INTERVAL_S`), and each cycle is approved against the allocation. When the allocation
is empty the next payment is refused and the stream stops. The clearinghouse's signer and Batteries see
the capability, the orchestrator, the fee and the payment id. They never see the job, its outcome or its
content.

### Routes

Kinds: `clearinghouse_admin` (every tenant, private listener only), `tenant_admin` (one tenant),
`auditor` (one tenant, read-only) and `actor` (itself). A payment key is no kind: it is a credential at
the signer and on no engine route.

**Clearinghouse half, public listener.** Another tenant's id in the path is 403, and a
`clearinghouse_admin` credential is 403 on this listener.

| Route | Who may call | What it does |
| --- | --- | --- |
| `GET /v1/me` | Any credential | The caller's kind and tenant. From ISS-29 (D72) |
| `GET /v1/clearinghouse/tenants/{tenant_id}/grant` | That tenant's admin or auditor | The grant's total, unallocated balance and status |
| `GET /v1/clearinghouse/tenants/{tenant_id}/actors` | That tenant's admin or auditor | The tenant's actors and their allocations |
| `GET …/actors/{actor_id}/allocation` | That tenant's admin or auditor | One allocation: allotted, spent, available and status |
| `POST …/actors/{actor_id}/allocation` | That tenant's admin | Allots an allocation from the tenant's grant and hands over the actor's payment key, once |
| `POST …/allocation/top-up`, `…/status`, `…/revoke`, `…/rotate-payment-key` | That tenant's admin | Adds to, pauses, ends or re-keys one allocation. A rotation hands over the new payment key, once |
| Key routes under `/v1/clearinghouse/tenants/{tenant_id}/keys` | That tenant's admin | Manages the tenant's auditor keys. A tenant admin cannot issue tenant admin keys |
| `GET /v1/clearinghouse/tenants/{tenant_id}/usage` | That tenant's admin or auditor | The tenant's payment records |
| `GET /v1/clearinghouse/discovery` | Any credential of a tenant | The clearinghouse's runner list |

The signer is no route of the engine. It is published beside the engine, and an enterprise's engine calls
its two paths itself, each with a payment key: `POST /sign-orchestrator-info` (the signer's identity,
which every payment begins with; no charge) and `POST /generate-live-payment` (signs one payment against
that actor's allocation; 402 when it is empty).

**Clearinghouse half, private listener.** For a `clearinghouse_admin` only. Every public route is also
served here, for any tenant.

| Route | What it does |
| --- | --- |
| `POST /v1/clearinghouse/tenants` | Onboards a tenant: creates its grant and returns the tenant's id and its first tenant admin key, once |
| `GET /v1/clearinghouse/tenants` | Every tenant, with its grant's balance and payment usage |
| `POST /v1/clearinghouse/tenants/{tenant_id}/grant/top-up` | Adds to the tenant's grant |
| `POST /v1/clearinghouse/tenants/{tenant_id}/status` | Pauses or resumes a tenant by pausing its grant |
| `POST /v1/clearinghouse/tenants/{tenant_id}/keys` with kind `tenant_admin` | Issues another tenant admin key |
| `GET /v1/clearinghouse/wallet` | The deposit and reserve, the total of unspent grants, and the signer's readiness |

**Work half.** The job, offering, runner, cost and allowance routes keep their paths. Reconciliation and
cost sync move here from `/v1/admin`, and the access keys of an enterprise's own engine get routes of
their own. These are the admin routes of the work half. Each takes an admin credential of the engine that
serves it, and an actor's or a service's key is 403. The engine's first admin key is of kind
`clearinghouse_admin` in every role (`lpb bootstrap-admin`), so on an enterprise's own engine that key is
the clearinghouse admin of this table. On an engine with two listeners, a clearinghouse admin credential
is accepted on the private one only.

| Route | Who may call it | What it does |
| --- | --- | --- |
| `GET /v1/costs/reconciliation` | `admin.read` on the tenant named: a clearinghouse admin for any tenant, a tenant admin or an auditor for its own | Compares what the provider says one allocation spent with the engine's cost records. `tenant_id` is required; `actor_id` names the actor and `allocation_id` an earlier allocation of that actor |
| `POST /v1/costs/sync` | `admin.costs`: a clearinghouse admin only. A tenant admin and an auditor are 403, because a sync reads the usage of every tenant | Runs one cost sync now, without waiting for the background sync |
| `POST /v1/keys` | `admin.keys` on the tenant the body names: a clearinghouse admin for any tenant and kind, a tenant admin for its own tenant and for no admin kind | Issues an access key and returns its token once |
| `GET /v1/keys`, `GET /v1/keys/{key_id}` | `admin.read`: a clearinghouse admin for every tenant's keys, a tenant admin or an auditor for its own tenant's | Lists the access keys, or reads one. Never a token |
| `POST /v1/keys/{key_id}/rotate`, `POST /v1/keys/{key_id}/revoke` | `admin.keys` on the key's tenant and kind, as for issuing: a tenant admin changes no admin key, and another tenant's key reads as missing | Replaces a key at once, or revokes it |

The `/v1/keys` routes exist only while engine API keys are on.

The names follow D83. The paths that began as proposals are locked (D87): usage, discovery, tenant
status, both sets of key routes, reconciliation and cost sync. There is no payments path: Mike amended
D87 point 3 on 19 November 2026, and an enterprise's engine calls the signer itself.

### What builder-core can do without an upstream change

| Need | Without upstream | How |
| --- | --- | --- |
| Separate one tenant from another | Yes | The clearinghouse's engine holds every Batteries management credential and checks the tenant itself |
| Per-actor allocations for many tenants | Yes | Each tenant's allocations sit under its own grant, which Batteries already keeps apart |
| A tenant's own usage and nothing more | Yes | The usage route filters by the tenant's grant |
| Pay without a Batteries management credential | Yes | The enterprise's engine signs at the signer with each allocation's own key, which the clearinghouse hands over once. Batteries approves every payment against that allocation |

The 13 November design needed grant-scoped Batteries credentials to allow `per_actor` under a separate
operator. This design does not, because no management credential leaves the clearinghouse. What leaves
is an allocation's own key, which spends that allocation and nothing else.

### The arrangement (to document, not a service)

Two Compose projects on one host, as D74 already decided, so the boundary is real: separate networks and
separate credential files.

- **Clearinghouse project:** a proxy, the engine in its `clearinghouse` role with a public and a private
  listener, Batteries, the signer and the wallet keystore. It publishes the engine's public listener and
  the signer, and nothing of Batteries. The signer keeps its Batteries authorization webhook configured:
  it is the only check at signing.
- **Enterprise project:** an enterprise app with its engine in its `work` role. It knows the
  clearinghouse's address, the signer's address and its tenant admin key, and nothing about Batteries.

Limits to write down: one host; the same people run both sides; top-up and wallet funding are manual
steps; one signer for the wallet; no availability promise.

The two Compose projects live in this repository's `deploy/` folder, in place of the planned
`deploy/separate-operator/` (D87). The separate `self-hosted-payments` template stays the installable
reference, which ISS-37 documents.

Built in P8 (#169) as `deploy/clearinghouse` and `deploy/enterprise`
([deploy/README.md](../../../deploy/README.md)), with two differences from the list above. There is no
proxy: the clearinghouse project publishes the engine's public listener itself, on `127.0.0.1`, and
the enterprise project reaches it over one Docker network that carries nothing else (T43). And the
enterprise project is the engine server alone, with no enterprise app in front of it: an engine server
in its `work` role serves no route that allots, so the project allots with a script that calls the
embedded API (T41).

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

P5 (#166) added the payment key handover and a tenant's usage to `payments.md`, the runner list to
`discovery.md`, who holds an allocation's key to `access.md` and `SECURITY.md`, `credential_custody` and
its migration to `persistence.md`, and the amendments of D85 point 1 and D87 points 1 and 3 to the
decision record. The old and the new answers and fields are in
[the contracts index](../../contracts/index.md#upgrading-to-the-payment-key-handover-a-tenants-usage-and-the-runner-list).

P8 (#169) added the roles to `RELIABILITY.md` (the setting, what each role needs, refuses and starts),
`access.md` (the routes of each role), `payments.md` (the role of each configuration, and the role in
`lpb payments check`), `SECURITY.md` and `ARCHITECTURE.md`, and the three READMEs under `deploy/`. What
a deployment does is in
[the contracts index](../../contracts/index.md#upgrading-to-the-engines-role-setting).

P6 (#167) added the listeners to `access.md`, the wallet report to `payments.md`, the two settings and
how two listeners start and stop to `RELIABILITY.md`, the private listener to `SECURITY.md`, the escrow
report to `batteries-api.md`, the signer's identity call as a readiness probe to `node-signer.md`, and
`escrow.read` to the grants credential in the dev stack. What a deployment does is in
[the contracts index](../../contracts/index.md#upgrading-to-a-public-and-a-private-listener).

P7 (#168) added the remote adapters, the key an enterprise's engine is handed, what a failure at the
clearinghouse means and `lpb payments check` to `payments.md`, the clearinghouse settings and the check to
`RELIABILITY.md`, the tenant admin key and the handed keys to `SECURITY.md`, the reader's credential to
`discovery.md`, the allocation read's amounts to `access.md`, and the signer's identity path asked with a
GET to `node-signer.md`. What a deployment does is in
[the contracts index](../../contracts/index.md#upgrading-to-an-engine-that-uses-a-payment-clearinghouse).

## Code areas

| File | Change |
| --- | --- |
| `config.py` | Remove the allocation mode and the shared-key settings; add `role`, `clearinghouse_url` (an origin: a scheme, a host and a port), `clearinghouse_token`, `clearinghouse_tenant_id`; rename the JWT application settings |
| `contracts/`, `store/`, a migration | `application_id` becomes `tenant_id`; `ProvisionedActor` becomes `ActorAllocation`; the kinds |
| `services/policy.py` | The `tenant_admin` kind; writes pass the target tenant to the policy |
| `services/payments.py`, `services/grants.py`, `services/keys.py` | Tenants, onboarding, grant top-up, allotment and rotation that hand the payment key over at a clearinghouse |
| `http/app.py`, `http/server.py` | Route groups for the two halves; the public and private listeners |
| `adapters/` (new files) | Remote adapters for the ports that reach the clearinghouse (`AllocationManager`, `AllowanceReader`, `UsageSource`); keeping the payment keys the clearinghouse hands over |
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
[x] at a standalone clearinghouse allotment and rotation hand the allocation's key over once, and the
    clearinghouse stores none (P5, #166)
[x] a repeat of a finished command hands over no key; a resumed one hands over the key Batteries
    returns again; a command whose actor was revoked, rotated or allotted again hands over none (P5, #166)
[x] the combined app stores the key and answers none; every allocation records who holds its key (P5, #166)
[x] a clearinghouse serves no signer path and no payments route (P5, #166)
[x] the usage route returns one tenant's records (P5, #166)
[x] an enterprise's engine stores the key it is handed and signs with it at the clearinghouse's signer (P7, #168)
[x] an answer without a key is never a success at an enterprise's engine and saves no actor; a lost key
    is replaced by a rotation, or by a revoke and a new allotment when the engine has no record (P7, #168)
[x] onboarding, top-up, tenant status and the wallet are 404 on the public listener (P6, #167)
[x] a clearinghouse_admin credential is 403 on the public listener (P6, #167)
[x] the wallet route reports the deposit, the reserve, unspent grants and signer readiness (P6, #167)
[x] the shared port suite passes against the local and the remote adapters, with a paid fixed job and a
    paid live job on each (P7, #168)
[x] role clearinghouse mounts no job routes; role work mounts no clearinghouse routes (P8, #169)
[x] role work runs a paid job through role clearinghouse with two tenants (P8, #169: a fixed job and a
    live job in one process, with a loopback signer and runner; the funded run is the proof runbook's,
    and is to be run on the direct-signer path)
[x] role both still runs a paid job by itself (P8, #169: the same)
[x] lpb payments check probes the signer and Batteries, or the clearinghouse and its signer, by the
    settings (P7, #168)
[x] lpb payments check never sends a non-GET request; it exits 1 when a probe is refused (P7, #168)
[=] a clearinghouse that is unreachable or not ready fails a job as retry; an unknown outcome as check
    (P7, #168: no job reaches a clearinghouse, since an engine signs at the signer itself. The rule holds
    for the admin calls and the reads there: `ProviderUnavailable` and `ProviderOutcomeUnknown`, which
    ISS-28 reads as retry and check)
```

**Definition of done.** Every item `[x]`, `[=]` or `[-]` with a reason; the checks in `AGENTS.md`
(`uv run pytest`, coverage gates, `ruff`, `mypy` strict, `scripts/check_docs.py`, `lpb gen-docs`,
`lpb smoke`); the arrangement run once with one paid job attributed; the docs above updated.

## The remote adapters: each port and its route

Built in P7 (#168). The contract is [payments.md](../../contracts/payments.md#the-remote-adapters); this
section is why the mapping is what it is. The ports and the clearinghouse's routes are not at the same
level, so the mapping is written down member by member, with every place it is not clean.

### The two levels

- **`AllocationManager` is written in the payment provider's terms.** It names an allocation by its id
  and a key by its id, and a rotation is two calls: `create_key`, then `revoke_key`.
- **The clearinghouse's routes are written in a tenant's terms.** Each names a tenant and an actor in its
  path, and none takes an allocation id. They are the commands of `PaymentsService`, one route each.

So the members that name an allocation or a key cannot find their route by themselves: the actor is not
in their arguments. The smallest change that closes the gap is one optional protocol,
`AllocationsByActor`, with one member, `for_actor(tenant_id, actor_id, operation_ref)`, which returns the
manager for that actor. `PaymentsService` knows the actor of every command and asks a manager that has
the protocol. The Batteries adapter does not have it and is called as before. No member of
`AllocationManager` changes, so a caller who supplies a manager of their own changes nothing. No hashed
text changes: the idempotency keys and fingerprints pinned in `tests/unit/test_hashed_forms.py` are
computed above the port.

### `AllocationManager`

`{t}` is the tenant, `{a}` the actor, and every path begins `/v1/clearinghouse/tenants/{t}`. Each
request carries the tenant admin key as a bearer.

| Member | Route | What the answer becomes |
| --- | --- | --- |
| `create_key_for_grant(grant_id, name, amount, idempotency_key)` | `POST /actors/{a}/allocation` with `amount`, `label` and `operation_ref` | `NewAllocationKey`: the allocation's id, its key's id, and `allocation_api_key` as the secret |
| `create_key(allocation_id, name, idempotency_key)` | `POST /actors/{a}/allocation/rotate-payment-key` with `operation_ref` | `NewAllocationKey`, with the new key as the secret |
| `revoke_key(api_key_id)` | `GET /actors/{a}/allocation`, and for the key the allocation holds now, `POST /actors/{a}/allocation/rotate-payment-key` | Nothing |
| `top_up_allocation(allocation_id, amount, idempotency_key)` | `POST /actors/{a}/allocation/top-up` with `amount` and `operation_ref` | Nothing |
| `set_allocation_status(allocation_id, status)` | `POST /actors/{a}/allocation/status` with `status` | Nothing |
| `revoke_allocation(allocation_id)` | `POST /actors/{a}/allocation/revoke` | Nothing |
| `read_allocation(allocation_id)` | `GET /actors/{a}/allocation`, whose answer carries the allowance | `Allowance` |
| `read_grant(grant_id)` | `GET /grant` | `TenantGrant` |
| `top_up_grant`, `set_grant_status`, `create_grant`, `list_grants`, `grant_page`, `grant_balances` | None. Only a clearinghouse admin does these, on the private listener, where a tenant admin key is refused | Refused in the engine with `AccessDenied` (403), and nothing is sent |

- **The reference.** The clearinghouse keys its own ledger on the `operation_ref` it is sent, for the
  tenant admin key that sent it. A top-up and a rotation carry the engine's idempotency key, which names
  the engine, the allocation and the admin's reference. An allotment's key is derived from the actor
  alone, so the admin's reference is folded into it: a clearinghouse answers a reference it finished
  without the key, for good, and an allotment whose answer was lost must be able to be made again under
  a new reference once the lost allocation is revoked.
- **The grant is the tenant.** `grant_id` is the tenant's id (D83), and it is the `{t}` of the path.
- **The label.** `name` is the actor's label, or `{t}/{a}` when it has none. The adapter sends an empty
  label in the second case, so the clearinghouse makes the same default itself.
- **The allocation is checked before a write.** `top_up_allocation`, `set_allocation_status`,
  `revoke_allocation` and `create_key` name an allocation, and the route acts on whichever allocation the
  actor holds at the clearinghouse. The adapter reads the actor's allocation first
  (`GET /actors/{a}/allocation`). For a top-up, a status change and a rotation it refuses with the
  provider's 404 when it is another one, as Batteries answers an allocation id it does not hold. A
  revoke of an allocation that has ended there, revoked or replaced by another, sends nothing: the
  other allocation is not that command's to revoke.

### `AllowanceReader`, `UsageSource`, `CredentialResolver` and signing

| Port and member | Reaches | Note |
| --- | --- | --- |
| `AllowanceReader.allocation_for(tenant_id, actor_id)` | The engine's own actor record | As with self-hosted payments: the allocation a job's attempt records is the one whose key the engine holds |
| `AllowanceReader.read_allowance(allocation_id)` | `GET /actors/{a}/allocation`, with the actor found in the engine's own records | See "Not clean" |
| `UsageSource.page(cursor, limit)` | `GET /usage?cursor=&limit=` | The route's answer is the port's own page. A refused cursor (400, `cursor_mismatch`) is `CursorMismatch`, so cost sync starts again from the first page |
| `CredentialResolver.credential(actor)` | The engine's own actor record and secret store | No adapter: `PaymentsService`, as with self-hosted payments. The key is the one the clearinghouse handed over |
| Signing, inside `RunnerTransport` | The clearinghouse's signer, at `LPB_SIGNER_URL` | No adapter: the gateway SDK's transport, as with self-hosted payments. The clearinghouse's engine is not in the path |

### The key the engine is handed

The clearinghouse hands an actor's key over once, in the answer to the allotment and to a rotation. The
adapter returns it as the secret of `NewAllocationKey`, so the engine's ordinary command keeps it exactly
where a self-hosted engine keeps its own: in the `SecretStore`, with the `SecretRef` on the actor's
record and `credential_custody: secret_store`.

- **An answer without a key is never a success.** A clearinghouse that finished a command answers its
  repeat with `replayed: true` and no key. The engine stores nothing and saves no actor record. The one
  exception is its own resume after it stored the key of exactly that answer: it finishes from its own
  copy.
- **A lost rotation** is replaced by a rotation under a new reference, from the engine.
- **A lost allotment** leaves the engine with no record of the actor, so it cannot rotate it. The
  tenant's admin revokes the allocation at the clearinghouse and allots again at the engine.
- **A resumed command at the clearinghouse** hands the key over again (`resumed: true`), and the engine
  stores it.
- **Rows a deployment already has.** An actor allotted while the engine ran self-hosted payments holds a
  Batteries key and an allocation the clearinghouse does not know. Its commands are refused before
  anything is sent, and nothing is rewritten or deleted (T30). Its jobs present the stored key to the
  clearinghouse's signer, which refuses it. Such actors are revoked before the move and allotted again.

### Who the enterprise's engine is at the clearinghouse

A tenant's id is its grant's id (D83), and the engine already passes the tenant of an allotment to the
provider as the grant. So the tenant of every actor the enterprise's engine allots must be the id the
clearinghouse gave the tenant at onboarding: in `LPB_JWT_TENANT_ID` or the tenant claim, in the
`tenant_id` of the access keys it issues, and in the `ActorContext` an embedding app builds. Nothing is
mapped. An actor in any other tenant is refused by the clearinghouse (403, "another tenant").

The usage read and `lpb payments check` name no actor, so they need the tenant's id from a setting,
`LPB_CLEARINGHOUSE_TENANT_ID`, until `GET /v1/me` (ISS-29) can say it.

### Not clean

| Where | What | What was built |
| --- | --- | --- |
| The members of `AllocationManager` that name an allocation or a key | No actor in their arguments | The optional `AllocationsByActor` protocol |
| `create_key_for_grant` | The engine's idempotency key for an allotment names no reference, and the clearinghouse's ledger is keyed by reference | `for_actor` carries the admin's `operation_ref`, and the adapter folds it into the reference it sends |
| An allotment whose answer was lost | The engine has no record of the actor, so it cannot rotate or revoke it | The refusal says so. The tenant's admin revokes the allocation at the clearinghouse, and the engine allots again under a new reference |
| `revoke_key` | A rotation at the clearinghouse is one command, and the port's is two. No route revokes a key by its id | The adapter reads the allocation. A key it no longer holds has ended. The key it holds now, which only an overtaken rotation asks about, is replaced by one more rotation whose key nobody keeps |
| Two rotations of one actor | The engine keeps the key of the rotation that wrote first in its own store, and the clearinghouse the key of the one that ran last there | When the two differ, the engine refuses the later rotation and the actor holds an ended key until the next rotation, which the refusal asks for |
| `read_allocation`, `read_allowance` | The allocation route answered the record alone, with no amounts | Decided by the owner on 19 November: the route answers the amounts as `allowance`, and the adapters read them there. No work route of the clearinghouse is called (T36, closed) |
| `read_allowance(allocation_id)` for an earlier allocation | The clearinghouse reads an actor's current allocation only. Reconciling an earlier allocation of an actor (#115, C9) reads the provider's `spent` by allocation id | Refused with the provider's 404 (T40). A route that reads an allocation by its id, if the read is wanted |
| The tenant's id | `UsageSource.page` and the check command name no tenant, and the engine cannot ask who it is | `LPB_CLEARINGHOUSE_TENANT_ID` (T38). `GET /v1/me` (ISS-29) can replace it |
| The grant members | Only a clearinghouse admin calls them | Refused in the engine, plainly |

## Upstream dependencies

| Upstream | Needed? | Why / workaround |
| --- | --- | --- |
| livepeer/node | No | The signer authorizes each request by the key it is given. An enterprise's engine presents the allocation's own key, as a self-hosted engine does |
| livepeer-python-gateway | No | The SDK calls the clearinghouse's signer as it calls any signer, with the allocation's key as the bearer |
| clearinghouse-batteries | No | No management credential leaves the clearinghouse, so global permissions are enough. An allocation's key spends that allocation only |

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
- **A signer proxy at the clearinghouse, with a second credential for each actor.** Built first for P5
  and withdrawn by Mike on 19 November 2026, after review of #180: an enterprise's engine signs at the
  signer itself, with the allocation's own key.

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
  keys; identity providers stay at the enterprise's own engine. Batteries is reachable only by the
  clearinghouse's engine. The signer is published: an enterprise's engine signs at it directly, with the
  allocation's own Batteries key, so the signer is reachable by enterprise engines and keeps its
  Batteries authorization webhook configured, which is the only check at signing (amended by Mike on 19
  November 2026, after review of #180).
- **Operation and scope** (D86): the console runs twice at a clearinghouse and top-up is manual; the work
  is inside M4.

**The open points, settled by Mike on 18 November (D87).** They were first left for John.

- **The key that signs for an actor:** the allocation's own Batteries key, which allotment and rotation
  at the clearinghouse hand to the enterprise's engine once. The clearinghouse keeps no allocation key
  and issues no second credential. A repeat of a finished command answers without the key, and a lost key
  is replaced by a rotation; a resumed command hands over the key Batteries returns again (amended by
  Mike on 19 and 23 November 2026, after review of #180 and #186).
- **The runner list:** the clearinghouse's list is the default, and an enterprise may use its own
  discovery. Nothing is enforced at signing in the first version.
- **The route paths** are as written under Routes. The clearinghouse has no payments route: an
  enterprise's engine calls the signer itself (amended by Mike on 19 November 2026).
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
- **The unreleased migration** that created `lpb_payment_keys` is deleted, not dropped by a later one
  (Mike, 23 November 2026). A database that ran this stack before is recreated.

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
   on the tenant list, and the payment key that allotment hands over, are P5's. The routes only a
   clearinghouse admin calls are one group, which P6 puts on the private listener.
5. **P5 (#166).** What an enterprise's engine needs from the clearinghouse: payment keys, usage and
   discovery. Built ([#180](https://github.com/NE-Build-Track/livepeer-builder-core/pull/180), with
   [#186](https://github.com/NE-Build-Track/livepeer-builder-core/pull/186)): at a standalone
   clearinghouse allotment and rotation hand the allocation's own Batteries key to the caller once and
   store none (`allot_for_delivery`, `rotate_for_delivery`, the routes with `deliver_allocation_keys`),
   with migration 0015 for who holds each key (`credential_custody`); a tenant's usage
   (`GET /v1/clearinghouse/tenants/{tenant_id}/usage`), as the `UsageSource` port's own page, with
   `spent` on the tenant list; and the runner list (`GET /v1/clearinghouse/discovery`), livepeer/node's
   document as the clearinghouse read it. The combined app still stores each key and answers none.
   Storing a handed key and signing with it at the clearinghouse's signer is P7's.
6. **P6 (#167).** A public and a private listener. Built: `LPB_HTTP_PRIVATE_PORT` turns the private
   listener on, and without it one listener serves everything, as before, so self-hosted payments is
   unchanged. With it, `lpb serve` runs two servers over one engine in one process: the engine starts
   once and stops once, an address that cannot be bound stops both, and neither serves without the
   other. One table (`http/listeners.py`) says which group of routes each listener serves and which
   credentials it takes: the private group is 404 on the public listener, a `clearinghouse_admin`
   credential is 403 there on every route, and the private listener takes a `clearinghouse_admin`
   only. The wallet report (`GET /v1/clearinghouse/wallet`) is in the private group, through two new
   optional protocols (`EscrowSource`, `SignerProbe`). Every listener's app keeps each allocation's
   key with the engine (`deliver_allocation_keys=False`): the engine behind it runs jobs. Handing keys
   over, and requiring the private listener, are the `clearinghouse` role's (P8).
7. **P7 (#168).** Remote adapters for the payment ports that reach the clearinghouse; the enterprise's
   engine keeps the keys it is handed and signs at the clearinghouse's signer. Built
   ([#182](https://github.com/NE-Build-Track/livepeer-builder-core/pull/182)): the settings
   `LPB_CLEARINGHOUSE_URL` (a scheme, a host and a port, and nothing more), `LPB_CLEARINGHOUSE_TOKEN` and
   `LPB_CLEARINGHOUSE_TENANT_ID` select remote adapters for `AllocationManager`, `AllowanceReader` and
   `UsageSource`, with the optional `AllocationsByActor` protocol, which tells an allocation manager the
   actor of a command, and `credentials` and `allowances` on `EngineSupply`. `CredentialResolver` and
   signing have no adapter: the engine stores the key each allotment and rotation hands over in its own
   secret store (`credential_custody: secret_store`) and signs with it at `LPB_SIGNER_URL`, the
   clearinghouse's published signer. An answer without a key is never a success and saves no actor. The
   allocation route answers the allocation's amounts, so the allowance is read there and no work route
   of the clearinghouse is called. One suite runs against both sets of adapters, a paid fixed job and a
   paid live job included, with the clearinghouse a second engine that hands keys over and stores none.
   A failure of an admin call or a read at the clearinghouse is one of three errors, with
   `ProviderOutcomeUnknown` new. The discovery reader carries the tenant admin key to the
   clearinghouse's runner list and no other address. `lpb payments check` sends GET requests only, and
   for an engine that uses a clearinghouse probes the clearinghouse and its signer. What is left is
   P8's: the role that serves a standalone clearinghouse, which routes each role serves, and the two
   Compose projects with a paid run through them.
8. **P8 (#169).** The engine's role setting. Built
   ([#183](https://github.com/NE-Build-Track/livepeer-builder-core/pull/183)): `LPB_ROLE` is `both` (the
   default, and what a deployment that sets nothing new is), `work` or `clearinghouse`. It chooses the
   routes an app mounts (the listener table of P6, which now names each group's half), who keeps each
   allocation's key, what is behind the payment ports (P7's settings no longer choose by themselves:
   the three clearinghouse settings need `LPB_ROLE=work`), and what the engine starts. An engine in its
   `work` role mounts no clearinghouse route and is nobody's clearinghouse. An engine in its
   `clearinghouse` role mounts no job route and no other work route, hands each allocation's key to the
   caller and stores none, starts no work-half machinery, refuses to run a job, signs nothing, and
   needs the private listener, Batteries and the grants credential. A setting that contradicts the role
   is refused at startup. `lpb payments check` and `lpb smoke` follow the role. The two Compose projects
   are `deploy/clearinghouse`, which publishes the engine's public listener and the signer, and
   `deploy/enterprise`, with a proof runbook in `deploy/README.md`. The paid proofs of 19 November 2026
   ran on the withdrawn signer proxy
   ([the evidence](../../references/evidence/2026-11-19-payment-clearinghouse-proofs.md) says which
   still stand); the proofs through a clearinghouse are to be run again on the direct-signer path. Left
   open: an engine server in its `work` role has no route that allots (T41), a clearinghouse has no
   route that issues a second clearinghouse admin key (T42), and the projects have no TLS proxy (T43).
