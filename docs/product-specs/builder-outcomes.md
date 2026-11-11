# Builder outcomes

The accepted baseline defines seven outcomes. This table maps each one to the engine's
responsibility and its M2 status.

| # | Outcome | Engine responsibility | M2 status |
| --- | --- | --- | --- |
| 1 | Obtain one credential | `Authenticator` chain; engine API keys; payment keys hidden behind `SecretStore` | API key and JWT bearer (D45) implemented |
| 2 | Discover capabilities | `DiscoveryService` over livepeer/node's `/discovery` contract, read from `LPB_DISCOVERY_URL` (D63) | Implemented |
| 3 | Understand the expected rate | `Offering.rate_low` / `rate_high`: each rate as advertised, with unit, currency, `price_usd`, source and age; `rate_status` and `stale` ([engine.md](../contracts/engine.md#rates-iss-10)). The enterprise sets retail price ([payments.md](../contracts/payments.md#wholesale-and-retail)) | Implemented (ISS-10, closed 2 November) |
| 4 | Invoke a capability | `JobService.run` (immediate), the same in REST and imported mode | Immediate implemented; async and persistent defined (M4) |
| 5 | Receive a result or an understandable failure | `JobResult`, `Failure.kind`, `uncertain` state, recovery after restart | Implemented |
| 6 | Pay without holding crypto | The operator funds the signer; Batteries authorizes per allocation; explicit provisioning | Implemented (per_actor and per_application) |
| 7 | See usage and the resulting charge | `MeasuredUsage` (app-reported), `JobCost` with classification, reconciliation | Implemented, with cost per app (D60); events feed defined and **held** ([M3 decision, item 4](https://github.com/Cloud-SPE/Network-Engineering-SPE/blob/3c365a46550c2099e92b1df1adfb3756b4508c95/docs/decisions/2026-10-21-m3-reference-app-and-scope.md), [D60](../design-docs/m3-decisions.md)) |

**Out of scope for the engine:** customer accounts, retail pricing, invoices, refunds, and the product
catalog's presentation. The reference application demonstrates these with mock commerce.
