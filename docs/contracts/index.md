# Contracts

What each public interface promises. **Implemented** means code and tests exist. **Defined** means
the contract is fixed but the implementation is scheduled later; calling it answers 501.

| Contract | Ticket | Status |
| --- | --- | --- |
| [engine.md](engine.md): discovery, jobs, attempts, results, usage, failures, idempotency | ISS-02 | Immediate and streamed jobs implemented (streaming is optional: `jobs.streaming`); async and persistent defined (M4) |
| [access.md](access.md): actor context, authenticators, policy, credentials | ISS-03 | API key, JWT bearer (D45) and policy implemented; trusted backend defined (M4) |
| [payments.md](payments.md): allocations, allotment, units, cost classification | ISS-04 | Implemented for self-hosted payments; the payment clearinghouse (D82) is M4 work (ISS-31) |
| [events.md](events.md): versioned event feed | ISS-02 | Defined; held in M3 (D60) |
| [persistence.md](persistence.md): store, migrations, retention, backup | ISS-06 | Implemented |
| [discovery.md](discovery.md): livepeer/node's `/discovery` contract | ISS-09, #45, D63 | `HttpDiscoverySource` reads `LPB_DISCOVERY_URL`. This repository ships no discovery process; a clearinghouse's engine serves the document it read (#166) |

The REST surface is generated into [../generated/openapi.json](../generated/openapi.json).

## Upgrading to the M4 hardening (#112 to #115)

- **An empty `operation_ref` is refused** (#113, C19). A job or session request that sends
  `operation_ref: ""` is a 422 that names the field (a `ValidationError` in process), before anything
  is written; it used to count as a reference. A client that sends an empty string for "no retry key"
  leaves the field out instead ([engine.md](engine.md#idempotency)).

## Upgrading to the domain-language names

On 18 November 2026 the names in every contract followed the domain language (D83, #163). Only names
changed: no behaviour, no status code and no permission, except where an old name would otherwise be
ignored without a word (#163 review): an engine with JWT must name where a token's tenant comes from,
a key request and a reconciliation must name their tenant, and every request body refuses a field it
does not know.
No old name is kept as an alias: an old route is 404, an old field is refused as unknown, and an old
environment variable is ignored.

| What | Before | Now |
| --- | --- | --- |
| The tenant, in every body, record, parameter and `ActorContext` | `application_id` | `tenant_id` |
| The tenant in a JWT | `LPB_JWT_APPLICATION_CLAIM`, `LPB_JWT_APPLICATION_ID` | `LPB_JWT_TENANT_CLAIM`, `LPB_JWT_TENANT_ID` |
| The kind of an admin credential | `admin` | `clearinghouse_admin` |
| The prefix of the admin routes | `/v1/admin` | `/v1/clearinghouse` |
| An actor's allocation | `/v1/admin/applications/{application_id}/actors/{actor_id}/allocation` | `/v1/clearinghouse/tenants/{tenant_id}/actors/{actor_id}/allocation` |
| Its commands | `.../fund`, `.../rotate-key` | `.../top-up`, `.../rotate-payment-key` |
| The grant top-up | `POST /v1/admin/grant/fund` | `POST /v1/clearinghouse/grant/top-up` |
| Allot | `ProvisionRequest`, `ProvisionedActor`, `payments.provision`, `AlreadyProvisioned` (409 `already_provisioned`) | `AllotRequest`, `ActorAllocation`, `payments.allot`, `AlreadyAllotted` (409 `already_allotted`) |
| Whether an actor has an allocation, on the actor list | `provisioned`, as a field and as a query parameter | `allotted` |
| Top up | `FundRequest`, `payments.fund`, `payments.fund_grant` | `TopUpRequest`, `payments.top_up`, `payments.top_up_grant` |
| The payment key | `PaymentCredential`, `payments.rotate_key` | `PaymentKey`, `payments.rotate_payment_key` |
| The Batteries credential for the grant desk | `LPB_BATTERIES_OPERATOR_TOKEN` | `LPB_BATTERIES_GRANTS_TOKEN` |
| The `AllocationManager` port | `fund_allocation`, `fund_grant` | `top_up_allocation`, `top_up_grant` |
| The optional store write | `ActorCommandRepo.provision_actor` | `allot_actor` |
| The command a ledger record names, as a 409 shows it | `provision`, `fund`, `fund_grant` | `allot`, `top_up`, `top_up_grant` |

**Before you start the new release:**

- **Rename the environment variables.** An old one names no setting and is ignored, like any variable
  the engine does not know. With `jwt` in `LPB_AUTH`, an engine still set with
  `LPB_JWT_APPLICATION_CLAIM` or `LPB_JWT_APPLICATION_ID` refuses to start: exactly one of
  `LPB_JWT_TENANT_CLAIM` and `LPB_JWT_TENANT_ID` must be set, and neither has a default (#163 review),
  so tokens are never put in a tenant nobody chose. With `LPB_BATTERIES_OPERATOR_TOKEN` ignored, the
  grant desk is off.
- **Migrate.** `lpb migrate` applies revision 0014, or the engine does at start with
  `LPB_AUTO_MIGRATE` ([persistence.md](persistence.md#migrations)). Every row is kept.
- **Move the clients.** A client that sends `application_id`, the kind `admin` or an old path is
  refused. So is a key request without `tenant_id` and a reconciliation without `?tenant_id=`: neither
  is read as `default` any more, and any field a request body does not know is a 422 (#163 review).
  The builder console and the sample apps move with their next pin.

**What a repeat gets.** An admin command recorded before the upgrade is still answered from its record
when it is repeated under the new names: the ledger compares the same fingerprint, and the provider
receives the same idempotency key ([repeat-safe commands](payments.md#repeat-safe-commands)).

**What keeps its name.** The failure kind `not_provisioned` (ISS-28 owns the failure table), the
permission strings `admin.*`, `LPB_JWT_ADMIN_SCOPE` and `LPB_JWT_ADMIN_READ_SCOPE`, `lpb bootstrap-admin`,
the `lpk_` prefix, an app ID (`app`), and the words Batteries uses on its own API (`/fund`,
`grants.fund`).

## Upgrading to the work routes and the clearinghouse routes

On 18 November 2026 the routes became two sets, the work routes and the clearinghouse routes (D84, D87,
#164). Three groups of work routes left the `/v1/clearinghouse` prefix, where the rename above had put
them, and the facade for operator work was split the same way. Only paths and names changed: who may
call each route, its body, its status codes and its repeat-safety are as they were. No old path or name
is kept as an alias: an old path is 404.

| What | Before | Now |
| --- | --- | --- |
| Reconciliation | `GET /v1/clearinghouse/reconciliation` | `GET /v1/costs/reconciliation` |
| Cost sync | `POST /v1/clearinghouse/costs/sync` | `POST /v1/costs/sync` |
| An engine's own access keys: issue and list | `POST`, `GET /v1/clearinghouse/keys` | `POST`, `GET /v1/keys` |
| One key, and its commands | `GET /v1/clearinghouse/keys/{key_id}`, `POST .../rotate`, `POST .../revoke` | `GET /v1/keys/{key_id}`, `POST /v1/keys/{key_id}/rotate`, `POST /v1/keys/{key_id}/revoke` |
| The operation ids of those seven routes in the OpenAPI document | `issue_key_v1_clearinghouse_keys_post`, `reconcile_v1_clearinghouse_reconciliation_get`, and so on | `issue_key_v1_keys_post`, `reconcile_v1_costs_reconciliation_get`, and so on: each follows its path |
| The facade for operator work, in embedded mode | `AdminApi` | `WorkApi` for the access keys, reconciliation and cost sync; `ClearinghouseApi` for allotment, top-ups and the grant desk. `EngineApi` is both |

**Move the clients.** A client that calls an old path gets 404, and a client generated from the
OpenAPI document is generated again, because the operation ids changed. Code typed against `AdminApi`
names the half it uses. The engine's members are unchanged: `engine.keys`, `engine.costs.reconcile`,
`engine.costs.sync_now` and `engine.payments` are called as before. The builder console and the sample
apps move with their next pin.

**What a repeat gets.** A key command recorded before the upgrade is still answered from its record
when it is repeated at the new path. The ledger compares the command and its request, and neither
names a path ([repeat-safe commands](payments.md#repeat-safe-commands)).

**What stays under `/v1/clearinghouse`.** The allocation routes, the actor list, the grant list and
creation, and the grant top-up. They are the clearinghouse set. The last four take their final shape,
nested under a tenant, with #165.

## Upgrading to tenants at the clearinghouse

On 18 November 2026 a tenant became an account at the clearinghouse, with an admin of its own (D83,
#165). A tenant's id is its grant's id, so no route or body takes a separate grant id, and the routes
that took a tenant in a query string or a grant in a body are nested under the tenant. No old path or
field is kept as an alias: an old path is 404, and an old field is a 422.

| What | Before | Now |
| --- | --- | --- |
| The actor list | `GET /v1/clearinghouse/actors?tenant_id=` | `GET /v1/clearinghouse/tenants/{tenant_id}/actors` |
| The grant list | `GET /v1/clearinghouse/grants` (`GrantRecord[]`) | `GET /v1/clearinghouse/tenants` (`TenantGrant[]`: each tenant with its grant's `total` and `unallocated`) |
| Creating a grant | `POST /v1/clearinghouse/grants` `{name, amount, operation_ref}`, `201` `GrantRecord` | `POST /v1/clearinghouse/tenants` `{name, amount, operation_ref, admin_actor_id?}`, `201` `OnboardedTenant`: the tenant's id (the grant's) and its first tenant admin key. `amount` may be zero |
| A grant's top-up | `POST /v1/clearinghouse/grant/top-up` with `grant_id` in the body | `POST /v1/clearinghouse/tenants/{tenant_id}/grant/top-up` |
| A grant's balance and status | not served | `GET /v1/clearinghouse/tenants/{tenant_id}/grant`; `POST /v1/clearinghouse/tenants/{tenant_id}/status` |
| A tenant's access keys | `/v1/keys`, with `tenant_id` in the body | also `/v1/clearinghouse/tenants/{tenant_id}/keys`, for the kinds `auditor` and `tenant_admin`. `/v1/keys` is unchanged |
| `grant_id` on allotment and top-up | A field of `AllotRequest`, `TopUpRequest` and their REST bodies; empty used the grant of the caller's own allocation | Gone. The grant is the tenant's. A body that names one is a 422, and so is the field on either contract |
| The kinds | `actor`, `clearinghouse_admin`, `auditor`, `service` | and `tenant_admin`, an admin of one tenant |
| The operations | `admin.keys`, `admin.allocations`, `admin.costs`, `admin.read` | and `admin.tenants`: onboarding, the tenant list, a grant's top-up, a tenant's status. A grant's top-up and the grant list needed `admin.allocations` before |
| `ResourceRef`, as an `AccessPolicy` receives it | `kind`, `tenant_id`, `owner_actor_id`, `id`; allot, top-up, status, revoke and key commands passed none | Those commands pass the tenant they act on. New: the kind `tenant`, and `credential_kind` for a key command |
| The grant desk, in embedded mode | `engine.payments.grants` (`GrantsApi`: `list_grants`, `create_grant`) | `engine.payments.tenants` (`TenantsApi`: `onboard`, `list_tenants`, `grant`, `set_status`) |
| A grant's top-up, in embedded mode | `payments.top_up_grant(admin, TopUpRequest(..., grant_id=))` | `payments.top_up_grant(admin, tenant_id, TopUpRequest(...))` |
| Key commands, in embedded mode | `keys.get(admin, key_id)`, `keys.rotate(...)`, `keys.revoke(admin, key_id)` | Each also takes an optional `tenant_id`, the tenant a route named |
| The `AllocationManager` port | | Three more methods, which the tenant desk calls on the grants manager: `read_grant`, `grant_balances`, `set_grant_status` |
| The command a ledger record names | `create_grant` | `onboard_tenant`, and `set_tenant_status`. Records of `create_grant` are kept |
| `FakeAllocationManager` (`livepeer_builder.testing`) | Took any grant id, and `calls` listed some calls | Holds grants: an unknown grant is its 404, and an allocation or top-up larger than the unallocated balance its 409. `add_grant(id)` gives it a grant under a chosen id, and `create_grant` the next `grant_<n>` no grant holds (#165 review). `calls` lists every call. `make_engine` starts with a grant for the tenant `default` |
| The `engine.started` log line | `grant_desk` | `tenant_desk` |

**Before you start the new release:**

- **No migration.** The schema is unchanged, and no row is rewritten or deleted. The kind `tenant_admin`
  fits the column revision 0014 widened.
- **Finish every `started` allotment and grant creation.** After the upgrade neither can be resumed
  (below). Repeat each under its reference until it answers, on the old release.
- **Give the grants credential what the tenant desk needs:** `grants.read`, `grants.create`,
  `grants.fund` (to onboard a tenant with an amount), `grants.status` and `allocations.read`
  (`LPB_BATTERIES_GRANTS_TOKEN`). It had `grants.read` and `grants.create`.
- **Move the clients.** The builder console and the sample apps move with their next pin.

**An engine that has tenants already.** Before this change a tenant's id was a free name, often
`default`, and its actors recorded their grant beside it. What such a deployment can and cannot do is
under [records from before a tenant's id was its grant's id](payments.md#records-from-before-a-tenants-id-was-its-grants-id):
every existing allocation works as before, on its own grant, and nothing new is allotted to a tenant
whose id is not a grant's id. Whether those records are re-keyed to the grant's id is not decided, and
this release does not do it.

**What a repeat gets.** No repeat of a command recorded before the upgrade reaches the provider under a
key it has not seen:

| Recorded before the upgrade | Repeated after it |
| --- | --- |
| An allocation's top-up, a payment key rotation, a key issue or rotation | As before: replayed, or resumed under the same provider key. Their requests did not change |
| A grant's top-up | Replayed, or resumed under the same provider key, when it is sent to `.../tenants/{that grant's id}/grant/top-up`: the fingerprint and the key hash the same text |
| An allotment that finished, whose request named its grant | Replayed, when the actor's record still names that grant. The request no longer names one, so the engine takes it from the record |
| An allotment that finished, whose request named no grant (the caller's own allocation chose it) | 409 `operation_conflict`. Nothing is sent. The allocation exists: read it |
| An allotment still `started` | 409 `operation_conflict`, however old and however often repeated. It is never resumed. For an actor allotted for the first time no actor is saved yet, so nothing says which grant it named. For an actor revoked and then allotted again, the actor's record names the grant, and the repeat is 409 all the same: this release allots only from the grant whose id is the tenant's, so it cannot finish that run. If its first call reached Batteries, that allocation stays there, funded, with no record in the engine, and has to be revoked at Batteries by hand. That is why each one is finished before the upgrade |
| A grant creation, finished or `started` | The route is gone. Onboarding under the same reference is another command: 409 `operation_conflict`, before any provider call |

**What keeps its name.** `ActorAllocation.grant_id` (the grant an allocation was allotted from: the
tenant's id for every allocation allotted from now on), `LPB_BATTERIES_GRANTS_TOKEN`,
`EngineSupply.grants_manager`, the permission strings, and the words Batteries uses on its own API.

## Upgrading to the payment key handover, a tenant's usage and the runner list

With #166 the clearinghouse gained what an enterprise's engine needs from it (D85 and D87, as Mike
amended them on 19 and 23 November 2026): each actor's payment key, handed over once by allotment and
rotation at a standalone clearinghouse, the tenant's usage and the runner list. Every allocation answer
gained a field, one contract gained a required field, and two routes are new. No path, body or status
code of an existing route changed.

| What | Before | Now |
| --- | --- | --- |
| `ActorAllocation` (every allocation answer and read, and each `KnownActor.allocation`) | | and `credential_custody`: `secret_store` (the engine's `SecretStore` holds the key) or `work_engine` (the key was handed to the enterprise's engine). It defaults to `secret_store` |
| The answer to allotting an allocation and to rotating a payment key, on the app `lpb serve` runs (`create_app`) | `201` and `200` `ActorAllocation` | The same, with `credential_custody: secret_store` and `Cache-Control: no-store`. The engine stores the key and returns none, as before |
| The same two answers, where the clearinghouse routes are mounted for a standalone clearinghouse | | The allocation, with `credential_custody: work_engine`, and `allocation_api_key` (the allocation's `lpg_` key, once), `replayed` and `resumed` ([payments.md](payments.md#payment-keys)) |
| Mounting the clearinghouse routes | `mount_clearinghouse_routes(app, engine)`, `mount_tenant_routes(app, engine)` | Each requires the keyword `deliver_allocation_keys`, which has no default: a call without it is a `TypeError`. `True` is a standalone clearinghouse, which hands each key to the caller and stores none. `False` is an engine that also runs jobs, which stores the key: `create_app` passes it, and so does an app that mounts the set itself on such an engine. With `True` there, that actor's jobs fail `not_provisioned` |
| Allot and rotate, in embedded mode | `payments.allot`, `payments.rotate_payment_key` return `ActorAllocation` and store the key | Unchanged. Beside them, `payments.allot_for_delivery` and `payments.rotate_for_delivery` return `DeliveredAllocation` and store no key. A command left unfinished is resumed only by the kind that began it: the other kind under that `operation_ref` is 409 `operation_conflict` ([payments.md](payments.md#payment-keys)) |
| A command that resumed an unfinished run, in embedded mode | `OPERATION_REPLAYED` (`livepeer_builder.contracts`) marks a replay | and `OPERATION_RESUMED`, `True` right after a command that resumed a run an earlier attempt left unfinished |
| `TenantGrant` (the tenant list, the grant read) | `tenant_id`, `name`, `status`, `total`, `unallocated` | and `spent`, required: what the tenant's allocations have spent |
| A tenant's payment records | not served | `GET /v1/clearinghouse/tenants/{tenant_id}/usage` (`UsagePage`); `engine.payments.usage` |
| The clearinghouse's runner list | not served | `GET /v1/clearinghouse/discovery`, livepeer/node's document; `engine.runner_list` |
| A usage cursor the provider refuses, over REST | `CursorMismatch` had no status of its own | 400, `code: cursor_mismatch` |
| The facade | `PaymentsAdminApi`, `ClearinghouseApi` | `PaymentsAdminApi.allot_for_delivery` and `rotate_for_delivery`, `PaymentsAdminApi.usage` (`TenantUsageApi \| None`), `ClearinghouseApi.runner_list` (`RunnerListApi \| None`). A facade of your own that stands in for `PaymentsAdminApi` adds them |
| The `AllocationManager` port | `read_grant` and `grant_balances` return a `TenantGrant` | The same, and each states `spent` |
| `ActorCommandRepo.set_actor_key` (a supplied store's conditional key write) | `(tenant_id, actor_id, allocation_id, api_key_id, secret_ref, updated_at, only_from)` | and the keyword `credential_custody`, which the engine always passes and the store writes with the key ([persistence.md](persistence.md#store)) |
| Optional protocols | | `TenantUsageSource` (a usage source), `DiscoveryDocumentSource` (a discovery source). A supplied part without one runs without what it serves |
| `FakeUsageSource` (`livepeer_builder.testing`) | `page` | and `tenant_page`, with `grant_of`, a map from an allocation to the grant it was allotted from |
| The published schema | | `ActorAllocation` has `credential_custody`. The schema is the combined app's (`create_app`), so it does not show the answer that hands a key over |
| Root exports | | `DeliveredAllocation`, `UsagePage`, `UsageRow` and `SetAsideUsage`. From `livepeer_builder.contracts` also `CredentialCustody` and `OPERATION_RESUMED` |

**Before you start the new release:**

- **Migrate.** `lpb migrate` applies revision 0015, or the engine does at start with `LPB_AUTO_MIGRATE`
  ([persistence.md](persistence.md#migrations)). It adds one column, `lpb_actors.credential_custody`,
  and every existing actor becomes `secret_store`.
- **Nothing to finish first.** No command hashes or sends anything it did not before, so a command
  recorded before the upgrade is replayed or resumed as it was. A record that notes no custody is read
  as `secret_store`: every secret then was the configured store's.
- **Existing allocations need nothing.** With self-hosted payments the engine keeps holding each key in
  its `SecretStore` and paying with it.
- **A supplied store** takes `credential_custody` on `set_actor_key`, when it has the conditional
  writes, and keeps `ActorAllocation.credential_custody` with the actor it saves and returns.
- **A supplied `AllocationManager`** states `spent` on the `TenantGrant` it returns, or building one
  fails.
- **Move the clients.** A client that reads the allocation from an answer keeps working. One that
  refuses unknown fields is generated again from the OpenAPI document.
- **A database that ran this work before it was released** is recreated. An earlier revision 0015
  created a table `lpb_payment_keys`; it was never released, and it is deleted, not undone by a later
  revision (Mike, 23 November 2026). The revision check will not tell you: that revision had the same
  number, so such a database records 0015, the engine starts on it and `lpb migrate` changes nothing.
  A database that has `lpb_payment_keys` is recreated. One at revision 0014 or earlier never ran that
  work and is migrated as usual ([persistence.md](persistence.md#migrations)).

**What a repeat gets.** The repeat of an allotment or a rotation recorded before the upgrade is answered
from its record, as before, with `credential_custody: secret_store`. No repeat of a finished command
hands a key over, on any app.

**What keeps its name.** `PaymentKey`, what the work half's `CredentialResolver` returns: the
allocation's Batteries key, which the runner call presents to the signer in both configurations.
`pipeline` on a usage row, Batteries' word for the capability. The route and the command
`rotate-payment-key`.

## Upgrading to a public and a private listener

On 19 November 2026 the engine gained a private listener and the wallet report (D84, #167). A deployment
that sets nothing new serves one listener, as before: no route, request body, status code or stored
record of an existing route changed, and no credential is refused that was accepted. One answer gained a
field, `TenantGrant`. There is no migration.

| What | Before | Now |
| --- | --- | --- |
| Listeners | One, on `LPB_HTTP_HOST` and `LPB_HTTP_PORT` | The same, unless `LPB_HTTP_PRIVATE_PORT` is set. Then two: that address is the public listener, and `LPB_HTTP_PRIVATE_HOST` (`127.0.0.1`) with `LPB_HTTP_PRIVATE_PORT` is the private one ([access.md](access.md#listeners)) |
| Onboarding, the tenant list, a grant's top-up and a tenant's status, with two listeners | | 404 on the public listener; served on the private one |
| A clearinghouse admin credential, with two listeners | | 403 on every route of the public listener; accepted on the private one |
| A tenant admin, auditor, actor or service credential, with two listeners | | Accepted on the public listener as before; 403 on every route of the private one |
| Allotment and payment key rotation, with two listeners | | As with one, on both: the engine stores the key and returns none ([access.md](access.md#listeners)) |
| `TenantGrant` (the tenant list, the grant read) | `tenant_id`, `name`, `status`, `total`, `unallocated`, `spent` | and `allocated_unspent`, required: what the tenant's allocations still hold, each one's amount less what it spent and nothing for one that spent more ([payments.md](payments.md#tenants)). The wallet report sums it with `unallocated` |
| The `AllocationManager` port | `read_grant` and `grant_balances` return a `TenantGrant` | The same, and each states `allocated_unspent` |
| The wallet | not served | `GET /v1/clearinghouse/wallet` (`WalletReport`), a clearinghouse admin's; `engine.payments.wallet` ([payments.md](payments.md#the-wallet-report)) |
| `create_app` | `create_app(engine, *, manage_lifecycle, max_request_bytes)` | and `listener`: `"only"` (the default), `"public"` or `"private"` |
| `serve`, `lpb serve` | One server | Two servers over one engine when `LPB_HTTP_PRIVATE_PORT` is set. `extend` runs once for each app |
| `build_app` | One app | The same. With `LPB_HTTP_PRIVATE_PORT` set it raises `ValueError`: it builds one listener's app |
| The published schema | | The routes of the private group carry the tag `private listener` |
| The facade | `PaymentsAdminApi` | `PaymentsAdminApi.wallet` (`WalletApi \| None`) |
| Optional protocols | | `EscrowSource` (a grants manager), `SignerProbe` (a runner transport). A supplied part without one runs without the wallet report |
| `FakeAllocationManager`, `ScriptedTransport` (`livepeer_builder.testing`) | | `escrow` and `escrow_accounts`; `signer` and `signer_readiness`. The fake's grants state `allocated_unspent` from its allocations: set `allocations[id].spent` above the allotted amount for an overdrawn one |
| Root exports | | `WalletReport`, `EscrowAccount`, `UnspentGrants`, `SignerReadiness` |
| The grants credential (`LPB_BATTERIES_GRANTS_TOKEN`) | `grants.read`, `grants.create`, `grants.fund`, `grants.status`, `allocations.read` | and `escrow.read`, for the wallet report |

**Before you start the new release:**

- **Nothing, to keep one listener.** Leave `LPB_HTTP_PRIVATE_PORT` unset.
- **For the wallet report, give the grants credential `escrow.read`.** Without it the route answers 502
  and names the permission; every other route is unaffected. For the deposit and reserve, run Batteries
  with its on-chain listener; without it the report's `escrow` is empty.
- **To serve two listeners,** set `LPB_HTTP_PRIVATE_PORT`, publish the public listener only, and move
  every use of a clearinghouse admin key to the private address: it is refused on the public one. Health
  checks may use either.
- **A caller's own implementation of `PaymentsAdminApi`** states `wallet` (`None` serves no report).
- **A supplied `AllocationManager`** states `allocated_unspent` on the `TenantGrant` it returns, or
  building one fails: the sum of what each allocation of the grant holds less what it spent, counting an
  allocation that spent more as zero. A test that builds a `TenantGrant` states it too.
- **Move the clients.** A client that refuses unknown fields is generated again from the OpenAPI
  document, for the new field of the tenant list and the grant read.
- **An entrypoint that calls `build_app`** keeps `LPB_HTTP_PRIVATE_PORT` unset, or builds each app with
  `create_app(engine, listener=...)` over an engine it starts itself.

**What a later release changes.** The `clearinghouse` role of ISS-31 (#169) will require the private
listener, and it is that role which hands each allocation's key to the caller. Until then two listeners
are a deployment's choice.

## Upgrading to an engine that uses a payment clearinghouse

The engine gained the remote adapters of the payment ports and `lpb payments check` (D84, D87, #168). A
deployment that sets nothing new runs self-hosted payments as before, and there is no migration. One
route's answer gained a field (the allocation read, below). An engine that uses a clearinghouse keeps
each actor's key, handed over once at allotment and rotation, in its own secret store, and signs with it
at the clearinghouse's signer itself ([payments.md](payments.md#the-remote-adapters)).

| What | Before | Now |
| --- | --- | --- |
| Settings | | `LPB_CLEARINGHOUSE_URL` (a scheme, a host and an optional port, and nothing after them), `LPB_CLEARINGHOUSE_TOKEN` and `LPB_CLEARINGHOUSE_TENANT_ID`, set together or not at all, and `LPB_CLEARINGHOUSE_TIMEOUT_S` (20; `LPB_OPERATION_STALE_S` must be at least six times it). Refused at startup beside `LPB_BATTERIES_URL`, `LPB_BATTERIES_TOKEN` or `LPB_BATTERIES_GRANTS_TOKEN` ([payments.md](payments.md#the-remote-adapters)) |
| `LPB_SIGNER_URL` | The engine's own signer | The same setting. An engine that uses a clearinghouse sets it to the clearinghouse's published signer. Unset, the engine does not pay, as before |
| `LPB_DISCOVERY_URL` | Required | Required unless `LPB_CLEARINGHOUSE_URL` is set: it then defaults to the clearinghouse's runner list, and the reader carries the tenant admin key there |
| `EngineSettings.from_env(defaults=...)` | A default fills a field the environment leaves unset | The same, except that with a clearinghouse set the discovery address is its runner list before any default |
| `EngineSupply`, `EngineParts` | | `credentials` (`CredentialResolver`) and `allowances` (`AllowanceReader`). Unset, or `None`: the engine's own records, as before |
| Optional protocols | | `AllocationsByActor` (an allocation manager): `for_actor(tenant_id, actor_id, operation_ref=None)`. `OneKeyPerAllocation` (an allocation manager whose provider keeps one key for an allocation and hands it over once): `holds_key(allocation_id, api_key_id)`. A supplied manager without them is called as before |
| Errors | | `ProviderOutcomeUnknown`, a `ProviderUnavailable` (503): a command left and no answer says whether it applied |
| Adapters | | `ClearinghouseAllocations`, `ClearinghouseAllowances`, `ClearinghouseUsage` (`livepeer_builder.adapters.clearinghouse`). `CredentialResolver` and signing have none: they are the engine's own |
| `HttpDiscoverySource` | `HttpDiscoverySource(discovery_url, *, client, timeout_s)` | and `bearer`, a credential for the discovery address |
| CLI | | `lpb payments check`: exit 0, 1 or 2 ([payments.md](payments.md#lpb-payments-check)) |
| `livepeer_builder.runtime` | | `check_payments(settings)` |
| Root exports | | `PaymentCheck`, `PaymentProbe`, `ProviderOutcomeUnknown`, `AllocationRead` |
| `GET /v1/clearinghouse/tenants/{tenant_id}/actors/{actor_id}/allocation` | The allocation's record (`ActorAllocation`) | `AllocationRead`: every field of the record where it was, and `allowance`, what `GET /v1/allowance` answers for that actor, or `null` when the provider could not be read ([payments.md](payments.md#allowance)) |
| The facade | `PaymentsAdminApi` | `PaymentsAdminApi.get_allocation`, which a caller's own implementation of the facade must state. `get_actor` still returns the record alone |
| An allotment or a rotation at an engine that uses a clearinghouse | | Answers the allocation, with `credential_custody: secret_store`: the engine's own store holds the key it was handed. An answer of the clearinghouse that carries no key is a refusal at the engine, never a success ([payments.md](payments.md#the-key-an-enterprises-engine-is-handed)) |

**Before you start the new release:**

- **Nothing, for self-hosted payments.** Leave the clearinghouse settings unset.
- **To use a clearinghouse,** set the three settings from what the clearinghouse admin handed over at
  onboarding (the address of the public listener, the tenant admin key and the tenant's id), set
  `LPB_SIGNER_URL` to the clearinghouse's published signer, keep `LPB_SECRET_KEY`, and unset every
  Batteries setting. The clearinghouse's address is a scheme, a host and an optional port: a path is
  refused. Run `lpb payments check`.
- **The clearinghouse must hand keys over.** Its tenant routes are mounted with
  `deliver_allocation_keys=True` ([payments.md](payments.md#payment-keys)). The app `lpb serve` runs
  keeps each key with its own engine and hands none over until the role setting (#169): an enterprise's
  engine pointed at it allots there and cannot use the answer, which carries no key.
- **A caller's own implementation of `PaymentsAdminApi`** states `get_allocation`.
- **A reader of the allocation route** that refuses a field it does not know meets `allowance`.
- **Put every actor in the tenant.** `LPB_JWT_TENANT_ID` or the tenant claim, the `tenant_id` of the
  access keys the engine issues, and the `ActorContext` an embedding app builds all carry the tenant's
  id at the clearinghouse. An actor in another tenant is refused there.
- **Revoke each actor before the move, and allot it again after.** An actor allotted with self-hosted
  payments holds a key of the deployment's own Batteries and an allocation the clearinghouse does not
  know. Nothing is rewritten (T30): its commands are refused, and its jobs present the old key to the
  clearinghouse's signer, which refuses it.
- **A caller's own allocation manager** changes nothing.
