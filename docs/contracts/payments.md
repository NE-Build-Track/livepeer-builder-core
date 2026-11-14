# Payments and cost contract

**Ticket:** ISS-04 · **Decisions:** D17–D24 (12 October 2026) · **Code:** `contracts/payments.py`,
`contracts/costs.py`, `services/payments.py`, `services/costs.py`, `adapters/batteries.py`

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

- provisioning and key rotation return the actor, never the `lpg_` key; keys are encrypted at rest;
- a job's `failure.message` for a payment failure is the engine's own words, and `failure.body` is
  only ever the runner's; the SDK's text, with the signer URL and its body, goes to the
  `runner.signer_error` log line as `detail`;
- `GET /v1/allowance` answers a provider error with a fixed message and the provider's status
  ("provider HTTP 500: the allowance could not be read"); the provider's text goes to the
  `payments.allowance_failed` log line;
- the engine refuses to start with an `LPB_SIGNER_URL` that has userinfo, a query or a fragment, so
  the URL it logs holds no secret; a refused setting is never echoed.

## Allocation modes

| Mode | Allocation | Who enforces a user's cap | Per-user spend |
| --- | --- | --- | --- |
| `per_actor` (**default**) | One allocation and key per actor | The signer, at authorization | Exact: the allocation's own `spent_*` |
| `per_application` | One allocation and key from settings (`LPB_SHARED_PAYMENT_KEY`, `LPB_SHARED_ALLOCATION_ID`) | Nobody per user; the enterprise must | Only through the engine's session log, which records that allocation |

In `per_actor` mode there is **no fallback** to a shared key. An unprovisioned actor's job fails
before payment with `not_provisioned`.

## Provisioning

**There is no automatic provisioning.** Every money-moving command is explicit and needs the
`admin.allocations` operation (an admin credential). Allocation and key writes carry an idempotency
key of `base64url(sha256(engine_id:operation:step))`.

Provisioning refuses an `application_id`, `actor_id`, `grant_id` or `label` that is longer than 200
characters or holds a NUL character (422 over REST), before any provider call: every record stores them
in 200 characters, PostgreSQL refuses a NUL, and the provider key is funded before the actor is saved.
The same limit applies to every id, label and reference a request carries (`StoredText`): a job's
`app`, `capability`, `model` and `operation_ref`, and an access key's `actor_id`, `application_id` and
`label`. Stored text also refuses a lone surrogate, half of a UTF-16 pair, which JSON can write
(`"\ud800"`) and no store can keep: a 422 over REST, which repeats it as that escape. It was a 500,
because the 422 could not be sent.

**A grant id is one segment of the provider's URL path (#113, C10).** `grant_id`, on provisioning and on
`FundRequest`, is a `GrantId`: stored text that also refuses `/`, `\`, `?`, `#`, `%`, control characters
and the values `.` and `..`, which a client or server would read as another path. Whitespace around the
id is removed before the check, so the value checked is the value the engine uses: `" .."` is refused as
`..`, `" grant_1"` is used as `grant_1`, and an id of only whitespace is no grant chosen. It is a 422 over REST (a
`ValidationError` in process) before the operation ledger records anything or the provider is called, and
it applies to the allocation fund route too, whose body is a `FundRequest`. It is held to no pattern such
as a UUID: the provider chooses its ids. Separately, the Batteries adapter percent-encodes every id it
places in a path, whatever its source (a grant, an allocation, a key, a created grant read back), so an id
the provider itself returned cannot name another resource either.

