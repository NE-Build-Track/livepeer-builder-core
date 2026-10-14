# livepeer/node signer (as relied on by the engine)

Baseline `f970be6` (was `1432123`). Since `f970be6` the signer takes the orchestrator's identity from `TicketParams.Recipient` and ignores `OrchestratorInfo.Address`; payment state and the `orch_address` in events are the recipient. Source: `signer/service.go`, `signer/events.go`, `docs/http-api.md`,
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
  The caller supplies both, and the signer doesn't verify them against the orchestrator (D20, T11).
- **Discovery TLS:** the signer verifies orchestrator certificates and has no opt-out (D27, T12).
- **Event fields:** `auth_id`, `manifest_id`, `session_id` (state ID), `sequence_number`,
  `num_tickets`, `computed_fee` (wei), `computed_fee_usd`, `billable_secs`, `pixels`,
  `payer_address`, `orch_address`.
- **No void or rejection event exists.** `create_signed_ticket` is the only event type. Tickets an
  orchestrator rejects after signing stay charged (D21).
- **Persistent sessions:** the orchestrator handles `POST /apps/{runner_id}/session` (reserve) and
  `…/session/{id}/app/{path}` (an HTTP/SSE/WebSocket proxy). A session keeps its reservation price,
  and ends on stop, expiry, failed health or exhausted credit. The SDK's `reserve_session` funds it
  with time-based payments.
