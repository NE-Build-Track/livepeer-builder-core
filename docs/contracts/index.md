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

## Upgrading to the domain-language names

On 18 November 2026 the names in every contract followed the domain language (D83, #163). Only names
changed: no behaviour, no status code and no permission. No old name is kept as an alias: an old route
is 404, an old field is refused as unknown, and an old environment variable is ignored.

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
  the engine does not know, and the engine still starts. With `LPB_JWT_APPLICATION_CLAIM` ignored, every
  token is read as the tenant `LPB_JWT_TENANT_ID` (`default`), so tenants that the claim kept apart are
  one tenant. With `LPB_JWT_APPLICATION_ID` ignored, tokens are read as `default` and no longer reach
  the records of the tenant they had. With `LPB_BATTERIES_OPERATOR_TOKEN` ignored, the grant desk is
  off.
- **Migrate.** `lpb migrate` applies revision 0014, or the engine does at start with
  `LPB_AUTO_MIGRATE` ([persistence.md](persistence.md#migrations)). Every row is kept.
- **Move the clients.** A client that sends `application_id`, the kind `admin` or an old path is
  refused. The builder console and the sample apps move with their next pin.

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
| `FakeAllocationManager` (`livepeer_builder.testing`) | Took any grant id, and `calls` listed some calls | Holds grants: an unknown grant is its 404, and an allocation or top-up larger than the unallocated balance its 409. `add_grant(id)` gives it a grant under a chosen id. `calls` lists every call. `make_engine` starts with a grant for the tenant `default` |
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
| An allotment still `started` | 409 `operation_conflict`, however old. It is never resumed: no actor is saved yet, so nothing says which grant it named. If its first call reached Batteries, that allocation stays there, funded, with no record in the engine, and has to be revoked at Batteries by hand. That is why each one is finished before the upgrade |
| A grant creation, finished or `started` | The route is gone. Onboarding under the same reference is another command: 409 `operation_conflict`, before any provider call |

**What keeps its name.** `ActorAllocation.grant_id` (the grant an allocation was allotted from: the
tenant's id for every allocation allotted from now on), `LPB_BATTERIES_GRANTS_TOKEN`,
`EngineSupply.grants_manager`, the permission strings, and the words Batteries uses on its own API.

## Upgrading to payment keys, a tenant's usage and the runner list

On 19 November 2026 the clearinghouse gained what an enterprise's engine needs from it besides a signed
payment (D87, #166): a payment key for each actor, the tenant's usage and the runner list. The payments
route itself follows. Two answers gained a field, one contract gained a required field, and two routes are
new. No path, body or status code of an existing route changed.

| What | Before | Now |
| --- | --- | --- |
| The answer to allotting an allocation | `201` `ActorAllocation` | `201` `KeyedAllocation`: every field of `ActorAllocation` where it was, and `payment_key`, `{token, replayed}`. The token (`lpp_...`) is shown once |
| The answer to rotating a payment key | `200` `ActorAllocation` | `200` `KeyedAllocation`. The rotation still rotates the Batteries key, and now replaces the payment key too: the old one stops at once |
| Allot and rotate, in embedded mode | `payments.allot`, `payments.rotate_payment_key` return `ActorAllocation` | They return `KeyedAllocation`, which is an `ActorAllocation`. It no longer compares equal to the stored record: compare `.allocation` |
| `TenantGrant` (the tenant list, the grant read) | `tenant_id`, `name`, `status`, `total`, `unallocated` | and `spent`, required: what the tenant's allocations have spent |
| A tenant's payment records | not served | `GET /v1/clearinghouse/tenants/{tenant_id}/usage` (`UsagePage`); `engine.payments.usage` |
| The clearinghouse's runner list | not served | `GET /v1/clearinghouse/discovery`, livepeer/node's document; `engine.runner_list` |
| A usage cursor the provider refuses, over REST | `CursorMismatch` had no status of its own | 400, `code: cursor_mismatch` |
| The facade | `PaymentsAdminApi`, `ClearinghouseApi` | `PaymentsAdminApi.usage` (`TenantUsageApi \| None`), `ClearinghouseApi.runner_list` (`RunnerListApi \| None`) |
| The `AllocationManager` port | `read_grant` and `grant_balances` return a `TenantGrant` | The same, and each states `spent` |
| Optional protocols | | `PaymentKeyRepo` (a store), `TenantUsageSource` (a usage source), `DiscoveryDocumentSource` (a discovery source). A supplied part without one runs without what it serves |
| `FakeUsageSource` (`livepeer_builder.testing`) | `page` | and `tenant_page`, with `grant_of`, a map from an allocation to the grant it was allotted from |
| Root exports | | `KeyedAllocation`, `IssuedPaymentKey`, `UsagePage`, `UsageRow`, `SetAsideUsage` |

**Before you start the new release:**

- **Migrate.** `lpb migrate` applies revision 0015, or the engine does at start with `LPB_AUTO_MIGRATE`
  ([persistence.md](persistence.md#migrations)). It adds one table and changes no other.
- **Nothing to finish first.** No command hashes or sends anything it did not before, so a command
  recorded before the upgrade is replayed or resumed as it was.
- **Existing allocations have no payment key.** With self-hosted payments none is needed. At a
  clearinghouse, rotate an actor's payment key to issue its first
  ([payments.md](payments.md#payment-keys)).
- **A supplied `AllocationManager`** states `spent` on the `TenantGrant` it returns, or building one
  fails.
- **Move the clients.** A client that reads the allocation from the two answers keeps working. One that
  refuses unknown fields is generated again from the OpenAPI document.

**What a repeat gets.** The repeat of an allotment or a rotation recorded before the upgrade is answered
from its record, with `payment_key` `{"token": null, "replayed": true}`: no key is issued by a replay.
An allotment still `started` at the upgrade and resumed after it issues the actor's first payment key
and shows it.

**What keeps its name.** `PaymentKey`, what the work half's `CredentialResolver` returns: the key the
runner call presents, which with self-hosted payments is the Batteries key. `pipeline` on a usage row,
Batteries' word for the capability. The route and the command `rotate-payment-key`.
