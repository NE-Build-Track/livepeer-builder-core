# Payments and cost contract

**Ticket:** ISS-04 · **Decisions:** D17–D24 (12 October 2026), D82, D83, D85 and D87 (18 November 2026) ·
**Code:** `contracts/payments.py`, `contracts/costs.py`, `services/payments.py`, `services/payment_keys.py`,
`services/allocation_delivery.py`, `services/tenants.py`, `services/tenant_usage.py`, `services/costs.py`,
`adapters/batteries.py`, `adapters/batteries_usage_rows.py`, `http/clearinghouse.py`, `http/delivered.py`

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

**What builders never see.** No builder-facing body carries a payment key, the signer URL or the
signer's or Batteries' own text:

- on a self-hosted engine, allotment and key rotation return the allocation, never the `lpg_` key, which
  is encrypted at rest. On a standalone clearinghouse their answer hands the key to the enterprise's
  engine, once ([payment keys](#payment-keys)). That answer is an admin's, never a builder's, and no read,
  list, record or log line carries the key;
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
  [access.md](access.md#defaultpolicy).
- **A tenant's routes come in two forms** (D87 point 3, as Mike amended it on 19 November 2026). Its
  admin and auditor call them at paths that name no tenant: the tenant is the credential's, and a
  clearinghouse admin, which has no tenant of its own, is 403 there. The clearinghouse admin calls the
  same routes at paths that name the tenant, under `/v1/clearinghouse/tenants/{tenant_id}`, and a tenant
  credential is 403 there, its own tenant's path included, before any lookup or provider call. Both
  forms call the same service with the tenant they resolve, so their answers are the same. No route lets
  one tenant's credential name another tenant: on its own routes another tenant's actor or key, named by
  its id, is missing.

| Route | Who may call | What it does |
| --- | --- | --- |
| `POST /v1/clearinghouse/tenants` | A clearinghouse admin | Onboards a tenant: creates its grant and answers `201` `OnboardedTenant`, the `tenant_id`, the `name` and the first tenant admin key (`admin_key`, an `IssuedKey`), whose token is shown once. Body: `name`, `amount` (the grant's first total; zero onboards a tenant to top up later), `operation_ref`, and optionally `admin_actor_id` (`tenant-admin`), the actor the key is issued to |
| `GET /v1/clearinghouse/tenants` | A clearinghouse admin | Every tenant with its grant and what it spent: `TenantGrant[]` |
| `POST /v1/clearinghouse/tenants/{tenant_id}/grant/top-up` | A clearinghouse admin | Adds to the tenant's grant. Body: `TopUpRequest` (`amount`, `operation_ref`). `204` |
| `POST /v1/clearinghouse/tenants/{tenant_id}/status` | A clearinghouse admin | Pauses or resumes the tenant by pausing or resuming its grant. Body: `status` (`active` or `paused`; anything else is a 422) and `operation_ref`. `200` `{tenant_id, status}` |
| `GET /v1/clearinghouse/wallet` | A clearinghouse admin | The wallet's deposit and reserve, the total of unspent grants and the signer's readiness ([the wallet report](#the-wallet-report)) |
| `GET /v1/clearinghouse/grant` | The tenant's admin or auditor | `TenantGrant`: `tenant_id`, `name`, `status`, `total`, `unallocated`, `spent` and `allocated_unspent` |
| `GET /v1/clearinghouse/actors` | The tenant's admin or auditor | The tenant's actors and their allocations ([access.md](access.md#answers)) |
| `GET`, `POST /v1/clearinghouse/actors/{actor_id}/allocation`, and `POST .../allocation/top-up`, `/status`, `/revoke`, `/rotate-payment-key` | The tenant's admin (the read: its auditor too) | [Allotment](#allotment). Each answers the allocation (`ActorAllocation`). On a standalone clearinghouse, allotting (`201`) and rotating the payment key (`200`) also hand over the allocation's key, once (`allocation_api_key`, with `replayed` and `resumed`); on a self-hosted engine the engine stores the key and the answer has none ([payment keys](#payment-keys)) |
| `GET`, `POST /v1/clearinghouse/keys`, `GET .../keys/{key_id}`, `POST .../keys/{key_id}/rotate`, `/revoke` | The tenant's admin (the reads: its auditor too) | The tenant's access keys ([access.md](access.md#a-tenants-access-keys)) |
| `GET /v1/clearinghouse/usage` | The tenant's admin or auditor | The tenant's payment records ([a tenant's usage](#a-tenants-usage)) |
| Each of the five rows above under `/v1/clearinghouse/tenants/{tenant_id}`: `.../grant`, `.../actors`, `.../actors/{actor_id}/allocation` and its commands, `.../keys` and its commands, `.../usage` | A clearinghouse admin | The same, for the tenant the path names. A `tenant_admin` key is issued here (`POST .../keys` with kind `tenant_admin`). On a standalone clearinghouse an allotment or a rotation here hands the allocation's key to the clearinghouse admin who called, once, to pass to the tenant: the clearinghouse stores none ([payment keys](#payment-keys)) |
| `GET /v1/clearinghouse/discovery` | Any credential of a tenant: its admin's or its auditor's key | The clearinghouse's runner list, in livepeer/node's format ([discovery.md](discovery.md#the-clearinghouses-runner-list)) |

The first five rows are what only a clearinghouse admin does; they are one group in
`http/clearinghouse.py` (`mount_clearinghouse_admin_routes`). A tenant's routes are two more groups, one
for each form (`mount_tenant_routes`, which names no tenant, and `mount_named_tenant_routes`), and the
runner list is a group of its own. A deployment with a private listener (`LPB_HTTP_PRIVATE_PORT`, #167)
serves the clearinghouse admin's groups, the paths that name a tenant among them, there only: they are
404 on the public listener, and a clearinghouse admin credential is 403 on every route there. A
tenant's own routes are served on the public listener only. The runner list is served on both
([access.md](access.md#listeners)). With one listener, which is the default and what self-hosted
payments runs, every route is served there, and each form of a tenant's routes takes the kinds it takes
on two listeners.

There is no payments route, and a clearinghouse serves no signer path: an enterprise's engine signs at
the signer itself, with the allocation's key ([payment keys](#payment-keys)).

**The tenant desk.** Onboarding, the tenant list, the grant read and the tenant's status use the grants
credential (`LPB_BATTERIES_GRANTS_TOKEN`: `grants.read`, `grants.create`, `grants.fund`, `grants.status`,
`allocations.read` and, for [the wallet report](#the-wallet-report), `escrow.read`). The token is optional. Without it the desk is off: `engine.payments.tenants` is
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

**What a tenant's allocations still hold (#167).** `allocated_unspent` is the sum, over the tenant's
allocations, of what each was allotted less what it has spent, and nothing for an allocation that has
spent more. Batteries charges after it authorizes and reserves nothing, so an allocation can spend more
than it holds (verified at Batteries v0.0.3, `internal/store/ingest.go`: its balance goes below zero and
it becomes `exhausted`). Such an allocation holds nothing, and its overdraft takes nothing off what
another allocation holds or off the unallocated balance. A paused allocation counts: it can be resumed.
A revoked one holds nothing: the revoke returned what it had not spent to the grant and took it off the
allocation's `allocated` amount (`internal/store/manage.go`), so that money is in `unallocated` and is
counted there only, and a charge that lands later raises `spent` and nothing else. `unallocated` and
`allocated_unspent` together are what the tenant may still spend, which
[the wallet report](#the-wallet-report) sums. The figure comes from the allocations the two reads
already make, at no further request, and from the same `allocated` and `spent` amounts: one the engine
cannot read is a `ProviderError`, never a zero.

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

`GET /v1/clearinghouse/usage` returns the tenant's payment records to its admin or auditor, the tenant
being the credential's, and `GET /v1/clearinghouse/tenants/{tenant_id}/usage` returns them to a
clearinghouse admin. Neither returns another tenant's (#166). It is what an enterprise's engine reads in place of Batteries' usage feed (the
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
- **A row the engine cannot read is set aside, here as on the cost-sync page.** One reader takes every
  Batteries usage item, for both pages, so the reasons are the same closed set
  ([cost classification](#cost-classification)): a fee that is not a decimal string as Batteries writes
  one is `unreadable_fee`, a blank id is `missing_id`, and an item the JSON decoder's limits refuse is
  `unreadable_row`, under a digest of its text and with no fee. The answer is 200: the row is in
  `set_aside`, with at most 2000 characters of its content as `raw`, and the page's other rows are in
  `items`. The clearinghouse records and logs nothing of such a row: its reader does, as cost sync does
  for its own page. A page that is not JSON, or whose `items` or `next_cursor` the engine cannot read,
  is 502. So is a page whose own object names a key twice, with such an item or not: read with the last
  of the two, the rows of a first `items` list would be dropped and the cursor followed past them. And
  an item is left out only when it is JSON that the decoder refuses for its size: the decoder stops at
  the limit it meets, so the engine reads the rest of such an item itself, and one that is no JSON
  after its long integer leaves its page no page. A page that begins with a byte order mark is read as
  the decoder reads it.
- **A cursor is Batteries', and bound to its filter.** One tenant's cursor read under another tenant is
  refused: 400, `code: cursor_mismatch`, and no row. The route takes a cursor of at most 200 characters;
  Batteries' is about 135 for a grant id it issues.
- **A cursor that does not advance.** A page whose `next_cursor` is the cursor it was asked with is 502
  ("the usage cursor did not advance"), and none of its rows is served: a reader that followed it would
  ask for that page without end. The route reads one page for each request and keeps nothing between
  two, so it cannot tell a cursor that comes back after other pages (A, B, A): each of those pages is
  served as Batteries answered it. The reader sees every cursor of its pass and finds the repeat, as
  cost sync does for the feed it reads.
- **Who may call.** The tenant's admin or auditor at `GET /v1/clearinghouse/usage`, for the tenant of
  its credential, and a clearinghouse admin for any tenant at the path that names it. A tenant
  credential at a path that names a tenant is 403, and a clearinghouse admin at the path that names
  none, before Batteries is asked. The tenant is named to the access policy first. An actor key is 403.
  A credential whose tenant id is no grant id is 403 here and at the grant read, as the path that names
  a tenant refuses such an id with 422: the id goes into Batteries' URL path, and a key may carry one a
  grant id may not.
- **When Batteries fails.** Unreachable is 503. An error status, or an answer the engine cannot read, is
  502 (Batteries' own 400, 404, 409 or 422 keeps its status). Never an empty page.
- **No orchestrator.** A Batteries usage row names no orchestrator at v0.0.3. Batteries keeps it on the
  payment session (`GET /v1/sessions`: `orchestrator`, `app`), and the route adds no call for each row to
  fetch it. An enterprise's engine has the orchestrator on its own attempt record, which cost sync joins
  the row to.
- **Records from before a tenant's id was its grant's id** are the grant's: the route reads the grant
  whose id the path, or the credential, names. A free-named tenant (`default`) is no grant, so its page is empty, and its
  allocations' records are read under the id of the grant they were allotted from
  ([below](#records-from-before-a-tenants-id-was-its-grants-id)).
- **Optional.** The route is present with a usage source that reads one tenant's records
  (`TenantUsageSource` in `protocols`, which `BatteriesUsageSource` has), and absent without one:
  `engine.payments.usage` is then `None`.

### The wallet report

Each clearinghouse has one wallet. Its deposit stays above the total of unspent grants, with a margin
for the ETH price, and the clearinghouse admin watches it and funds it by hand (D82). The report
(`GET /v1/clearinghouse/wallet`, `engine.payments.wallet.report`, #167) states the three things that
sentence needs. It converts nothing between ETH and a grant's currency, computes no margin, refuses
nothing by these figures and sends no alert: a person judges.

Only a clearinghouse admin reads it (`admin.tenants`): a tenant admin, an auditor and an actor are 403.
It is in the private listener's group, so a deployment with two listeners serves it on the private one
only ([access.md](access.md#listeners)).

```json
{
  "escrow": [
    {
      "chain_id": "42161",
      "contract_address": "0xc0c0…",
      "sender": "0x5e5e…",
      "deposit": {"amount": "1.5", "currency": "ETH"},
      "reserve": {"amount": "0.25", "currency": "ETH"},
      "confirmed_through_block": 412
    }
  ],
  "unspent_grants": [
    {"unspent": {"amount": "1077", "currency": "USD"}, "grants": 3}
  ],
  "signer": {"ready": true, "reason": null}
}
```

| Field | Unit | Source | What it says |
| --- | --- | --- | --- |
| `escrow[]` | | Batteries, `GET /v1/escrow/report` (`escrow.read`, with the grants credential) | One account for each chain, TicketBroker contract and sender that Batteries' on-chain listener follows. A clearinghouse has one wallet, so one account |
| `escrow[].chain_id`, `contract_address`, `sender` | | The same | The chain, the TicketBroker contract and the wallet's public address, as Batteries reports them |
| `escrow[].deposit`, `reserve` | ETH, exact to the wei | The same | The wallet's TicketBroker deposit and reserve, as Batteries' listener has confirmed them from the chain's events |
| `escrow[].confirmed_through_block` | A block number | The same | How far the listener has read. The amounts are as of that block, not the chain's head: Batteries waits for confirmations (64 blocks by default) and polls (every 5 s by default). `-1` is a first snapshot from before the chain's first block. There is no timestamp |
| `unspent_grants[]` | | Batteries, the grants and their allocations (`grants.read`, `allocations.read`), as the tenant list reads them | One entry for each currency a grant is in, by currency |
| `unspent_grants[].unspent` | The grants' currency (`USD` or `ETH`) | The same | The sum, over every grant Batteries holds in that currency, of the grant's `unallocated` and its `allocated_unspent` ([tenants](#tenants)) |
| `unspent_grants[].grants` | A count | The same | How many grants were summed |
| `signer.ready` | | The signer, `POST /sign-orchestrator-info` at the root of `LPB_SIGNER_URL` | Whether the signer answered its identity call just now |
| `signer.reason` | | The same | `null` when ready. `not_configured`: the engine has no `LPB_SIGNER_URL`. `unavailable`: the signer could not be reached, timed out or answered a 5xx. `no_identity`: something answered, but not with a signer's address and a signature |

**What "unspent" sums.** A grant's unspent amount is what is not yet allotted (`unallocated`) and what
is allotted and not yet spent (`allocated_unspent`). Both are the tenant's to spend, so the wallet may
still have to pay for both. Every grant counts, whatever its status: a paused grant can be resumed. It
is not the grant's total less what was spent. Batteries charges after it authorizes, so an allocation
can spend more than it held. That overdraft is charged already, and it takes nothing off what another
allocation holds or off what the grant has left to allot: a grant of 100 with 60 allotted, of which 70
was spent, still has 40 to allot and spend, and counts as 40, not 30. A figure that is too low is the
unsafe one, since the deposit is held above this total. Neither part of a grant counts below zero, so a
balance stated below zero hides nothing: not another grant's unspent amount, and not what the same
grant's allocations still hold. The figure is read as the tenant list is (T31): the grants, then every
allocation, so a command that lands between the reads can put it off by its amount.

**The deposit and reserve need Batteries' on-chain listener.** Batteries records escrow only with
`--enable-onchain-listener`, its own RPC address and the signer's address, and the listener is off by
default. With it off, Batteries reports no account and `escrow` is `[]`: never a deposit of zero. The
engine reads no chain, holds no RPC address and gains no setting for one.

**What "ready" proves.** The identity call is the one every paid job begins with. It needs no
credential, signs no payment and costs nothing, and `ready` says the signer is reachable where the engine
calls it and answered with an address and a signature. The engine does not check that signature: the
answer does not carry what was signed. `ready` does not say the wallet is funded, that the signer's
price feed or event stream is up, or that the signer can reach Batteries: those are the signer's own
`/readyz`, which livepeer/node serves on a separate metrics listener the engine is not given (T34). The
payment SDK remembers a signer's identity for the life of the process; the report asks the signer each
time and not that memory.

**Failures.** The three are read one after another and are not one moment. A Batteries that fails a
read fails the report (503 when it cannot be reached, 502 for an answer the engine cannot read): no
part is served as if the rest were known, and an amount the engine cannot read is never a zero. A deposit
or a reserve below zero is such an amount, and is never served as a balance. A grants
credential without `escrow.read` (Batteries' 403) is a 502 whose message names the permission. A grants
token Batteries does not know or has revoked (its 401) is a 502 in Batteries' own words, with no
permission named: none would mend it. A signer that does not answer is an answer, `ready: false`, not an
error.

**What is never in it.** No key material, no keystore path and no RPC address. The wallet's public
address is there because Batteries reports it.

**When the route is absent.** It needs a grants manager that reports the escrow (`EscrowSource`, which
`BatteriesClient` has) and a runner transport that can ask the signer (`SignerProbe`, which
`SdkRunnerTransport` has). Without the grants credential, or with a supplied part that has neither, the
engine serves no wallet report: `engine.payments.wallet` is `None` and the route is 404.

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
and the grant routes (`.../tenants/{tenant_id}/grant`, `.../grant/top-up`, `.../status`, and
`.../tenants/{tenant_id}/usage`) place it in the provider's path. On a tenant's own routes
(`GET /v1/clearinghouse/grant` and `/usage`) the tenant is the credential's, as its key holds it, and no
path carries it. In a path the tenant id is a `GrantId`: stored text that also refuses `/`, `\`, `?`, `#`,
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

- **At startup.** An engine that stores allocation keys, one in its `both` or its `work` role, with a
  payment provider and the built-in secret store refuses to start when that store cannot write, which
  is when `LPB_SECRET_KEY` is not set, and the error names the setting. An engine in its
  `clearinghouse` role stores none and is not asked (#169); `lpb purge` and `lpb bootstrap-admin`,
  which never allot, give the store a key for their run only (#106 review). The check runs on the parts the engine will run with, after `decorate`: a
  decorator that turns the provider off, or installs a store of its own, is not refused, and one that
  adds a provider is. Only the built-in store is asked at startup. A supplied store that cannot write
  at boot, such as a sealed vault, still starts and is asked per request.
- **Per request.** Allotment and key rotation ask the secret store whether it can write in the step
  that mints the key, just before the provider is called. The check is the store's optional
  `writable()` (`WritableSecretStore`); a supplied store that leaves it out is not asked. A `False` is
  `InvalidRequest` (HTTP 400). A command that hands its key over (`allot_for_delivery`,
  `rotate_for_delivery`) stores nothing and is not asked.
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
| `allot(AllotRequest(tenant, actor, amount, operation_ref))`, which stores the new key, or `allot_for_delivery(...)`, which hands it to the caller ([payment keys](#payment-keys)) | `POST /v1/api-keys` with `grant_id`, the tenant's id (atomic: allocation + key + funding) | the [ledger](#repeat-safe-commands), and a provider key derived from the actor, so two references for one actor still meet at Batteries. An actor already allotted is 409 `already_allotted`; top it up instead |
| `top_up(…, TopUpRequest(amount, operation_ref))` | `POST /v1/allocations/{id}/fund` | the [ledger](#repeat-safe-commands), and `base64url(sha256(engine_id:fund:{allocation}:{operation_ref}:fund))` |
| `top_up_grant(tenant, TopUpRequest(amount, operation_ref))` | `POST /v1/grants/{tenant}/fund` | the ledger, and `base64url(sha256(engine_id:fund-grant:{tenant}:{operation_ref}:fund))` |
| `tenants.onboard(name, amount, operation_ref=…)` (the tenant desk, `payments.tenants`) | `POST /v1/grants` with `LPB_BATTERIES_GRANTS_TOKEN`, tagged `metadata.lpb_operation`; then the tenant admin key, an engine record | the ledger; Batteries has no key on grants, so a resume finds the tagged grant (Q5) |
| `tenants.list_tenants()`, `tenants.grant(tenant)` | `GET /v1/grants`, `GET /v1/grants/{tenant}` and `GET /v1/allocations` with `LPB_BATTERIES_GRANTS_TOKEN` | read |
| `tenants.set_status(tenant, active / paused, operation_ref=…)` | `PATCH /v1/grants/{tenant}/status` with `LPB_BATTERIES_GRANTS_TOKEN` | the ledger; the call itself sets a state |
| `set_status(active / paused)` | `PATCH /v1/allocations/{id}/status` | naturally repeatable |
| `revoke` | `POST /v1/allocations/{id}/revoke` (unused funds return to the grant) | repeat-safe upstream |
| `rotate_payment_key(operation_ref)`, which stores the new key, or `rotate_for_delivery(operation_ref)`, which hands it to the caller | `POST /v1/api-keys` with `allocation_id`, then revoke the old key | the ledger, and `rotate:{allocation}:{operation_ref}` at the provider |

- **Zero amount** sets up identity without money. Batteries marks the allocation `exhausted` until an
  explicit top-up.
- **A resume names the allocation as the first run did.** An allotment is saved with a label: the
  request's `label`, else the name the person last signed in with (D48), else none. Batteries is sent
  that label as the allocation's name, or `{tenant_id}/{actor_id}` when there is none. The label is
  noted in the command's record as the record begins, before the provider call (`sent_label` in its
  context), and every run of the command sends the noted one and saves the actor with it, whatever the
  person's sign-in name is by then. It matters because Batteries replays a saved success only to the
  request it was first given: another name under the first run's idempotency key is its 409, every
  time. So a person who signs in for the first time, or under a new name, between a run that funded
  the allocation and the resume that saves the actor does not stop the resume. A record written before
  the label was noted has none, and its resume reads the name again: it finishes when the name is
  what the first run sent, and is refused by Batteries (409) when it is not, keeping its reference.
- **One command never undoes another on the same actor (#115, C4).** `allot`, `top_up`, `set_status`,
  `revoke` and `rotate_payment_key` read the actor, call the provider, then write. Each writes only its own
  columns, with its condition in the SQL statement:

  | Command | Writes | Only where |
  | --- | --- | --- |
  | `set_status`, `revoke` | `status`, `updated_at` | the actor is not `revoked` |
  | `rotate_payment_key` | `api_key_id`, `secret_ref`, `credential_custody`, `updated_at` | the actor is not `revoked` and still holds the key the rotation began from (or already holds the rotation's own) |
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
  the actor's allocation route (`GET /v1/clearinghouse/actors/{actor_id}/allocation`, or under
  `/v1/clearinghouse/tenants/{tenant_id}` for a clearinghouse admin). Before, each of these commands saved the
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
  which is opaque text of any length (#115): a supplied secret store may return a long path. It has
  to be text every database keeps, so the engine checks the reference the store returns before it
  notes it (#158): one with a NUL, which PostgreSQL stores in no text, or with a lone surrogate, which
  no database stores, is deleted from the store again, and the allotment or the rotation fails with
  nothing noted and no actor written. That is no refusal: the provider has minted the key, so the
  command's reference stays held, and the answer is HTTP 500, as when a supplied store raises an
  error of its own. The repeat finishes once the store returns text: Batteries replays the key. A
  payment clearinghouse handed it over once, so there the repeat is answered as
  [a key that never arrived](#the-key-an-enterprises-engine-is-handed), with the same way out. When
  the store fails the delete too, its error is the command's answer and the copy stays in the store,
  under a reference nothing names. A key
  handed to an enterprise's engine goes into no store: the actor then holds a receipt in place of a
  store handle, and `credential_custody` says which it is ([payment keys](#payment-keys)).
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

A payment key is the allocation's own Batteries key (`lpg_`): the bearer an engine presents to the
signer to pay for one actor's work ([payment path](#payment-path)). Allotment mints it and a rotation
replaces it. There is no second payment credential: a clearinghouse issues no key of its own and serves
no signer path (D85 point 1 and D87 point 1, as Mike amended them on 19 and 23 November 2026; #166).

The engine that runs an actor's jobs keeps that actor's key:

| | Self-hosted payments: the `both` role | A standalone clearinghouse: the `clearinghouse` role |
| --- | --- | --- |
| Keeps the key | The engine, in its `SecretStore` | The enterprise's engine. The clearinghouse keeps none |
| The commands | `payments.allot`, `payments.rotate_payment_key` | `payments.allot_for_delivery`, `payments.rotate_for_delivery` |
| They answer | `ActorAllocation` | `DeliveredAllocation`: the allocation, and `allocation_api_key`, `replayed` and `resumed` |
| `credential_custody` on the allocation | `secret_store` | `work_engine` |
| The routes are mounted by | `create_app`, the app `lpb serve` runs, with `deliver_allocation_keys=False`, for one listener and for each of two ([access.md](access.md#listeners)) | `create_app` too, with `deliver_allocation_keys=True`, for each of its two listeners. A caller that mounts the routes itself says which: `mount_clearinghouse_routes(app, engine, deliver_allocation_keys=...)` has no default |
| The published schema | [openapi.json](../generated/openapi.json) | [openapi-clearinghouse.json](../generated/openapi-clearinghouse.json) |
| Signs at the signer | The engine, with the stored key | The enterprise's engine, with the key it was handed |

The engine's role chooses the column (`LPB_ROLE`, #169): `delivers_allocation_keys(role)` in
`http/listeners.py` is the one place that decides, and `create_app` passes its answer to every app it
builds. An engine in its `work` role mounts none of these routes: it is the enterprise's engine, which
stores a handed key and pays with it ([the remote adapters](#the-remote-adapters), #168). A clearinghouse
needs no `LPB_SECRET_KEY`, since it stores no allocation's key; the storing commands, which only an
embedding caller reaches there, are refused before the provider is called when its store cannot write.

- **Custody is recorded, never read from a reference.** `ActorAllocation.credential_custody`
  (`lpb_actors.credential_custody`, migration 0015) says who holds the key that `secret_ref` names.
  `secret_store`: the engine's configured `SecretStore` issued the reference, and resolves and deletes
  it. `work_engine`: the key was handed to the enterprise's engine, and the reference is a receipt that
  no store resolves. Every answer and read of an allocation carries the field. Nothing is inferred from
  the text of a reference, since a supplied store may issue any handle. An allocation written before
  migration 0015 is `secret_store`. The operation ledger notes the custody beside each secret reference
  it records, and every delete follows it: a store's handle is deleted on the store that issued it, and a
  receipt never reaches a store.
- **The handover.** On a standalone clearinghouse `POST .../actors/{actor_id}/allocation` answers `201`
  and `POST .../allocation/rotate-payment-key` answers `200`, each with the allocation, every field
  where it was, and three more:

  | Field | What it holds |
  | --- | --- |
  | `allocation_api_key` | The key (`lpg_...`), or `null` |
  | `replayed` | `true` exactly when `allocation_api_key` is `null`. The enterprise's engine checks it before it stores anything |
  | `resumed` | `true` when this call finished a run that an earlier attempt left unfinished |

  The answers to allotment and rotation carry `Cache-Control: no-store`, on a self-hosted engine too.
- **The clearinghouse keeps no key.** While a delivering command runs, the key is in a buffer of that one
  call (`DeliverySecrets` in `services/allocation_delivery.py`), which is emptied when the call ends,
  answered or failed. It reaches no secret store, no table, no ledger record and no log line, and two
  commands at the same moment share no buffer. `DeliveredAllocation.allocation_api_key` is a `SecretStr`,
  so a dump or a log of the model shows it masked; only the route's answer writes it out
  (`http/delivered.py`). The clearinghouse cannot pay for such an actor: its own `CredentialResolver`
  answers `NotProvisioned` (HTTP 402, "allocation credential is held by the work engine") and asks no
  store.
- **A repeat of a finished command hands over nothing.** It is answered from the ledger, whose record
  holds the allocation alone: the original status, `allocation_api_key: null`, `replayed: true` and
  `Idempotent-Replayed: true` ([repeat-safe commands](#repeat-safe-commands)). An admin whose engine
  lost the key rotates it, under a new reference.
- **A resumed command hands the key over** (Mike, 23 November 2026). A run that stopped before it
  finished answered nobody. The repeat that resumes it sends its provider call again under the
  idempotency key the first run recorded, and Batteries replays the original success, the secret
  included, and never expires it ([batteries-api.md](../references/batteries-api.md)). Nothing is
  allotted, funded or minted twice, and the answer carries that key with `resumed: true` and
  `replayed: false`, without `Idempotent-Replayed`. An allotment whose first run had already saved the
  actor still sends that call, only to be handed the key again.
- **Two delivering allotments of one actor under two references,** at the provider at the same moment,
  get one allocation and one key from Batteries. Each answer carries that key only while the actor
  still holds it. The allotment that did not write the row answers from the actor as stored, by the
  table under [an allotment that another command overtook](#allotment): an actor rotated or allotted
  again since is 409 `already_allotted`, and one revoked since is answered as the revoked actor, or 404,
  with no key. For either allotment, the answer of an actor that has moved past the key carries none
  (next). An allotment under a new reference that begins after the actor is allotted is 409
  `already_allotted`. When the two are one of each kind, the allotment that wrote the row keeps the key:
  a delivering one's caller is handed it, and the other answers the allocation, `work_engine`; a storing
  one (`allot`) keeps it in the store, and the delivering one's answer has `replayed: true` and no key.
- **Never the key of an allocation the actor has moved past.** Just before it answers, a delivering
  command reads the actor again (`delivered()`). The answer carries the key only while the actor is not
  `revoked` and still holds the allocation, the key and the receipt the answer names. An actor that was
  revoked, rotated or allotted again meanwhile gets `allocation_api_key: null` and `replayed: true`,
  without `Idempotent-Replayed`: the command ran, and its key is no longer the actor's.
- **Only the kind of command that began a run resumes it** (Mike, 23 November 2026). The two kinds are
  the commands that store the key (`allot`, `rotate_payment_key`) and the ones that hand it over
  (`allot_for_delivery`, `rotate_for_delivery`), and one `operation_ref` is one record for both. The
  record notes which kind began it when it begins, before any provider call (`begun_for` in its context:
  `secret_store` or `work_engine`). While the run is unfinished, the same request from the other kind is
  409 `OperationConflict`, `code: operation_conflict`, in the stale window and after it. Nothing is sent
  to the provider and nothing is written, so the record stays as it was, and the command that began it
  still resumes it. The error names that command. It matters only where both kinds are called for one
  actor: in embedded mode, or on two apps over one database. Finished by the other kind, a delivering
  run's key would be handed to nobody, and a storing run's would stay in the store while the delivering
  caller is answered with none.
  - A repeat of a *finished* command is not a resume. Either kind is answered from the ledger, the
    delivering one with `allocation_api_key: null` and `replayed: true`.
  - A record written before the kind was noted is the kind that the custody of its stored secret
    implies (`new_secret_custody`). One that notes neither was begun by `allot` or `rotate_payment_key`.
  - Two references are two commands, so the rule does not join them: an allotment of each kind for one
    actor, under two references, ends as the bullet on two allotments above says.
- **A rotation ends an actor's live work.** It mints a new key on the same allocation and revokes the old
  one. Batteries binds a payment session to the key that opened it (`internal/store/auth.go`: 403,
  "session is revoked or binding does not match"), so the next payment cycle of a stream that is
  running is refused and its funding stops. The same holds in both configurations.
- **Revoking the allocation** revokes it at Batteries, which then refuses every payment with its key
  ([who answers what](#who-answers-what)). A key the store holds is deleted there before the actor
  is marked revoked, so a store that fails leaves the actor active and a retry deletes it. A key an
  enterprise's engine holds is not the clearinghouse's to delete: no store is asked, and that engine
  drops its own copy.
- **Whether an allocation can pay is Batteries' decision alone.** The signer asks Batteries'
  authorization webhook before it signs, and at a standalone clearinghouse that is the only check at
  signing: the clearinghouse's engine is not in the path. So the signer is reachable by every
  enterprise's engine, and it keeps that webhook configured. An enterprise's engine reads each refusal
  as a self-hosted one does ([who answers what](#who-answers-what)): an empty allocation is the signer's
  402 (`payment` / `allowance_exhausted`), and a paused or revoked allocation, a paused tenant and a
  revoked key are its 403 or 401 (`payment` / `not_authorized`).
- **What a clearinghouse sees of a payment.** Its signer and Batteries see the payment's terms: the
  orchestrator's payment parameters, the capability (`app`), the price ceiling and the payment's id.
  Never the job, its prompt or its result. Its engine sees no payment as it happens; it reads the
  records afterwards ([a tenant's usage](#a-tenants-usage)).
- **Self-hosted payments is unchanged.** One engine has both halves: allotment and rotation store the
  key, the work half's `CredentialResolver` returns it, and the runner call presents it to the signer.
- **A supplied store** records custody with the key write: `ActorCommandRepo.set_actor_key` takes
  `credential_custody`, and a store without the conditional writes gets it as a field of the actor it
  saves ([persistence.md](persistence.md#store)).

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
| names an allotment or a payment key rotation still `started`, which the other kind of command began | 409 `OperationConflict`, `code: operation_conflict`, before any provider call and any write: repeat the command that began it ([payment keys](#payment-keys)) |
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
`OPERATION_RESUMED`, beside it, is `True` right after a command that resumed a run an earlier attempt
left unfinished; a replay of a finished command is not a resume.

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
  again after `LPB_OPERATION_STALE_S`. The one exception is `already_allotted`, below. A provider error
  an admin command answers with ("provider HTTP 409: ...") carries Batteries' own error text: the `error`
  of its JSON body, else the body as text, and at most 4096 characters of either, the limit a runner's
  or a signer's error body has (`BODY_LIMIT`, #158). Whether a 400 on the usage feed refuses the cursor is read
  from all of that text, before the cut: a refusal that names the cursor further on is still a
  `CursorMismatch`, which is what restarts cost sync from the beginning.
- **A provider answer the engine cannot read (#114, C21).** One strict reader takes every Batteries
  answer that is not an error status. It must be a JSON object: a body that is not JSON, not UTF-8, or
  nested deeper than the decoder reads (that one answered 500) is none, and so is an object that names
  one of its own keys twice (#158: the decoder keeps the last, and a page that named `items` twice lost
  the rows of its first list). A list page must have `items` as
  a list of JSON objects and `next_cursor` as a string, or no `next_cursor`. Anything else is a
  `ProviderError`: HTTP 502 from the REST service, `invalid_answer` as `cost_sync_error`, and an unknown
  outcome to the ledger, so the reference stays held. Before, a 2xx that was not a JSON object answered
  500, and a page with no `items` read as an empty last page. An empty 2xx body was read as `{}`; it is
  now refused too, since Batteries answers every management route with a JSON object. A redirect (3xx)
  is no answer either, whatever its body: the engine follows none, and it is the same `ProviderError`
  with an unknown outcome (as `cost_sync_error` it is `unavailable`). An answer whose content encoding
  does not decode (it says `gzip` over a body that is not) is a `ProviderError` with the answer's own
  status, from a management call as from the usage feed: a refusal when that status is a 4xx other than
  408, an unknown outcome otherwise (#158). A malformed page
  never reads as an empty one, and one malformed item fails its page. A page whose `next_cursor` is the
  cursor it was asked with is the same `ProviderError` ("cursor did not advance"), from a grant list, a
  resumed create's search, cost sync and [a tenant's usage page](#a-tenants-usage) alike: before, the engine asked for that page again without end,
  and the grant list (`GET /v1/clearinghouse/tenants` since #165) and `POST /v1/costs/sync` never answered. A longer cycle (a cursor that
  comes back after other pages: A, B, A) is that `ProviderError` too, from the first three of those readers (#158; a tenant's usage page reads one page for each request).
  A cursor that does not advance is refused at once, the first
  time its page is read. A longer cycle is found after at most a few turns of it and not always at its
  first repeat, because the engine keeps one earlier cursor to compare with, not every cursor; pages
  read twice meanwhile store no usage row twice. A grant, in a list or read alone,
  must have a non-empty string `id`, and its `name`, `status`, `currency`, total and `metadata` must be
  strings where present: an object that is not that (`{}`, an error object, `metadata` as `null` or as a
  JSON object) is the same `ProviderError`, never a grant with no tag, and nothing is turned into text
  (`null` was read as the name `"None"`). `metadata` that is free text, or `""`, is a grant the engine did
  not create, and so is `metadata` whose JSON is nested deeper than the decoder reads (#158): a resumed onboarding's
  search of the grants reads past it. The same holds for every value a command's result is built from: the `id` a grant create
  answers with, the `id`, `allocation_id` and `api_key` of a new key, and an allocation's `id` must each
  be a non-empty string, and an allocation's `status` and `currency` strings where present. Before, a
  `null` there was recorded as the text `"None"` in a finished command (allotment answered 201 with
  `allocation_id: "None"`), and an answer that lacked the key answered 500. Each of those values must
  also be Unicode text: one with a lone surrogate is the same `ProviderError` (#158), so no such id is
  put in the path of a later call, the read-back of a new grant included.
  **An id the engine keeps** has to fit its records as well: a new key's `allocation_id` and `id`, and
  the `id` of a tenant's grant, are each at most 200 characters, with no NUL. A key or a grant with any
  other id is the same `ProviderError` ("an allocation id the engine cannot store"), whoever answered it:
  Batteries, a payment clearinghouse or a supplied `AllocationManager`. It is raised before anything is
  stored, on SQLite and on PostgreSQL alike: no secret, nothing noted in the command's record, no actor
  and no tenant admin key. The provider has applied the command by then (a key funded, a grant
  created), so this is an unknown outcome, never a refusal: 502, the reference stays held, and a resume
  that is answered the same id is refused the same way. Such an id is quoted nowhere: not by this error,
  not by the error of a new grant that was not read back, which otherwise names the grant by its id,
  and not in a log line or the command's record. What follows when a payment clearinghouse answered it
  is under [the key an enterprise's engine is handed](#the-key-an-enterprises-engine-is-handed). An
  allocation's `allocated`,
  `spent` and `available` amounts are each `null` in the `Allowance` where the answer states none
  (`null`, `""`, `"[error]"`) or the engine reads none: unknown, never zero. One stated as `NaN` or
  `Infinity` is the same `ProviderError` ("no usable `available_usd`"), as a grant's total is (#158):
  502 from `GET /v1/allowance` and from reconciliation. The error does not quote the
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
    resume that runs out of its budget in a slow catalog leaves the next one to go on from there, and
    with the cursor the earlier cursor it compares each new one with, so a list that goes round (A, B, A)
    is found even when every resume reads only a page or two (#158); pages
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
- **A payment key** is never in a record either. An allotment's and a rotation's recorded result is the
  allocation alone, so a replay hands over no key: on a standalone clearinghouse it answers
  `allocation_api_key: null` and `replayed: true` ([payment keys](#payment-keys)).
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
| `delivered` | The attempt succeeded and its answer reached the job's owner. For an immediate job and a stream that is the attempt that succeeded; for an async job, whose owner reads the stored answer, the job must also have ended `succeeded` | Bill under its retail policy |
| `failed_after_payment` | Payment went out and the attempt failed (refused, runner error, **rejected tickets**), including an attempt the job failed over from, even when a later one delivered (D52). Also an async job's attempt that succeeded when the job did not: the runner answered and was paid, and the engine holds no answer for the owner to read, because it was too large to store, the write of the job's end failed, or the engine stopped before that write (D69 (4), [engine.md](engine.md#results-of-an-async-job)) | Absorb; don't bill |
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
| `missing_id` | has no `id` (absent, `null`, empty, blank, or not a string or a whole number; a whole number is kept as its digits). Blank is white space alone (`"  "`, #158). An id with text in it is stored as received: nothing is trimmed |
| `unknown_status` | has a `status` other than `applied`, `quarantined`, `ignored`, `duplicate` |
| `unreadable_created_at` | has no `created_at_ms`, or one that is not a JSON number (a string of digits and `true` are not) or that no clock holds |
| `unreadable_fee` | states a `computed_fee_eth` or `computed_fee_usd` that is not a decimal string as Batteries writes one: digits, then a point and digits when the fee has a fraction, after a minus sign when it is negative (`"0.000000039"`, `"7"`, `"-0.25"`). So not `NaN` or `Infinity`, not something that is no number at all (`"abc"`, `"0,5"`, `true`, an object), and not a form only Python's decimal reader takes (#158): an exponent (`"1e999999"`, `"3.9e-8"`), a digit separator (`"1_000"`), digits of another script, padded text, a plus sign, `".5"` or `"5."`. A JSON number (`0.1`, `5`) is no fee either: a fee is text, and the engine turns no number into text. `null`, `""` and `"[error]"` state no fee: Batteries serves them for a fee it has not or could not convert, and such a row is stored with no fee |
| `unreadable_row` | is one the JSON decoder's own limits refuse: an integer of more than 4300 digits, or a field nested deeper than the decoder reads (#158). Nothing of such a row is read, its id and its fees included: it is recorded under a digest of its text, with no fee, and the log line carries the start of that text. The page's other rows are stored and the cursor moves. Also a row that passes the checks above and is still refused by the usage row's own type (a backstop: no known row reaches it), and the code for another `UsageSource` whose row fits no other reason |
| `unstorable_<field>` | has in `<field>` a value the store's column cannot keep: one that is not text (a number, `true`, a list or an object in a text field; `null` and `""` are no value), or a text longer than the column (200 characters for an id, 64 for a fee, 8 for `currency`), with a NUL, or with a lone surrogate. A fee is measured by its digits written out with no exponent, as Batteries writes one, whatever form its source gave it (#158): `0.000000039` is eleven characters however it is written, and `9E+999999`, which another `UsageSource` can deliver (a supplied one, or a clearinghouse's usage page read by an engine in its `work` role), is a million and is set aside. So no applied fee has more than 64 digits, or more than 62 decimal places, and two applied fees can always be added. Checked before the store is asked, so SQLite and PostgreSQL do the same, whatever the `UsageSource` |

Each value is judged as it was received, and that value is the one stored: apart from a whole-number
`id`, the engine turns no number, list or object into text.

The reasons are a closed set (`SetAsideReason` in `contracts`): a record with any other code is refused
where it is built. The record is one row of `lpb_usage_set_aside`: the provider's row id, the reason, and
`computed_fee_eth` and `computed_fee_usd` as the provider stated them, whatever the row's status. A fee
the engine does not read (`unreadable_fee`, above), or whose digits are longer than the column, is recorded as `null`. A row with no id the
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
not a JSON object still fails the page, and so does a page that is not JSON or whose own envelope
(`items`, `next_cursor`) the engine cannot read: only an item is ever set aside.

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
from newer evidence. Two syncs that store a job's first projection at the same moment both end: one row
is kept, from the newer evidence of the two (#158). Reads compute an out-of-date job live, and the next sync stores it again, even
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
| `CostSummary.start`, `end` | UTC instants; the window holds the jobs **created** in `[start, end)`. Without `end` it ends at the engine's `Clock` now; without `start` it starts 30 days of time before `end`, counted in UTC: the same instant whichever zone an embedded caller names `end` in, across a clock change too. In process, a `start` or `end` that names no offset is read as UTC, whatever `tzinfo` it carries, and never in the host's own zone. Over REST a `start` or `end` is converted to UTC when the request is parsed (one with no offset is read as UTC) and answered in UTC; one that UTC cannot hold is a 422. An `end` before `0001-01-31T00:00:00Z` with no `start` is refused (`InvalidRequest`, 400 over REST; it was a 500, #158): its start, 30 days earlier, is a time UTC cannot hold. With its own `start` it is read as before |
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

The actor's allocation route answers the same amounts: `GET /v1/clearinghouse/actors/{actor_id}/allocation`
for the tenant's admin or auditor, and `GET /v1/clearinghouse/tenants/{tenant_id}/actors/{actor_id}/allocation`
for a clearinghouse admin (#168). Its answer is `AllocationRead`: every
field of the allocation's record, where it was, and `allowance`, which is what `GET /v1/allowance`
answers for that actor (`allocation_id`, `status`, `granted`, `spent`, `available`). Both routes make
the one read of the provider through the same `AllowanceReader` member, so an amount means the same on
both. `granted` is what was allotted, top-ups included, and a revoke lowers it by what it returns.

| The allocation... | `allowance` |
| --- | --- |
| is active, paused or exhausted | The provider's amounts and its status |
| is revoked | The same: the provider still reports what a revoked allocation spent |
| could not be read at the provider (it is down, or answered an error) | `null`. The record is still answered, as it was before the route read the provider: unknown, never zero. The failure is logged (`payments.allocation_allowance_unread`) |

An actor with no allocation is 404, as before, and the provider is not asked. In embedded mode the read
is `payments.get_allocation`; `payments.get_actor` still returns the record alone. An enterprise's
engine reads an actor's allowance from this route ([the remote adapters](#the-remote-adapters)).

## Payment configurations

A payment configuration is who runs Batteries, the signer and the wallet. There are two (ISS-31, D82).
The design is the [ISS-31 guide](../design-docs/m4-guides/iss-31-payment-configurations.md).

| Configuration | Who runs Batteries, the signer and the wallet | State |
| --- | --- | --- |
| Self-hosted payments | The enterprise, for itself: one deployment with one tenant | Built (M3, ISS-13). The rest of this contract describes it |
| Payment clearinghouse | A clearinghouse, for many enterprise apps. Its engine does payments only and never runs jobs. Each enterprise app runs its jobs in its own engine, which reaches the clearinghouse for allotment, the allowance, usage and the runner list, and signs at the clearinghouse's signer itself | Built (M4, #162 to #169). The two Compose projects that run it on one host are in [deploy/](../../deploy/README.md) |

**The engine's role says which side a deployment is** (`LPB_ROLE`, D84, #169):

| `LPB_ROLE` | Configuration | Routes | Allotment and rotation | Payment ports | Signs |
| --- | --- | --- | --- | --- | --- |
| `both`, the default | Self-hosted payments | The work routes and the clearinghouse routes | The engine stores each key for its own jobs and returns none | Batteries and its signer | At its signer |
| `work` | The enterprise's side of a payment clearinghouse | The work routes | No route allots. The engine's own commands store the key the clearinghouse hands over | The clearinghouse, for allocations, the allowance and usage; its own store for keys | At the clearinghouse's published signer, `LPB_SIGNER_URL` |
| `clearinghouse` | The clearinghouse | The clearinghouse routes, on a public and a private listener | Each key is handed to the caller once, and none is stored | Batteries | Nothing: it never runs a job |

A deployment that sets nothing new is in its `both` role, and nothing it served or wired changed. What
each role needs, refuses and starts is in [RELIABILITY.md](../RELIABILITY.md#roles), and the routes of
each role are in [access.md](access.md#listeners).

Both use `per_actor` allocations ([Allocation modes](#allocation-modes)) and the tenant rules
([Tenants](#tenants)). What an enterprise's engine calls at a clearinghouse is built (#166): allotment
and rotation that hand over each actor's [payment key](#payment-keys); [a tenant's usage](#a-tenants-usage);
and the [runner list](discovery.md#the-clearinghouses-runner-list). The engine that calls them is built
too (#168): it reaches them through [the remote adapters](#the-remote-adapters). It reaches the signer
with those keys, and Batteries never: only the clearinghouse's own engine does.

The engine's routes are two sets (D84, #164). The **clearinghouse routes** are tenants and their grants,
a tenant's actors and their allocations, and a tenant's access keys and usage, all under
`/v1/clearinghouse/tenants` since #165 ([Tenants](#tenants)), and the runner list at
`/v1/clearinghouse/discovery`. The
**work routes** are everything else: jobs, runners and the catalog, the cost reads with reconciliation
(`GET /v1/costs/reconciliation`) and cost sync (`POST /v1/costs/sync`), the allowance read, and an
engine's own access keys (`/v1/keys`). With self-hosted payments one deployment serves both sets; an
engine in its `work` role serves the work routes alone, and one in its `clearinghouse` role the
clearinghouse routes alone (#169). The
old paths of the routes that moved are under
[Upgrading to the work routes and the clearinghouse routes](index.md#upgrading-to-the-work-routes-and-the-clearinghouse-routes).

### Mode B

"Mode B" was the M2 name for the second configuration: a hosted payment operator, one Batteries
instance serving many enterprises (D23). "Mode A" was self-hosted payments. The gap D23 recorded is
that Batteries management credentials are global (a permission covers every grant), so an operator
cannot safely give an engine a management credential.

D74 (13 November 2026) answered it with a separately operated engine in `per_application` mode. That
was not built. D82 (18 November 2026) replaces it with the payment clearinghouse, which needs no scoped
credential: only the clearinghouse's own engine reaches Batteries' management API, and no management
credential leaves it. What leaves is each allocation's own key, handed once to the engine of the tenant
it belongs to ([payment keys](#payment-keys)).

### Provisioning

"Provisioning" was the name of allotment until 18 November 2026 (D83, #163): an admin allots an
allocation to an actor, where the engine said it provisioned the actor. See [Allotment](#allotment).
The failure kind `not_provisioned` keeps its name: the failure table belongs to ISS-28. Every name that
changed with it is under
[Upgrading to the domain-language names](index.md#upgrading-to-the-domain-language-names).

## The remote adapters

An engine that uses a payment clearinghouse (#168, D84) reaches the clearinghouse, in place of Batteries,
for allotment, the allowance and usage. It keeps each actor's key itself and signs at the clearinghouse's
signer itself ([payment keys](#payment-keys)). It is an engine in its `work` role: `LPB_ROLE=work`
selects the remote adapters (#169), and three settings, set together, say which clearinghouse:

| Setting | Meaning |
| --- | --- |
| `LPB_CLEARINGHOUSE_URL` | The clearinghouse's public listener, as `http(s)://host[:port]` and nothing after it: a path, a query, a fragment or userinfo is refused at startup, and the refusal does not echo the value. The engine calls the clearinghouse's routes at the root of this address. A trailing slash alone is taken off |
| `LPB_CLEARINGHOUSE_TOKEN` | The tenant admin key the clearinghouse admin handed over at onboarding. A secret: it is sent as a bearer to the clearinghouse and is in no log line, error text or `repr` |
| `LPB_CLEARINGHOUSE_TENANT_ID` | The tenant's id there, which is its grant's id (D83). No path carries it: the clearinghouse takes the tenant from the key. The engine refuses a command about any other tenant with it, and refuses every request when the key turns out to belong to another tenant ([who the engine is at the clearinghouse](#who-the-engine-is-at-the-clearinghouse)). Tech debt T38 |

- **`LPB_SIGNER_URL` names the clearinghouse's signer.** It is set as on any engine that pays, and it is
  a second address: the clearinghouse's engine serves no signer path, and its address never stands in for
  the signer's. With it unset the engine does not pay, as a self-hosted engine without it, and
  [`lpb payments check`](#lpb-payments-check) fails.
- **A half setting is refused at startup**, and so is any of the three beside `LPB_BATTERIES_URL`,
  `LPB_BATTERIES_TOKEN` or `LPB_BATTERIES_GRANTS_TOKEN`: only the clearinghouse's own engine reaches
  Batteries. The refusal names the settings and echoes no value.
- **The role and the three settings go together** (#169). `LPB_ROLE=work` without them is refused, and
  so are they with `LPB_ROLE` unset, `both` or `clearinghouse`: the role is the one switch, and the
  three settings alone do not turn an engine into a clearinghouse's tenant.
- **`LPB_SECRET_KEY` is required**, as with Batteries: the engine keeps each actor's key.
- **`LPB_CLEARINGHOUSE_TIMEOUT_S`** (20) bounds each request. `LPB_OPERATION_STALE_S` must be at least
  six times it. The ledger cancels a run at half the stale window
  ([a resume is a lease](#repeat-safe-commands)), and the longest command must fit in that half. Through
  the remote adapters the longest makes three calls: a rotation that another one overtook reads the
  allocation, sends the rotation, and reads which key the allocation takes. Every other command makes
  one or two, and none writes after its second call, so a read that is cut short leaves nothing in
  flight and the resume asks again. Three calls in the half is six timeouts in the window, where
  Batteries' two calls make four. The defaults, 20 and 120, meet it exactly. The read of the key's
  grant, which the engine makes once in a process
  ([who the engine is at the clearinghouse](#who-the-engine-is-at-the-clearinghouse)), is none of the
  three: a command makes it before its record is begun, so it spends none of that half.
- **`LPB_DISCOVERY_URL` defaults to the clearinghouse's runner list** (D87), and the reader carries the
  tenant admin key there and to no other address ([discovery.md](discovery.md#the-clearinghouses-runner-list)).
- A caller who supplies a part through `EngineSupply` keeps it: a supplied part is never replaced.

### Each port and its route

The work half reaches payments through five ports (D84). Three have a remote adapter. Two are the
engine's own in both configurations.

| Port | Self-hosted payments | With a payment clearinghouse |
| --- | --- | --- |
| Signing, inside `RunnerTransport` | The engine's signer, at `LPB_SIGNER_URL`, with the actor's key | The clearinghouse's signer, directly, at `LPB_SIGNER_URL`, with the actor's key. Nothing of the clearinghouse's engine is in this path |
| `CredentialResolver` | The engine's stored key (`PaymentsService`) | The same: the key the clearinghouse handed over at allotment or rotation, kept in this engine's `SecretStore` |
| `AllocationManager` | Batteries' management API | `ClearinghouseAllocations`: the tenant's own routes at the clearinghouse, with the tenant admin key. Allotment and rotation answer the actor's key, once |
| `AllowanceReader` | Batteries | `ClearinghouseAllowances`: the actor's allocation at the clearinghouse |
| `UsageSource` | Batteries' usage feed, for every tenant | `ClearinghouseUsage`: the usage route, for the caller's tenant only |

Member by member: `{a}` is the actor. Every path begins `/v1/clearinghouse`, and none names the tenant:
they are the tenant's own routes, which act on the tenant of the key (D87 point 3, as Mike amended it on
19 November 2026). The paths that name a tenant are the clearinghouse admin's, on its private listener.

| Port and member | Route | The answer becomes |
| --- | --- | --- |
| `AllocationManager.create_key_for_grant` | `POST /actors/{a}/allocation` with `amount`, `label`, `operation_ref` | The allocation's id, its key's id, and `allocation_api_key` as the secret: the port's new key ([the key](#the-key-an-enterprises-engine-is-handed)) |
| `AllocationManager.create_key` | `POST /actors/{a}/allocation/rotate-payment-key` with `operation_ref` | The same, with the new key. The clearinghouse ends the old key in the same command |
| `AllocationManager.revoke_key` | `GET /actors/{a}/allocation` | Nothing, for a key the allocation no longer holds: it has ended. The key it holds now is refused. The engine's rotation never calls it ([two rotations](#two-rotations-of-one-actor-through-a-clearinghouse)) |
| `OneKeyPerAllocation.holds_key`, which only the remote manager has | `GET /actors/{a}/allocation` | Whether the allocation is live and takes that key now |
| `AllocationManager.top_up_allocation` | `POST /actors/{a}/allocation/top-up` with `amount`, `operation_ref` | Nothing |
| `AllocationManager.set_allocation_status` | `POST /actors/{a}/allocation/status` with `status` | Nothing |
| `AllocationManager.revoke_allocation` | `POST /actors/{a}/allocation/revoke` | Nothing |
| `AllocationManager.read_allocation`, `AllowanceReader.read_allowance` | `GET /actors/{a}/allocation` | The `allowance` of the answer ([Allowance](#allowance)). `null` there is `ProviderUnavailable`: unknown, never zero |
| `AllocationManager.read_grant` | `GET /grant` | `TenantGrant` |
| `AllocationManager.top_up_grant`, `set_grant_status`, `create_grant`, `list_grants`, `grant_page`, `grant_balances` | None | `AccessDenied` (403) in the engine, with nothing sent: only a clearinghouse admin does these, at the clearinghouse |
| `AllowanceReader.allocation_for` | None: the engine's own actor record | The allocation whose key the engine holds |
| `UsageSource.page` | `GET /usage?cursor=&limit=` | The route's page, which is the port's own. A refused cursor is `CursorMismatch` |
| `CredentialResolver.credential` | None: the engine's own actor record and secret store | The actor's key |
| Signing, inside `RunnerTransport` | None at the clearinghouse's engine. `POST /sign-orchestrator-info` and `POST /generate-live-payment` at `LPB_SIGNER_URL`, with the actor's key | The signer's own answer ([payment path](#payment-path)) |

- **A command about another tenant is refused before it is sent.** The routes name no tenant, so the
  clearinghouse would apply it to the key's own tenant. Each member that names an actor, and
  `read_grant`, compares the tenant it is given with `LPB_CLEARINGHOUSE_TENANT_ID` first and refuses
  another with a `ProviderError` 403, "resource belongs to another tenant": a refusal that applied
  nothing, as the clearinghouse's own 403 was when the paths named the tenant.
- **Nothing is sent with another tenant's key.** Every request of the three adapters goes through
  `TenantWire` (`adapters/clearinghouse_tenant.py`), which reads the key's grant before the first
  request of a process and reads the tenant every answer states
  ([who the engine is at the clearinghouse](#who-the-engine-is-at-the-clearinghouse)).
- **The port names an allocation and the routes name an actor.** `AllocationManager` was written for
  Batteries, which takes an allocation's id. A manager that needs the actor has the optional protocol
  `AllocationsByActor`: the engine calls `for_actor(tenant_id, actor_id, operation_ref)` and sends the
  command to the manager that returns. The Batteries adapter does not have it. No member of
  `AllocationManager` changed.
- **The reference a command carries.** The clearinghouse keys its own operation ledger on the
  `operation_ref` it is sent, for the tenant admin key that sent it
  ([repeat-safe commands](#repeat-safe-commands)). It knows nothing of the idempotency key the engine
  derives for a provider.
  - A top-up and a rotation carry the engine's idempotency key as it is: it names the engine, the
    allocation and the admin's `operation_ref`.
  - An allotment carries `base64url(sha256(<the engine's idempotency key>:<the admin's operation_ref>))`.
    The engine's key for an allotment is derived from the actor alone, so that two references for one
    actor meet at Batteries. A clearinghouse answers a reference it finished without the key, for good,
    so the admin's reference is folded in: a new reference at the engine is a new command at the
    clearinghouse, which judges it by its own record of the actor (409 `already_allotted` while the
    actor holds an allocation there).

  A repeat at the enterprise's engine is therefore a repeat at the clearinghouse, which answers it from
  its own ledger. The adapter itself never sends a request twice.
- **The allocation is checked before a write, and in its answer.** The routes act on whichever
  allocation the actor holds at the clearinghouse when a command arrives. Before a top-up, a status
  change, a revoke or a rotation the adapter reads the actor's allocation there. It refuses with 404
  when it is not the one the engine names, and a revoke of an allocation the clearinghouse has already
  ended sends nothing. That read proves what the clearinghouse held then, and no more: a revoke and a new
  allotment there can land before the command does. So the adapter also reads the allocation each route
  answers, which is the one it acted on. When that is another allocation than the one named:
  - the command is no success and no refusal: `ProviderError` with the answer's 2xx (502 over REST), as
    for any answer the engine cannot use. The reference of a top-up or a rotation stays held, since
    something was applied;
  - the engine's record of the actor is not changed, and a rotation's key is not kept;
  - the message names both allocations (the one acted on as "an id this engine cannot store" when it
    is more than 200 characters or has a NUL, #158), says nothing is recorded here, and says what to do: for a
    top-up, where the amount is now; for a status, to set it again there; for a revoke, to repeat it,
    which sends nothing and records that the named allocation has ended; for a rotation, that the other
    allocation's holder rotates its key again.

  The adapter finds this out and cannot prevent it: the routes take no allocation id (tech debt T53). A
  read that is answered for another allocation is a plain 404, since it applied nothing.
- **No work route of the clearinghouse is called.** The allowance is read from the actor's allocation
  route, which answers it with the record ([Allowance](#allowance)). The remote adapters, the discovery
  reader and `lpb payments check` call the tenant's routes and the runner list, and nothing else of the
  clearinghouse's engine: `tests/unit/test_clearinghouse_without_work_routes.py` runs them against a
  standalone clearinghouse's app, which has no work route and no signer path.
- **Only an actor's current allocation can be read.** The route names an actor. An allocation the
  actor no longer holds is a 404 in the engine, with nothing asked, so reconciling an earlier
  allocation of an actor has no provider figure at an engine that uses a clearinghouse (tech debt T40).

### The key an enterprise's engine is handed

The clearinghouse hands an actor's key over once, in the answer to an allotment or a rotation, and keeps
none ([payment keys](#payment-keys)). The enterprise's engine runs its ordinary commands, `payments.allot`
and `payments.rotate_payment_key`, over the remote manager, which returns `allocation_api_key` as the
port's new key. So the key goes where a self-hosted engine's goes: into the engine's own `SecretStore`,
with the `SecretRef` on the actor's record and `credential_custody: secret_store`. For the enterprise's
engine, its own store holds the key; the clearinghouse's record of the same allocation says
`work_engine`. The key is in no answer of the enterprise's engine, no record, no ledger context, no log
line and no error, and no error raised while an answer is read can lead to it
(`adapters/clearinghouse_wire.py`).

What the engine does depends on what the answer carries:

| The clearinghouse answers | The engine |
| --- | --- |
| A key | Stores it, and saves the actor or points it at the key |
| A key, with `resumed: true`: the clearinghouse's own run had not finished, and this repeat finished it | The same. This is the ordinary recovery from a failure at the clearinghouse |
| No key, `replayed: true`, and the engine's own record of the actor names the allocation and the key of the answer, with the secret in its store | The manager returns the engine's own copy: the engine was handed that key before. The engine's own rotation does not come this far, since its resume asks for no key (below); a caller of the port alone does |
| No key, `replayed: true`, otherwise | Refused, `InvalidRequest` (400). Nothing is stored, and no actor record is saved or changed. The message says what replaces the key (below) |
| No key and no `replayed`: the clearinghouse's routes are mounted to keep each key with its own engine | `ProviderError`, an answer the engine cannot use and no refusal: the allocation exists there, and the reference stays held |
| A key whose `allocation_id` or `api_key_id` the engine's records cannot keep (more than 200 characters, a NUL), or no key and `replayed: true` under such an id | `ProviderError`, an answer the engine cannot use and no refusal (502), in the same words both times. The key is not taken: nothing reaches the engine's secret store or its record of the actor, the reference stays held, and neither id is in the error, a log line or the command's record. The message says what replaces the key (below; [repeat-safe commands](#repeat-safe-commands), #158) |

**A key that never arrived.** A clearinghouse that finished a command answers its repeat without the
key, so a lost answer loses the key: nobody holds it. A lost key is replaced by a rotation (D87 point 1).

- **A lost rotation.** The repeat is refused in those words. The engine still holds the key the
  rotation replaced, which pays for nothing now. Rotate again under a new `operation_ref`: the engine is
  handed the new key and stores it. The first rotation's record never finishes. Its repeat is refused
  the same way for good and changes nothing, so leave it.
- **A lost allotment.** The engine has no record of the actor, so it can neither rotate nor revoke it:
  both are 404 here. The tenant's admin revokes the allocation at the clearinghouse
  (`POST .../allocation/revoke` with the tenant admin key), which returns what it has not spent to the
  grant, and allots again at the engine under a new `operation_ref`. The first allotment's record then
  finishes: its repeat is 409 `already_allotted`, which frees the reference.
- **Under a new reference without the revoke,** an allotment is the clearinghouse's 409
  `already_allotted`, passed on. Nothing is applied and the amount is not added.

**A key the engine did not keep (#158).** A clearinghouse that names the allocation or its key by an id
of more than 200 characters, or with a NUL, hands over a key the engine cannot record: the engine takes
none of it. The clearinghouse has finished the command and hands a key over once, so its answer to the
repeat carries no key under the same ids, and is the same `ProviderError`: no repeat of that reference
can finish. The key is replaced as a key that never arrived is, and the message says how, without
either id:

- **An allotment.** The tenant's admin revokes the allocation at the clearinghouse
  (`POST .../allocation/revoke` with the tenant admin key; the route names the actor and no allocation
  id), and the engine allots again under a new `operation_ref`. The first allotment's record then
  finishes as a lost allotment's does: its repeat is 409 `already_allotted`, which frees the reference.
- **A rotation.** The clearinghouse rotated, so the key the engine holds is ended. Rotate again under a
  new `operation_ref`. The first rotation's record never finishes; leave it.

Either works once the clearinghouse answers ids the engine can keep. One that names every allocation or
key by such an id cannot be used by this engine, and that is the clearinghouse's to put right. An engine
of this repository in its `clearinghouse` role answers no such id: it refuses one from its own provider
before it stores or hands over anything, so the enterprise's engine is answered an error and no key was
handed over to be lost.

**The engine's own resume.** A run of the engine that died before it stored a key repeats the same
reference at the clearinghouse.

| The clearinghouse's command | Its answer | The engine |
| --- | --- | --- |
| Is still running, or stopped less than `LPB_OPERATION_STALE_S` ago | 409 `operation_in_progress` | `ProviderOutcomeUnknown`: the reference stays held, for a later repeat |
| Stopped unfinished | The key, with `resumed: true` | Stores it and finishes |
| Finished | No key, `replayed: true` | The refusal above: the key never reached the engine's store |

A run that died after it stored the key asks the clearinghouse nothing. A rotation's record notes the
stored key of its call, and the resume reads it back from the engine's own store. An allotment resumed
after the engine saved the actor answers from that record.

#### Two rotations of one actor through a clearinghouse

**The clearinghouse's order decides which key the engine keeps** (Mike, 23 November 2026). A
clearinghouse keeps one key for an allocation: its rotation ends the key before it in the same command.
Of two rotations it ran one after the other, only the later one's key is live, whichever of them the
engine wrote first. With self-hosted payments both keys are live until one is revoked, the rotation that
writes the actor first keeps its key, and the other revokes its own ([two rotations](#allotment)). The
difference comes from the allocation manager, through the optional protocol `OneKeyPerAllocation`, which
the remote manager has and the Batteries adapter does not:

- **No key is revoked by its id.** The clearinghouse ended the old key when it minted the new one, so
  the rotation never calls `revoke_key`, and no rotation is ever made to end a key.
- **An overtaken rotation asks which key the allocation takes** (`holds_key`, one read of the actor's
  allocation). The engine still writes a rotation's key only over the key that rotation began from. A
  rotation that finds another rotation's key on the actor asks about its own:

  | The allocation at the clearinghouse | The rotation |
  | --- | --- |
  | Takes this rotation's key: the clearinghouse ran it last, and the key the actor holds is ended | Points the actor at its key, over the key the actor holds at that moment and no other, drops that key's secret, and answers the actor: a success. When a third rotation wrote the actor in between, it asks again |
  | Takes another key, or is revoked: this rotation's key is ended | Drops its own secret and is refused, `InvalidRequest`: another key rotation finished after this one began. The reference stays held, as with Batteries |

- **A resume asks for no key.** A rotation resumed after it stored its key reads that key back from
  the engine's own store, by the note in its record, and sends the clearinghouse no command: the
  clearinghouse would answer the repeat without the key. So an overtaken rotation that died before its
  write of the actor still keeps its key, and one that died after it drops the ended key's secret, which
  its record noted before the write.
- **Every outcome ends the same way.** The engine's record of the actor and its secret store hold the
  one key the clearinghouse takes, and no other secret. A revoke that lands in between leaves the actor
  revoked, no key live and no secret stored. The one exception is a key that never reached the engine's
  store, because the engine died between the clearinghouse's answer and its own write of the secret:
  that key is lost, and a rotation under a new reference replaces it, as above.

`revoke_key` on the remote manager, for a caller of the port alone, reads the actor's allocation: a key
it no longer holds has ended and nothing is sent, and the key it holds now is refused, since a
clearinghouse ends a key only by the rotation that replaces it.

**Rows a deployment already has.** An actor allotted while the engine ran self-hosted payments holds a
Batteries key and an allocation the clearinghouse does not know. Its commands are refused before
anything is sent, since the clearinghouse holds no such allocation, and nothing is rewritten or deleted
(T30). Its jobs still present the stored key, to the signer the engine now names: the clearinghouse's,
which refuses a key its Batteries never minted (`not_authorized`). Revoke such actors before the engine
is pointed at a clearinghouse, and allot them again there.

### Who the engine is at the clearinghouse

A tenant's id is its grant's id (D83), and the engine passes the tenant of an allotment to the provider
as the grant. So every actor an enterprise's engine allots must be in the tenant the clearinghouse
gave it: `LPB_CLEARINGHOUSE_TENANT_ID` is that id, and so is `LPB_JWT_TENANT_ID` or the value of the
tenant claim, the `tenant_id` of the access keys the engine issues, and the `tenant_id` of the
`ActorContext` an embedding app builds. Nothing is mapped. An actor in any other tenant is refused by the
engine, before anything is sent (403): the routes it calls name no tenant, and the clearinghouse acts on
the tenant of the key.

**The engine learns whose key it holds, and holds itself to that tenant** (#165). Nothing at the
clearinghouse compares `LPB_CLEARINGHOUSE_TENANT_ID` with `LPB_CLEARINGHOUSE_TOKEN`: a key of another
tenant is simply answered for that tenant. With such a key an engine would allot from that tenant's
grant, be handed the keys of allocations there, read that tenant's usage as its own costs, and record
all of it under its own tenant. So:

- **Before the first request of a process** the engine reads the key's grant
  (`GET /v1/clearinghouse/grant`), whose `tenant_id` says whose key it is. The read is made when the
  first request is, not at start: an engine starts with its clearinghouse unreachable, as before. A read
  that fails is that request's failure (`ProviderUnavailable`, or what the clearinghouse answered) and
  says nothing about the key, so the next request reads again. Once one read has answered for the
  engine's tenant, no other is made while every answer agrees with it: a process has one key. Requests
  that arrive together before that share one read, since each read has the clearinghouse read the
  grant's balances: those that waited for a read that failed fail with it, and the next request reads
  again. An answer that states another tenant ends what the engine knew of its key, so the next
  request reads the grant again before anything else: a usage page states no tenant, and is not taken
  on a word the clearinghouse has since contradicted. Nor is what was already on its
  way when that answer arrived: a read of the grant sent before it confirms nothing when it comes back,
  and a usage page sent before it is refused (403) and none of its rows is stored, whatever a later
  read of the grant has said by then. The error of an answer for another tenant holds no answer: it is
  raised from a frame that never held one, so a handed key is not reachable from it.
- **A command makes that read before its record is begun.** An allotment, a top-up and a payment key
  rotation run on the operation ledger, inside half of `LPB_OPERATION_STALE_S`, which is sized for the
  three calls the longest of them makes ([the remote adapters](#the-remote-adapters)). So the payments
  service asks the allocation manager to be ready before it enters the ledger (the optional protocol
  `ReadyBeforeCommands`, which the remote manager has and Batteries' client does not): the read spends
  none of that budget, and inside it the command makes the calls it always made. A refusal or a failure
  there leaves no operation record and the reference free, so the same request runs as new once the
  clearinghouse answers, with no stale window to wait for. A status change and a revoke, which take no
  reference, are made ready the same way. The repeat of a finished command is made ready too, before it
  is answered from the record: in a process that has not yet read the grant it costs that one read, and
  fails while the clearinghouse cannot be reached. A read that is not a command (the allowance, a usage
  page) makes the read itself, and so does a manager that is called through the port alone, by a caller
  that never asks it to be ready: that is as safe, and spends that caller's own time.
- **A command that is under way stops when the key comes into question.** Made ready, a command goes by
  what was known of the key then, and reads no grant between its calls. If the clearinghouse answers
  any request of the engine for another tenant while the command runs, the command sends nothing more,
  and a later read of the grant that names the engine's tenant again does not start it again: only a
  new request does, which is made ready anew. The check is a comparison inside the engine and spends
  none of the ledger's budget. What the command is answered with depends on what it had sent:
  - **Nothing that could be applied** (it was stopped before its first write, as a top-up is between
    the read of the allocation and the payment): a `ProviderError` 403, a refusal. The operation ledger
    frees the reference, and the same request runs as new.
  - **A write** (it was stopped after one, as a rotation is before the read that decides which key it
    holds): `ProviderOutcomeUnknown` (503). The clearinghouse may have applied it, so the reference is
    kept, and the repeat under the same `operation_ref` is answered from the clearinghouse's record.
  - **A write that was already on its way** is answered as it always was: by the tenant its answer
    states, or as an unknown outcome when no answer arrives.
- **Another tenant than `LPB_CLEARINGHOUSE_TENANT_ID`** is a refusal of whatever was asked: a
  `ProviderError` 403 that names both tenants and both settings, and says to set the key and the id of
  one tenant and start the engine again. Nothing of the request is sent, so nothing is applied anywhere
  and a command's reference is never claimed. The read is repeated at each request while the answer is
  another tenant's: only a restart changes the key.
- **This covers the answers that state no tenant.** A usage page is not read, so cost sync fails, stores
  no row and moves no cursor, and a cursor kept from the engine's own tenant is not followed with the
  other tenant's key. `GET /readyz` shows the failure as a code (`cost_sync_error`), the log has the
  refusal's words, and a cost that was `pending` stays so.
- **The clearinghouse's runner list is read with whatever key the engine has.** It is the same list for
  every tenant, so the discovery reader sends the key, which the route needs, and does not ask whose it
  is: a refresh reads no grant and spends none of its deadline on one.
- **The check of the key is no switch that stops jobs.** A job calls no route of the clearinghouse: it
  pays at the signer with the allocation key the engine stored when the actor was allotted, and it
  never sends `LPB_CLEARINGHOUSE_TOKEN`. With another tenant's admin key the engine has a catalog, and
  an actor that was allotted before still runs paid jobs, on its own allocation. What that key cannot
  do is the tenant admin's traffic, which is all the check holds: allot, rotate, top up, change a
  status, revoke, read an allowance or the grant, and sync usage. An engine that never held its own
  tenant's key has allotted nobody, so it has no key to pay with.
- **Every answer that states a tenant is read too:** each allocation answer and the grant. Another
  tenant after a read is the same refusal. After a command, the clearinghouse has applied it at the
  other tenant, so it is no refusal: a `ProviderError` with the answer's 2xx (502 over REST), as for a
  command applied to [another allocation](#the-remote-adapters), and the reference stays held. No actor
  is saved or changed, and a key the answer handed over is dropped with the answer: it is stored
  nowhere, logged nowhere and in no error. The message says what was left at the other tenant and who
  puts it right: after an allotment, that tenant's admin revokes the allocation at the clearinghouse,
  which returns what it has not spent to its grant. With the first rule in force this needs a
  clearinghouse that answers the grant read for one tenant and a command for another.
- **`lpb payments check` says the same, without an engine:** the grant probe fails when the key's
  grant is another tenant's than the setting names, and the usage probe with it.

The key the engine holds is a tenant admin's. A clearinghouse admin's key does nothing on the public
listener ([access.md](access.md#listeners)), so the engine can never onboard a tenant, top up a grant or
pause a tenant, whatever it is asked.

### What a failure at the clearinghouse means

A clearinghouse failure is classified like a signer failure (D87): unreachable or not ready is `retry`,
an unknown outcome is `check`, and a refusal keeps its reason. That covers the admin calls and the reads
an engine makes at a clearinghouse, which all go through `adapters/clearinghouse_wire.py`, and it is
three errors. The words `retry` and `check` are ISS-28's, which will read them from these. A failure
while signing is the signer's own, and is read as a self-hosted engine reads it
([who answers what](#who-answers-what)).

| What happened | A read | A command |
| --- | --- | --- |
| The connection was never made (refused, no route, a TLS failure, a connect timeout) | `ProviderUnavailable` | `ProviderUnavailable`: nothing was sent |
| No answer came after the request left (a timeout, a dropped connection) | `ProviderUnavailable` | `ProviderOutcomeUnknown` |
| 500, 502, 504 | `ProviderError` with the status | `ProviderOutcomeUnknown` |
| 503, 408 | `ProviderUnavailable` | `ProviderOutcomeUnknown` |
| 429 | `ProviderUnavailable` | `ProviderUnavailable` |
| 409 with `code: operation_in_progress` | | `ProviderOutcomeUnknown`: the same reference is still running there |
| 400, 401, 403, 404, 409, 422 | `ProviderError` with the status and the clearinghouse's own words | The same: a refusal, which applied nothing |
| 400 with `code: cursor_mismatch` | `CursorMismatch` | |
| A 2xx that is not the expected shape, or a redirect | `ProviderError` with the answer's status. Its body is not quoted | The same, and no refusal: the command may have been applied |

- `ProviderOutcomeUnknown` is a `ProviderUnavailable` (503): callers that catch that keep working. It
  says a command left and no answer says whether it was applied. The operation ledger keeps the
  reference, as it does for any failure that is not a refusal, and the repeat is answered by the
  clearinghouse's own ledger. Under a new reference the command could apply twice.
- A refusal frees the reference when nothing was applied, as with Batteries.
- No redirect is followed, so the tenant admin key goes to the clearinghouse's address only.
- An answer is read strictly: a field the engine does not know makes it unreadable. The two engines run
  the same release (tech debt T37).
- No error carries a credential. An error is built where the request or the answer is in hand and
  raised from a frame that holds neither, so it chains no error of the HTTP client or the parser, and
  no frame of its traceback holds the tenant admin key or a handed key.

### `lpb payments check`

A read-only command (D87): it prints the engine's role, what that role needs for payments and whether
each answered (#169). **Every request it makes is a GET**, it builds no engine and it opens no database.
An engine in its `both` or its `clearinghouse` role reaches Batteries itself, so its configuration is
`self_hosted`; an engine in its `work` role has the configuration `clearinghouse`. Every role probes the
signer at `LPB_SIGNER_URL`: the `both` and the `work` role sign there, and a clearinghouse runs the
signer its tenants' engines sign at and reads its readiness there for the
[wallet report](#the-wallet-report).

| Configuration | Probe | Request | Passes when |
| --- | --- | --- | --- |
| Both | `signer` | `GET /sign-orchestrator-info` at the root of `LPB_SIGNER_URL`, with no credential | The answer is 405 |
| Self-hosted | `batteries` | `GET /v1/usage?limit=1` with `LPB_BATTERIES_TOKEN` | A Batteries list is answered |
| Self-hosted | `batteries_grants`, when `LPB_BATTERIES_GRANTS_TOKEN` is set, and always in the `clearinghouse` role | `GET /v1/grants?limit=1` with that credential | A Batteries list is answered. A clearinghouse onboards its tenants with this credential, so without it the probe is `not_configured` and the check fails |
| Clearinghouse | `clearinghouse_grant` | `GET /v1/clearinghouse/grant` with the tenant admin key | The key's tenant's grant is answered, and its id is `LPB_CLEARINGHOUSE_TENANT_ID`. It is printed as `tenant`. Another id fails the probe as `unexpected`: the key in `LPB_CLEARINGHOUSE_TOKEN` belongs to another tenant, whose grant and id are not printed. A 403 means the key is not a tenant admin's or an auditor's, and names `LPB_CLEARINGHOUSE_TOKEN` |
| Clearinghouse | `clearinghouse_usage` | `GET /v1/clearinghouse/usage?limit=1` | A usage page is answered, and the grant probe did not find the key to be another tenant's: the page states no tenant, and another tenant's page is `unexpected` |
| Clearinghouse | `clearinghouse_runner_list`, when it is the discovery address | `GET /v1/clearinghouse/discovery` | A list is answered |

- **Exit codes:** 0 when every probe passed, 1 when one was refused or failed, 2 when the settings
  themselves are refused, a role without what it needs included. Nothing is sent in the last case.
- **Output:** one JSON object on standard output: `role`, `configuration`, `ok`, `probes` (each with `name`,
  `setting`, `request`, `outcome`, `status_code` and `detail`) and `tenant`. `outcome` is `ok`,
  `refused`, `unavailable`, `unexpected` or `not_configured`.
- **Never printed:** a credential, an address, or anything the other side said. A probe names the
  setting to look at.
- **An engine that uses a clearinghouse probes the clearinghouse and its signer.** The signer is asked
  at `LPB_SIGNER_URL`, as a self-hosted engine asks its own, and never at the clearinghouse's address.
- **A missing signer address fails the check in every role, and the probe says what follows.** No role
  is refused at startup for it, so this is where it is said. `both`: the engine runs its jobs unpaid.
  `work`: the same, and the address to set is the signer its clearinghouse publishes. `clearinghouse`:
  the wallet report cannot say whether the signer answers. Nothing is sent for the probe.
- **A clearinghouse's own check does not test the published signer.** It asks the signer where its own
  engine reaches it, which may be an address inside its network. Whether enterprise engines can reach
  the published address is what their own check shows.
- **No work route is called,** and the tenant admin key is sent to the tenant's routes and the runner
  list only, never to the signer.
- **What the signer probe proves.** The signer's identity call is a `POST`, so it cannot be made here.
  livepeer/node registers that path for `POST` alone, so a signer answers a `GET` with 405. That says
  an HTTP server is at the address and has the signer's identity route. It does not say the signer
  signs, that its wallet is funded, or that it is ready.
- `check_payments(settings)` in `livepeer_builder.runtime` returns the same report to an embedding app.
