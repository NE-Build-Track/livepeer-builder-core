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
4. The SDK (#71) returns the `auth_ids` on results and errors. The engine records them on the attempt.

## Allocation modes

| Mode | Allocation | Who enforces a user's cap | Per-user spend |
| --- | --- | --- | --- |
| `per_actor` (**default**) | One allocation and key per actor | The signer, at authorization | Exact: the allocation's own `spent_*` |
| `per_application` | One allocation and key from settings (`LPB_SHARED_PAYMENT_KEY`) | Nobody per user; the enterprise must | Only through the engine's `auth_id` join |

In `per_actor` mode there is **no fallback** to a shared key. An unprovisioned actor's job fails
before payment with `not_provisioned`.

## Provisioning

**There is no automatic provisioning.** Every money-moving command is explicit, needs the
`admin.allocations` operation (an admin credential), and carries an idempotency key of
`base64url(sha256(engine_id:operation:step))`.

| Command | Provider call | Idempotency |
| --- | --- | --- |
| `provision(application, actor, amount)` | `POST /v1/api-keys` with `grant_id` (atomic: allocation + key + funding) | per actor; a repeat returns the original |
| `fund(…, FundRequest(amount, operation_ref))` | `POST /v1/allocations/{id}/fund` | per `operation_ref` (required) |
| `set_status(active / paused)` | `PATCH /v1/allocations/{id}/status` | naturally repeatable |
| `revoke` | `POST /v1/allocations/{id}/revoke` (unused funds return to the grant) | repeat-safe upstream |
| `rotate_key(operation_ref)` | `POST /v1/api-keys` with `allocation_id`, then revoke the old key | per `operation_ref` |

- **Zero amount** sets up identity without money. Batteries marks the allocation `exhausted` until an
  explicit fund.
- **The new key** goes straight into the `SecretStore`. The database stores only its `SecretRef`.
- **The engine's management credential** is least-privilege: `allocations.*`, `api_keys.*` and
  `usage.read`. It has **no `grants.*`**. The operator creates and funds the grant.

## Units

`Money(amount: Decimal, currency: "USD" | "ETH")` everywhere. **USD grants are the default**
(`LPB_DEFAULT_CURRENCY`). An allocation inherits its grant's currency. Reported cost carries
`fee_eth` (always present, because signer events require it) and `fee_usd` (when every row is
priced). Batteries amounts are exact decimal strings; the engine stores them as strings and sums
them as `Decimal`.

## Correlation

A charge joins an attempt by **`(allocation_id, payment_session_id ∈ attempt.auth_ids)`**.
`manifest_id` is a label only: the 7 October run showed a client can put any string there
([evidence](../references/evidence/2026-10-07-arbitrum-run.md)). The node signer requires it to equal
the `Auth.SessionID` in the caller's `OrchestratorInfo`, which the caller also supplies, so this still
holds. **Under review (D20, T11):** upstream prefers ManifestID to `auth_id`
([livepeer-python-gateway#74](https://github.com/livepeer/livepeer-python-gateway/issues/74)).

## Cost classification

Each attempt that opened a payment session is classified:

| Class | Rule | Typical enterprise treatment |
| --- | --- | --- |
| `delivered` | The attempt succeeded | Bill under its retail policy |
| `failed_after_payment` | Payment went out and the attempt failed (refused, runner error, **rejected tickets**) | Absorb; don't bill |
| `uncertain` | The job is `uncertain`, or the attempt never reported back | Hold, then reconcile |
| `pending` | Payment went out; no applied usage row yet | Wait. **Never zero** |

Only `applied` usage rows count. **Rows on an allocation that match no recorded attempt are
`unattributed`.**

**Reconciliation:** provider `spent` = `delivered` + `failed_after_payment` + `uncertain` +
`unattributed`. `GET /v1/admin/reconciliation` returns each class and `balanced`. In `per_actor`
mode it balances exactly per user.

**Network cost is the expected value at signing.** Tickets the orchestrator rejects after signing are
still charged, because node has no void event. The operator's wallet loses nothing on-chain, since
rejected tickets are never redeemed. The engine reports these charges as `failed_after_payment`.
Settlement (winning-ticket redemption) is a separate operator-level figure and never per job.

### Job cost status

`none` (no payment), `pending` (some paid attempt lacks rows), `observed`, `corrected` (the total
changed after it was first observed, for example a metered session that signed again). Projections
are persisted in `lpb_cost_projections` and updated by each cost sync.

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
