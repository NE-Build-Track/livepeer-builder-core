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
| [discovery.md](discovery.md): livepeer/node's `/discovery` contract | ISS-09, #45, D63 | `HttpDiscoverySource` reads `LPB_DISCOVERY_URL`. This repository ships no discovery process |

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
