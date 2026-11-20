# livepeer/node signer (as relied on by the engine)

Baseline livepeer/node `eee09c4` (v1.0.7; was `f45d83f`, 10 commits after v1.0.6, `406b248` / v1.0.2 before that, and `f970be6` and `1432123` earlier). Since `f970be6` the signer takes the orchestrator's identity from `TicketParams.Recipient` and ignores `OrchestratorInfo.Address`; payment state and the `orch_address` in events are the recipient. Source: `signer/service.go`, `signer/events.go`, `docs/http-api.md`,
`docs/signer.md`.

- **Authorization webhook:** before signing, the signer POSTs `{headers, state}` to the configured
  webhook (Batteries). The request carries the client's headers, including the nested `Authorization:
  Bearer lpg_…`. The decision is a JSON `status` (200 approves), with an optional `auth_id` and
  `maxPrice`. A webhook that is unavailable or returns non-200 → 502.
- **Accounting events: one per signing request, and no payment without an event.** Every signing
  request emits exactly one `create_signed_ticket` event (`service.go:468`). It is **persisted to a
  SQLite outbox before the payment is returned**. If it cannot be persisted, the signer withholds the
  payment (503 when the outbox is full or unavailable, 413 when the event is too large). Delivery to
  Kafka is at least once, with stable event IDs.
- **ManifestID:** an empty `ManifestID` defaults to `OrchestratorInfo.Auth.SessionID`; any other value must
  equal it, or the signer answers 400 `manifest ID does not match payment params` (`service.go:371`).
  The caller supplies both, and the signer doesn't verify them against the orchestrator (D20, T13).
- **Chain observer (since v1.0.7, node #38 to #43):** the signer keeps the chain state a payment needs
  in memory: the block, the round and its block hash, the ticket validity period, and each known
  payer's deposit and reserve. A background task refreshes it every 5 seconds and treats it as expired
  after 30 seconds without a refresh; both are fixed in `eth/payment_observer.go`, and no flag or config
  option changes them. Measured on the dev stack on 17 and 18 November: about 28 RPC requests per
  refresh with no jobs running (about 480,000 a day), most of them one `eth_getBlockByNumber` per
  Arbitrum block; the first payment to an orchestrator added 2 foreground requests
  (`livepeer_rpc_work_total{work="foreground",kind="transport"}` on the metrics port). An operator's RPC
  plan must allow for the idle traffic. When the signer has no usable observation (the RPC provider is
  down or lags by 30 seconds, or four first reads of a new payer and orchestrator pair are already running), signing fails
  400 `payer funds unavailable`, as it did before when a direct read failed, and `/readyz` on the
  metrics port answers 503.
- **Behind a clearinghouse (#166):** a clearinghouse's engine serves the signer's two paths and hands
  each call to this signer (`adapters/signer_forward.py`). It relies on four things read at the pin.
  `POST /sign-orchestrator-info` reads no body and asks nobody: it signs the signer's own address
  (`service.go`, `signInfo`). `POST /generate-live-payment` reads at most 1 MiB and answers 400 for more.
  The authorization webhook gets every header of the request (`auth.go`: `{headers, state}`), so the
  clearinghouse sends the Batteries key and no header of its caller's, and `docs/signer.md` asks a proxy
  to overwrite `Signer-` headers. A webhook decision of 400 to 599 is the signer's status, which is how
  an empty allocation is its 402 and a paused one its 403.
- **Readiness, as the engine can ask it (#167):** the wallet report asks the signer with
  `POST /sign-orchestrator-info` on its main port, the call the SDK begins every payment with. At the
  pin it takes no credential (`signer/service.go`, `signInfo`): it answers the wallet's address and a
  signature over it, so an answer says the signer is reachable and holds the key. The signer's own
  `/readyz` (payer funds, the price policy and the event producer) is on its metrics listener,
  `127.0.0.1:8938` by default (`signer/app.go`), which the engine is given no address for (T34).
- **Discovery:** the engine does not use the signer for discovery, and the signer contacts no
  orchestrator (D63; T12 is resolved). It only signs.
- **Event fields:** `auth_id`, `manifest_id`, `session_id` (state ID), `sequence_number`,
  `num_tickets`, `computed_fee` (wei), `computed_fee_usd`, `billable_secs`, `pixels`,
  `payer_address`, `orch_address`.
- **No void or rejection event exists.** `create_signed_ticket` is the only event type. Tickets an
  orchestrator rejects after signing stay charged (D21).
- **Persistent sessions:** the orchestrator handles `POST /apps/{runner_id}/session` (reserve) and
  `…/session/{id}/app/{path}` (an HTTP/SSE/WebSocket proxy). A session keeps its reservation price,
  and ends on stop, expiry, failed health or exhausted credit. The SDK's `reserve_session` funds it
  with time-based payments.
