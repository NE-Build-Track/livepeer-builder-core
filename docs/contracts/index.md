# Contracts

What each public interface promises. **Implemented** means code and tests exist. **Defined** means
the contract is fixed but the implementation is scheduled later; calling it answers 501.

| Contract | Ticket | Status |
| --- | --- | --- |
| [engine.md](engine.md): discovery, jobs, attempts, results, usage, failures, idempotency | ISS-02, ISS-26 | Immediate, streamed and async jobs implemented (streaming is optional: `jobs.streaming`); persistent defined (M4) |
| [access.md](access.md): actor context, authenticators, policy, credentials | ISS-03 | API key, JWT bearer (D45) and policy implemented; trusted backend defined (M4) |
| [payments.md](payments.md): allocations, allotment, units, cost classification | ISS-04 | Implemented for self-hosted payments; the payment clearinghouse (D82) is M4 work (ISS-31) |
| [events.md](events.md): versioned event feed | ISS-02 | Defined; held in M3 (D60) |
| [persistence.md](persistence.md): store, migrations, retention, backup | ISS-06 | Implemented |
| [discovery.md](discovery.md): livepeer/node's `/discovery` contract | ISS-09, #45, D63 | `HttpDiscoverySource` reads `LPB_DISCOVERY_URL`. This repository ships no discovery process; a clearinghouse's engine serves the document it read (#166) |

The REST surface is generated into [../generated/openapi.json](../generated/openapi.json): every route
of an engine in its `both` role, each tagged with its half. The routes of an engine in its
`clearinghouse` role, whose allotment and rotation answer the allocation with its key, are generated
into [../generated/openapi-clearinghouse.json](../generated/openapi-clearinghouse.json) (#169): its
public and its private listener's routes in one document, each tagged with its listener.

## Upgrading to the discovery address checked at start (#154)

On 21 November 2026 the check that an engine has a discovery address moved from reading settings to
starting the engine. Nothing changes for a deployment that sets `LPB_DISCOVERY_URL`, and an engine
still does not start without it (D63).

| What | Before | Now | What an embedder does |
| --- | --- | --- | --- |
| `EngineSettings.discovery_url` | `str`, required | `str \| None`, `None` when unset | Code that reads it handles `None`, or calls `settings.require_discovery_url()`, which returns the address or raises |
| Settings without the address | `EngineSettings(...)` and `EngineSettings.from_env()` raise pydantic's `ValidationError` (`discovery_url`: field required), before any database is opened | They are read. `BuilderEngine.create` builds the engine: it opens the database, and migrates it when `LPB_AUTO_MIGRATE` is `true`. `start()` raises a `ValueError` that names `LPB_DISCOVERY_URL`, before it polls or writes anything, and stops the engine | An application that relied on reading settings to fail without the address checks `settings.discovery_url` itself, or lets `start()` refuse. One that starts its own engine calls `settings.require_discovery_url()` before `create` |
| `EngineSettings.from_env` | Takes `defaults=` | Takes the environment only | For a value the environment leaves unset, give `from_env` a mapping with your values under the environment's: `EngineSettings.from_env({"LPB_SIGNER_URL": url, **os.environ})`. Every rule of the settings runs on them. An empty variable in the environment is unset and hides your value, where `defaults=` kept it: leave empty variables out of the mapping to keep yours. `model_copy(update=...)` runs no rule, so it is only for a value that is valid by construction |
| `lpb serve`, `serve` and `build_app` without the address | `lpb serve` exits with code 1, `ValidationError` | They raise the `ValueError` naming the variable before the database is opened; `lpb serve` exits with code 1 | Nothing |
| `lpb purge`, `lpb bootstrap-admin` without the address | Run, with a placeholder address they never polled | Run, with no address | Nothing |

**Before you start the new release:** an application that builds its engine with
`BuilderEngine.create` and then starts it should call `settings.require_discovery_url()` first. Without
that call, a deployment that lost its address migrates the database to this release's revision and
then refuses to start, and the release before it does not start on that revision
([embedded mode](../guides/embedded-mode.md#1-construct-and-start-the-engine-once)). `lpb serve`,
`serve` and `build_app` make the call themselves.

A malformed address is refused when settings are read, as before, and an empty `LPB_DISCOVERY_URL` is
unset, as before. An engine whose `decorate` stage replaces the discovery source still needs the
address to start, as it did before ([discovery.md](discovery.md#what-the-engine-reads)).

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
  number, so such a database records 0015 and passes for a sound one. The engine starts on it, and
  `lpb migrate` adds only what later revisions add. A database that has `lpb_payment_keys` is
  recreated. One at revision 0014 or earlier never ran that work and is migrated as usual
  ([persistence.md](persistence.md#migrations)).

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

**What a later release changed.** An engine in its `clearinghouse` role requires the private listener,
and it is that role which hands each allocation's key to the caller
([the engine's role setting](#upgrading-to-the-engines-role-setting)). In its other two roles two
listeners are a deployment's choice.

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
| `LPB_DISCOVERY_URL` | Required to start | The same, and with `LPB_CLEARINGHOUSE_URL` set and no address given it is the clearinghouse's runner list, where the reader carries the tenant admin key. An address of your own, in the environment or in the mapping you give `from_env`, is kept |
| `EngineSupply`, `EngineParts` | | `credentials` (`CredentialResolver`) and `allowances` (`AllowanceReader`). Unset, or `None`: the engine's own records, as before |
| Optional protocols | | `AllocationsByActor` (an allocation manager): `for_actor(tenant_id, actor_id, operation_ref=None)`. `OneKeyPerAllocation` (an allocation manager whose provider keeps one key for an allocation and hands it over once): `holds_key(allocation_id, api_key_id)`. `ReadyBeforeCommands` (an allocation manager that must learn something of its provider before any command, #165): `ready_for_commands()`, awaited before a command's operation record is begun. A supplied manager without them is called as before |
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
- **The clearinghouse must hand keys over.** It is an engine in its `clearinghouse` role
  ([the engine's role setting](#upgrading-to-the-engines-role-setting),
  [payments.md](payments.md#payment-keys)). An engine in its `both` role keeps each key with itself and
  hands none over: an enterprise's engine pointed at one allots there and cannot use the answer, which
  carries no key.
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

## Upgrading to the engine's role setting

The engine has a role setting, and two Compose projects run a payment clearinghouse and an enterprise
that uses it (D84, D87, #169). A deployment that sets nothing new is in its `both` role and serves,
wires and starts what it did: self-hosted payments, as one deployment. There is no migration of its
own. Two things changed for deployments that had set something new: an engine that names a
clearinghouse now needs `LPB_ROLE=work`, and it no longer serves clearinghouse routes of its own.

| What | Before | Now |
| --- | --- | --- |
| Settings | | `LPB_ROLE`: `both` (the default), `work` or `clearinghouse` ([RELIABILITY.md](../RELIABILITY.md#roles)) |
| `LPB_CLEARINGHOUSE_URL`, `LPB_CLEARINGHOUSE_TOKEN`, `LPB_CLEARINGHOUSE_TENANT_ID` | Set together, they selected the remote adapters (#168) | They need `LPB_ROLE=work`, and `LPB_ROLE=work` needs them. With `LPB_ROLE` unset, `both` or `clearinghouse` they are refused at startup |
| The routes of an engine that names a clearinghouse | Every route. What only a clearinghouse admin does answered 403 | The work routes alone. Every `/v1/clearinghouse` route is 404 |
| That engine's embedded API | `engine.runner_list` served its discovery document | `engine.runner_list` is `None`, as `payments.wallet` is: it is nobody's clearinghouse. `engine.payments.allot` and the other commands are unchanged |
| A payment clearinghouse | An engine with one listener or two, serving every route, which stored each allocation's key and answered none | `LPB_ROLE=clearinghouse`: the clearinghouse routes alone, on two listeners. Every work route is 404, `GET /v1/offerings` and `/v1/keys` included. Allotment and rotation hand the allocation's key to the caller once (`allocation_api_key`, `replayed`, `resumed`), and nothing stores it |
| What a `clearinghouse` engine needs | | `LPB_HTTP_PRIVATE_PORT`, Batteries (`LPB_BATTERIES_URL`, `LPB_BATTERIES_TOKEN`) and the grants credential (`LPB_BATTERIES_GRANTS_TOKEN`), or the parts supplied in their place. Without one it does not start. It needs no `LPB_SECRET_KEY`, and it starts without `LPB_SIGNER_URL`: its wallet report then says the signer is `not_configured` |
| A `clearinghouse` engine's runner transport | | `IdentityOnlyTransport` (`livepeer_builder.adapters.sdk`): it asks the signer's identity for the wallet report and cannot pay. `engine.jobs.streaming` is `None` |
| What a `clearinghouse` engine starts | | Discovery alone. No interrupted job is resolved, and no cost sync or retention purge runs. `jobs.run` raises `InvalidRequest` before the job's reference is claimed, and `jobs.streaming` is `None` with the role's default transport |
| `GET /readyz`, `EngineHealth` | | and `role` |
| `lpb payments check`, `PaymentCheck` | `configuration`, `ok`, `probes`, `tenant` | and `role`. In the `clearinghouse` role the `batteries_grants` probe is always made, and is `not_configured` without the credential. A `signer` probe that is `not_configured` says what follows in that role |
| `lpb smoke` | An engine with the environment's settings | The same, in the role the environment names, and its output has `role` |
| `create_app(engine, ...)` | Mounted every group of routes, with `deliver_allocation_keys=False` | Mounts the groups of `engine.role`, and hands allocation keys over in the `clearinghouse` role alone. For an engine in its `clearinghouse` role, `listener="only"` raises `ValueError` |
| The facade | `EngineApi` | `EngineApi.role`, which a caller's own implementation of the whole-engine facade must state |
| `http/listeners.py` | `EVERY_LISTENER` and `PRIVATE_ONLY`, written by hand. `mount_listener_routes(app, engine, name)` | `ROUTE_GROUPS` (each group of routes, its half, and whether it is private) and `ROLE_HALVES`. The two tuples are read from the table. `delivers_allocation_keys(role)`, and `mount_listener_routes(app, engine, name, *, deliver_allocation_keys)`: the keyword is required, and a value that disagrees with the engine's role raises `ValueError` |
| The published schema | The private group's routes carry the tag `private listener` | and every route but health carries `work half` or `clearinghouse half`. `docs/generated/openapi-clearinghouse.json` is the `clearinghouse` role's own schema |
| `AdmissionLimiter` | `AdmissionLimiter(max_concurrent, max_per_app)` | and `runs_jobs` (true unless said) |
| Contracts | | `Role` (`livepeer_builder.contracts`) |
| The log line `engine.started` | | and `role` |
| Compose projects | `deploy/dev-stack` | and `deploy/clearinghouse` and `deploy/enterprise` ([deploy/README.md](../../deploy/README.md)) |

**Before you start the new release:**

- **Nothing, for self-hosted payments.** Leave `LPB_ROLE` unset.
- **An engine that uses a clearinghouse** sets `LPB_ROLE=work`. Whatever called that engine's own
  `/v1/clearinghouse` routes stops: an embedding app allots through `engine.payments.allot`, as before,
  and an engine server has no route for it (tech debt T41). Run `lpb payments check`.
- **A payment clearinghouse** sets `LPB_ROLE=clearinghouse` and `LPB_HTTP_PRIVATE_PORT`, publishes the
  public listener and its signer and nothing else, and moves every use of its admin key to the private
  address. The signer keeps its Batteries authorization webhook: enterprise engines sign there
  themselves, and it is the only check at signing. Nothing may call a work route there: its admin's own
  key is managed under `/v1/clearinghouse/tenants/default/keys` (tech debt T42).
- **A caller of a clearinghouse's allotment or rotation** reads the key from the answer and keeps it:
  it is shown once. An enterprise's engine in its `work` role does this itself.
- **A caller's own implementation of `EngineApi`** states `role`.
- **An entrypoint that calls `create_app` for a clearinghouse** builds two apps, `listener="public"` and
  `listener="private"`, or calls `serve`.

## Upgrading to a tenant's routes that name no tenant

On 19 November 2026 Mike amended D87 point 3 after the reviewer's comment on #175 (#165, #166). A
tenant's routes come in two forms, which call the same service with the tenant they resolve. On the
public listener they name no tenant: the tenant is the credential's, a tenant admin's or an auditor's.
On the private listener the clearinghouse admin names the tenant, at the paths it used before. One rule
holds on every listener, one listener included: a path that names no tenant refuses a clearinghouse
admin, and a path that names a tenant refuses a tenant credential, its own tenant's included. No service
rule changed: grant limits, repeat-safety, the allocation answer's amounts and who keeps each
allocation's key are as they were. There is no migration.

| What | Before | Now |
| --- | --- | --- |
| A tenant admin's or an auditor's routes | `/v1/clearinghouse/tenants/{tenant_id}/grant`, `.../usage`, `.../actors`, `.../actors/{actor_id}/allocation` and its `/top-up`, `/status`, `/revoke` and `/rotate-payment-key`, and `.../keys`, `.../keys/{key_id}` and its `/rotate` and `/revoke` | The same routes without `tenants/{tenant_id}`: `GET /v1/clearinghouse/grant`, `GET /v1/clearinghouse/usage`, `GET /v1/clearinghouse/actors`, `/v1/clearinghouse/actors/{actor_id}/allocation` and its commands, and `/v1/clearinghouse/keys` and its commands. The tenant is the credential's. Who may call each, its body and its answers are as they were |
| A tenant credential at a path that names a tenant | Its own tenant was answered; another tenant was 403 (`resource belongs to another tenant`) | 403 for its own tenant too, before any lookup or provider call. On the public listener such a path is 404 |
| A clearinghouse admin at a path that names a tenant | Answered on the private listener and with one listener | The same |
| A clearinghouse admin at a path that names no tenant | | 403: it has no tenant of its own |
| An actor or a service credential at a tenant's routes | 403 (the policy's) | 403, in either form |
| The public listener | Every route but the private group | Every route but the private group, which now holds the paths that name a tenant |
| The private listener | Every route | Every route but a tenant's own routes, which name no tenant (404 there) |
| `/v1/clearinghouse/keys`, `/v1/clearinghouse/keys/{key_id}` and its commands | 404: an engine's own keys left them for `/v1/keys` in #164 | A tenant's own access keys. A clearinghouse admin is 403 there, and the engine's own keys stay at `/v1/keys` |
| `GET /v1/clearinghouse/actors` | 404 since #165 | A tenant's own actor list. A `tenant_id` in its query is not read |
| An engine in its `work` role | Called `/v1/clearinghouse/tenants/{LPB_CLEARINGHOUSE_TENANT_ID}/...`, and the clearinghouse refused another tenant, and a key of another tenant than the path's (403) | Calls the paths that name no tenant. A command about another tenant than `LPB_CLEARINGHOUSE_TENANT_ID` is refused in the engine before anything is sent: a `ProviderError` 403, `resource belongs to another tenant`, as the clearinghouse's was. A key of another tenant than that setting is refused in the engine too: it reads the key's grant before its first request of a process, one `GET /v1/clearinghouse/grant` more, which a command makes before its operation record is begun, and every request is a `ProviderError` 403 while the key is another tenant's ([payments.md](payments.md#who-the-engine-is-at-the-clearinghouse)) |
| `lpb payments check` | `GET /v1/clearinghouse/tenants/{t}/grant` and `.../usage` | `GET /v1/clearinghouse/grant` and `GET /v1/clearinghouse/usage`. A 403 names `LPB_CLEARINGHOUSE_TOKEN`: the key is not a tenant admin's or an auditor's. A grant of another tenant than `LPB_CLEARINGHOUSE_TENANT_ID` is `unexpected`, says the key belongs to another tenant, and fails the usage probe too; no grant is printed |
| `http/clearinghouse.py` | `mount_tenant_routes` (a tenant's routes and the runner list) and `mount_clearinghouse_admin_routes` | `mount_tenant_routes` (a tenant's own routes, which name no tenant), `mount_named_tenant_routes`, `mount_runner_list_routes` and `mount_clearinghouse_admin_routes`. `mount_clearinghouse_routes` mounts them all. Each form of a tenant's routes takes the required `deliver_allocation_keys` |
| `http/listeners.py` | `RouteGroup(mount, half, private=False)`, `Listener.serves_private_routes`, `EVERY_LISTENER` and `PRIVATE_ONLY` | `RouteGroup(mount, half, served_on="every", allots=False)`, where `served_on` is `every`, `public` or `private` and `allots` says the group's mount function takes `deliver_allocation_keys`, and `RouteGroup.served_by(listener)`; `Listener` has its name alone; and `PUBLIC_ONLY` beside the two tuples |
| The published schema | The private group's routes carry the tag `private listener` | and a tenant's own routes carry `public listener` |

**Before you start the new release:**

- **A tenant's admin and auditor** (an enterprise app, a dashboard, a console's tenant view) drop
  `tenants/{tenant_id}` from every path they call. Nothing else in the request changes.
- **A clearinghouse admin** keeps its paths, on the private listener, or on the one listener of a
  deployment that has one. A script that used a tenant admin key at those paths moves to the paths that
  name no tenant.
- **Upgrade a clearinghouse and the engines that use it together.** An engine in its `work` role of this
  release calls paths that an earlier clearinghouse does not serve, and one of an earlier release calls
  paths that this clearinghouse serves on its private listener only. Run `lpb payments check` after.

## Upgrading to async jobs

ISS-26 built async jobs (D69): a job queued in the engine's own database, run by a worker in the
engine's process, and read back by id with its stored result ([engine.md](engine.md#async-jobs)). The
database migrates to revision 0016 ([persistence.md](persistence.md#migrations)). An engine that is sent
no async job runs as before, with three exceptions: results retention is on by default, the result
route of an immediate job answers 410, not 501, and a runner's very long error answer or 402 is no
longer read to its end (the last row but one).

| What | Before | Now |
| --- | --- | --- |
| `POST /v1/jobs` with `mode="async"` | 501 | 202 with the queued job, `Location` and `Livepeer-Job-Id`; 200 with `Idempotent-Replayed: true` for a repeated `operation_ref`; 400 with `?stream=true`, `Accept: text/event-stream` or `return_http_errors` |
| `GET /v1/jobs/{id}/result` | 501, to the job's owner | An async job's stored bytes, with the runner's `Content-Type`; 409 `job_not_finished` with `Retry-After` before it ends; the failure's status for a failed or uncertain job; 410 `result_expired` for an immediate job and for an expired result |
| `POST /v1/jobs/{id}/cancel` | | New: cancels a queued job (`jobs.run`, the owner); 409 `job_not_queued` for any other |
| `GET /v1/jobs/{id}` | | `Retry-After: 1` while the job is `queued` or `running` |
| `jobs.run`, `jobs.streaming.stream` with `mode="async"` | `NotImplementedInMilestone` (501) | `InvalidRequest` (400): queue it with `jobs.submit` |
| `jobs.submit`, `jobs.result` | `NotImplementedInMilestone`; `submit` was not on `JobsApi` | `submit` returns the queued `Job`; `result` returns the `JobResult`, or raises `JobNotFinished`, `JobFailed` or `ResultExpired` |
| `JobsApi` (the facade) | | `submit` and `cancel`: a caller's own implementation of the facade adds them |
| `JobRepo` (a supplied store) | `finish_job` returned `None` and wrote whatever the job's state | `finish_job` returns whether it ended the job, and ends only a `running` one. Nine methods for the queue: `enqueue_job`, `queued_jobs`, `claim_job`, `requeue_unstarted`, `cancel_queued`, `expire_queued`, `finish_job_with_result`, `read_result`, `queued_stats` |
| Contracts | | `Job.started_at`; `ResultRef.expires_at`; the failure kind `result_not_stored`; `FailureReason`, the type of `Failure.reason`, which adds `cancelled` to the four payment reasons; `EngineHealth.queued_jobs`, `oldest_queued_s`; `PurgeReport.results_deleted` |
| Errors | | `JobNotFinished` (409), `ResultExpired` (410), `JobNotQueued` (409), exported from `livepeer_builder` |
| `BoundedRunnerTransport` (a supplied transport) | | New and optional, as `StreamingRunnerTransport` is: `call_bounded(runner, request, credential, on_signed, *, max_bytes)` beside `call`. The async worker calls it, with `LPB_ASYNC_RESULT_MAX_BYTES`, on a transport that has it, and `call` on one that has not, which then reads an async answer whole. `SdkRunnerTransport` has it. `RunnerTransport` itself is unchanged |
| `RunnerReply` | | `size_bytes` and `sha256`, both `None` unless `call_bounded` set them: the size and SHA-256 of the runner's whole answer, whose `content` is then all of it, or empty when it was longer than `max_bytes` |
| Settings | | `LPB_ASYNC_WORKERS`, `LPB_ASYNC_MAX_QUEUED`, `LPB_ASYNC_QUEUE_TTL_S`, `LPB_ASYNC_RESULT_MAX_BYTES`, `LPB_ASYNC_POLL_S`, `LPB_ASYNC_DRAIN_TIMEOUT_S`, and `LPB_RETENTION_RESULTS_DAYS` (7), which turns the retention purge on by default ([RELIABILITY.md](../RELIABILITY.md#configuration)) |
| Schema | Revision 0015 | 0016: `lpb_jobs.started_at` and `claims`; the tables `lpb_job_requests` and `lpb_job_results` |
| `FakeEngine` | Ran `mode="async"` as an immediate job and recorded it `immediate`; `jobs.result` returned an immediate job's result | `jobs.submit` queues, `await fake.jobs.advance()` runs what is queued, `jobs.result` and `jobs.cancel` answer by the engine's rules, and `jobs.run(mode="async")` is `InvalidRequest` |
| `make_engine` (testing) | | `async_workers=1`; step the worker with `await engine.jobs.worker.run_once()` |
| `lpb purge`, `BuilderEngine.purge()` | `jobs_deleted`, `usage_rows_deleted` | and `results_deleted` |
| Background loops | | `lpb-async-jobs`, in the `both` and `work` roles |
| A runner's error answer or 402 payment challenge, for every kind of job | Read whole, whatever its length | An error answer is read for 16 KiB: `failure.body` is its first 4096 characters, as before, and an error still arriving at the job's deadline now fails with the runner's status, not `timeout`. A 402 is read up to 64 KiB; a longer one is refused unsigned, `http` (402) ([engine.md](engine.md#failures)) |
| The log | | `jobs.async_step_failed`, `jobs.async_failed`, `jobs.async_unclaim_failed`, `jobs.async_drained`, `jobs.queue_expired`, `jobs.finish_missed`; `jobs.recovered` gains `requeued` |

**Before you start the new release:**

- **Migrate** (`lpb migrate`, or start with `LPB_AUTO_MIGRATE=true`).
- **A database that ran the withdrawn payment-key work** is recreated, as said under
  [the payment key handover](#upgrading-to-the-payment-key-handover-a-tenants-usage-and-the-runner-list). The revision check does not find it here either: that work's second migration was
  numbered 0016, the number of this one, so a database that ran it records the head and has neither
  `lpb_actors.credential_custody` nor the tables of async jobs ([persistence.md](persistence.md#migrations)).
- **Decide how long results are kept.** `LPB_RETENTION_RESULTS_DAYS` is 7 unless set. The stored
  requests and results are not encrypted by the engine: protect the database and its backups.
- **A supplied store** implements the queue's methods of `JobRepo`, and `finish_job` returns `bool`.
- **A caller's own implementation of `JobsApi`** adds `submit` and `cancel`.
- **A test that relied on `FakeEngine` running `mode="async"` at once** calls `jobs.submit` and then
  `jobs.advance()`; one that read an immediate job's result from the fake expects `ResultExpired`.
- **A client that matched the 501 of `/result` or of `mode="async"`** reads the new answers above.
