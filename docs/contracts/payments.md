# Payments and cost contract

Batteries and the livepeer/node remote signer are the payment path. Batteries is the authoritative
network-accounting ledger. The engine projects cost per job and never keeps a second ledger.

## Payment path

1. The SDK presents the actor's allocation key (`lpg_…`) to the signer as `Authorization: Bearer`.
2. The signer calls the Batteries webhook. Batteries resolves the key to its allocation, checks the
   balance, and returns an `auth_id` (the payment session).
3. The signer signs and **persists one `create_signed_ticket` event per signing request before
   returning payment** (livepeer/node durable outbox, at-least-once delivery). Batteries
   de-duplicates events by ID and debits the allocation.
4. The engine's runner call (`adapters/runner_call.py`, D53) answers the orchestrator's 402 itself
   and signs through the SDK's signer client. Before `get_payment`, `remember` stores
   `LivePaymentSession.manifest_id` on the attempt as an `unknown` session (D54); if that write fails,
   nothing is signed, and a cancel during it stores the session `unpaid`, since the signer was never
   asked. `on_signed` makes it `paid` once the signer validates the manifest, and marks
   the attempt paid before the paid request goes out. Batteries' `payment_session_id` is not known at
   sign time; cost sync fills it on `(allocation_id, manifest_id)`. An expired challenge (a signer
   480) and a 400, 401, 402, 403 or 481 refusal make the session `unpaid`. If the signer charged an
   unpaid manifest anyway, the debit is unattributed on the allocation and the customer is not
   billed. A connection the signer refused, or a signer name that did not resolve, is `unpaid` too:
   that request never reached the signer (D58, Q3). A lost reply, a timeout, a dropped connection, a
   signer 5xx, a cancel during `get_payment`, or the engine dying then leaves the session `unknown`:
   the cost stays `pending` until usage matches it, and the job pays no other orchestrator.

## Who answers what