**The new key must have somewhere to go (#112, C2).** Provisioning funds the provider key before the
actor is saved, so a store that cannot keep the key would leave a funded allocation with no record. Two
checks stop that:

- **At startup.** An engine in `per_actor` mode with a payment provider and the built-in secret store
  refuses to start when that store cannot write, which is when `LPB_SECRET_KEY` is not set, and the
  error names the setting. The check runs on the parts the engine will run with, after `decorate`: a
  decorator that turns the provider off, or installs a store of its own, is not refused, and one that
  adds a provider is. Only the built-in store is asked at startup. A supplied store that cannot write
  at boot, such as a sealed vault, still starts and is asked per request.
- **Per request.** Provisioning and key rotation ask the secret store whether it can write in the step
  that mints the key, just before the provider is called. The check is the store's optional
  `writable()` (`WritableSecretStore`); a supplied store that leaves it out is not asked. A `False` is
  `InvalidRequest` (HTTP 400).
  - On a first run nothing was sent to the provider and nothing was noted, so the reference stays
    free and the same command runs once the store can write.
  - A repeat of a finished command stores nothing and is not asked: it gets the recorded result.
  - A resume whose key is already stored is not asked either: it goes on, for a rotation to revoke the
    old key. The stored key must be the one of the provider call about to be sent: the record notes the
    call's idempotency key beside the secret's reference, and only a match skips the check. A resume
    after the actor was revoked, or provisioned again, sends another provider call whose key is not
    stored yet, so it is asked before that call.
  - A resume that still has a key to store is refused, and the message says that an earlier attempt
    may have reached the provider. It does not say that nothing was sent, and the refusal does not
    free the reference ([refused or unknown](#repeat-safe-commands)): the same request resumes once
    the store can write.

| Command | Provider call | Idempotency |
| --- | --- | --- |
| `provision(ProvisionRequest(application, actor, amount, grant_id, operation_ref))` | `POST /v1/api-keys` with `grant_id` (atomic: allocation + key + funding) | the [ledger](#repeat-safe-commands), and a provider key derived from the actor, so two references for one actor still meet at Batteries. An actor already provisioned is 409 `already_provisioned`; fund it instead |
| `fund(…, FundRequest(amount, operation_ref))` | `POST /v1/allocations/{id}/fund` | the [ledger](#repeat-safe-commands), and `base64url(sha256(engine_id:fund:{allocation}:{operation_ref}:fund))`. `grant_id` on this body is ignored |
| `fund_grant(FundRequest(amount, operation_ref, grant_id))` | `POST /v1/grants/{grant_id}/fund` | the ledger, and `base64url(sha256(engine_id:fund-grant:{grant_id}:{operation_ref}:fund))` |
| `grants.list_grants()` (the grant desk, `payments.grants`) | `GET /v1/grants` with `LPB_BATTERIES_OPERATOR_TOKEN` | read |
| `grants.create_grant(name, amount, operation_ref=…)` | `POST /v1/grants` with `LPB_BATTERIES_OPERATOR_TOKEN`, tagged `metadata.lpb_operation` | the ledger; Batteries has none on grants, so a resume finds the tagged grant (Q5) |
| `set_status(active / paused)` | `PATCH /v1/allocations/{id}/status` | naturally repeatable |
| `revoke` | `POST /v1/allocations/{id}/revoke` (unused funds return to the grant) | repeat-safe upstream |
| `rotate_key(operation_ref)` | `POST /v1/api-keys` with `allocation_id`, then revoke the old key | the ledger, and `rotate:{allocation}:{operation_ref}` at the provider |

- **Zero amount** sets up identity without money. Batteries marks the allocation `exhausted` until an
  explicit fund.
- **Amounts the provider takes (#112, C11).** The engine mirrors Batteries' amount format (v0.0.3,
  `internal/units/eth.go`): unsigned, with at most 18 decimal places, sent as a plain decimal string with
  no exponent (`format(amount, "f")`). It also sets its own bound, a fixed constant and not a setting: at
  most 1,000,000,000 (10^9) USD or ETH per command, since the provider's own (256 digits once scaled) is
  effectively none. Provisioning takes zero or more (`ProvisionAmount`); funding an allocation, funding a
  grant and creating a grant take an amount within the format and bound (`FundingAmount`) and still need
  it positive. An amount outside these is refused when the request is parsed: 422 over REST, a
  `ValidationError` in process (for `grants.create_grant`, when it is called), before the operation
  ledger records anything and before any provider call. A zero or negative funding or grant amount is
  still the command's own refusal, 400, unless it also breaks the format: one with more than 18
  decimal places (`-0.0000000000000000001`, `0E-19`) is refused when it is parsed, with the 422.
  **Upgrading:** resolve any `started` operation whose amount is above the bound or has more than
  18 decimal places before moving to this release. Its repeat is refused when it is parsed, before
  the ledger is read, so it can no longer be replayed or resumed through that request.
- **The new key** goes straight into the `SecretStore`. The database stores only its `SecretRef`.
- **Which grant.** The request names it. When `grant_id` is empty, the engine uses the grant of the
  caller's own allocation (the actor that key was provisioned as). When that caller has no allocation,
  the engine returns 400 `choose a grant`. There is no deployment-wide default. An admin may name any
  grant. Batteries still applies `grants.fund` to every grant on the instance; the engine does not
  narrow that. An actor key cannot call these routes (`admin.allocations`).
- **The engine's management credential** is least-privilege: `allocations.*`, `api_keys.*`,
  `usage.read`, and `grants.fund`. It has **no `grants.*`**.
- **Listing and creating grants** uses `LPB_BATTERIES_OPERATOR_TOKEN` (`grants.read`, `grants.create`).
  The token is optional. Without it the grant desk is off: `engine.payments.grants` is `None` and the
  grant list and create routes are absent. Funding grants still works with `LPB_BATTERIES_TOKEN`.
- **Cost sync** reads `GET /v1/usage` with no grant filter. Rows for allocations this engine did not
  provision are stored and are not part of any actor's reconciliation. A cursor saved under the old
  `grant_id` filter does not match, so the first sync after this change restarts from the beginning
  and skips rows it already stored.

## Repeat-safe commands

Each admin command that moves money or creates a credential takes a caller-chosen `operation_ref`,
scoped to the caller `(application_id, actor_id)` (D57, ISS-11). The engine records it in the operation
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
  again after `LPB_OPERATION_STALE_S`. The one exception is `already_provisioned`, below.
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
  and `GET /v1/admin/grants` and `POST /v1/admin/costs/sync` never answered. A longer cycle (a cursor that
  comes back after other pages) is not detected. A grant, in a list or read alone,
  must have a non-empty string `id`, and its `name`, `status`, `currency`, total and `metadata` must be
  strings where present: an object that is not that (`{}`, an error object, `metadata` as `null` or as a
  JSON object) is the same `ProviderError`, never a grant with no tag, and nothing is turned into text
  (`null` was read as the name `"None"`). `metadata` that is free text, or `""`, is a grant the engine did
  not create. The same holds for every value a command's result is built from: the `id` a grant create
  answers with, the `id`, `allocation_id` and `api_key` of a new key, and an allocation's `id` must each
  be a non-empty string, and an allocation's `status` and `currency` strings where present. Before, a
  `null` there was recorded as the text `"None"` in a finished command (provisioning answered 201 with
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
- **Rotating the payment key** records the old key's id and secret reference when it begins, and every
  step reads them from there, never from the actor as it is now. A resume after the new key was stored
  still revokes the old one, and a repeat never revokes the live key (B1). The new key's secret
  reference is recorded before the actor points at it, so a resume reuses that copy.
- **Provisioning** an actor that is already provisioned (not revoked) under a new reference is refused:
  409 `code: already_provisioned` with the actor, and nothing is applied, the amount included (fund it
  instead). The refusal frees the reference. A resumed run answers with the actor only if the actor
  holds the provider key its earlier run recorded: that allocation is this command's (the provider key
  is derived from the actor, so another reference may have had the same key replayed). Otherwise, a
  resume that never got that far or an actor revoked and provisioned again under another reference,
  it is refused the same way and the reference is freed. Re-provisioning after a revoke is a new
  command: it needs a new reference.
- **Creating a grant.** Batteries has no idempotency key on `POST /v1/grants`, so the engine tags each
  grant it creates with `metadata` `{"lpb_operation": <a key derived from the engine, the caller and the
  reference>}` (Batteries stores metadata as text, so the tag is sent as a JSON object in that string). A
  resumed create reads the grants page by page, oldest first, and adopts the one carrying its tag instead
  of creating a second. It records the list cursor after each page, so a resume that runs out of its
  budget in a slow catalog leaves the next one to go on from there; pages already read lacked the tag,
  and a grant created later is on a later page. A list answer the engine cannot read fails the resume as an unknown outcome and never counts as "no such grant": before #114 (C21), a 2xx page with no `items` read as an empty last page, and a list item that was no grant (`{}`, or the grant with its `metadata` as `null` or as an object) read as a grant with no tag, and the resume created a second grant. A read-back of the new grant that the engine cannot read is an unknown outcome too (503, the reference held), never the command's result. Grant names stay free-form: two references may create two grants with one name.
- **Access keys** are written in the same transaction as their record, so they are never `started`:
  a replay returns the key with `token: null` ([access.md](access.md#repeated-key-commands)).
- **Lifetime.** Ledger records are kept; there is no purge in M3.

Which commands take a reference:

| Command | `operation_ref` |
| --- | --- |
| Provision, fund allocation, fund grant, create grant, rotate payment key | **required** (422 when missing): moves money or creates a provider credential |
| Issue access key, rotate access key | **required**: creates a live engine credential |
| Set status, revoke allocation, revoke access key | not accepted: set-to-state or repeat-safe, a repeat cannot apply twice. A repeated set-status sets the same status at the provider again and answers with the actor in that status; after a revoke it is `404`, as for an actor that is not provisioned. A repeated revoke answers as the first did: the actor, `revoked`, with no provider call; `204` for a key (D61). An actor or key that never existed keeps its `404` or `400` |

## Allowance, signer funding and spending guarantees

Three different things, often all called "funding":

| Term | What it is | Who moves it | Repeat-safe |
| --- | --- | --- | --- |
| **Allowance change** | An admin command moves value between a grant and an allocation in Batteries (provision, fund allocation, fund grant, create grant). Bookkeeping in the payment operator's ledger | The engine, on an admin's command | Yes, per `operation_ref` ([above](#repeat-safe-commands)) |
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
`pending` (fees matched to jobs still running) + `unattributed`. `GET /v1/admin/reconciliation` returns each class and `balanced`. In `per_actor`
mode it balances exactly per user.

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
can run at once (the loop and `POST /v1/admin/costs/sync`). Each job has an evidence version: a new
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
- **The job** (`GET /v1/jobs/{id}`) says who and what: `application_id`, `actor_id`, `app`,
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
admin's or auditor's whole application, or one actor with `?actor_id=`) by class (`by_class`), and the
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

## Mode B

Mode B is a hosted payment operator: one Batteries instance serving many enterprises. **Deferred to
M4 (P4).** The known gap: Batteries management credentials are global (a permission covers every
grant), so an operator cannot safely give an engine a management credential. The options to decide
in M4:

- grant-scoped credentials (an upstream Batteries change);
- operator-provisioned keys plus a usage export (per application only);
- an operator-run tenant layer.
