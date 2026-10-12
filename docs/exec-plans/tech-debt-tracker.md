# Tech-debt tracker

Known shortcuts and gaps. Each entry names its exit condition.

| # | Debt | Why it exists | Exit condition |
| --- | --- | --- | --- |
| T1 | The SDK is pinned by direct reference to the **head of eliteprox/livepeer-python-gateway PR 5** (`84ec542`: the `on_payment_session` callback plus the lowercase `"state"` fix); it can't be published to PyPI | PR 5 (formerly livepeer #71) isn't merged into `releases/m2` yet | Re-pin to PR 5's merge commit on `releases/m2`; later, a released version |
| T2 | Idempotency: interim 409 semantics | John owns the final design (D11) | John's PR merged; engine.md updated |
| T3 | Capability → app resolution is not implemented (callers name `app`) | Not in M2 scope | Descriptor/config resolver contract (later milestone) |
| T4 | No metrics endpoint (Prometheus) | The harness asks for observability; M2 has structured logs only | Add `/metrics` with job, discovery and cost-sync counters |
| T5 | `per_application` reconciliation needs `LPB_SHARED_ALLOCATION_ID` set by hand | No provider lookup from key to allocation | Batteries API-key read at start (`GET /v1/api-keys/{id}`) |
| T6 | Usage rows assumed immutable per ID (re-reads skip known IDs) | Batteries rows are append-only today | Confirm with the Batteries maintainers, or update on change |
| T7 | Engine-level async, sessions, events, JWT and trusted-backend authenticators | Reserved by decision | Their M3/M4 tickets |
| T8 | The dev stack builds node and Batteries from source (slow first build) | No published images at the pinned SHAs | Use published images when they exist |
| T9 | ~~The SDK can't read `auth_id` from livepeer/node~~ **Resolved 12 October** by SDK PR 5 `84ec542`, which reads `state["State"]` or `state["state"]`. Guarded by `test_sdk_reads_auth_id_from_livepeer_node_state` | Found in the 12 October paid dev-stack run ([evidence](../references/evidence/2026-10-12-dev-stack-paid-run.md)) | Closed. A live confirmation against the node signer waits on T10 |
| T10 | **The node signer rejects every public orchestrator tested (12 October).** `signer/service.go` requires `OrchestratorInfo.address == TicketParams.recipient`. The xode, speedybird, open-pool and hky orchestrators all report an address different from their ticket recipient (a separate signing key), so signing fails with 400 `incomplete or expired orchestrator payment params` | Found in the paid dev-stack run against public orchestrators ([evidence](../references/evidence/2026-10-12-dev-stack-paid-run.md#public-orchestrators)) | Either the orchestrator operators align address and recipient, or livepeer/node's signer accepts a distinct recipient. **12 October: no upstream change for now (D25)** |
