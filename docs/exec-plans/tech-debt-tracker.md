# Tech-debt tracker

Known shortcuts and gaps. Each entry names its exit condition.

| # | Debt | Why it exists | Exit condition |
| --- | --- | --- | --- |
| T1 | The SDK is pinned to fork commit `3250c08` by direct reference; it can't be published to PyPI | SDK #71 isn't merged or released | Re-pin to `eliteprox/livepeer-python-gateway` `releases/m2` after John's PR; then to a released version |
| T2 | Idempotency: interim 409 semantics | John owns the final design (D11) | John's PR merged; engine.md updated |
| T3 | Capability → app resolution is not implemented (callers name `app`) | Not in M2 scope | Descriptor/config resolver contract (later milestone) |
| T4 | No metrics endpoint (Prometheus) | The harness asks for observability; M2 has structured logs only | Add `/metrics` with job, discovery and cost-sync counters |
| T5 | `per_application` reconciliation needs `LPB_SHARED_ALLOCATION_ID` set by hand | No provider lookup from key to allocation | Batteries API-key read at start (`GET /v1/api-keys/{id}`) |
| T6 | Usage rows assumed immutable per ID (re-reads skip known IDs) | Batteries rows are append-only today | Confirm with the Batteries maintainers, or update on change |
| T7 | Engine-level async, sessions, events, JWT and trusted-backend authenticators | Reserved by decision | Their M3/M4 tickets |
| T8 | The dev stack builds node and Batteries from source (slow first build) | No published images at the pinned SHAs | Use published images when they exist |
| T9 | **SDK #71 cannot read `auth_id` from livepeer/node.** `auth_id_from_signer_state` reads `state["State"]` (go-livepeer); node returns `state["state"]`. Paid calls through node record no `auth_ids`, so their cost stays `pending` and lands in `unattributed` | Found in the 12 October paid dev-stack run ([evidence](../references/evidence/2026-10-12-dev-stack-paid-run.md)) | John's simplified #71 also reads `"state"` (a one-line fix); re-pin T1 |
| T10 | **The node signer rejects every public orchestrator tested (12 October).** `signer/service.go` requires `OrchestratorInfo.address == TicketParams.recipient`. The xode, speedybird, open-pool and hky orchestrators all report an address different from their ticket recipient (a separate signing key), so signing fails with 400 `incomplete or expired orchestrator payment params` | Found in the paid dev-stack run against public orchestrators ([evidence](../references/evidence/2026-10-12-dev-stack-paid-run.md#public-orchestrators)) | Either the orchestrator operators align address and recipient, or livepeer/node's signer accepts a distinct recipient. **12 October: no upstream change for now (D25)** |
