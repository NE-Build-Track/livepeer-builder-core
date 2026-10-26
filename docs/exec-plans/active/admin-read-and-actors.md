# Actor collection and a read-only admin, for JWT-only deployments

## Status

- **Owner:** Mike.
- **Ticket:** [#52](https://github.com/NE-Build-Track/livepeer-builder-core/issues/52), a follow-up to
  JWT bearer (#49, #50).
- **Branch:** `tasks/mz/admin-read-actors`, stacked on `tasks/mz/jwt-bearer` at `238f7a9`.
- **Progress:** decisions locked 26 October (D46); built and in review.
- **Decision:** D46.

## Goal

An engine running with `LPB_AUTH=jwt` has no engine keys. An admin console must still be able to:
- list the actors the engine has provisioned, because today that list is derived from keys;
- read everything with a credential that cannot change anything, so a dashboard's background reader is
  not a full admin.

## Acceptance

- [x] **Listing actors:**
  - `GET /v1/admin/actors` (`application_id`, `status`, `limit`) lists provisioned actors;
  - `GET /v1/admin/applications/{app}/actors/{actor}/allocation` reads one;
  - both check `admin.read` before any lookup.
- [x] **A label on the actor record:** `ProvisionedActor.label` stores the provisioning label. Migration
  `0006` adds `lpb_actors.label`.
- [x] **A read operation:** `Operation.ADMIN_READ` (`admin.read`) is checked by every admin read: the key
  list and key inspection, the grant list, reconciliation, and the two actor reads. Admin writes keep
  `admin.keys`, `admin.allocations` and `admin.costs`.
- [x] **The `auditor` kind:** a new `CredentialKind` that holds `admin.read`, plus `discover`,
  `jobs.read`, `costs.read` and `events.read`, across every actor of its own application. Every write is
  `AccessDenied`. Wherever the engine lets an admin see every actor (jobs, cost projections and summary,
  allowance), an auditor sees every actor in its application.
- [x] **Issuing one:** engine keys can be issued with `kind="auditor"`; a JWT carrying
  `LPB_JWT_ADMIN_READ_SCOPE` is an auditor. `LPB_JWT_ADMIN_SCOPE` still wins when both are present.
- [x] **`FakeEngine`:** follows the same rule, checked by the shared contract test.
- [x] **Docs:** `access.md` (the auditor, the actor routes, why `sub` is the identity default),
  `RELIABILITY.md`, D46, generated docs.
- [x] Tests written first; local CI green, the store suite on PostgreSQL 16 included.

## Design

| Credential kind | Reads | Writes |
| --- | --- | --- |
| `actor` | its own records, with the permission | its own jobs, with `jobs.run` |
| `auditor` (new) | every actor in its application, and admin reads in its application | none |
| `admin` | everything | everything |

**Listing.** An admin's actor list covers every application unless it names one. An auditor's covers its
own application, and naming another is `AccessDenied`.

**Identity.** `LPB_JWT_ACTOR_CLAIM` stays `sub`, an identity provider's immutable subject. A readable name
is the actor's `label`, given by the operator at provisioning, so a renamed or reused username can never
take over an actor's money or history.

## Out of scope

- The console's switch to these routes and to Keycloak login: a console change, pinned to this branch.
- Pagination beyond `limit`: provisioned actors number in the hundreds, not millions.

## Progress

- 26 October: issue #52 opened; decisions locked (D46); branch started.
- 26 October: built test-first.
  - **Mutations,** each caught: an auditor that writes, crosses applications, or is not treated as seeing
    every actor; unscoped key lists and reconciliation.
  - **A gap the new contract case found:** `FakeEngine` never checked `jobs.run` before a job; it does now.
  - **Results:** 1006 passed locally, and the store suite on PostgreSQL 16 (48, migration `0006`
    included).

## Decision log

- **26 October (Mike, D46).**
  - **Where:** a new PR stacked on #50.
  - **Read-only admin:** a new `auditor` credential kind, with `admin.read` checked by every admin read.
  - **Identity:** `sub` as `actor_id`, with a label stored on the actor at provisioning (migration
    `0006`).
  - **Why:** best practice for a resource server behind an identity provider. The identity provider
    lists people; the engine lists its own records, and actors are engine records. A credential is not a
    principal, so a key list is no actor list. A service identity should hold least privilege.
