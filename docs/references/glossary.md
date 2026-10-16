# Glossary

| Term | Meaning |
| --- | --- |
| **Actor** | Whoever a job runs for. An opaque `actor_id` within an `application_id`; the engine never sees customer identity |
| **ActorContext** | The validated actor the core receives: IDs, permissions, credential kind, non-secret attributes |
| **Admin credential** | A credential kind (not a permission) required for `admin.*` operations. The first one comes from `lpb bootstrap-admin` |
| **Allocation** | A Batteries budget carved from a grant, with its own balance, status and API keys |
| **Allocation key (`lpg_…`)** | The Batteries API key the signer authorizes. Held in a `SecretStore`; records hold a `SecretRef` |
| **Allowance** | An allocation's granted, spent and available amounts. A cap that can overshoot (no reservation) |
| **App (runner app)** | The name a Live Runner advertises (for example `blueclaw/chat/qwen3.8-27b`); jobs target an app and a path |
| **Attempt** | One runner call within a job. Records `allocation_id`, signed sessions, `payment_sent` and outcome |
| **`auth_id` / payment session** | The ID Batteries assigns when it authorizes a signing session (`payment_session_id` on usage rows). **The cost join key** |
| **Batteries (Clearinghouse Batteries)** | The payment core: authorization webhook, management API, accounting ledger |
| **Cost class** | `delivered`, `failed_after_payment`, `uncertain` or `pending`, plus `unattributed` at reconciliation |
| **Engine API key (`lpk_…`)** | A caller credential the engine issues; only its hash is stored |
| **Expected value (EV)** | Ticket face value × win probability. What Batteries charges at signing: **network cost** |
| **Grant** | The operator-created Batteries budget that allocations draw from. The engine may `grants.fund` `LPB_GRANT_ID` only, and has no `grants.*` permission |
| **Live Runner** | An application server registered with an orchestrator. Single-shot or persistent mode |
| **`manifest_id`** | The orchestrator's session label from the 402 challenge. The session log uses it, with `allocation_id`, to find the Batteries `payment_session_id`. It is not itself the billing key (D20, T13) |
| **Mode A / Mode B** | Self-operated payments, or a hosted payment operator (deferred to M4) |
| **Node key vs recipient** | An orchestrator's operational signing account vs its registered account that tickets pay. Since `f970be6` the node signer pays the recipient and ignores the node address (T10) |
| **`operation_ref`** | The caller's retry key; one dispatch per (application, actor, ref) |
| **`per_actor` / `per_application`** | Allocation modes: one allocation per actor (default), or one shared configured key |
| **Remote signer** | The livepeer/node service that signs payment tickets for gateways, after Batteries authorizes |
| **Reserved** | Defined in the contract, implemented in a later milestone; answers 501 |
| **Settlement** | On-chain redemption of winning tickets. An operator-level figure, not per job |
| **`uncertain`** | Payment went out (or may have) and the outcome is unknown. Never retried automatically |
