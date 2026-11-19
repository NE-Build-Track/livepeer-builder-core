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

On 19 November 2026 the clearinghouse gained what an enterprise's engine needs from it (D87, #166): a
payment key for each actor, the signer's two paths that take one, the tenant's usage and the runner list.
Two answers gained a field, one contract gained a required field, and four routes are new. No path, body
or status code of an existing route changed.

| What | Before | Now |
| --- | --- | --- |
| The answer to allotting an allocation | `201` `ActorAllocation` | `201` `KeyedAllocation`: every field of `ActorAllocation` where it was, and `payment_key`, `{token, replayed}`. The token (`lpp_...`) is shown once |
| The answer to rotating a payment key | `200` `ActorAllocation` | `200` `KeyedAllocation`. The rotation still rotates the Batteries key, and now replaces the payment key too: the old one stops at once |
| Allot and rotate, in embedded mode | `payments.allot`, `payments.rotate_payment_key` return `ActorAllocation` | They return `KeyedAllocation`, which is an `ActorAllocation`. It no longer compares equal to the stored record: compare `.allocation` |
| `TenantGrant` (the tenant list, the grant read) | `tenant_id`, `name`, `status`, `total`, `unallocated` | and `spent`, required: what the tenant's allocations have spent |
| A tenant's payment records | not served | `GET /v1/clearinghouse/tenants/{tenant_id}/usage` (`UsagePage`); `engine.payments.usage` |
| The clearinghouse's runner list | not served | `GET /v1/clearinghouse/discovery`, livepeer/node's document; `engine.runner_list` |
| Signing a payment for another engine | not served | `POST /sign-orchestrator-info` and `POST /generate-live-payment`, at the root, each with a payment key, on an engine with a signer (`LPB_SIGNER_URL`); `engine.payments.signing` |
| A usage cursor the provider refuses, over REST | `CursorMismatch` had no status of its own | 400, `code: cursor_mismatch` |
| The facade | `PaymentsAdminApi`, `ClearinghouseApi` | `PaymentsAdminApi.usage` (`TenantUsageApi \| None`), `PaymentsAdminApi.signing` (`PaymentSigningApi \| None`), `ClearinghouseApi.runner_list` (`RunnerListApi \| None`) |
| The `AllocationManager` port | `read_grant` and `grant_balances` return a `TenantGrant` | The same, and each states `spent` |
| Optional protocols | | `PaymentKeyRepo` (a store), `TenantUsageSource` (a usage source), `DiscoveryDocumentSource` (a discovery source), `SignerForwarder` (a runner transport). A supplied part without one runs without what it serves |
| `FakeUsageSource` (`livepeer_builder.testing`) | `page` | and `tenant_page`, with `grant_of`, a map from an allocation to the grant it was allotted from |
| The signer behind a clearinghouse, in a test | | `FakeSigningTransport` (`livepeer_builder.testing`): a scripted transport that also forwards signer calls and records each as a `ForwardedSignerCall` |
| The published schema | | The two signer paths, with the security scheme `PaymentKey`. `lpb gen-docs` builds its engine with a signer address so they are in it |
| Root exports | | `KeyedAllocation`, `IssuedPaymentKey`, `UsagePage`, `UsageRow`, `SetAsideUsage`, `SignerAnswer`, `SignerCall`, and the errors `SignerNotReached` and `SignerOutcomeUnknown` |

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
- **An engine with a signer serves two more paths, at the root.** A deployment whose proxy publishes the
  engine publishes `POST /sign-orchestrator-info` and `POST /generate-live-payment` with it. They answer
  a payment key only. With self-hosted payments nothing calls them, and a proxy may leave them out.

**What a repeat gets.** The repeat of an allotment or a rotation recorded before the upgrade is answered
from its record, with `payment_key` `{"token": null, "replayed": true}`: no key is issued by a replay.
An allotment still `started` at the upgrade and resumed after it issues the actor's first payment key
and shows it.

**What keeps its name.** `PaymentKey`, what the work half's `CredentialResolver` returns: the key the
runner call presents, which with self-hosted payments is the Batteries key. `pipeline` on a usage row,
Batteries' word for the capability. The route and the command `rotate-payment-key`.

## Upgrading to a public and a private listener

