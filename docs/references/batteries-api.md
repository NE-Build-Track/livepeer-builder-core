# Batteries API (as used by the engine)

Baseline v0.0.3, `03bc9fb` (`internal/app/management.go` is unchanged since `ca09e47`).
Authoritative source: the Batteries `README.md` and `internal/app/management.go`.

- **Auth:** `Livepeer-Clearinghouse-Token: <management secret>`. A permission covers **every**
  resource of its type. There is no per-grant scoping (that is the [Mode B](../contracts/payments.md#mode-b) gap),
  so the engine keeps one tenant from another itself ([tenants](../contracts/payments.md#tenants)).
- **A tenant's id is a grant's id (D83, #165).** Every `grant_id` and grant `{id}` below is the
  engine's `tenant_id`.
- **Engine credential** (`LPB_BATTERIES_TOKEN`): `grants.fund`, `allocations.create`,
  `allocations.fund`, `allocations.read`, `allocations.status`, `allocations.revoke`, `api_keys.create`,
  `api_keys.read`, `api_keys.revoke`, `usage.read`. **No `grants.*`.** `grants.fund` covers every
  grant. The engine sends the tenant's id as the grant id.
- **Ids in paths:** every id the engine places in a route below (`{id}`) is percent-encoded as one path
  segment (`adapters/batteries.py`, `segment`; #113, C10), and the ids `.` and `..` are encoded too
  (`%2E`, `%2E%2E`).
- **Tenant desk** (`LPB_BATTERIES_GRANTS_TOKEN`): `grants.read`, `grants.create`, `grants.status`,
  `allocations.read`, and `grants.fund`, which Batteries also asks of a grant created with an amount
  (`internal/app/management.go`: a create with a non-zero amount checks `<resource>.fund`). For the
  wallet report (#167) it also needs `escrow.read`. Optional:
  without it the engine starts with the tenant desk off (onboarding, the tenant list, a grant's read
  and a tenant's status are absent).

| Engine call | Route | Notes |
| --- | --- | --- |
| `create_key_for_grant` | `POST /v1/api-keys` `{grant_id, name, amount_usd \| amount_eth}` | Atomic: a funded allocation plus a key. Returns `{allocation_id, id, api_key}`. Needs `allocations.create` and `.fund`. An amount larger than the grant's unallocated balance is 409 "insufficient unallocated grant balance"; an unknown grant is 404; a closed grant is 409. A paused grant still takes it |
| `create_key` | `POST /v1/api-keys` `{allocation_id, name}` | Key rotation |
| `top_up_allocation` | `POST /v1/allocations/{id}/fund` `{amount_*}` | Reactivates an `exhausted` allocation. Drawn from the allocation's own grant, which Batteries finds by the allocation: the same 409 when the grant's unallocated balance is smaller |
| `top_up_grant` | `POST /v1/grants/{id}/fund` `{amount_*}` | `Idempotency-Key` is `base64url(sha256(engine_id:fund-grant:{grant_id}:{operation_ref}:fund))` |
| `list_grants`, `grant_page` | `GET /v1/grants` | Grants token. `{items: [...], next_cursor}`. A resumed onboarding reads it page by page for its tag |
| `create_grant` | `POST /v1/grants` `{name, status, sponsor, amount_*, metadata}` | Grants token. `grants.create`, and `grants.fund` unless the amount is zero. No `Idempotency-Key`: Batteries takes none on a grant. Answers `{id}`, which is the tenant's id |
| `read_grant` | `GET /v1/grants/{id}`, then `GET /v1/allocations?grant_id={id}` | Grants token. `grants.read` and `allocations.read`. The same allocations give `spent`, the sum of their `spent_<cur>` (#166): Batteries keeps it on a revoked allocation, late charges included. A grant is served with `total_<cur>` and `status`, and with no unallocated balance. The engine answers the total less the sum of the allocations' `allocated_<cur>`: allotting and topping up move an amount from the grant's unallocated account to the allocation and add it to `allocated`, and a revoke moves back what was not spent and takes it off `allocated` (`internal/store/manage.go`). The same allocations give `allocated_unspent` (#167): for each allocation its `allocated_<cur>` less its `spent_<cur>`, and nothing where it spent more, summed. Batteries charges an allocation when it ingests signed usage, after it authorized, and moves the fee from the allocation's available account to its spent account whatever is left (`internal/store/ingest.go`): an overdrawn allocation is served with `spent_<cur>` above `allocated_<cur>`, `available_<cur>` below zero and the status `exhausted`. A revoked allocation adds nothing: the revoke took what it returned off `allocated`, which is then no more than `spent`, and a revoke with nothing available returns nothing. An unknown grant is 404 |
| `grant_balances` | `GET /v1/grants`, then `GET /v1/allocations` (every page of both) | Grants token. The same figures for every grant, the unallocated balance, `spent` and `allocated_unspent`, grouped by the allocations' `grant_id` |
| `set_grant_status` | `PATCH /v1/grants/{id}/status` `{status: active \| paused}` | Grants token. `grants.status`. No `Idempotency-Key`: the call sets a state. Batteries also takes `draft` and `closed`; the engine sends neither, and a closed grant cannot be reopened (409) |
| `escrow_accounts` | `GET /v1/escrow/report` | Grants token. `escrow.read`. The wallet's deposit and reserve, for the wallet report (#167). A JSON array, not an object: one item for each chain, TicketBroker contract and sender the on-chain listener follows, `{chain_id, contract_address, sender, confirmed_through_block, deposit_balance_eth, reserve_balance_eth, total_balance_eth}`, every value a string (`internal/store/settlements.go`, `EscrowReport`). The store holds wei and the API serves each amount as a decimal string of ETH (`internal/app/presentation.go`). The amounts are the listener's ledger of confirmed TicketBroker events through `confirmed_through_block`: it opens with `getSenderInfo` at its first block and applies events after `Confirmations` blocks (64 by default), polled every `PollInterval` (5 s). The listener is off by default (`EnableOnchainListener`, `--enable-onchain-listener`, with an RPC address and `SignerAddresses`): then the array is empty. The engine reads `deposit_balance_eth` and `reserve_balance_eth` and never the sum; an item it cannot read fails the call, never a zero, and an amount below zero is one it cannot read |
| `set_allocation_status` | `PATCH /v1/allocations/{id}/status` `{status: active \| paused}` | |
| `revoke_allocation` | `POST /v1/allocations/{id}/revoke` | Unused funds return to the grant; repeat-safe |
| `revoke_key` | `POST /v1/api-keys/{id}/revoke` | |
| `read_allocation` | `GET /v1/allocations/{id}` | `allocated_<cur>`, `spent_<cur>`, `available_<cur>`, where `<cur>` is the allocation's `usd`/`eth` |
| usage feed | `GET /v1/usage?limit=&cursor=` | Optional filters `grant_id`, `allocation_id`, `manifest_id` (exact IDs; a row must match every one; usage with no session appears only unfiltered). Cost sync sends none: it reads every row (D28). A tenant's usage route (`tenant_page`, #166) sends `grant_id`, the tenant's id: Batteries keeps a row whose payment session belongs to an allocation of that grant (`internal/store/manage.go`: `payment_session_id IN (SELECT id FROM payment_sessions WHERE allocation_id IN (SELECT id FROM grant_allocations WHERE grant_id=?))`), and an unknown grant is an empty page. A row names no orchestrator: that is on the session (`GET /v1/sessions`), which the engine does not read. `{items, next_cursor}`. The cursor is opaque and bound to its filters; empty on the last page; 400 `invalid cursor` on a mismatch. An item's `id` and its other text fields are strings (or `null`), `created_at_ms` is a whole number, and a fee is a decimal string, `null`, `""` or `"[error]"`. The decimal string is digits, then a point and up to 18 digits when the amount has a fraction, after a minus sign when it is negative, with no exponent whatever the size (`internal/units/eth.go`, `UnitsToDecimal`; the columns are `TEXT` in a `STRICT` table, so no fee is a JSON number; read at `03bc9fb`), and an `id` is 22 base64url characters (`internal/store/store.go`, `ID`). An item the engine cannot read as a usage row (no `id` or a blank one, an unknown `status`, no usable `created_at_ms`, a text field that is not text, a fee that is stated and is not such a decimal string, or an item the JSON decoder's limits refuse) is set aside, not an error, on the cost-sync page and on a tenant's page alike (#114, C13; #158; [payments.md](../contracts/payments.md#cost-classification)) |

- **Answers:** every route above answers a success with a JSON object (`managementResult`), and a list
  with `{"items": [...], "next_cursor": "<string>"}`: `items` is `[]` when empty, never `null`, and
  `next_cursor` is `""` on the last page (`internal/app/pagination.go`, read at `03bc9fb`). The engine
  holds every answer to that: a 2xx that is not a JSON object, `items` that is not a list of objects, or
  a `next_cursor` that is not a string is a `ProviderError` (502), never an empty answer (#114, C21;
  `adapters/batteries.py`, `json_object` and `list_page`). The engine also takes a list with no
  `next_cursor` as a last page, and a page that answers the cursor it was asked with, or one the list
  was at earlier (#158), as that `ProviderError`: Batteries' cursor is the last row's `seq`, so it always
  advances. A grant's `id`, `name`, `status`, `currency`, total and `metadata` are
  strings: `metadata` is `""` for a grant with none, never `null` (`TEXT NOT NULL DEFAULT ''` in
  `migrations/001_initial.sql`). A grant with no `id`, or with one of these that is not a string, is the
  same `ProviderError` (`grant_from_item`); the engine takes a grant with no `metadata` key as one with
  none. Every id Batteries answers with is a string, and so is a new key's `api_key`
  (`map[string]string` in `internal/store/idempotency.go`; `TEXT` columns): a key answer or an allocation read
  whose `id`, `allocation_id` or `api_key` is absent, empty or not a string is that `ProviderError` too.
- **A paused grant** (`internal/store/auth.go`, read at `fb76150`): the authorization webhook refuses
  every payment whose grant is not `active` (403, "grant or allocation is inactive"). The management API
  refuses only a `closed` grant, so allotting, topping up, revoking and a grant's top-up still work on a
  paused one.
- **Idempotency:** `Idempotency-Key` on allocation create, API-key create, and grant and allocation
  fund. It is shared by every caller of a grant, which is why the engine namespaces its keys. A key is
  kept per grant: the same key under another grant is another operation.
  - A matching retry replays the original success, including the secret.
  - A different body returns 409.
  - Errors are not saved.
  - Saved successes never expire. A replay after revocation does not restore access.
  - **Batteries stores replayed API-key secrets in plaintext in `management_idempotency`. Protect its
    backups like a vault.**
- **Accounting:** authorization checks for a positive balance and **does not reserve funds**. Each
  applied `create_signed_ticket` event debits the allocation (USD allocations by `computed_fee_usd`).
  Events are de-duplicated by ID.
- **Migrations:** #19 and #20 edited `001_initial.sql` in place, so a database migrated before them
  must be rebuilt.
