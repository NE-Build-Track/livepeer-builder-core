# ISS-02: Define the shared engine contracts and supported boundaries

## Status

Owners: John, Mike. GitHub: #8. **Closed by Mike on 19 October.** The contracts are on
`releases/final`. The final idempotency rules (D11, T2) carry forward to M3 (ISS-14, ISS-23); until
then the interim 409 behavior stands.

## Goal

Reviewable definitions for access context, capabilities and rates, jobs and attempts, results,
measured usage, cost states and extension boundaries, covering immediate, async and
persistent-endpoint execution, with deployment, performance and recovery assumptions decided
explicitly.

## Acceptance

- [x] Access context: [contracts/access.md](../../contracts/access.md)
- [x] Capabilities and rates, jobs and attempts, results, usage: [contracts/engine.md](../../contracts/engine.md)
- [x] Cost states and classification: [contracts/payments.md](../../contracts/payments.md#cost-classification)
- [x] Extension boundaries: [ARCHITECTURE.md](../../../ARCHITECTURE.md#providers)
- [x] Immediate (implemented), async and persistent (defined; M4): [engine.md](../../contracts/engine.md#persistent-sessions)
- [x] Events feed (defined; M3): [contracts/events.md](../../contracts/events.md)
- [x] Deployment, performance and recovery assumptions: [engine.md](../../contracts/engine.md#deployment-performance-and-recovery-assumptions)
- [x] Examples: tests in `tests/unit/` and the generated OpenAPI schema
- [ ] Idempotency semantics finalized by John's PR (D11): carried forward to M3 (ISS-14, ISS-23; T2)
- [x] Mike's review (closed 19 October)

## Progress

- [x] 2026-10-12: Scaffolded contracts, protocols and docs. The attempt is written before the call;
      restart recovery and admission limits are implemented.
- [x] 2026-10-12: Failover on an orchestrator-specific signer refusal before payment (D26).
- [x] 2026-10-19: Closed by Mike. John's idempotency PR and the [engine.md#idempotency](../../contracts/engine.md#idempotency)
      update move to M3 (T2).

## Decision log

- 2026-10-12: Async and persistent are defined now and implemented in M4 (D8). The engine proxies the
  persistent `app_url` (D9). The events feed is a definition only (D10).
