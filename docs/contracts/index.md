# Contracts

What each public interface promises. **Implemented** means code and tests exist. **Defined** means
the contract is fixed but the implementation is scheduled later; calling it answers 501.

| Contract | Ticket | Status |
| --- | --- | --- |
| [engine.md](engine.md): discovery, jobs, attempts, results, usage, failures, idempotency | ISS-02 | Immediate and streamed jobs implemented (streaming is optional: `jobs.streaming`); async and persistent defined (M4) |
| [access.md](access.md): actor context, authenticators, policy, credentials | ISS-03 | API key and policy implemented; JWT and trusted backend defined (M4) |
| [payments.md](payments.md): allocation modes, provisioning, units, cost classification | ISS-04 | Implemented; Mode B defined as a gap (M4) |
| [events.md](events.md): versioned event feed | ISS-02 | Defined (M3) |
| [persistence.md](persistence.md): store, migrations, retention, backup | ISS-06 | Implemented |
| [discovery.md](discovery.md): the discovery service, orchestrator directories (static, on-chain), `GET /discovery` | ISS-09, #45 | Implemented (its own process, `lpb discovery`) |

The REST surface is generated into [../generated/openapi.json](../generated/openapi.json).
