# ISS-10: Network rates and pricing assumptions

## Status

- **Owner:** John (engine). **Reviewers:** Mike (design, console), Josh (engine code).
- **Ticket:** ISS-10 ([Network-Engineering-SPE#23](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/23)).
  Guide: `docs/design-docs/m3-guides/iss-10-rates.md` in [PR #74](https://github.com/NE-Build-Track/livepeer-builder-core/pull/74) (`2e82565`).
- **Branch:** `tasks/m3/iss-10-rates`, off `releases/m3` at `d3df806`.
- **State:** built test-first; awaiting review. The console change (Mike) and the go-livepeer upstream
  request (below) are open.
- **Decision:** D58 (proposed).

## Goal

Before invoking anything, a caller can read every available rate with its exact amount, currency and
unit, who stated it, how old it is, and the assumptions behind it. A missing or stale rate says so. A
network rate is never presented as a total-job quote or a retail price, and the engine never signs a
payment without a price ceiling.

## Acceptance

From the 21 October M3 plan and the guide's "Done when":

- [x] Every rate carries amount, currency, unit, source and age: `ObservedRate` (`amount`, `currency`,
  `unit`, `price_usd`, `source`, `observed_at`, `observed_by`) on `Offering.rate_low`/`rate_high`.
- [x] Missing and stale rates are explicit: `Offering.rate_status`, `stale`, `rates_excluded`;
  `Runner.rate_error` says why an advertised price is not a rate.
- [x] A network rate is not represented as a total-job quote or a retail price: no defaults, no
  conversion, no job total; [payments.md](../../contracts/payments.md#pricing-assumptions).
- [x] `payments.md` has "Wholesale and retail" and "Pricing assumptions".
- [x] Tests written first, each seen failing for the right reason; characterization tests proved by
  mutation.
- [x] `engine.md`, `RELIABILITY.md`, `m3-decisions.md` (D58) and the generated docs.
- [ ] Mike's review of D58 and the contract; Josh's review of the engine code.
- [ ] Console (Mike): show `price_usd`, source, age and the engine's `stale`; drop the G14 note and its
  own 300 s rule.
- [ ] The guide amendments below, applied to `iss-10-rates.md` once PR #74 merges.

## Design

The contract and rules are in [engine.md](../../contracts/engine.md#rates-iss-10) and
[payments.md](../../contracts/payments.md#pricing-assumptions). In brief:

| Concern | Where | Rule |
| --- | --- | --- |
| Parse | `adapters/sdk.py::_rate`, `_stamp` | A rate exactly as advertised, or a reason. No `usd`/`hour` defaults. `price_usd` kept when it is a finite, nonnegative decimal. `observed_at` read as [RFC 3339](https://www.rfc-editor.org/rfc/rfc3339) in UTC |
| Age | `services/discovery.py::_observed` | The orchestrator's stamp when present and at most 30 s ahead of the poll, else the poll time; `observed_by` names the clock |
| Status | `DiscoveryService.offerings` | `rate_status`, `stale` (snapshot not fresh, or oldest rate older than `LPB_RATE_MAX_AGE_S`), `rates_excluded` |
| Money | `adapters/runner_call.py::PaidRequest._answered` | A 402 for a runner with no ceiling is refused before the signer is asked |

### Why the plan departs from the guide

| Guide | Built | Why |
| --- | --- | --- |
| `observed_at`/`observed_by` on `NetworkRate` | On `ObservedRate`, which the catalog serves; `Runner.observed_at` holds the raw stamp | Sources (static, embedders') build rates but cannot know the engine's poll time, and the 30 s rule needs "now". The adapter parses; the service decides, with its injected `Clock` |
| `rate_status ∈ {available, missing, stale}` plus `stale` | `rate_status ∈ {available, missing}` plus `stale` | The guide's pair admits two contradictory combinations; every combination of these two is legal |
| `amount_usd` | `price_usd` | livepeer-node's own name; the engine echoes the node's published language |
| `LPB_RATE_MAX_AGE_S` 300 s (Q2) | 600 s | `lpb discovery` evicts a failing orchestrator after about 3 min, so 300 s never fires there; livepeer-node discovery refreshes every 5 min by default, so 300 s flags healthy rates |
| "No change to … `max_price()`" | Unchanged, plus the ceiling guard | Dropping the defaults turns an incomplete price into `rate=None`, which sends no `maxPrice`; the node signer then checks no price at all (`signer/service.go::checkMaxPrice`) |
| "`hour` for older registrations" | `hour` is registration-side only | No released livepeer-node or go-livepeer advertises `hour` in `/discovery` |
| `testing/fake_engine.py` fills the new fields | No change | `FakeDiscovery` returns the offerings its caller supplies |

### Upstream evaluation

| Path | `price_info` |
| --- | --- |
| livepeer-python-gateway registration (`LiveRunnerPriceInfo.to_json`), used by runner-app-examples | Always `price` (USD), `currency` (default `usd`), `unit` (default `hour`); the examples also use `fixed` and `720p` |
| livepeer-node registration (`validateHeartbeat`, `normalizePriceAt`; `v1.0.1` to `main`) | `usd`; `hour` or `fixed`; refuses `720p` |
| livepeer-node `/discovery` | On-chain `{price: wei (rounded up), price_usd, currency: "wei", unit: "seconds" or "fixed"}`, listed only while the price is available; off-chain no `price_info`; `observed_at` from the `v1.0.3` tag |
| go-livepeer registration (`normalizeLiveRunnerPriceInfo`, `v0.9.0` to master) | `usd`; `hour`, `720p` or `fixed`; the USD price is kept in `runner.PriceInfo` |
| go-livepeer `/discovery` (`discoveryRunner`, `convertPrice`) | `{price: wei (rounded down), currency: "wei", unit: "seconds", "fixed" or "720p-pixel-seconds"}`; no `price_usd`, no `observed_at`; no `price_info` off-chain **or while an on-chain runner's price feed is not ready** |
| Node signer (`checkMaxPrice`) | The ceiling must be wei with the payment type's unit; with no ceiling, no check |

### go-livepeer upstream request (draft; not filed)

To file on livepeer/go-livepeer only after the owner approves (AGENTS.md: upstream changes escalate):

> **Advertise `price_usd` in orchestrator `/discovery`, as livepeer/node does.**
> `LiveRunnerPriceInfo.PriceUSD` exists since v0.9.3 but only the remote signer fills it, with its own
> wei-to-USD conversion. The orchestrator already holds the runner's registered USD price
> (`runner.PriceInfo`). In `discoveryRunner()`, set `PriceUSD` from it per the advertised unit, as an
> exact decimal string: `hour` → USD/3600 per second; `fixed` → USD per request; `720p` → USD per
> 720p pixel-second (USD / (3600 × 1280 × 720 × 30)). Optionally stamp `observed_at` (RFC 3339, response
> time) as livepeer/node v1.0.4 does. Consumers can then read one meaning of `price_usd` from both
> implementations.

### Guide amendments (for `iss-10-rates.md` once PR #74 merges)

1. `observed_at` is the orchestrator's response time. It measures inventory freshness and carries
   information only through a cache (`lpb discovery`, livepeer-node discovery).
2. Upstream dependency: no orchestrator sampled on 29 October stamps `observed_at`, so today every rate
   is `observed_by=engine`. They serve go-livepeer's inventory, not livepeer-node `v1.0.2`'s as #70
   assumed (see Progress).
3. The engine is deliberately looser than livepeer-node's collectors, which drop an unstamped runner.
4. `720p-pixel-seconds` (go-livepeer) is kept as advertised and is not payable; `hour` and `usd` are
   registration-side only.
5. `amount_usd` becomes `price_usd`; the raw stamp is `Runner.observed_at`; the catalog type is
   `ObservedRate`; the status is two fields.
6. go-livepeer's `price_usd` is absent pending the upstream request above.
7. The stamp is in the `v1.0.3` tag (first released in `v1.0.4`). Replace the unresolvable `e75e5b6`
   with `a4b68ed` (`main`) or `v1.0.6` (`c7be946`).
8. `LPB_RATE_MAX_AGE_S` defaults to 600 s (Q2), with the reason above.
9. The ceiling guard (D58 (5)).

## Out of scope

- The console change (Mike).
- The SDK's own paid path, which re-applies `usd`/`hour` in `maxPrice` and signs uncapped when
  `price_info` is absent (T23).
- A go-livepeer adapter module: none is needed until a go-livepeer-only rule appears (for example, a
  `price_usd` whose meaning differs from livepeer-node's).
- Moving runner-app-examples from go-livepeer to livepeer-node (their `720p` unit is refused there).

## Progress

- 29 October: reviewed the guide against #70, livepeer/node (`406b248`, `v1.0.1`–`v1.0.6`, `a4b68ed`),
  go-livepeer (`v0.9.0`–master `e4bedf43`), livepeer-python-gateway `2017a80` and runner-app-examples
  `a526b43`; plan agreed with John.
- 29 October: built test-first on `d3df806`. Baseline 1318 passed, 31 skipped. One baseline failure was
  environmental and deselected: `test_built_wheel_contains_migrations` reads a stale `dist/` wheel built
  27 October, before migrations `0007` and `0008`.
  - **Parsing** (`tests/unit/test_rates.py`): no currency, no unit, unreadable price (each with its
    reason), no `price_info`, a livepeer-node on-chain price with `price_usd`, a bad `price_usd`, and
    `observed_at` in UTC.
  - **Catalog:** orchestrator stamp, engine poll, the 30 s skew bound on both sides, `rate_status`,
    `stale` from the snapshot and from the max age at its boundary, `rates_excluded`, and
    `LPB_RATE_MAX_AGE_S` through `EngineSettings` and assembly.
  - **Money** (`tests/unit/test_runner_call.py`): a runner with no usable price that answers 402 is
    refused before the signer is asked; seen red without the guard (the engine signed uncapped).
  - **Wire** (`tests/unit/test_http.py`): offered rates carry source and age.
  - **go-livepeer** (`tests/unit/test_go_livepeer_compat.py`): three characterization tests, each shown
    to fail under a mutation (guard removed; `720p-pixel-seconds` made payable).
  - **One existing test changed:** `test_offerings_are_public_and_name_no_runner` pins the offering's
    top-level keys; the set gains `rate_status`, `stale` and `rates_excluded`. Its runner-URL checks are
    unchanged.
  - **Results:** 1338 passed, 31 skipped; coverage 97.59 % (diff coverage 100 % against
    `origin/releases/m3`); ruff, mypy (strict), `check_docs.py`, `lpb gen-docs` and `lpb smoke` clean.
- 29 October: live, read-only, through `HttpDiscoverySource` and `DiscoveryService` with `SystemClock`:
  - `https://ai1.eliteencoder.net:8936`: 77 runners, 77 offerings, all `available`, none stale,
    `observed_by=engine`, `price_usd` null; two offerings priced in `720p-pixel-seconds`.
  - `https://hky.eliteencoder.net:8936`: 11 runners, the same shape.
  - **Finding:** these, and `lpt.sundara.tv:8935`, serve **go-livepeer's** inventory: wei with no
    `price_usd` (livepeer-node `v1.0.1`+ always adds it on-chain), go-livepeer's key set, and the
    `720p-pixel-seconds` unit livepeer-node refuses. #70 read them as livepeer-node `v1.0.2`. The
    go-livepeer compatibility tests therefore describe today's fleet, and the upstream request is what
    brings `price_usd` to it.
  - Not checked live: a stamping (`v1.0.4`+) source; the unit tests cover `observed_by=orchestrator`.
- 29 October: `price_usd` digits. The SDK's `json.loads` makes a JSON number a float before the adapter sees it ([livepeer-python-gateway#88](https://github.com/livepeer/livepeer-python-gateway/issues/88)). The engine and `lpb discovery` parse and re-serve the body with `Decimal` until that ships. No wei-to-USD conversion.

## Decision log

- 29 October (John, from the guide review and plan): the decisions in D58. Q1: keep the USD figure,
  named `price_usd`. Q2: 600 s. Q3: keep the orchestrator's stamp when present and sane.
- 29 October (John): go-livepeer compatibility is packaged as fixtures and docs, deleted together when
  go-livepeer is retired (D2). `price_usd` stays absent for go-livepeer until the upstream request lands;
  no engine-side conversion (ISS-10 derives no price).
- 29 October (John): the guide's corrections go in this branch's plan; `iss-10-rates.md` is amended after
  PR #74 merges, not on Mike's branch.
- 29 October (John): keep `price_usd` exact on this side of the gateway. Discovery JSON is parsed here
  until livepeer-python-gateway#88 parses it as `Decimal`. The gateway issue is the upstream fix.
