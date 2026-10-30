# Batteries API (as used by the engine)

Baseline `ca09e47`. Authoritative source: the Batteries `README.md` and
`internal/app/management.go`.

- **Auth:** `Livepeer-Clearinghouse-Token: <management secret>`. A permission covers **every**
  resource of its type. There is no per-grant scoping (that is the [Mode B](../contracts/payments.md#mode-b) gap).
- **Engine credential** (`LPB_BATTERIES_TOKEN`): `grants.fund`, `allocations.create`,
  `allocations.fund`, `allocations.read`, `allocations.status`, `allocations.revoke`, `api_keys.create`,
  `api_keys.read`, `api_keys.revoke`, `usage.read`. **No `grants.*`.** `grants.fund` covers every
  grant. The engine sends the grant id on the request, or the grant of the caller's own allocation.
- **Grant desk** (`LPB_BATTERIES_OPERATOR_TOKEN`): `grants.read` and `grants.create`. Optional: without
  it the engine starts with the grant desk off (`list_grants`/`create_grant` and their routes absent).

| Engine call | Route | Notes |
| --- | --- | --- |
| `create_key_for_grant` | `POST /v1/api-keys` `{grant_id, name, amount_usd \| amount_eth}` | Atomic: a funded allocation plus a key. Returns `{allocation_id, id, api_key}`. Needs `allocations.create` and `.fund` |
| `create_key` | `POST /v1/api-keys` `{allocation_id, name}` | Key rotation |
| `fund_allocation` | `POST /v1/allocations/{id}/fund` `{amount_*}` | Reactivates an `exhausted` allocation |
| `fund_grant` | `POST /v1/grants/{id}/fund` `{amount_*}` | `Idempotency-Key` is `base64url(sha256(engine_id:fund-grant:{grant_id}:{operation_ref}:fund))` |
| `list_grants` | `GET /v1/grants` | Operator token. `{items: [...]}` |
| `create_grant` | `POST /v1/grants` `{name, status, sponsor, amount_*}` | Operator token. `grants.create` |
| `set_allocation_status` | `PATCH /v1/allocations/{id}/status` `{status: active \| paused}` | |
| `revoke_allocation` | `POST /v1/allocations/{id}/revoke` | Unused funds return to the grant; repeat-safe |
| `revoke_key` | `POST /v1/api-keys/{id}/revoke` | |
| `read_allocation` | `GET /v1/allocations/{id}` | `allocated_<cur>`, `spent_<cur>`, `available_<cur>`, where `<cur>` is the allocation's `usd`/`eth` |
| usage feed | `GET /v1/usage?limit=&cursor=` | Optional filters `grant_id`, `allocation_id`, `manifest_id` (exact IDs; a row must match every one; usage with no session appears only unfiltered). The engine sends none: cost sync reads every row (D28). `{items, next_cursor}`. The cursor is opaque and bound to its filters; empty on the last page; 400 `invalid cursor` on a mismatch |

- **Idempotency:** `Idempotency-Key` on allocation create, API-key create, and grant and allocation
  fund. It is shared by every caller of a grant, which is why the engine namespaces its keys.
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