A payment refusal is decided by Batteries, passed on by the signer, and reported by the engine as
`Failure.reason` (D58; [engine.md](engine.md#failures)). Verified at livepeer/node `406b248` (the pin on 29 October; `f45d83f` since D63) and
`e75e5b6` and Batteries `ca09e47`.

| Batteries decides (`internal/store/auth.go`) | The signer answers (`signer/auth.go`) | The engine reports | REST |
| --- | --- | --- | --- |
| 401: invalid or revoked key | 401, "signer auth rejected request with status 401" | `payment` / `not_authorized` | 403 |
| 403: grant or allocation inactive (paused included), outside its window, or a session binding mismatch | 403 | `payment` / `not_authorized` | 403 |
| 402: allocation exhausted, or no balance left | 402 | `payment` / `allowance_exhausted` | 402 |
| (Batteries unreachable, or its answer invalid) | 502 | while signing: `payment_unknown` / `signer_unknown`; at `/sign-orchestrator-info`: `payment` / `signer_unavailable` | 503 |
| (no answer) | connection refused, or no such host | `payment` / `signer_unavailable`: nothing was signed | 503 + `Retry-After: 10` |
| (no answer) | a timeout or a dropped connection while signing | `payment_unknown` / `signer_unknown`: it may have charged | 503 |

The engine's own `not_provisioned` (no allocation, or a revoked one) is checked before any of this and
answers 402 with no reason.

**Upstream limits, documented and not fixed** (both are servers, so neither is vendorable). The signer
keeps Batteries' status and drops its reason, so a **paused allocation reads as `not_authorized`**,
like an invalid binding; only the operator's view of the allocation tells them apart. And Batteries
being down reaches the engine as a signer 502, like any other signer fault.

**What builders never see.** No ordinary builder-facing body carries a payment key, the signer URL or the
signer's or Batteries' own text:

- a job's `failure.message` for a payment failure is the engine's own words, and `failure.body` is
  only ever the runner's; the SDK's text, with the signer URL and its body, goes to the
  `runner.signer_error` log line as `detail`;
- `GET /v1/allowance` answers a provider error with a fixed message and the provider's status
  ("provider HTTP 500: the allowance could not be read"); the provider's text goes to the
  `payments.allowance_failed` log line;
- the engine refuses to start with an `LPB_SIGNER_URL` that has userinfo, a query or a fragment, so
  the URL it logs holds no secret; a refused setting is never echoed, in any form of the refusal
  ([SECURITY.md](../SECURITY.md#secrets)).

## Allocation modes

`per_actor` is the only allocation mode (D82, 18 November 2026).

| Mode | Allocation | Who enforces a user's cap | Per-user spend |
| --- | --- | --- | --- |
| `per_actor` | One allocation and key per actor | The signer, at authorization | Exact: the allocation's own `spent_*` |

There is **no shared key**, so there is nothing to fall back to. A job for an actor with no allocation,
or with a revoked one, fails before payment with `not_provisioned` (HTTP 402), and no runner is called.
An application that wants one shared budget runs every job as a single actor.

**Removed in M4 (#162).** The `per_application` mode took one allocation and one key from the engine's
settings and paid every actor's jobs with it. Nobody enforced a cap per user, and one engine could serve
only one tenant. It is gone with its settings, `LPB_ALLOCATION_MODE`, `LPB_SHARED_PAYMENT_KEY` and
`LPB_SHARED_ALLOCATION_ID`, and with `allocation_mode` in `GET /readyz`. **Upgrading:**

- **From the environment.** The three variables name no setting and are ignored, like any variable the
  engine does not know. An engine that still has them set does not pay with the shared key: each job
  fails `not_provisioned` until its actor has an allocation.
- **In code.** `EngineSettings` refuses the three fields as unknown, and the error does not repeat
  their values.
- **What to do.** Allot an allocation for each actor, or run every job as one actor and allot
  that one ([Allotment](#allotment)).
- **Unpaid calls.** `per_application` with no key ran every job unpaid. Nothing replaces that: every job
  needs an allocation. An engine with no `LPB_SIGNER_URL` still sends unpaid calls, for allotted
  actors only.
- **What was paid before.** Attempts stored in that mode name the shared allocation, where
  `LPB_SHARED_ALLOCATION_ID` was set. It is no actor's allocation, so it is never the current one. It
  still reconciles when it is named, without an `actor_id`, over every actor of the tenant
  ([one allocation at a time](#cost-classification)).

## Tenants

A tenant is one enterprise app's account (D83, #165). Self-hosted payments has one tenant; a payment
clearinghouse has one for each enterprise app.

- **A tenant and its grant share one id.** Onboarding creates the grant at Batteries, and the id
  Batteries gives it is the tenant's id from then on. Nothing maps one to the other, there is no binding
  table, and no route or body takes a separate grant id. A body that still names one (`grant_id`) is a
  422, never a field the engine ignores while money moves.
- **One grant, for life.** It is prepaid and topped up as needed. A tenant that must stop is paused. The
  engine never closes a grant.
- **A tenant admin divides the grant and cannot enlarge it.** The kinds and what each may do are in
  [access.md](access.md#defaultpolicy). Another tenant's id in a path is 403, before any lookup or
  provider call.

| Route | Who may call | What it does |
| --- | --- | --- |
| `POST /v1/clearinghouse/tenants` | A clearinghouse admin | Onboards a tenant: creates its grant and answers `201` `OnboardedTenant`, the `tenant_id`, the `name` and the first tenant admin key (`admin_key`, an `IssuedKey`), whose token is shown once. Body: `name`, `amount` (the grant's first total; zero onboards a tenant to top up later), `operation_ref`, and optionally `admin_actor_id` (`tenant-admin`), the actor the key is issued to |
| `GET /v1/clearinghouse/tenants` | A clearinghouse admin | Every tenant with its grant and what it spent: `TenantGrant[]` |
| `POST /v1/clearinghouse/tenants/{tenant_id}/grant/top-up` | A clearinghouse admin | Adds to the tenant's grant. Body: `TopUpRequest` (`amount`, `operation_ref`). `204` |
| `POST /v1/clearinghouse/tenants/{tenant_id}/status` | A clearinghouse admin | Pauses or resumes the tenant by pausing or resuming its grant. Body: `status` (`active` or `paused`; anything else is a 422) and `operation_ref`. `200` `{tenant_id, status}` |
| `GET /v1/clearinghouse/tenants/{tenant_id}/grant` | That tenant's admin or auditor | `TenantGrant`: `tenant_id`, `name`, `status`, `total`, `unallocated` and `spent` |
| `GET /v1/clearinghouse/tenants/{tenant_id}/actors` | That tenant's admin or auditor | The tenant's actors and their allocations ([access.md](access.md#answers)) |
| `GET`, `POST .../actors/{actor_id}/allocation`, and `POST .../allocation/top-up`, `/status`, `/revoke`, `/rotate-payment-key` | That tenant's admin (the read: its auditor too) | [Allotment](#allotment) |
| `GET`, `POST .../tenants/{tenant_id}/keys`, `GET .../keys/{key_id}`, `POST .../keys/{key_id}/rotate`, `/revoke` | That tenant's admin (the reads: its auditor too) | The tenant's access keys ([access.md](access.md#a-tenants-access-keys)) |
| `GET /v1/clearinghouse/tenants/{tenant_id}/usage` | That tenant's admin or auditor | The tenant's payment records ([a tenant's usage](#a-tenants-usage)) |
| `GET /v1/clearinghouse/discovery` | Any credential of a tenant: its admin's or its auditor's key | The clearinghouse's runner list, in livepeer/node's format ([discovery.md](discovery.md#the-clearinghouses-runner-list)) |

A clearinghouse admin may call every route, for any tenant. The first four are what only a clearinghouse
admin does; they are one group in `http/clearinghouse.py` (`mount_clearinghouse_admin_routes`), which
#167 serves on a private listener. Today one listener serves everything.

**The tenant desk.** Onboarding, the tenant list, the grant read and the tenant's status use the grants
credential (`LPB_BATTERIES_GRANTS_TOKEN`: `grants.read`, `grants.create`, `grants.fund`, `grants.status`
and `allocations.read`). The token is optional. Without it the desk is off: `engine.payments.tenants` is
`None` and those four routes are absent. A grant's top-up uses `LPB_BATTERIES_TOKEN` (`grants.fund`) and
stays.

**The unallocated balance is a figure to read, never one the engine refuses by.** Batteries serves a
grant's total and no unallocated balance. The engine reads the grant, then the grant's allocations, and
answers the total less what those allocations hold (verified at Batteries v0.0.3,
`internal/store/manage.go`: allotting and topping up move an amount out of the grant, and a revoke
returns what was not spent). The two reads are moments apart, so a command that lands between them can
put the figure off by its amount; it is never below zero.

**What a tenant spent (#166).** `spent` is the tenant's payment usage as one figure: the sum of what its
allocations have spent, the revoked ones included. Batteries serves `spent_<currency>` on each allocation,
and keeps it on a revoked one, late charges too. The tenant list and the grant read already read the
allocations for the unallocated balance, so `spent` adds no request. The records behind the figure are
[a tenant's usage](#a-tenants-usage). A `spent` the engine cannot read is a `ProviderError`, never a zero.

**Batteries refuses what the grant cannot cover.** An allocation, or an allocation's top-up, larger than
the grant's unallocated balance is refused by Batteries: 409, with its reason, "insufficient unallocated
grant balance". The engine passes that on as a definite refusal, so the reference is free again and
nothing is stored. It reads no balance first and decides nothing itself, so it cannot race the provider's
ledger. The shipped fake (`FakeAllocationManager`) refuses the same way.

**A paused tenant.** Pausing sets the grant's status at Batteries, and what that stops is Batteries'
rule, not the engine's. At v0.0.3 (`internal/store/auth.go`) Batteries refuses every payment of a paused
grant's allocations (403, reported as `payment` / `not_authorized`,
[who answers what](#who-answers-what)). It still takes allotments, allocation top-ups, revokes and grant
top-ups on a paused grant (`internal/store/manage.go`), so the engine does too: it adds no check of its
own. Resuming sets the status back to `active`.

**A tenant that was never onboarded** has no grant. Allotting to it is 404 ("tenant ... has no grant at
the payment provider"), from Batteries' own 404, and so are its grant read, top-up and status.

### A tenant's usage

`GET /v1/clearinghouse/tenants/{tenant_id}/usage` returns the tenant's payment records, and none of
another tenant's (#166). It is what an enterprise's engine reads in place of Batteries' usage feed (the
`UsageSource` port, #168), so it answers with that port's own page, `UsagePage`, and not a summary of it.

| Field | What it holds |
| --- | --- |
| `items` | Usage rows, oldest first, each as cost sync stores one (`UsageRow`): `id`, `event_id`, `status` (`applied`, `quarantined`, `ignored`, `duplicate`), `allocation_id`, `manifest_id` (the payment's id at the orchestrator), `payment_session_id` (its id at Batteries), `request_id`, `pipeline` (the capability, in Batteries' word), `computed_fee_eth` and `computed_fee_usd` (exact decimals as strings; `null` where Batteries states none), `currency` and `created_at` |
| `set_aside` | The rows of the page the source could not read as a usage row, each with its `reason` and the fees it stated ([cost classification](#cost-classification)), so a reader records them as cost sync does |
| `next_cursor` | The cursor of the next page; `""` on the last |

`?cursor=` is the last page's `next_cursor`, and `?limit=` is 1 to 1000 rows (1000 when absent), as at
Batteries. Cost sync attributes a fee by `(allocation_id, manifest_id)` and then by `payment_session_id`
([correlation](#correlation)); every field it reads is here.

- **How one tenant's rows are chosen.** The engine sends Batteries' own `grant_id` filter with the
  tenant's id, which is the grant's id (D83). Batteries keeps a row when the payment session it was
  charged to belongs to an allocation of that grant (`internal/store/manage.go` at v0.0.3). The engine
  keeps no second copy and decides no row itself. A row with no payment session, which Batteries could
  tie to no allocation, is in no tenant's usage: only the unfiltered feed has it, and only the
  clearinghouse's own cost sync reads that.
- **A cursor is Batteries', and bound to its filter.** One tenant's cursor read under another tenant is
  refused: 400, `code: cursor_mismatch`, and no row. The route takes a cursor of at most 200 characters;
  Batteries' is about 135 for a grant id it issues.
- **Who may call.** That tenant's admin or auditor, and a clearinghouse admin for any tenant. The tenant
  is named to the access policy first, so another tenant's id is 403 before Batteries is asked. An actor
  key is 403.
- **When Batteries fails.** Unreachable is 503. An error status, or an answer the engine cannot read, is
  502 (Batteries' own 400, 404, 409 or 422 keeps its status). Never an empty page.
- **No orchestrator.** A Batteries usage row names no orchestrator at v0.0.3. Batteries keeps it on the
  payment session (`GET /v1/sessions`: `orchestrator`, `app`), and the route adds no call for each row to
  fetch it. An enterprise's engine has the orchestrator on its own attempt record, which cost sync joins
  the row to.
- **Records from before a tenant's id was its grant's id** are the grant's: the route reads the grant
  whose id the path names. A free-named tenant (`default`) is no grant, so its page is empty, and its
  allocations' records are read under the id of the grant they were allotted from
  ([below](#records-from-before-a-tenants-id-was-its-grants-id)).
- **Optional.** The route is present with a usage source that reads one tenant's records
  (`TenantUsageSource` in `protocols`, which `BatteriesUsageSource` has), and absent without one:
  `engine.payments.usage` is then `None`.

### Records from before a tenant's id was its grant's id

Before #165 a tenant's id was a free name (often `default`, from `LPB_JWT_TENANT_ID`), and each allotted
actor recorded the grant it was allotted from beside it (`lpb_actors.grant_id`). #165 rewrites and
deletes no row: there is no migration.

- **An existing allocation stays on its grant.** Top-up, status, revoke, payment key rotation, payments,
  the allowance and reconciliation name the allocation, and Batteries keeps each allocation on the grant
  it was allotted from. None of them sends a grant id, so none can move it. `ActorAllocation.grant_id`
  still shows the recorded grant.
- **Nothing new is allotted to such a tenant.** Its id is not a grant's id. Allotting a new actor is the
  404 above, and allotting again to a revoked actor whose record names another grant is refused before
  any provider call (400, "an allocation is never moved to another grant").
- **Its grant is reached by its own id.** `GET /v1/clearinghouse/tenants/{grant id}/grant`, `.../top-up`
  and `.../status` act on the grant, as the tenant it is. Actors allotted under that id are that
  tenant's, and are not the free-named tenant's.
- **To go on allotting,** onboard a tenant, or use the grant's id as the tenant's: issue keys for it, and
  set `LPB_JWT_TENANT_ID` (or the tenant claim) to it. The free-named tenant's actors, jobs, keys and
  costs stay where they are.

## Allotment

**There is no automatic allotment.** Every money-moving command is explicit and needs the
`admin.allocations` operation (an admin credential). Allocation and key writes carry an idempotency
key of `base64url(sha256(engine_id:operation:step))`.

Allotment refuses a `tenant_id`, `actor_id` or `label` that is longer than 200
characters or holds a NUL character (422 over REST), before any provider call: every record stores them
in 200 characters, PostgreSQL refuses a NUL, and the provider key is funded before the actor is saved.
The same limit applies to every id, label and reference a request carries (`StoredText`): a job's
`app`, `capability`, `model` and `operation_ref`, and an access key's `actor_id`, `tenant_id` and
`label`. Stored text also refuses a lone surrogate, half of a UTF-16 pair, which JSON can write
(`"\ud800"`) and no store can keep: a 422 over REST, which repeats it as that escape. It was a 500,
because the 422 could not be sent.

**A grant id is one segment of the provider's URL path (#113, C10).** A tenant's id is its grant's id,
and the grant routes (`.../tenants/{tenant_id}/grant`, `.../grant/top-up`, `.../status`) place it in the
provider's path. There the tenant id is a `GrantId`: stored text that also refuses `/`, `\`, `?`, `#`,
`%`, control characters and the values `.` and `..`, which a client or server would read as another path.
Whitespace around the id is removed before the check, so the value checked is the value the engine uses:
`" .."` is refused as `..`, and `" grant_1"` is used as `grant_1`. It is a 422 over REST before the
operation ledger records anything or the provider is called. It is held to no pattern such as a UUID:
the provider chooses its ids. Separately, the Batteries adapter percent-encodes every id it places in a
path, whatever its source (a grant, an allocation, a key, a created grant read back), so an id the
provider itself returned, or one an in-process caller passed, cannot name another resource either.

**The new key must have somewhere to go (#112, C2).** Allotment funds the provider key before the
actor is saved, so a store that cannot keep the key would leave a funded allocation with no record. Two
checks stop that:

- **At startup.** An engine with a payment provider and the built-in secret store refuses to start
  when that store cannot write, which is when `LPB_SECRET_KEY` is not set, and the error names the
  setting; `lpb purge` and `lpb bootstrap-admin`, which never allot, give the store a key for their run
  only (#106 review). The check runs on the parts the engine will run with, after `decorate`: a
  decorator that turns the provider off, or installs a store of its own, is not refused, and one that
  adds a provider is. Only the built-in store is asked at startup. A supplied store that cannot write
  at boot, such as a sealed vault, still starts and is asked per request.
- **Per request.** Allotment and key rotation ask the secret store whether it can write in the step
  that mints the key, just before the provider is called. The check is the store's optional
  `writable()` (`WritableSecretStore`); a supplied store that leaves it out is not asked. A `False` is
  `InvalidRequest` (HTTP 400).
  - On a first run nothing was sent to the provider and nothing was noted, so the reference stays
    free and the same command runs once the store can write.
  - A repeat of a finished command stores nothing and is not asked: it gets the recorded result.
  - A resume whose key is already stored is not asked either: it goes on, for a rotation to revoke the
    old key. The stored key must be the one of the provider call about to be sent: the record notes the
    call's idempotency key beside the secret's reference, and only a match skips the check. A resume
    after the actor was revoked, or allotted again, or under another `LPB_ENGINE_ID`, sends another
    provider call whose key is not stored yet, so it is asked before that call.
  - A record with the secret's reference and no idempotency key beside it was written by a release
    that did not yet note it (v0.2.x). A rotation resumed from such a record after the upgrade is not
    asked when its actor already holds the key and secret the record notes: the provider call is still
    sent and replays that key, nothing is left to store, and the old key is revoked. A record that has
    the note is never judged by what the actor holds.
  - A resume that still has a key to store is refused, and the message says that an earlier attempt
    may have reached the provider. It does not say that nothing was sent, and the refusal does not
    free the reference ([refused or unknown](#repeat-safe-commands)): the same request resumes once
    the store can write.

| Command | Provider call | Idempotency |
| --- | --- | --- |
| `allot(AllotRequest(tenant, actor, amount, operation_ref))` | `POST /v1/api-keys` with `grant_id`, the tenant's id (atomic: allocation + key + funding); then local work-engine storage or credential delivery | the [ledger](#repeat-safe-commands), and a provider key derived from the actor, so two references for one actor still meet at Batteries. An actor already allotted is 409 `already_allotted`; top it up instead |
| `top_up(…, TopUpRequest(amount, operation_ref))` | `POST /v1/allocations/{id}/fund` | the [ledger](#repeat-safe-commands), and `base64url(sha256(engine_id:fund:{allocation}:{operation_ref}:fund))` |
| `top_up_grant(tenant, TopUpRequest(amount, operation_ref))` | `POST /v1/grants/{tenant}/fund` | the ledger, and `base64url(sha256(engine_id:fund-grant:{tenant}:{operation_ref}:fund))` |
| `tenants.onboard(name, amount, operation_ref=…)` (the tenant desk, `payments.tenants`) | `POST /v1/grants` with `LPB_BATTERIES_GRANTS_TOKEN`, tagged `metadata.lpb_operation`; then the tenant admin key, an engine record | the ledger; Batteries has no key on grants, so a resume finds the tagged grant (Q5) |
| `tenants.list_tenants()`, `tenants.grant(tenant)` | `GET /v1/grants`, `GET /v1/grants/{tenant}` and `GET /v1/allocations` with `LPB_BATTERIES_GRANTS_TOKEN` | read |
| `tenants.set_status(tenant, active / paused, operation_ref=…)` | `PATCH /v1/grants/{tenant}/status` with `LPB_BATTERIES_GRANTS_TOKEN` | the ledger; the call itself sets a state |
| `set_status(active / paused)` | `PATCH /v1/allocations/{id}/status` | naturally repeatable |
| `revoke` | `POST /v1/allocations/{id}/revoke` (unused funds return to the grant) | repeat-safe upstream |
| `rotate_payment_key(operation_ref)` | `POST /v1/api-keys` with `allocation_id`, then revoke the old key; then local work-engine storage or credential delivery | the ledger, and `rotate:{allocation}:{operation_ref}` at the provider |

- **Zero amount** sets up identity without money. Batteries marks the allocation `exhausted` until an
  explicit top-up.
- **One command never undoes another on the same actor (#115, C4).** `allot`, `top_up`, `set_status`,
  `revoke` and `rotate_payment_key` read the actor, call the provider, then write. Each writes only its own
  columns, with its condition in the SQL statement:

  | Command | Writes | Only where |
  | --- | --- | --- |
  | `set_status`, `revoke` | `status`, `updated_at` | the actor is not `revoked` |
  | `rotate_payment_key` | `api_key_id`, `secret_ref`, `updated_at` | the actor is not `revoked` and still holds the key the rotation began from (or already holds the rotation's own) |
  | `top_up` | `status` = `active`, `updated_at` | the status is `exhausted` |
  | `allot` | the whole row | the actor is as allotment read it: it has no row, or it still has the `revoked` row being replaced |

  Every write also requires the allocation the command acted on, so none lands on an allocation
  allotted after a revoke. A key rotation also notes the allocation in its record when it begins:
  resumed after the actor was revoked and allotted again, it is refused before any provider call,
  404 with the two allocations named, and keeps its reference
  ([refused or unknown](#repeat-safe-commands)). The old key its record names belongs to the revoked
  allocation, so on the new one it would mint a key and leave the key that allocation had live. A
  record written before the allocation was noted is not checked.

  No command moves an actor out of `revoked`; only a new allotment gives the actor an allocation
  again. A top-up of an actor that is not `exhausted` writes nothing, `updated_at` included, and answers
  with the actor as stored. When a revoke finishes while another command is at
  the provider, the actor stays `revoked`: `set_status` and `rotate_payment_key` then answer 404 as for an actor
  that has no allocation (the rotation's new key is on a revoked allocation, and its secret is
  deleted), and a top-up, whose money was already sent, answers with the revoked actor. A revoke also
  deletes the secret of a key a rotation stored meanwhile. A revoke that another revoke overtook
  answers with the revoked actor as stored, as a repeat does. If the actor was allotted again
  before it wrote, it writes nothing and answers with the allocation it revoked, `revoked`: the
  answer to a revoke is never an active actor, and the new allocation is read with
  `GET /v1/clearinghouse/tenants/{tenant_id}/actors/{actor_id}/allocation`. Before, each of these commands saved the
  whole row it had read: a pause during a rotation put back the old key, which the rotation had just
  revoked, and any of them could put a revoked actor back to `active`.

  **An allotment that another command overtook.** Allotment is the one command that writes the
  whole row, so it writes only where the actor is still as it read it. Two allotments of one actor
  under two references send the same provider operation (its idempotency key is derived from the
  actor, under one `LPB_ENGINE_ID`), so both get one allocation and one key, and the first to write
  stores them. The other then writes nothing, whatever was done to the actor since, deletes its own
  copy of the key's secret, and answers from the actor as stored:

  | The actor as stored | Answer |
  | --- | --- |
  | holds the allocation and the key this allotment got | the actor, with the status it has now (a pause that finished meanwhile stays) |
  | holds that allocation, `revoked` | the actor, `revoked`: the allocation was allotted and then revoked, and it stays revoked, as for a top-up |
  | holds that allocation with another key (rotated meanwhile), or another allocation (revoked and allotted again) | 409 `already_allotted`, as below |
  | holds another allocation, `revoked` | 404: revoked while this allotment was at the provider. The record stays `started`; repeated after `LPB_OPERATION_STALE_S` it is a resumed allotment of a revoked actor |

  Before, it saved the row it had built from its first read: `active` on an allocation revoked or
  paused meanwhile, or the key a rotation had just revoked.

  **Two rotations of one actor's key.** A rotation writes its key only over the key it began from. When
  another rotation gave the actor a key first, whether it ran to its end while this one was at the
  provider or finished between this one's interrupted run and its resume, this one writes nothing and
  leaves nothing of its own live:

  1. it revokes its own new key, then deletes that key's secret. The revoke comes first, so a key that
     is still live still has its secret stored; when the revoke fails, the record stays `started` and a
     repeat, which gets the same key replayed, does it again;
  2. it revokes the key it began from and deletes that key's secret, unless the actor holds them. The
     actor no longer does, and for a rotation that stored its key, failed to revoke the old one and was
     then replaced by a newer rotation, no other command would;
  3. it notes in its record the key the actor holds, and answers `InvalidRequest` (HTTP 400): another
     key rotation finished after this one began.

  The refusal never frees the reference: under a free reference the provider would replay the revoked
  key to a new rotation. The record stays `started` with that note. A repeat inside
  `LPB_OPERATION_STALE_S` is 409 `operation_in_progress`; a later one is refused in the same words
  before any provider call. To replace the key again, rotate under a new reference. Before, the key
  write asked only for the same allocation: the slower rotation overwrote the finished one, whose key
  stayed live with its secret stored and nothing pointing at it, and a resumed rotation put back a key
  the newer one had revoked, with the reference of a deleted secret, so the actor could not pay.

  **What the guarantee covers, and the known gap.** It covers commands that write different columns,
  and the cases named above: a revoke, two rotations, an allotment that another command overtook.
  Two status commands at the same moment (a pause and an activate) write the same column, and the last
  write wins at the provider and in the store separately. Each sets the status at the provider and
  then stores it, so when a pause is at the provider while an activate runs to its end, the provider
  ends `paused` then `active` and the store `active` then `paused`: the stored status is the opposite
  of the provider's. Closing this needs a version on the row or a read-back of the provider's status,
  and #115 adds neither. Until then, send the status you want again: a repeated `set_status` sets it
  at the provider and in the store.

  The conditional writes are optional for a store (`ActorCommandRepo` in `protocols`:
  `set_actor_status`, `set_actor_key`, `allot_actor`). The packaged SQL store has them; a supplied
  store has all three or is treated as having none. With a supplied store without them, the engine
  reads the actor again after the provider call, checks the same conditions itself and saves the whole
  row with `save_actor`. The rules above then hold except in the moment between that read and the
  save, where a second command can still be overwritten, as it could before: for such a store two
  rotations that write at the same moment can still leave the slower one's key on the actor, and a
  allotment can still save over a command that wrote in that moment.
- **Amounts the provider takes (#112, C11).** The engine mirrors Batteries' amount format (v0.0.3,
  `internal/units/eth.go`): unsigned, with at most 18 decimal places, sent as a plain decimal string with
  no exponent (`format(amount, "f")`). It also sets its own bound, a fixed constant and not a setting: at
  most 1,000,000,000 (10^9) USD or ETH per command, since the provider's own (256 digits once scaled) is
  effectively none. Allotment takes zero or more (`AllotAmount`); topping up an allocation, topping up a
  grant and creating a grant take an amount within the format and bound (`TopUpAmount`) and still need
  it positive. An amount outside these is refused when the request is parsed: 422 over REST, a
  `ValidationError` when an in-process request is built, and `InvalidRequest` (400) from
  `tenants.onboard`, which takes the grant's amount, the tenant's name and its first admin's actor id
  itself and names the field, not the value (#106 review). Each comes before the operation ledger
  records anything and before any provider call. A zero or negative top-up or grant amount is still
  the command's own refusal, 400, unless it also breaks the format: one with more than 18
  decimal places (`-0.0000000000000000001`, `0E-19`) is refused when it is parsed, with the 422.
  **Upgrading:** resolve any `started` operation whose amount is above the bound or has more than
  18 decimal places before moving to this release. Its repeat is refused when it is parsed, before
  the ledger is read, so it can no longer be replayed or resumed through that request.
- **The new key** goes straight into the `SecretStore`. The database stores only its `SecretRef`,
  which is opaque text of any length (#115): a supplied secret store may return a long path.
- **Which grant.** The tenant's: a tenant's id is its grant's id ([Tenants](#tenants)), and no request
  names a grant. Before #165 the request named it, or the grant of the caller's own allocation was used;
  the caller's allocation chooses nothing now. Batteries still applies `grants.fund` to every grant on the
  instance, so the engine draws the line: only a clearinghouse admin tops up a grant
  (`admin.tenants`), and a tenant admin allots from its own tenant's grant alone.
- **The engine's management credential** is least-privilege: `allocations.*`, `api_keys.*`,
  `usage.read`, and `grants.fund`. It has **no `grants.*`**.
- **Creating, reading and pausing a grant** use `LPB_BATTERIES_GRANTS_TOKEN`
  ([the tenant desk](#tenants)). Topping up grants works with `LPB_BATTERIES_TOKEN`.
- **Cost sync** reads `GET /v1/usage` with no grant filter. Rows for allocations this engine did not
  allot are stored and are not part of any actor's reconciliation. A cursor saved under the old
  `grant_id` filter does not match, so the first sync after this change restarts from the beginning
  and skips rows it already stored.

## Payment keys

The work engine presents the allocation's Batteries credential directly to the signer.
There is no clearinghouse signer proxy and no separate payment token.

The clearinghouse route factories default to credential delivery. Allotment and rotation return
`DeliveredAllocation`: allocation metadata plus `allocation_api_key`, shown only in the issuing
response, with `Cache-Control: no-store`. The clearinghouse retains identifiers and an opaque
`delivered:` receipt in `secret_ref`, never a usable allocation credential. The request-local
buffer is cleared after success, failure or cancellation. Reads and ledger records contain no token.
A completed repeat returns `allocation_api_key: null`; if the response was lost, explicitly rotate
under a new operation reference. A resumed command may also return no credential. It never mints
another allocation simply to recover a lost response.

The embedded methods `allot_for_delivery` and `rotate_for_delivery` implement this handoff.
Existing `allot` and `rotate_payment_key` keep local work-engine storage for self-hosted use.
The generated OpenAPI describes the standalone delivery response. The combined `create_app` mounts routes with `deliver_allocation_keys=False`, preserving that mode;
standalone `mount_clearinghouse_routes` and `mount_tenant_routes` default to `True`.
P6/P8 must preserve this distinction when selecting listeners and roles, and apply the writable
secret-store startup requirement only where the local work engine persists allocation keys. P7 implements the remote
adapter that receives the credential, persists it in the work engine and resolves it for the SDK;
this PR does not implement those later tickets.

The signer must be reachable by the work engine and configured with Batteries authorization.
The SDK calls `/sign-orchestrator-info` and `/generate-live-payment` directly at the signer origin.
The engine still checks `jobs.run`; no new access-key permission is introduced. Existing guarded
allocation writes and provider idempotency remain. A delayed credential handoff cannot replace a
newer allocation's key because it writes no additional credential row.

## Repeat-safe commands

Each admin command that moves money or creates a credential takes a caller-chosen `operation_ref`,
scoped to the caller `(tenant_id, actor_id)` (D57, ISS-11). The engine records it in the operation
ledger (`lpb_operations`, unique per scope and reference) **before the first provider call**, with a
fingerprint of the command, its target and the validated request (the reference excluded), and stores the
public result when the command ends. A repeat is answered from that record and never applies twice.
Provider calls keep their derived idempotency keys, so a resumed command replays them at Batteries.

| The reference… | Answer |
| --- | --- |
| is new | Runs the command |
| was used with another command or request | 409 `OperationConflict`, `code: operation_conflict`, before any provider call |
| names a command that ended | The recorded result, with the original status and `Idempotent-Replayed: true` |
| names a command still `started`, for less than `LPB_OPERATION_STALE_S` (120 s) | 409 `OperationInProgress`, `code: operation_in_progress`, `Retry-After`: the seconds until a repeat may resume it |
| names a command still `started` for longer | **Resumes** it: runs every step again from the top; each step is safe to repeat |

The fingerprint and each provider idempotency key are digests, so the text they hash is a format, not a
name: it keeps the words it was first written with (`provision`, `fund`), whatever a command or a field
is called now. A command recorded before a rename is therefore still replayed, or resumed without
applying twice, when it is repeated after it (#163, `tests/unit/test_hashed_forms.py`). The same holds
for a grant's top-up since #165 moved the grant from the body to the path: the hashed text is the same.
What a repeat of an allotment or of a grant's creation recorded before #165 gets is under
[Upgrading to tenants at the clearinghouse](index.md#upgrading-to-tenants-at-the-clearinghouse).

A 409 for an admin command carries `operation: {command, operation_ref, state}` instead of a job, and
no `Livepeer-Job-Id`. In embedded mode the replay is the return value; `OPERATION_REPLAYED` (a context
variable in `livepeer_builder.contracts`) is `True` right after a call answered from the ledger.

- **Refused or unknown.** A command refused outright (an invalid request, an unknown actor or grant, a
  provider 4xx other than 408) before any step applied anything: the engine deletes its record, so the
  caller may fix the request and repeat it under the same reference. A refusal after a step applied (a
  rotation's new key stored, then the old key's revoke refused) leaves the record `started`, so a resume
  finishes the command from what the record noted. A timeout, a transport error, a provider 5xx or a
  crash leaves the outcome *unknown*: the record stays `started` for a resume, never a retry under a
  new reference. So does a provider answer the engine cannot read (below). A refusal on a *resumed*
  run never frees the reference either (#112): the attempt it follows ended in an unknown outcome, and
  a provider call that timed out may have been applied with nothing noted. The record stays `started`,
  another request on that reference is still 409 `operation_conflict`, and the same request resumes
  again after `LPB_OPERATION_STALE_S`. The one exception is `already_allotted`, below.
- **A provider answer the engine cannot read (#114, C21).** One strict reader takes every Batteries
  answer that is not an error status. It must be a JSON object: a body that is not JSON, not UTF-8, or
  nested deeper than the decoder reads (that one answered 500) is none. A list page must have `items` as
  a list of JSON objects and `next_cursor` as a string, or no `next_cursor`. Anything else is a
  `ProviderError`: HTTP 502 from the REST service, `invalid_answer` as `cost_sync_error`, and an unknown
  outcome to the ledger, so the reference stays held. Before, a 2xx that was not a JSON object answered
  500, and a page with no `items` read as an empty last page. An empty 2xx body was read as `{}`; it is
  now refused too, since Batteries answers every management route with a JSON object. A redirect (3xx)
  is no answer either, whatever its body: the engine follows none, and it is the same `ProviderError`
  with an unknown outcome (as `cost_sync_error` it is `unavailable`). A malformed page
  never reads as an empty one, and one malformed item fails its page. A page whose `next_cursor` is the
  cursor it was asked with is the same `ProviderError` ("cursor did not advance"), from a grant list, a
  resumed create's search and cost sync alike: before, the engine asked for that page again without end,
  and the grant list (`GET /v1/clearinghouse/tenants` since #165) and `POST /v1/costs/sync` never answered. A longer cycle (a cursor that
  comes back after other pages) is not detected. A grant, in a list or read alone,
  must have a non-empty string `id`, and its `name`, `status`, `currency`, total and `metadata` must be
  strings where present: an object that is not that (`{}`, an error object, `metadata` as `null` or as a
  JSON object) is the same `ProviderError`, never a grant with no tag, and nothing is turned into text
  (`null` was read as the name `"None"`). `metadata` that is free text, or `""`, is a grant the engine did
  not create. The same holds for every value a command's result is built from: the `id` a grant create
  answers with, the `id`, `allocation_id` and `api_key` of a new key, and an allocation's `id` must each
  be a non-empty string, and an allocation's `status` and `currency` strings where present. Before, a
  `null` there was recorded as the text `"None"` in a finished command (allotment answered 201 with
  `allocation_id: "None"`), and an answer that lacked the key answered 500. The error does not quote the
  body. A usage item that is an object the engine cannot read as a usage row is not malformed in this
  sense: it is set aside ([cost classification](#cost-classification)).
- **Fencing.** A resume takes the record over with a compare-and-set that bumps its `fencing_token`.
  Every later write of the slower run (its context, its result, its abandonment) checks the token and
  is refused, so a run presumed dead that was only slow answers with the recorded result instead. A
  repeat that loses the race to resume answers from the record as it is then: the result once done,
  else 409 with `Retry-After` counted from the winner's start.
- **A resume is a lease, not a lock.** Fencing guards the engine's records, not a provider call already
  in flight, and grant creation has no provider idempotency key. So every run of a command is cancelled
  at half `LPB_OPERATION_STALE_S` (its outcome then unknown, the record `started`), and no run is still
  calling Batteries when a repeat resumes it. Each Batteries call has one deadline of
  `LPB_BATTERIES_TIMEOUT_S` for the whole request (httpx alone times each phase separately), and the
  engine refuses to start unless `LPB_OPERATION_STALE_S` is at least four of them, so the longest
  ordinary command (two calls) fits in a run's half. Its limit is a process paused
  longer than the window, which only a provider idempotency key on grant creation would close.
- **Rotating the payment key** records the old key's id and secret reference when it begins, with the
  allocation, and every step reads them from there, never from the actor as it is now. A resume after the new key was stored
  still revokes the old one, and a repeat never revokes the live key (B1). The new key's secret
  reference is recorded before the actor points at it, so a resume reuses that copy. A rotation that
  another rotation of the same actor overtook, or that is resumed on an allocation allotted since
  it began, is refused and keeps its reference ([two rotations](#allotment)).
- **Allotting** to an actor that already has an allocation (not revoked) under a new reference is refused:
  409 `code: already_allotted` with the actor, and nothing is applied, the amount included (top it up
  instead). The refusal frees the reference. A resumed run answers with the actor only if the actor
  holds the provider key its earlier run recorded: that allocation is this command's (the provider key
  is derived from the actor, so another reference may have had the same key replayed). Otherwise, a
  resume that never got that far or an actor revoked and allotted again under another reference,
  it is refused the same way and the reference is freed. A second allotment after a revoke is a new
  command: it needs a new reference.
- **Onboarding a tenant** is two steps on one record: create the grant, then issue the tenant's first
  admin key.
  - **Its scope.** The record is scoped to the caller and the reference, as every command's is, so it
    exists before Batteries is asked: the tenant has no id until Batteries answers with the grant's. The
    record notes that id (`tenant_id`) as soon as it is known, and the key's id (`admin_key_id`) before
    the key is stored.
  - **The grant, once.** Batteries has no idempotency key on `POST /v1/grants`, so the engine tags each
    grant it creates with `metadata` `{"lpb_operation": <a key derived from the engine, the caller and the
    reference>}` (Batteries stores metadata as text, so the tag is sent as a JSON object in that string). A
    resumed onboarding that has no tenant id noted reads the grants page by page, oldest first, and adopts
    the one carrying its tag instead of creating a second. It records the list cursor after each page, so a
    resume that runs out of its budget in a slow catalog leaves the next one to go on from there; pages
    already read lacked the tag, and a grant created later is on a later page. A list answer the engine
    cannot read fails the resume as an unknown outcome and never counts as "no such grant": before #114
    (C21), a 2xx page with no `items` read as an empty last page, and a list item that was no grant (`{}`,
    or the grant with its `metadata` as `null` or as an object) read as a grant with no tag, and the resume
    created a second grant. A read-back of the new grant that the engine cannot read is an unknown outcome
    too (503, the reference held), never the command's result. Tenant names stay free-form: two references
    may onboard two tenants with one name.
  - **The key, once, and its token shown once.** The token is in the answer of the run that issued the
    key and nowhere else: the engine keeps its hash, and the record holds the key without it. A repeat of
    a finished onboarding answers `201` with the same tenant and key, **`"token": null`** and
    `"replayed": true`, as a repeated key issue does ([access.md](access.md#repeated-key-commands)). So
    does a resume after the key was stored (the engine stopped between storing the key and finishing the
    record): it finds the key by its noted id and issues no second one. A clearinghouse admin who lost
    the token issues another tenant admin key (`POST .../tenants/{tenant_id}/keys`, kind `tenant_admin`).
  - With engine access keys off (`LPB_AUTH=jwt`) the grant is created and `admin_key` is `null`.
- **A tenant's status** takes a reference although the provider call sets a state: a late repeat of a
  pause is answered from its record, so it never pauses again a tenant that was resumed since.
- **Access keys** are written in the same transaction as their record, so they are never `started`:
  a replay returns the key with `token: null` ([access.md](access.md#repeated-key-commands)).
- **A payment key's token** is never in a record either. An allotment's and a rotation's recorded
  result is the allocation alone, so a replay answers `payment_key` with `token: null` and
  `replayed: true` ([payment keys](#payment-keys)).
- **Lifetime.** Ledger records are kept; there is no purge in M3.

Which commands take a reference:

| Command | `operation_ref` |
| --- | --- |
| Allot, top up allocation, top up grant, onboard a tenant, rotate payment key | **required** (422 when missing): moves money or creates a provider credential |
| Set a tenant's status | **required**: stops or restarts every payment of a tenant |
| Issue access key, rotate access key | **required**: creates a live engine credential |
| Set status, revoke allocation, revoke access key | not accepted: set-to-state or repeat-safe, a repeat cannot apply twice. A repeated set-status sets the same status at the provider again and answers with the actor in that status; after a revoke it is `404`, as for an actor that is not allotted. A repeated revoke answers as the first did: the actor, `revoked`, with no provider call; `204` for a key (D61). An actor or key that never existed keeps its `404` or `400` |

## Allowance, signer funding and spending guarantees

Three different things, often all called "funding":

| Term | What it is | Who moves it | Repeat-safe |
| --- | --- | --- | --- |
| **Allowance change** | An admin command moves value between a grant and an allocation in Batteries (allot, top up allocation, top up grant, onboard a tenant). Bookkeeping in the payment operator's ledger | The engine, on an admin's command | Yes, per `operation_ref` ([above](#repeat-safe-commands)) |
| **Signer funding** | The payer's TicketBroker deposit and reserve on Arbitrum, which back the tickets the signer signs | The payment operator, outside the engine | No engine command touches it |
| **Spending guarantee** | A hard limit no job can exceed | Nobody in M3 | n/a: the allowance is a cap that [can overshoot](#allowance); hard limits are stretch scope |

## Units

`Money(amount: Decimal, currency: "USD" | "ETH")` everywhere. **USD grants are the default**
(`LPB_DEFAULT_CURRENCY`). An allocation inherits its grant's currency. Reported cost carries
`fee_eth` (always present, because signer events require it) and `fee_usd` (when every row is
priced). Batteries amounts are exact decimal strings; the engine stores them as strings and sums
them as `Decimal`.

## Wholesale and retail

Two prices exist, and the engine handles one of them.

| | Wholesale (network) | Retail |
| --- | --- | --- |
| What it is | What the Livepeer network charged: tickets the signer signed for orchestrators | What an application charges its own users |
| Who sets it | Orchestrators advertise rates; Batteries accounts the charges | The application |
| Where the engine shows it | Before a job: `Offering.rate_low`/`rate_high` ([engine.md](engine.md#rates-iss-10)). After: `JobCost.fees` and `billable`, from Batteries accounting ([Cost classification](#cost-classification)) | Nowhere. The engine never computes, stores or applies a retail price, margin or invoice (core belief 1) |

An application turns a delivered job's `billable` fees, or its own price list, into a retail charge in
its own records. The engine's figures are inputs to that decision, not the decision.

## Pricing assumptions

What a rate means, and what it does not:

1. **A rate is a price per unit, exactly as advertised.** It is not a quote for a job, and the engine
   derives no other price from it: no currency conversion, no job total, no margin.
2. **The unit decides the total.** A metered rate (`seconds`) costs the rate times the time the session
   is funded, so a job's total is known only after it ends. A `fixed` rate costs one price per paid
   request.
3. **Failover can pay more than once.** A job may pay up to `LPB_MAX_PAID_ATTEMPTS` orchestrators (D52,
   D55); the attempts it failed over from are `failed_after_payment`.
4. **The ceiling is the advertised rate plus 1.2 %.** The engine asks the signer to pay at most the
   runner's advertised wei rate times 1.012 (`_PRICE_PADDING` in `adapters/runner_price.py`), and the
   signer refuses a higher price (481).
5. **The engine never signs without a ceiling the signer takes.** A runner that asks for payment but
   advertised no usable rate is refused before the signer is asked (`payment_refused`, nothing paid; the
   job fails over). No usable rate is `rate is None` (none advertised, or an unreadable `price` /
   `currency` / `unit`, or a `price` above `0` but below `1e-300`, or from `1e300`), a free rate (`0`),
   or a rate in any currency and unit but `wei` per `seconds` or `fixed`. The currency and the unit are
   checked when the runner asks for payment, not before: a runner that answers without a 402 is not
   refused for them. Paid jobs try runners with a usable rate first ([engine.md](engine.md#behavior-the-contract-fixes)). A `price_usd` the reader cannot hold is absent on `price_info`. The wei
   price is still what is paid when `payable_rate` accepts it. The node signer checks no price at all when it gets no ceiling, and refuses (400) one that is
   not a positive `wei` price in the payment type's unit.
6. **Tickets are probabilistic.** The reported cost is the expected value at signing. A single job's
   settled on-chain cost can differ; settlement is an operator-level figure.
7. **`price_usd` is the advertiser's figure, not a conversion.** livepeer-node advertises the runner's
   registered USD price per the same unit. It is shown for reading only: the wei `amount` is what is paid.
   go-livepeer advertises no `price_usd`, so it is `null` for its runners.
8. **A rate has an age.** Each served rate carries `observed_at` and `observed_by`; an offering is
   `stale` when its snapshot or its oldest rate is too old (`LPB_RATE_MAX_AGE_S`). Staleness is
   information, never a payment gate.

## Correlation

A charge joins an attempt by **`(allocation_id, payment_session_id)`**. The engine does not learn
`payment_session_id` from the SDK. Each attempt records the allocation whose key paid and one
session-log entry per `manifest_id` it asked the signer for (`lpb_payment_sessions`), with its
`payment_state`: `unknown`, `paid` or `unpaid` (D54). Cost sync resolves an entry when exactly one
applied usage row-set and one log entry share `(allocation_id, manifest_id)`, and stores that row's
`payment_session_id` as `matched`; that also confirms an `unknown` entry as `paid`. An `unpaid` entry
is never matched.

If Batteries has no session for the key yet, the entry stays `unmatched` and the attempt's cost is
`pending`. A signed session whose usage has not arrived keeps the attempt `pending`, so the job is
not stored `observed` with a zero bill. If more than one Batteries session or more than one log
entry shares the key, the entry is `ambiguous` and those rows stay `unattributed`. An ambiguous
signed session makes the attempt `uncertain` once nothing on that attempt is still waiting for
usage; a sibling session that already matched keeps its fee on the attempt.
Reconciliation still balances. A later sync can turn a `matched` entry into `ambiguous`, and every
sync checks `ambiguous` keys again: when the signer refuses one of the entries, the one left matches.

`manifest_id` is the lookup, not the cost key. The 7 October run showed a client can put any string
there ([evidence](../references/evidence/2026-10-07-arbitrum-run.md)). A manifest on another
allocation does not attribute.

The orchestrator mints a fresh manifest for each 402 challenge and reserves the runner session under it,
so the orchestrator's session ID is the `manifest_id` the engine records
([sdk-gateway.md](../references/sdk-gateway.md#runner-session-stop-83)). The manifest is 4 random bytes
(8 hex characters) from Go's `math/rand`, and the orchestrator refuses a duplicate only while the first
session is live. Two sessions on one allocation can therefore share a manifest, and the chance grows
with the allocation's volume: about 1% by 10,000 sessions. The match then turns `ambiguous`, including
for an older entry that had matched (T13, T16,
[livepeer-python-gateway#74](https://github.com/livepeer/livepeer-python-gateway/issues/74)).

**A manifest id the engine cannot record is never signed for (#114, C13).** The orchestrator chooses
the `manifest_id` of its payment challenge, and the engine stores it before the signer is asked (D54).
A challenge whose `manifest_id` is longer than 200 characters, or holds a NUL, another control
character (U+0000 to U+001F, U+007F to U+009F) or a lone surrogate, is refused before signing as
`payment_refused`: nothing is stored, signed or paid, and the job fails over to another orchestrator.
Before, PostgreSQL refused the intent write and the job ended `payment` with no failover, and SQLite
stored the id and paid.

## Cost classification

Each attempt that opened a payment session is classified:

| Class | Rule | Typical enterprise treatment |
| --- | --- | --- |
| `delivered` | The attempt succeeded | Bill under its retail policy |
| `failed_after_payment` | Payment went out and the attempt failed (refused, runner error, **rejected tickets**), including an attempt the job failed over from, even when a later one delivered (D52) | Absorb; don't bill |
| `uncertain` | The attempt that left the job `uncertain`, or an attempt that never reported back | Hold, then reconcile |
| `pending` | The job is still running (its fees so far are not final), or payment went out and no applied usage row has arrived yet | Wait. **Never zero** |

Only `applied` usage rows count. **Rows on an allocation that match no recorded attempt are
`unattributed`.**

**Usage rows cost sync sets aside (#114, C13).** One usage row the engine cannot read or store no
longer stops the sync. Such a row failed its page on every pass, so the cursor never moved and no later
cost arrived; on PostgreSQL one long or NUL-bearing id was enough. The sync now leaves the row out,
records it and moves the cursor:

| Reason | The row |
| --- | --- |
| `missing_id` | has no `id` (absent, `null`, empty, or not a string or a whole number; a whole number is kept as its digits) |
| `unknown_status` | has a `status` other than `applied`, `quarantined`, `ignored`, `duplicate` |
| `unreadable_created_at` | has no `created_at_ms`, or one that is not a JSON number (a string of digits and `true` are not) or that no clock holds |
| `unreadable_fee` | states a `computed_fee_eth` or `computed_fee_usd` that is not a finite decimal number: `NaN`, `Infinity`, or no number at all (`"abc"`, `"0,5"`, `true`, an object). `null`, `""` and `"[error]"` state no fee: Batteries serves them for a fee it has not or could not convert, and such a row is stored with no fee |
| `unreadable_row` | passes the checks above and is still refused by the usage row's own type. A backstop: no known row reaches it. Also the code for another `UsageSource` whose row fits no other reason |
| `unstorable_<field>` | has in `<field>` a value the store's column cannot keep: one that is not text (a number, `true`, a list or an object in a text field; `null` and `""` are no value), or a text longer than the column (200 characters for an id, 64 for a fee, 8 for `currency`), with a NUL, or with a lone surrogate. Checked before the store is asked, so SQLite and PostgreSQL do the same, whatever the `UsageSource` |

Each value is judged as it was received, and that value is the one stored: apart from a whole-number
`id`, the engine turns no number, list or object into text.

The reasons are a closed set (`SetAsideReason` in `contracts`): a record with any other code is refused
where it is built. The record is one row of `lpb_usage_set_aside`: the provider's row id, the reason, and
`computed_fee_eth` and `computed_fee_usd` as the provider stated them, whatever the row's status. A fee
that is not a finite number, or longer than the column, is recorded as `null`. A row with no id the
table can keep is recorded under `sha256:` and a digest of its content, so such a row whose content
differs on every read (a field the provider fills in as it serves the row) is recorded once per read.
A row is recorded, counted and logged once, however often its page is read again; the first record of
an id is kept. The row's content goes only to the log: one WARNING `costs.usage_set_aside` with the id,
the reason, the fees and up to 2000 characters of the row as received (for a row another `UsageSource`
delivered as a `UsageRow` and the store cannot keep, the row as that source read it).
`SyncReport.rows_set_aside` counts the rows a sync recorded, and `EngineHealth.cost_sync_set_aside`
(`GET /readyz`) the rows left out in all, as of the last sync, including one that failed after it set
rows aside.

Recording the rows is optional for a store (`UsageSetAsideRepo` in `protocols`). The packaged SQL
store has it. A supplied store without it still syncs: such a row is left out and logged once per
process, `rows_set_aside` still counts it, and `cost_sync_set_aside` stays `null`.

A set-aside row is in no job's cost and not in `unattributed`: if it was a charge, reconciliation is
not `balanced` until an operator accounts for it. Nothing reads the table yet; `lpb costs verify`
(ISS-35) will name these rows ([T29](../exec-plans/tech-debt-tracker.md)). A usage page whose item is
not a JSON object still fails the page.

**`JobCost.fees` and `JobCost.billable`.** `fees` is what every attempt of the job cost the allocation.
`billable` is only the `delivered` attempts' fees: what the enterprise bills its user for. A job that
failed has `billable` zero, and a job that failed over and then succeeded bills only the attempt that
delivered, while its allocation was debited for both (D52). Both are final once the cost status is
`observed` or `corrected`.

**Reconciliation:** provider `spent` = `delivered` + `failed_after_payment` + `uncertain` +
`pending` (fees matched to jobs still running) + `unattributed`. `GET /v1/costs/reconciliation?tenant_id=...` returns each class and `balanced`; the tenant is required (#163 review). Each
actor has its own allocation, so it balances exactly per user.

**One allocation at a time (#115, C9).** The provider reports `spent` per allocation, and an actor
revoked and allotted again has a new one. The engine side therefore counts only the attempts the
reconciled allocation paid (each attempt records its allocation when it starts), plus the unattributed
rows on that allocation. Before, it counted every job the actor ever ran against the current
allocation, so after a second allotment reconciliation never balanced again.

- **The default** is the actor's current allocation (`allocation_id` in the answer).
- **An earlier allocation** is reconciled by naming it: `costs.reconcile(admin, tenant_id,
  actor_id, allocation_id)`, or `?allocation_id=` on the route (stored text: over 200 characters or a
  NUL is 422, and so is an empty value, which names no allocation and is not read as the current
  one). Batteries still reports `spent_*` for a revoked allocation, late charges after the
  revocation included (`API.md`, v0.0.3), so an earlier allocation reconciles like the current one.
- **`other_allocations`** in the answer lists the actor's other allocations, sorted: the current one,
  and every earlier one that paid an attempt. It is empty for an actor that had one allocation.
- **An allocation that is not the actor's** (it is not the current one and paid none of the actor's
  attempts) is 404, so a caller who may read one tenant never reads what another's allocation
  spent. An earlier allocation that paid no attempt is not known to the engine: the actor's record
  keeps only the current one.
- **Without `actor_id`** no allocation is the current one, since an allocation is an actor's, so the
  call is 404 unless it names one. Any allocation that paid an attempt in the tenant can be
  named: the answer is 200 with `actor_id: null`, it counts the attempts that allocation paid, and
  `other_allocations` lists every other allocation that paid an attempt in the tenant. There is
  still no single figure for a whole tenant: each allocation reconciles on its own.

Which allocation paid each attempt is an optional read of a store's cost snapshot
(`AllocationCostSnapshot` in `protocols`: `attempt_allocations`). The packaged SQL store has it. With a
supplied store without it, reconciliation works as it did before: every attempt of the actor is
counted against the current allocation, `other_allocations` is empty, and naming an earlier allocation
is 404.

**Network cost is the expected value at signing.** Tickets the orchestrator rejects after signing are
still charged, because node has no void event. The operator's wallet loses nothing on-chain, since
rejected tickets are never redeemed. The engine reports these charges as `failed_after_payment`.
Settlement (winning-ticket redemption) is a separate operator-level figure and never per job.

### Job cost status

`none` (the job ended and no attempt recorded a session), `pending` (the job is still running, or a
recorded session lacks rows), `observed`, `corrected` (the total changed after it was first observed, for
example a metered session that signed again).

Only cost sync stores projections (`lpb_cost_projections`), and only for jobs that have ended: a
running job may still pay, so a projection of it would go stale (#33). Every read (`for_job`,
`recent`, `summary`, reconciliation) computes live any job no projection covers yet, running or ended
since its projection was stored, from its matched usage rows. A paid job therefore never reads as
`none`, even between syncs. Each read uses one consistent snapshot of the store, so a sync committing
meanwhile can never make it count a usage row twice or nowhere.

Sync commits usage rows, then session matches, then projections, one transaction each, and two syncs
can run at once (the loop and `POST /v1/costs/sync`). Each job has an evidence version: a new
usage row on a matched session, a changed match or a late session report increments it in the same
transaction. A projection records the version it was computed from, read before the job's evidence,
and stands for the job only while that version is current; a save never replaces a projection computed
from newer evidence. Reads compute an out-of-date job live, and the next sync stores it again, even
when a sync stopped (a crash, an error) before it reached that job, or a concurrent sync saved an older
classification. A state between two of sync's commits therefore still counts every row once.

## Attribution

A cost reaches a job, and from the job its attempts, through engine records only (D60, ISS-18):

- **`JobCost`** (`GET /v1/jobs/{id}/cost`, `GET /v1/costs/projections`) names the job by `job_id` and
  carries `fees`, `billable` and one `AttemptCost` per attempt that opened a payment session:
  `attempt_number`, `cost_class` and that attempt's `fees`.
- **The job** (`GET /v1/jobs/{id}`) says who and what: `tenant_id`, `actor_id`, `app`,
  `capability`, `model`, and the app-reported `usage`.
- **Its attempts** (`job_id`, `number`; `number` is the `attempt_number` above) say who was paid and
  from what: `allocation_id` (the allocation whose key paid), `orchestrator_url` (the orchestrator the
  tickets went to), `runner_url`, and the signed `sessions` (`manifest_id`, and `payment_session_id` once
  cost sync [matched](#correlation) it).

Units, on every field:

| Field | Unit |
| --- | --- |
| `fee_eth` | ETH, an exact `Decimal` (a JSON string). Always present: signer events carry it |
| `fee_usd` | USD, an exact `Decimal`. `null` when an applied row lacks a USD price; `0` when there are no rows |
| `row_count` | Applied Batteries usage rows counted in the fees |
| `MeasuredUsage.quantities` | The app's own units (`tokens`, `seconds`, ...), integers, per job; never summed across apps |
| `CostSummary.start`, `end` | UTC instants; the window holds the jobs **created** in `[start, end)`. Without `end` it ends at the engine's `Clock` now; without `start` it starts 30 days before `end`. Over REST a `start` or `end` is converted to UTC when the request is parsed (one with no offset is read as UTC) and answered in UTC; one that UTC cannot hold is a 422 |
| `jobs_with_cost`, `jobs_pending` | Jobs: `observed` or `corrected`, and `pending` |

**The summary** (`GET /v1/costs/summary`) totals the jobs in its window and scope (an actor's own; an
admin's or auditor's whole tenant, or one actor with `?actor_id=`) by class (`by_class`), and the
same attempt fees by runner app and class (`by_app`, D60): "what did each app cost, and how much of
it is settled". `by_app[app][class]` keeps classes apart as `by_class` does, so a final fee and a
`pending` one that can still grow are never added into one figure; for every class, the apps add up to
its `by_class` total. An app appears once one of its jobs has a costed attempt, with only the classes
its attempts have. There is no
breakdown by orchestrator (an operator's view: each attempt's `orchestrator_url`, and
[reconciliation](#cost-classification)) and no usage total, because usage units differ per capability.

## Allowance

`GET /v1/allowance` reads the allocation (`allocated_*`, `spent_*`, `available_*` in its currency).
The allowance is a cap that **can overshoot**: Batteries authorizes on a positive balance and does
not reserve funds, so accounting lag can take a balance slightly negative. Guaranteed hard limits are
stretch scope.

## Payment configurations

A payment configuration is who runs Batteries, the signer and the wallet. There are two (ISS-31, D82).
The design is the [ISS-31 guide](../design-docs/m4-guides/iss-31-payment-configurations.md).

| Configuration | Who runs Batteries, the signer and the wallet | State |
| --- | --- | --- |
| Self-hosted payments | The enterprise, for itself: one deployment with one tenant | Built (M3, ISS-13). The rest of this contract describes it |
| Payment clearinghouse | A clearinghouse, for many enterprise apps. Its engine does payments only and never runs jobs. Each enterprise app runs its jobs in its own engine, and reaches the clearinghouse only for signing, allotment, allowance and usage | Not built yet. M4 builds it as #162 to #169 |

Both use `per_actor` allocations ([Allocation modes](#allocation-modes)) and the tenant rules
([Tenants](#tenants)). Of the routes an enterprise's engine calls at a clearinghouse, two are built
(#166): [a tenant's usage](#a-tenants-usage) and the
[runner list](discovery.md#the-clearinghouses-runner-list). The key the third one takes is built too
([payment keys](#payment-keys)); signing goes directly to the signer.

The engine's routes are two sets (D84, #164). The **clearinghouse routes** are tenants and their grants,
a tenant's actors and their allocations, and a tenant's access keys and usage, all under
`/v1/clearinghouse/tenants` since #165 ([Tenants](#tenants)), and the runner list at
`/v1/clearinghouse/discovery`. The
**work routes** are everything else: jobs, runners and the catalog, the cost reads with reconciliation
(`GET /v1/costs/reconciliation`) and cost sync (`POST /v1/costs/sync`), the allowance read, and an
engine's own access keys (`/v1/keys`). With self-hosted payments one deployment serves both sets. The
old paths of the routes that moved are under
[Upgrading to the work routes and the clearinghouse routes](index.md#upgrading-to-the-work-routes-and-the-clearinghouse-routes).

### Mode B

"Mode B" was the M2 name for the second configuration: a hosted payment operator, one Batteries
instance serving many enterprises (D23). "Mode A" was self-hosted payments. The gap D23 recorded is
that Batteries management credentials are global (a permission covers every grant), so an operator
cannot safely give an engine a management credential.

D74 (13 November 2026) answered it with a separately operated engine in `per_application` mode. That
was not built. D82 (18 November 2026) replaces it with the payment clearinghouse, which needs no scoped
management credential at the enterprise: only the clearinghouse reaches Batteries management.
Under the 19 November direct-signer amendment, the work engine holds its allocation credential and
reaches the signer directly.

### Provisioning

"Provisioning" was the name of allotment until 18 November 2026 (D83, #163): an admin allots an
allocation to an actor, where the engine said it provisioned the actor. See [Allotment](#allotment).
The failure kind `not_provisioned` keeps its name: the failure table belongs to ISS-28. Every name that
changed with it is under
[Upgrading to the domain-language names](index.md#upgrading-to-the-domain-language-names).
