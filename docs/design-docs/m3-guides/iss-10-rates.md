# ISS-10: Network rates and pricing assumptions

**Status:** proposal for discussion, 29 October 2026.\
**Tracker:** ISS-10 ([Network-Engineering-SPE#23](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/23)).\
**Owner:** John (engine); Mike shows it in the console. **Reviewers:** Mike, John.\
**Verified against:** builder-core `releases/m3` at `d3df806`; livepeer/node `406b248` (the console stack's
pin), `v1.0.2` and `e75e5b6`.

## Done when (from the 21 October M3 plan)

Every rate carries amount, currency, unit, source and age; missing or stale rates are marked;
builder-core's payments contract has a "Wholesale and retail" section.

Acceptance: before invocation, callers can inspect available rates with exact units and currency,
source, and relevant assumptions. Missing or stale rates are explicit. A network rate is not represented
as an immutable total-job quote or an enterprise retail price.

## Current state (verified)

**What the network advertises.** A runner registers a USD price per `hour` or `fixed` (per request).
In on-chain mode the orchestrator converts it and advertises, per runner, in its `/discovery` inventory
(`orchestrator/registry.go`, `normalizePriceAt`, present in `v1.0.2` and later):

```json
"price_info": {"price": "4492197255", "price_usd": "0.0000027…", "currency": "wei", "unit": "seconds"}
```

`price` is wei per unit; `unit` is `seconds` (an hourly price divided by 3600) or `fixed`; `price_usd` is
the runner's own USD quote per the same unit, as registered. Off-chain registrations advertise no price.
From `v1.0.4`, orchestrators also stamp each runner with `observed_at`; `v1.0.2` orchestrators (most of
today's fleet, per #70) do not.

**What the engine keeps** (`adapters/sdk.py` `_rate`, `contracts/discovery.py` `NetworkRate`):

| Requirement | Today | Gap |
| --- | --- | --- |
| Amount | `price` as `Decimal` | none |
| Currency | `currency`, **defaulting to `"usd"` when absent** | A missing currency is labelled USD. The contract comment says "usd today"; the network sends `wei` |
| Unit | `unit`, defaulting to `"hour"` | The comment says "hour or request"; the network sends `seconds` or `fixed` |
| USD figure | dropped | `price_usd` is never read (console gap G14) |
| Source | `Runner.orchestrator_url` on `/v1/runners`; nothing on `/v1/offerings` | The public catalog cannot say where a rate came from |
| Age | `Offering.observed_at` = the engine's last poll | The age of the orchestrator's own reading is lost; the discovery service may also serve an orchestrator's last inventory for several failed probes (`LPD_MAX_FAILED_REFRESHES`) |
| Stale | `DiscoveryService.is_fresh()` (3 × `discovery_interval_s`), used only for health | Not on the offering; the console computes its own (300 s) |
| Missing | `rate_low`/`rate_high` are `null` | No reason; rates in another currency or unit are dropped silently by `_comparable_rates` |
| Assumptions | none on the wire; scattered in `engine.md`, `payments.md` | No single statement that a rate is per unit, not a job quote |
| Wholesale and retail | one table row in `engine.md`; the M3 decision (item 6) | No section in `payments.md` |

## Decision proposed

**Report each rate exactly as advertised, with its provenance and age attached, and never derive a price.**
The engine adds fields; it does not convert currencies, quote job totals or apply margins.

### Contract (`contracts/discovery.py`)

`NetworkRate` gains:

| Field | Type | Meaning |
| --- | --- | --- |
| `amount`, `currency`, `unit` | unchanged types | As advertised. **No defaults:** a rate with no currency or unit is not a rate (it becomes missing, below) |
| `amount_usd` | `Decimal \| None` | The runner's own USD quote per the same unit (`price_usd`), as the orchestrator advertises it. Not an engine conversion; `None` when absent or malformed |
| `source` | `Literal["orchestrator"]` | Who stated the rate. One value today; a field so a later price oracle or operator override is explicit |
| `observed_at` | `datetime` | The orchestrator's `observed_at` for that runner when present and sane; otherwise the engine's poll time |
| `observed_by` | `Literal["orchestrator", "engine"]` | Which clock `observed_at` came from |

`Offering` gains:

| Field | Meaning |
| --- | --- |
| `rate_status` | `available`, `missing` (no runner advertises a valid rate), or `stale` |
| `stale` | `true` when the engine's snapshot is not fresh, or the oldest rate in the range is older than `LPB_RATE_MAX_AGE_S` |
| `rates_excluded` | How many advertised rates were left out of the range because their (currency, unit) differs from the group's most common one |

`Offering.observed_at` stays (the engine's poll time). Offerings still name no runner or orchestrator URL
(the public catalog rule, #5); `/v1/runners` (authenticated) keeps `orchestrator_url` beside each rate.

### Parsing (`adapters/sdk.py`)

- `_rate` returns `None` when `price`, `currency` or `unit` is missing or invalid, and the runner keeps
  `rate=None`. It no longer invents `usd` or `hour`.
- Parse `price_usd` into `amount_usd`; a bad value drops only `amount_usd`.
- Parse the runner's `observed_at` (RFC 3339). Reject it, and fall back to the poll time with
  `observed_by="engine"`, when it is more than 30 s in the future (the node's own rule,
  `discovery/shared/inventory.go`).
- Keep the unit vocabulary explicit: `seconds`, `fixed`, and `hour` for older registrations; anything
  else is kept as advertised but cannot be paid (the existing `payment_type` refusal stays).

### Service (`services/discovery.py`)

- `offerings()` computes `rate_status`, `stale` and `rates_excluded` from the snapshot and the injected
  `Clock`. `LPB_RATE_MAX_AGE_S` defaults to 300 s, the console's threshold today (open question Q2).
- No change to `candidates()`, failover or `max_price()`: payment still uses the advertised wei rate.

### Docs

- **`payments.md`, new section "Wholesale and retail"**, from the M3 decision (item 6): wholesale is what
  the network charged (tickets to orchestrators, reported from Batteries accounting); retail is what an
  application charges its users, set and recorded by the application; the engine never computes or
  stores retail. Then **"Pricing assumptions"**:
  - a rate is a price per unit as advertised, not a quote for a job;
  - a metered (`seconds`) job costs the rate times the time it is funded, so its total is known only
    after it ends; a `fixed` job costs one price per paid request;
  - failover may pay more than one orchestrator, up to `LPB_MAX_PAID_ATTEMPTS` (D52);
  - the engine pays at most the advertised rate plus 1.2 % (`_PRICE_PADDING`), the ceiling the signer
    enforces;
  - tickets are probabilistic: the reported cost is the expected value at signing, and a single job's
    settled on-chain cost can differ (core belief 1);
  - `amount_usd` is the runner's registered quote, not a live conversion; the wei amount is what is paid.
- **`engine.md#discovery`**: the new fields, staleness and missing rates; fix the `NetworkRate` comment.
- **OpenAPI**: regenerate `docs/generated/openapi.json`.

## Code areas

| File | Change |
| --- | --- |
| `contracts/discovery.py` | Fields above; docstrings corrected |
| `adapters/sdk.py` | `_rate`, `observed_at` parsing in `_runner_from_raw` |
| `services/discovery.py` | `rate_status`, `stale`, `rates_excluded`; uses `Clock` |
| `config.py` | `rate_max_age_s` (`LPB_RATE_MAX_AGE_S`) |
| `testing/fake_engine.py` | `offerings()` fills the new fields; contract test parity |
| `docs/contracts/payments.md`, `engine.md`, `docs/generated/openapi.json` | As above |

## Test plan (test-driven)

**Levels.** Parsing is unit-tested on raw inventory dicts (the adapter boundary). Offering rules are
unit-tested on `DiscoveryService` with a `FixedClock` and a static source. One HTTP route test pins the
wire shape. No network.

**Starter test.** A runner whose `price_info` has no `currency` produces no rate (today it is labelled
`usd`).

```text
[ ] a price with no currency is not a rate (regression: no "usd" default)
[ ] a price with no unit is not a rate (regression: no "hour" default)
[ ] wei, seconds, and price_usd are kept as advertised; amount_usd parsed
[ ] a malformed price_usd drops only amount_usd
[ ] a runner's observed_at is used, observed_by=orchestrator
[ ] no observed_at: the poll time, observed_by=engine
[ ] observed_at more than 30 s in the future: the poll time, observed_by=engine
[ ] an offering with no valid rate is rate_status=missing
[ ] an offering is stale when the snapshot is not fresh
[ ] an offering is stale when its oldest rate is older than LPB_RATE_MAX_AGE_S
[ ] rates in a minority (currency, unit) are counted in rates_excluded
[ ] GET /v1/offerings carries the new fields and still names no runner URL
[ ] FakeEngine offerings match
[ ] max_price is unchanged for a wei rate (the paid path is untouched)
```

**Definition of done.** The list is empty; the checks in `AGENTS.md` pass; the docs and OpenAPI updated;
the console shows `amount_usd`, source, age and the engine's `stale`, and drops its own G14 note and
300 s rule (console change, Mike).

## Upstream dependencies

None. livepeer/node already advertises everything needed (`price_usd` since `v1.0.2`, per-runner
`observed_at` since `v1.0.4`); the engine falls back to its own poll time for older orchestrators. No SDK
function is involved (the engine parses the raw inventory itself), and Batteries is not in this path.

## Open questions to lock

- **Q1.** Show `amount_usd` (the runner's registered USD quote) next to the wei amount (recommended), or
  keep the engine wei-only and leave USD to applications?
- **Q2.** `LPB_RATE_MAX_AGE_S` default 300 s, or tie it to the discovery interval (3 × 15 s)?
- **Q3.** Keep `observed_at` from the orchestrator when present (recommended), knowing a `v1.0.2`
  orchestrator gives only the engine's poll time?