On 19 November 2026 the engine gained a private listener and the wallet report (D84, #167). A deployment
that sets nothing new serves one listener, as before: no route, body, status code or stored record of an
existing route changed, and no credential is refused that was accepted. There is no migration.

| What | Before | Now |
| --- | --- | --- |
| Listeners | One, on `LPB_HTTP_HOST` and `LPB_HTTP_PORT` | The same, unless `LPB_HTTP_PRIVATE_PORT` is set. Then two: that address is the public listener, and `LPB_HTTP_PRIVATE_HOST` (`127.0.0.1`) with `LPB_HTTP_PRIVATE_PORT` is the private one ([access.md](access.md#listeners)) |
| Onboarding, the tenant list, a grant's top-up and a tenant's status, with two listeners | | 404 on the public listener; served on the private one |
| A clearinghouse admin credential, with two listeners | | 403 on every route of the public listener that takes an access credential; accepted on the private one |
| A tenant admin, auditor, actor or service credential, with two listeners | | Accepted on the public listener as before; 403 on every route of the private one |
| The signer's two paths (#166), with two listeners | | Served on both, to a payment key. An access key of any kind is 401 on them, as with one listener |
| The wallet | not served | `GET /v1/clearinghouse/wallet` (`WalletReport`), a clearinghouse admin's; `engine.payments.wallet` ([payments.md](payments.md#the-wallet-report)) |
| `create_app` | `create_app(engine, *, manage_lifecycle, max_request_bytes)` | and `listener`: `"only"` (the default), `"public"` or `"private"` |
| `serve`, `lpb serve` | One server | Two servers over one engine when `LPB_HTTP_PRIVATE_PORT` is set. `extend` runs once for each app |
| `build_app` | One app | The same. With `LPB_HTTP_PRIVATE_PORT` set it raises `ValueError`: it builds one listener's app |
| The published schema | | The routes of the private group carry the tag `private listener` |
| The facade | `PaymentsAdminApi` | `PaymentsAdminApi.wallet` (`WalletApi \| None`) |
| Optional protocols | | `EscrowSource` (a grants manager), `SignerProbe` (a runner transport). A supplied part without one runs without the wallet report |
| `FakeAllocationManager`, `ScriptedTransport` (`livepeer_builder.testing`) | | `escrow` and `escrow_accounts`; `signer` and `signer_readiness` |
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
- **An entrypoint that calls `build_app`** keeps `LPB_HTTP_PRIVATE_PORT` unset, or builds each app with
  `create_app(engine, listener=...)` over an engine it starts itself.

**What a later release changed.** An engine in its `clearinghouse` role requires the private listener
([the engine's role setting](#upgrading-to-the-engines-role-setting)). In its other two roles two
listeners are a deployment's choice.

## Upgrading to an engine that uses a payment clearinghouse

On 19 November 2026 the engine gained the remote adapters of the payment ports and
`lpb payments check` (D84, D87, #168). A deployment that sets nothing new runs self-hosted payments as
before, and there is no migration. One route's answer gained a field (the allocation read, below). An
engine that uses a clearinghouse signs there with the gateway SDK's transport and its actors' payment
keys ([payments.md](payments.md#what-signs-for-an-engine-that-uses-a-clearinghouse)).

| What | Before | Now |
| --- | --- | --- |
| Settings | | `LPB_CLEARINGHOUSE_URL` (a scheme, a host and an optional port, and nothing after them), `LPB_CLEARINGHOUSE_TOKEN` and `LPB_CLEARINGHOUSE_TENANT_ID`, set together or not at all, and `LPB_CLEARINGHOUSE_TIMEOUT_S` (30). Refused at startup beside `LPB_BATTERIES_URL`, `LPB_BATTERIES_TOKEN`, `LPB_BATTERIES_GRANTS_TOKEN` or `LPB_SIGNER_URL` ([payments.md](payments.md#the-remote-adapters)) |
| `LPB_DISCOVERY_URL` | Required | Required unless `LPB_CLEARINGHOUSE_URL` is set: it then defaults to the clearinghouse's runner list, and the reader carries the tenant admin key there |
| `EngineSettings.from_env(defaults=...)` | A default fills a field the environment leaves unset | The same, except that with a clearinghouse set the discovery address is its runner list before any default |
| `EngineSupply`, `EngineParts` | | `credentials` (`CredentialResolver`) and `allowances` (`AllowanceReader`). Unset, or `None`: the engine's own records, as before |
| Optional protocols | | `AllocationsByActor` (an allocation manager): `for_actor(tenant_id, actor_id)`. A supplied manager without it is called as before |
| Errors | | `ProviderOutcomeUnknown`, a `ProviderUnavailable` (503): a command left and no answer says whether it applied |
| Adapters | | `ClearinghouseAllocations`, `ClearinghouseAllowances`, `ClearinghouseUsage`, `ClearinghouseCredentials` (`livepeer_builder.adapters.clearinghouse`) |
| `SdkRunnerTransport` | `SdkRunnerTransport(signer_url, max_payment_challenge_retries)` | and `signer_is_clearinghouse` (false unless said): the address is a clearinghouse's, whose `signer_not_reached` is then read as `signer_unavailable`. The engine says it from its settings |
| A job met by a clearinghouse that could not reach its signer, while signing | | `failed`, `payment` / `signer_unavailable`, nothing paid. Every other 5xx while signing is `uncertain`, as with a signer |
| An engine that uses a clearinghouse | | Serves neither the signer's two paths nor the wallet report: `payments.signing` and `payments.wallet` are `None` |
| `HttpDiscoverySource` | `HttpDiscoverySource(discovery_url, *, client, timeout_s)` | and `bearer`, a credential for the discovery address |
| CLI | | `lpb payments check`: exit 0, 1 or 2 ([payments.md](payments.md#lpb-payments-check)) |
| `livepeer_builder.runtime` | | `check_payments(settings)` |
| Root exports | | `PaymentCheck`, `PaymentProbe`, `ProviderOutcomeUnknown`, `AllocationRead` |
| `GET /v1/clearinghouse/tenants/{tenant_id}/actors/{actor_id}/allocation` | The allocation's record (`ActorAllocation`) | `AllocationRead`: every field of the record where it was, and `allowance`, what `GET /v1/allowance` answers for that actor, or `null` when the provider could not be read ([payments.md](payments.md#allowance)) |
| The facade | `PaymentsAdminApi` | `PaymentsAdminApi.get_allocation`, which a caller's own implementation of the facade must state. `get_actor` still returns the record alone |
| An allotment's answer at an engine that uses a clearinghouse | | `payment_key: null`: the engine issues none of its own and shows the clearinghouse's to nobody |

**Before you start the new release:**

- **Nothing, for self-hosted payments.** Leave the clearinghouse settings unset.
- **To use a clearinghouse,** set the three settings from what the clearinghouse admin handed over at
  onboarding (the address of the public listener, the tenant admin key and the tenant's id), keep
  `LPB_SECRET_KEY`, and unset every Batteries and signer setting. The address is a scheme, a host and
  an optional port: a path is refused, because the signer's two paths are called at its root. Run
  `lpb payments check`.
- **A caller's own implementation of `PaymentsAdminApi`** states `get_allocation`.
- **A reader of the allocation route** that refuses a field it does not know meets `allowance`.
- **Put every actor in the tenant.** `LPB_JWT_TENANT_ID` or the tenant claim, the `tenant_id` of the
  access keys the engine issues, and the `ActorContext` an embedding app builds all carry the tenant's
  id at the clearinghouse. An actor in another tenant is refused there.
- **Allot each actor again.** An actor allotted with self-hosted payments holds a Batteries key, which
  is never sent to a clearinghouse: its jobs fail `not_provisioned`. Nothing is rewritten (T30).
- **A caller's own allocation manager** changes nothing.

## Upgrading to the engine's role setting

On 19 November 2026 the engine gained its role setting and the two Compose projects that run a payment
clearinghouse and an enterprise that uses it (D84, D87, #169). A deployment that sets nothing new is in
its `both` role and serves, wires and starts what it did: self-hosted payments, as one deployment.
There is no migration. Two things changed for deployments that had set something new: an engine that
names a clearinghouse now needs `LPB_ROLE=work`, and it no longer serves clearinghouse routes of its
own.

| What | Before | Now |
| --- | --- | --- |
| Settings | | `LPB_ROLE`: `both` (the default), `work` or `clearinghouse` ([RELIABILITY.md](../RELIABILITY.md#roles)) |
| `LPB_CLEARINGHOUSE_URL`, `LPB_CLEARINGHOUSE_TOKEN`, `LPB_CLEARINGHOUSE_TENANT_ID` | Set together, they selected the remote adapters (#168) | They need `LPB_ROLE=work`, and `LPB_ROLE=work` needs them. With `LPB_ROLE` unset, `both` or `clearinghouse` they are refused at startup |
| The routes of an engine that names a clearinghouse | Every route. What only a clearinghouse admin does answered 403 | The work routes alone. Every `/v1/clearinghouse` route and the signer's two paths are 404 |
| That engine's embedded API | `engine.runner_list` served its discovery document | `engine.runner_list` is `None`, as `payments.signing` and `payments.wallet` are: it is nobody's clearinghouse. `engine.payments.allot` and the other commands are unchanged |
| A payment clearinghouse | An engine with one listener or two, serving every route | `LPB_ROLE=clearinghouse`: the clearinghouse routes alone, on two listeners. Every work route is 404, `GET /v1/offerings` and `/v1/keys` included |
| What a `clearinghouse` engine needs | | `LPB_HTTP_PRIVATE_PORT`, a signer (`LPB_SIGNER_URL`), Batteries (`LPB_BATTERIES_URL`, `LPB_BATTERIES_TOKEN`) and the grants credential (`LPB_BATTERIES_GRANTS_TOKEN`), or the parts supplied in their place. Without one it does not start |
| What a `clearinghouse` engine starts | | Discovery alone. No interrupted job is resolved, and no cost sync or retention purge runs. `jobs.run` and `jobs.streaming.stream` raise `InvalidRequest` |
| `GET /readyz`, `EngineHealth` | | and `role` |
| `lpb payments check`, `PaymentCheck` | `configuration`, `ok`, `probes`, `tenant` | and `role`. In the `clearinghouse` role the `batteries_grants` probe is always made, and is `not_configured` without the credential |
| `lpb smoke` | An engine with the environment's settings | The same, in the role the environment names, and its output has `role` |
| `create_app(engine, ...)` | Mounted every group of routes | Mounts the groups of `engine.role`. For an engine in its `clearinghouse` role, `listener="only"` raises `ValueError` |
| The facade | `EngineApi` | `EngineApi.role`, which a caller's own implementation of the whole-engine facade must state |
| `http/listeners.py` | `EVERY_LISTENER` and `PRIVATE_ONLY`, written by hand | `ROUTE_GROUPS` (each group of routes, its half, and whether it is private) and `ROLE_HALVES`. The two tuples are read from the table |
| The published schema | The private group's routes carry the tag `private listener` | and every route but health carries `work half` or `clearinghouse half` |
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
  public listener only, and moves every use of its admin key to the private address. Nothing may call a
  work route there: its admin's own key is managed under `/v1/clearinghouse/tenants/default/keys`
  (tech debt T42).
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
rule changed: grant limits, repeat-safety, the allocation answer's amounts and payment keys are as they
were. There is no migration.

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
| An engine in its `work` role | Called `/v1/clearinghouse/tenants/{LPB_CLEARINGHOUSE_TENANT_ID}/...`, and the clearinghouse refused another tenant (403) | Calls the paths that name no tenant. A command about another tenant than `LPB_CLEARINGHOUSE_TENANT_ID` is refused in the engine before anything is sent: a `ProviderError` 403, `resource belongs to another tenant`, as the clearinghouse's was |
| `lpb payments check` | `GET /v1/clearinghouse/tenants/{t}/grant` and `.../usage` | `GET /v1/clearinghouse/grant` and `GET /v1/clearinghouse/usage`. A 403 names `LPB_CLEARINGHOUSE_TOKEN`: the key is not a tenant admin's or an auditor's. A grant of another tenant than `LPB_CLEARINGHOUSE_TENANT_ID` is `unexpected` and names that setting |
| `http/clearinghouse.py` | `mount_tenant_routes` (a tenant's routes and the runner list) and `mount_clearinghouse_admin_routes` | `mount_tenant_routes` (a tenant's own routes, which name no tenant), `mount_named_tenant_routes`, `mount_runner_list_routes` and `mount_clearinghouse_admin_routes`. `mount_clearinghouse_routes` mounts them all with the signer's two paths |
| `http/listeners.py` | `RouteGroup(mount, half, private=False)`, `Listener.serves_private_routes`, `EVERY_LISTENER` and `PRIVATE_ONLY` | `RouteGroup(mount, half, served_on="every")`, where `served_on` is `every`, `public` or `private`, and `RouteGroup.served_by(listener)`; `Listener` has its name alone; and `PUBLIC_ONLY` beside the two tuples |
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
