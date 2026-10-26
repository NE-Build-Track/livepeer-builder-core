# ISS-08: standalone credentials and ownership controls

## Status

- **Owner:** Mike builds it; John reviews. John wrote the gap analysis and plan.
- **Ticket:** ISS-08, [Network-Engineering-SPE#21](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/21) (M3).
- **Branch:** `tasks/mz/iss-08-credentials`, off `releases/m3` at `629f1ab`.
- **Progress:** decisions locked 26 October (D44); built, shown live, and in review (this PR, and console
  #6).
- **Plan source:** [John's 20 October plan](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/21#issuecomment-6303045369),
  checked again against `629f1ab` on 26 October. Every gap it names was still open.
- **Decision:** D44.

## Goal

Finish the standalone access path that ISS-03 started:
- **Keys:** an operator can inspect and rotate engine API keys (`lpk_`) without ever seeing a stored
  secret, and every key change is logged without one;
- **Answers:** every job and cost route answers the same way, 403 when the caller lacks the operation's
  permission and 404 when the job belongs to someone else;
- **Docs:** one documented example flow shows the whole lifecycle, and the enterprise flow stays
  replaceable.

## Acceptance

From the ticket, and the "Done when" of 21 October:

- [x] Keys can be inspected (`GET /v1/admin/keys/{key_id}`) and rotated
  (`POST /v1/admin/keys/{key_id}/rotate`) without disclosing a secret.
- [x] A missing permission answers 403 and another actor's job answers 404, on every job and cost route:
  - get, list, result, usage report and submit;
  - cost, projections, summary and allowance.
- [x] Key lifecycle events (bootstrapped, issued, rotated, revoked) are logged, with no secret in any
  log record, and that is tested across the full lifecycle, `lpg_` included.
- [x] An example standalone flow is documented in `docs/contracts/access.md`:
  bootstrap, issue, authenticate, provision, inspect, rotate, revoke. Enterprise enrollment is shown as
  replaceable.
- [x] `FakeEngine` follows the same rules, checked by the shared contract test.
- [x] The console gets **Inspect key** and **Rotate key** operator actions, in a console PR pinned to
  this branch ([builder-core-mgmt-ui#6](https://github.com/NE-Build-Track/builder-core-mgmt-ui/pull/6), at `d057935`).
- [x] Generated docs regenerated; local CI green: tests, coverage floors, ruff, mypy, `check_docs`,
  `gen-docs --check`, `lpb smoke`; the store suite also on PostgreSQL 16.

## Design

### Key inspection and rotation

| Layer | Change |
| --- | --- |
| `KeyRepo` (`protocols/`) | `get_access_key(key_id) -> AccessKey \| None`, and `rotate_access_key(old_key_id, new_key, new_token_hash, revoked_at) -> bool` |
| `SqlStore` | Both. Rotation is one transaction: revoke the old key only if it is active, insert the new one, or do nothing and return `False`. No migration |
| `KeyService` | `get(admin, key_id)`: `NotFound` when unknown. `rotate(admin, key_id, label=None) -> IssuedKey`: same kind, actor, application and permissions. The old token fails at once. Rotating an unknown or revoked key is `InvalidRequest`, as revoking one is |
| `KeysApi` (`api.py`) | `get` and `rotate` |
| REST | `GET /v1/admin/keys/{key_id}` returns an `AccessKey` (no token, no hash). `POST /v1/admin/keys/{key_id}/rotate` takes an optional `{"label": ...}` and returns `201` with an `IssuedKey`, whose token is shown once |

### One rule on every job and cost route

1. Check the operation's permission first, before any lookup. A missing permission is
   `AccessDenied` (403) and reveals nothing about any job.
2. Then check ownership against the job. Another actor's or application's job is `NotFound` (404), as
   is a missing one.

| Route | Permission | Change |
| --- | --- | --- |
| `GET /v1/jobs/{id}` | `jobs.read` | Permission before lookup (was 404 for both) |
| `GET /v1/jobs` | `jobs.read` | An admin lists every actor's jobs in its application, like `costs.recent` and `costs.summary`. An actor lists its own. The check names the target |
| `GET /v1/jobs/{id}/result` | `jobs.read` | Permission, then ownership, then `501` (was `501` for anyone) |
| `POST /v1/jobs/{id}/usage` | `jobs.run` only | No longer goes through `get`, so it no longer also needs `jobs.read` |
| `POST /v1/jobs` (`mode=async`) | `jobs.run` | Permission before `501` (already true: `_admit_request` checks first) |
| `GET /v1/jobs/{id}/cost` | `costs.read` | Permission before lookup (was 404 for both) |
| `GET /v1/costs/projections`, `/summary`, `/v1/allowance` | `costs.read` | Already permission first; covered by the new tests |

### Audit log

`KeyService` logs one INFO event per change: `keys.bootstrapped`, `keys.issued`, `keys.rotated` and
`keys.revoked`.
- **Fields:** `key_id`, `kind`, `actor_id`, `application_id`, `by` (the acting admin's `actor_id`, or
  `cli` for bootstrap) and, on rotation, `replaced_key_id`.
- **Never logged:** the token or its hash.
- **Test:** a full lifecycle at DEBUG, covering keys and the payment key's provision, rotation and
  revocation. It asserts that no record contains an `lpk_` or `lpg_` secret, or a token hash.

### Console

The console's operator actions gain **Inspect key** (`GET /v1/admin/keys/{key_id}`) and **Rotate key**
(`POST /v1/admin/keys/{key_id}/rotate`). That is a console PR pinned to this branch, and re-pinned to
the merge.

## Out of scope

- An overlap window in which the old key keeps working after rotation. That would need an `expires_at`
  column.
- Re-scoping permissions on rotation. Issue a new key instead.
- An audit table. Logs only.
- JWT bearer and trusted-backend authenticators: ISS-33, M4.

## Progress

- 26 October: gaps re-checked against `629f1ab`; decisions locked with Mike (D44); branch started.
- 26 October: built test-first. Every new test was seen failing first, and the no-secrets test was also
  checked by two mutations, logging a token and logging its hash.
  - **Keys:** `KeyRepo.get_access_key` and an atomic `rotate_access_key`; `KeyService.get` and
    `rotate`; the `keys.*` audit events; `KeysApi`; the two REST routes.
  - **Answers:** the answer rule in `services/ownership.py`, used by jobs and costs; `FakeEngine` follows
    it, checked by the shared contract test.
  - **Docs:** the example flow in `access.md`.
  - **One existing test changed:** `test_reserved_execution_modes_fail_loudly` now expects 404 for
    `result()` on an unknown job, because the checks run before the 501.
  - **Results:** 736 passed locally, and the store suite on PostgreSQL 16 (48).
  - **Found while testing:** at DEBUG, the SQLite driver echoes SQL parameters, token hashes among
    them. The engine never enables DEBUG, and a hash is not a credential. The test pins what the engine
    controls; the contract says so.

- 26 October: the console's Rotate action and key inspection
  ([builder-core-mgmt-ui#6](https://github.com/NE-Build-Track/builder-core-mgmt-ui/pull/6)). Shown live on
  the console stack with the engine built at `d057935`:
  - console API test 33/33;
  - an engine probe 22/22: inspect, rotate, the old token 401 at once, 403/404 on job, cost and result, an
    admin listing every actor, the `keys.*` lines in the engine log, and no token in it;
  - the Rotate flow in headless Chrome 15/15.
  - ChatLPT `main` (583) and inference-gateway `main` (132) pass on this branch's code, unchanged.

## Decision log

- **26 October (Mike, D44), one decision for each suggestion in John's plan:**
  1. **Inspection:** adopted as proposed.
  2. **Rotation:** instant and atomic; the same kind, actor, application and permissions; an optional
     new label; `201`. No overlap window, and no re-scoping.
  3. **403 and 404:** permission first (403), then ownership (404), on every job and cost route,
     `result` and `submit` included.
  4. **Admin job list:** an admin lists every actor's jobs in its application, matching the cost reads.
  5. **Usage reports:** need `jobs.run` only. Ownership is 404.
  6. **Audit log:** structured INFO events with the acting admin, and never a secret. A full-lifecycle
     test covers `lpk_` and `lpg_`. No audit table.
  7. **Docs:** the example standalone flow goes in `access.md`; this plan; `milestones.md` corrected.
  8. **Console:** Inspect and Rotate key ship now, in a console PR pinned to this branch.
  9. **Base and ownership:** the PR targets `releases/m3`, independent of #46. Mike builds it and John
     reviews; the decisions are posted on #21.
