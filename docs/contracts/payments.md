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
4. The SDK ([#77](https://github.com/livepeer/livepeer-python-gateway/pull/77)) calls
   `on_payment_session` with each signed session. The engine records that session's `manifest_id`
   on its own session log, with the allocation whose key paid.

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
| `provision(application, actor, amount, grant_id)` | `POST /v1/api-keys` with `grant_id` (atomic: allocation + key + funding) | per actor; a repeat returns the original |
| `fund(…, FundRequest(amount, operation_ref))` | `POST /v1/allocations/{id}/fund` | per `operation_ref` (required). `grant_id` on this body is ignored |
| `fund_grant(FundRequest(amount, operation_ref, grant_id))` | `POST /v1/grants/{grant_id}/fund` | `base64url(sha256(engine_id:fund-grant:{grant_id}:{operation_ref}:fund))` |
| `list_grants()` | `GET /v1/grants` with `LPB_BATTERIES_OPERATOR_TOKEN` | read |
| `create_grant(name, amount)` | `POST /v1/grants` with `LPB_BATTERIES_OPERATOR_TOKEN` | a repeat creates another grant |
| `set_status(active / paused)` | `PATCH /v1/allocations/{id}/status` | naturally repeatable |
| `revoke` | `POST /v1/allocations/{id}/revoke` (unused funds return to the grant) | repeat-safe upstream |
| `rotate_key(operation_ref)` | `POST /v1/api-keys` with `allocation_id`, then revoke the old key | per `operation_ref` |

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
  The engine does not start when `LPB_BATTERIES_URL` is set and that token is missing.
- **Cost sync** reads `GET /v1/usage` with no grant filter. Rows for allocations this engine did not
  provision are stored and are not part of any actor's reconciliation. A cursor saved under the old
  `grant_id` filter does not match, so the first sync after this change restarts from the beginning
  and skips rows it already stored.

## Units

`Money(amount: Decimal, currency: "USD" | "ETH")` everywhere. **USD grants are the default**
(`LPB_DEFAULT_CURRENCY`). An allocation inherits its grant's currency. Reported cost carries
`fee_eth` (always present, because signer events require it) and `fee_usd` (when every row is
priced). Batteries amounts are exact decimal strings; the engine stores them as strings and sums
them as `Decimal`.

## Correlation

A charge joins an attempt by **`(allocation_id, payment_session_id)`**. The engine does not learn
`payment_session_id` from the SDK. Each attempt records the allocation whose key paid and one
session-log entry per signed `manifest_id` (`lpb_payment_sessions`). Cost sync resolves an entry
when exactly one applied usage row-set and one log entry share `(allocation_id, manifest_id)`, and
stores that row's `payment_session_id` as `matched`.

If Batteries has no session for the key yet, the entry stays `unmatched` and the attempt's cost is
`pending`. If more than one Batteries session or more than one log entry shares the key, every
entry is `ambiguous`: the attempt is `uncertain`, those rows stay `unattributed`, and reconciliation
still balances. A later sync can turn a `matched` entry into `ambiguous`.

`manifest_id` is the lookup, not the cost key. The 7 October run showed a client can put any string
there ([evidence](../references/evidence/2026-10-07-arbitrum-run.md)). A manifest on another
allocation does not attribute. Whether a fresh manifest per challenge is an orchestrator guarantee
is open (T13, [livepeer-python-gateway#74](https://github.com/livepeer/livepeer-python-gateway/issues/74)).

## Cost classification

Each attempt that opened a payment session is classified:

| Class | Rule | Typical enterprise treatment |
| --- | --- | --- |
| `delivered` | The attempt succeeded | Bill under its retail policy |
| `failed_after_payment` | Payment went out and the attempt failed (refused, runner error, **rejected tickets**) | Absorb; don't bill |
| `uncertain` | The job is `uncertain`, or the attempt never reported back | Hold, then reconcile |
| `pending` | The job is still running, or payment went out and no applied usage row has arrived yet | Wait. **Never zero** |

Only `applied` usage rows count. **Rows on an allocation that match no recorded attempt are
`unattributed`.**

**Reconciliation:** provider `spent` = `delivered` + `failed_after_payment` + `uncertain` + `pending` +
`unattributed`, where `pending` holds the fees matched so far on running jobs. `GET /v1/admin/reconciliation` returns each class and `balanced`. In `per_actor`
mode it balances exactly per user.

**Network cost is the expected value at signing.** Tickets the orchestrator rejects after signing are
still charged, because node has no void event. The operator's wallet loses nothing on-chain, since
rejected tickets are never redeemed. The engine reports these charges as `failed_after_payment`.
Settlement (winning-ticket redemption) is a separate operator-level figure and never per job.

### Job cost status

`none` (the job ended as `succeeded` or `failed` with no payment), `pending` (the job has not ended,
it ended `uncertain` with no payment evidence, or some paid attempt lacks rows), `observed`,
`corrected` (the total changed after it was first observed, for example a metered session that
signed again). Projections are persisted in `lpb_cost_projections` and updated by each cost sync,
for ended jobs only. A job that has not ended is computed on each read, with the fees matched so far
and status `pending`, so ending without payment becomes `none` without waiting for the next sync.
An ended job whose projection is missing, `pending`, or older than its `completed_at` is recomputed
(and stored) on read, so a fee matched while it ran shows as soon as it ends. Summaries and
reconciliation do the same for ended jobs in their window, and include running jobs as `pending`.

In code, statuses and classes are the `CostStatus` and `CostClass` `StrEnum`s, whose values are the
strings above. `CostStatus.has_cost` is true for `observed` and `corrected`.

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
