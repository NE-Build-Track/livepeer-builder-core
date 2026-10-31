# Builder outcomes

The accepted baseline defines seven outcomes. This table maps each one to the engine's
responsibility and its M2 status.

| # | Outcome | Engine responsibility | M2 status |
| --- | --- | --- | --- |
| 1 | Obtain one credential | `Authenticator` chain; engine API keys; payment keys hidden behind `SecretStore` | API key implemented; JWT bearer defined (M4) |
| 2 | Discover capabilities | `DiscoveryService` over the discovery service (`LPB_DISCOVERY_URL`; on-chain or static orchestrator directories, #45) | Implemented |
| 3 | Understand the expected rate | `Offering.rate_low` / `rate_high`: each rate as advertised, with unit, currency, `price_usd`, source and age; `rate_status` and `stale` ([engine.md](../contracts/engine.md#rates-iss-10)). The enterprise sets retail price ([payments.md](../contracts/payments.md#wholesale-and-retail)) | Implemented (ISS-10 in review) |
| 4 | Invoke a capability | `JobService.run` (immediate), the same in REST and imported mode | Immediate implemented; async and persistent defined (M4) |
| 5 | Receive a result or an understandable failure | `JobResult`, `Failure.kind`, `uncertain` state, recovery after restart | Implemented |
| 6 | Pay without holding crypto | The operator funds the signer; Batteries authorizes per allocation; explicit provisioning | Implemented (per_actor and per_application) |
| 7 | See usage and the resulting charge | `MeasuredUsage` (app-reported), `JobCost` with classification, reconciliation | Implemented; events feed defined (M3) |

**Out of scope for the engine:** customer accounts, retail pricing, invoices, refunds, and the product
catalog's presentation. The reference application demonstrates these with mock commerce.
