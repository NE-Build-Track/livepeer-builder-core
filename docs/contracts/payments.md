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
   billed. A lost reply, a signer 5xx, a cancel during `get_payment`, or the engine dying then leaves
   the session `unknown`: the cost stays `pending` until usage matches it, and the job pays no other
   orchestrator.

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
  new reference.
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
  and a grant created later is on a later page. Grant names stay free-form: two references may create two grants with one name.
- **Access keys** are written in the same transaction as their record, so they are never `started`:
  a replay returns the key with `token: null` ([access.md](access.md#repeated-key-commands)).
- **Lifetime.** Ledger records are kept; there is no purge in M3.

Which commands take a reference:

| Command | `operation_ref` |
| --- | --- |
| Provision, fund allocation, fund grant, create grant, rotate payment key | **required** (422 when missing): moves money or creates a provider credential |
| Issue access key, rotate access key | **required**: creates a live engine credential |
| Set status, revoke allocation, revoke access key | not accepted: set-to-state or repeat-safe, a repeat cannot apply twice |

## Allowance, signer funding and spending guarantees

Three different things, often all called "funding":

| Term | What it is | Who moves it | Repeat-safe |
| --- | --- | --- | --- |
| **Allowance change** | An admin command moves value between a grant and an allocation in Batteries (provision, fund allocation, fund grant, create grant). Bookkeeping in the payment operator's ledger | The engine, on an admin's command | Yes, per `operation_ref` ([above](#repeat-safe-commands)) |
| **Signer funding** | The payer's TicketBroker deposit and reserve on Arbitrum, which back the tickets the signer signs | The payment operator, outside the engine | No engine command touches it |
| **Spending guarantee** | A hard limit no job can exceed | Nobody in M3 | n/a: the allowance is a cap that [can overshoot](#allowance); hard limits are stretch scope |

## Units
## Units

`Money(amount: Decimal, currency: "USD" | "ETH")` everywhere. **USD grants are the default**
(`LPB_DEFAULT_CURRENCY`). An allocation inherits its grant's currency. Reported cost carries
`fee_eth` (always present, because signer events require it) and `fee_usd` (when every row is
priced). Batteries amounts are exact decimal strings; the engine stores them as strings and sums
them as `Decimal`.

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
