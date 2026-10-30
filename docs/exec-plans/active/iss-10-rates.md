# ISS-10: Network rates and pricing assumptions

## Status

- **Owner:** John (engine). **Reviewers:** Mike (design, console), Josh (engine code).
- **Ticket:** ISS-10 ([Network-Engineering-SPE#23](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/23)).
  Guide: `docs/design-docs/m3-guides/iss-10-rates.md` in [PR #74](https://github.com/NE-Build-Track/livepeer-builder-core/pull/74) (`2e82565`).
- **Branch:** `tasks/m3/iss-10-rates`, off `releases/m3` at `d3df806`.
- **State:** built test-first; awaiting review. Mike's review of `53b12d7` is answered on this branch
  (Progress, 30 October). The console change (Mike) is open.
- **Decision:** D62 (Q1–Q3 locked by Mike, 30 October).

## Goal

Before invoking anything, a caller can read every available rate with its exact amount, currency and
unit, who stated it, how old it is, and the assumptions behind it. A missing or stale rate says so. A
network rate is never presented as a total-job quote or a retail price, and the engine never signs a
payment without a price ceiling.

## Acceptance

From the 21 October M3 plan and the guide's "Done when":

- [x] Every rate carries amount, currency, unit, source and age: `ObservedRate` (`amount`, `currency`,
  `unit`, `price_usd`, `source`, `observed_at`, `observed_by`) on `Offering.rate_low`/`rate_high`.
- [x] Unpaid, missing and stale rates are explicit: one `Offering` per app and `(currency, unit)`, and one
  with no range for an app's runners with no rate; `rate_status`, `stale`; `Runner.rate_error` says why
  an advertised price is not a rate.
- [x] A network rate is not represented as a total-job quote or a retail price: no defaults, no
  conversion, no job total; [payments.md](../../contracts/payments.md#pricing-assumptions).
- [x] `payments.md` has "Wholesale and retail" and "Pricing assumptions".
- [x] Tests written first, each seen failing for the right reason; characterization tests proved by
  mutation.
- [x] `engine.md`, `RELIABILITY.md`, `m3-decisions.md` (D62) and the generated docs.
- [ ] Mike's review of D62 (Q1–Q3 locked 30 October) and the contract; Josh's review of the engine code.
- [ ] Console (Mike): show `price_usd`, source, age and the engine's `stale`; drop the G14 note and its
  own 300 s rule.
- [ ] The guide amendments below, applied to `iss-10-rates.md` once PR #74 merges.

## Design

The contract and rules are in [engine.md](../../contracts/engine.md#rates-iss-10) and
[payments.md](../../contracts/payments.md#pricing-assumptions). In brief:

| Concern | Where | Rule |
| --- | --- | --- |
| Parse | `adapters/sdk.py::_rate`, `_stamp` | A rate exactly as advertised, or a reason. No `usd`/`hour` defaults. A `price` from `1e-300` to below `1e300`, so it pads into a positive float ceiling. `price_usd` kept when it is a finite, nonnegative decimal. `observed_at` read as [RFC 3339](https://www.rfc-editor.org/rfc/rfc3339) in UTC |
| Age | `services/discovery.py::_observed` | The orchestrator's stamp when present and at most 30 s ahead of the poll, else the poll time; `observed_by` names the clock |
| Status | `DiscoveryService.offerings` | One offering per app and `(currency, unit)`; `rate_status` (the signer takes a ceiling in the currency and unit: `utils.is_payable`), `stale` (snapshot not fresh, or oldest rate older than `LPB_RATE_MAX_AGE_S`) |
| Money | `adapters/runner_call.py::PaidRequest._answered`, `max_price` | A 402 for a runner with no ceiling the signer takes is refused before the signer is asked |

### Why the plan departs from the guide

| Guide | Built | Why |
| --- | --- | --- |
| `observed_at`/`observed_by` on `NetworkRate` | On `ObservedRate`, which the catalog serves; `Runner.observed_at` holds the raw stamp | Sources (static, embedders') build rates but cannot know the engine's poll time, and the 30 s rule needs "now". The adapter parses; the service decides, with its injected `Clock` |
| `rate_status ∈ {available, missing, stale}` plus `stale` | `rate_status ∈ {available, missing}` plus `stale` | The guide's pair admits two contradictory combinations; every combination of these two is legal |
| `amount_usd` | `price_usd` | livepeer-node's own name; the engine echoes the node's published language |
| `LPB_RATE_MAX_AGE_S` 300 s (Q2) | 600 s | `lpb discovery` evicts a failing orchestrator after about 3 min, so 300 s never fires there; livepeer-node discovery refreshes every 5 min by default, so 300 s flags healthy rates |
| "No change to … `max_price()`" | Unchanged, plus the ceiling guard | Dropping the defaults turns an incomplete price into `rate=None`, which sends no `maxPrice`; the node signer then checks no price at all (`signer/service.go::checkMaxPrice`) |
| "`hour` for older registrations" | `hour` is registration-side only, and not paid | No released livepeer-node or go-livepeer advertises `hour` in `/discovery`, and the node signer takes no `hour` ceiling |
| `testing/fake_engine.py` fills the new fields | No change | `FakeDiscovery` returns the offerings its caller supplies |

### Upstream evaluation

| Path | `price_info` |
| --- | --- |
| livepeer-python-gateway registration (`LiveRunnerPriceInfo.to_json`), used by runner-app-examples | Always `price` (USD), `currency` (default `usd`), `unit` (default `hour`); the examples also use `fixed` and `720p` |
| livepeer-node registration (`validateHeartbeat`, `normalizePriceAt`; `v1.0.1` to `main`) | `usd`; `hour` or `fixed`; refuses `720p` |
| livepeer-node `/discovery` | On-chain `{price: wei (rounded up), price_usd, currency: "wei", unit: "seconds" or "fixed"}`, listed only while the price is available; off-chain no `price_info`; `observed_at` from the `v1.0.3` tag |
| go-livepeer registration (`normalizeLiveRunnerPriceInfo`, `v0.9.0` to master) | `usd`; `hour`, `720p` or `fixed`; the USD price is kept in `runner.PriceInfo` |
| go-livepeer `/discovery` (`discoveryRunner`, `convertPrice`) | `{price: wei (rounded down), currency: "wei", unit: "seconds", "fixed" or "720p-pixel-seconds"}`; no `price_usd`, no `observed_at`; no `price_info` off-chain **or while an on-chain runner's price feed is not ready** |
| Node signer (`checkMaxPrice`) | The ceiling must be a positive wei price in the payment type's unit (`seconds` for `live`, `fixed` for `fixed`), else 400; with no ceiling, no check |

### Guide amendments (for `iss-10-rates.md` once PR #74 merges)

1. `observed_at` is the orchestrator's response time. It measures inventory freshness and carries
   information only through a cache (`lpb discovery`, livepeer-node discovery).
2. Upstream dependency: no orchestrator sampled on 29 October stamps `observed_at`, so today every rate
   is `observed_by=engine`. They serve go-livepeer's inventory, not livepeer-node `v1.0.2`'s as #70
   assumed (see Progress).
3. The engine is deliberately looser than livepeer-node's collectors, which drop an unstamped runner.
4. Any unit but `seconds` or `fixed` is kept as advertised and is not payable. `hour` and `usd` are
   registration-side only, and not payable either.
5. `amount_usd` becomes `price_usd`; the raw stamp is `Runner.observed_at`; the catalog type is
   `ObservedRate`; the status is two fields.
6. go-livepeer orchestrators send no `price_usd` or `observed_at`, and no upstream change is planned:
   go-livepeer is deprecated (Mike, 30 October).
7. The stamp is in the `v1.0.3` tag (first released in `v1.0.4`). Replace the unresolvable `e75e5b6`
   with `a4b68ed` (`main`) or `v1.0.6` (`c7be946`).
8. `LPB_RATE_MAX_AGE_S` defaults to 600 s (Q2), with the reason above.
9. The ceiling guard (D62 (5)).

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
    `stale` from the snapshot and from the max age at its boundary, one offering per unit, and
    `LPB_RATE_MAX_AGE_S` through `EngineSettings` and assembly.
  - **Money** (`tests/unit/test_runner_call.py`): a runner with no usable price that answers 402 is
    refused before the signer is asked; seen red without the guard (the engine signed uncapped).
  - **Wire** (`tests/unit/test_http.py`): offered rates carry source and age.
  - **go-livepeer** (`tests/unit/test_go_livepeer_compat.py`): three characterization tests, each shown
    to fail under a mutation (guard removed; `720p-pixel-seconds` made payable).
  - **One existing test changed:** `test_offerings_are_public_and_name_no_runner` pins the offering's
    top-level keys; the set gains `rate_status` and `stale`. Its runner-URL checks are
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
    go-livepeer compatibility tests therefore describe today's fleet.
  - Not checked live: a stamping (`v1.0.4`+) source; the unit tests cover `observed_by=orchestrator`.
- 29 October: `price_usd` digits. The SDK's `json.loads` makes a JSON number a float before the adapter sees it ([livepeer-python-gateway#88](https://github.com/livepeer/livepeer-python-gateway/issues/88)). The engine and `lpb discovery` parse and re-serve the body with `Decimal` until that ships. No wei-to-USD conversion.
- 29 October: PR #77 review, ceilings the signer refuses. livepeer/node `406b248` `checkMaxPrice` takes only
  a positive `wei` ceiling, per `seconds` for `live` and per `fixed` for `fixed`; anything else is a 400
  after the intent is recorded. `NetworkRate.amount` is now from `1e-300` (`PRICE_FLOOR`) to below `1e300`:
  `0`, `1E-400` and `1E-999999` padded to a `0.0` ceiling, and below the normal float range the padding
  rounds under the price (`7e-324` pads to `5e-324`). `max_price` returns `None` for any currency and unit but `wei` per
  `seconds` or `fixed`, so the 402 guard refuses first; `hour` left the paid units; the catalog's
  `rate_status` uses the same rule (`utils.is_payable`). Each new test was seen red for the right reason
  (the `wei`/`hour` catalog case passed once `hour` left the paid units). Fixtures changed: the test
  runners in `tests/unit/livepeer_net.py` and `testing.fakes.runner()` advertise `wei`, not `usd`, as
  livepeer-node does; `test_sdk_contract.py` compares the engine's units with the SDK's minus `hour`.
- 30 October: a number such as `1e1000000000000000000` is `inf` to the probe's float parser, so the raw
  token was re-served. `loads_json_numbers` then raised and every engine refresh failed. The probe now
  parses fractional numbers with `decimal_json_number` and fails that orchestrator only.

- 30 October: Mike's review of `53b12d7`, engine side (the `lpb discovery` items are T25). Test-first;
  each new test seen red for the right reason, or, where the behaviour already held, shown to fail under a
  mutation:
  - **Discovery fetch** (`tests/unit/test_adapter_edges.py`): a body that sends a byte every 10 ms times
    out at the deadline, an inventory one byte over 1 MiB is refused and one of exactly 1 MiB is read,
    and a 301 is a failed refresh that keeps the last snapshot and requests nothing else (mutation:
    following redirects). Live, against a local server sending a byte every 2 s: the refresh gives up at
    5.0 s, against 24.5 s in the review.
  - **Malformed entries:** a `null` entry, a `runners` that is not a list (`5`, a string, an object) and a
    runner record that is not an object each lose nothing else; one `discovery.inventory_skipped`
    warning counts what was skipped, and a clean inventory logs none.
  - **Catalog** (`tests/unit/test_rates.py`): an app with no rate is listed as `missing` with no range; an
    app's runners with no rate are an offering after its priced ones (mutation: missing group first);
    `available` with no range is not an `Offering`. 10(c): two `ObservedRate`s per offering, not one per
    runner.
  - **Paid order** (`tests/unit/test_jobs.py`): a paid job tries a runner it can cap before a busier one
    with no rate; the one with no rate is still tried last (mutation: dropping it); an unpaid job keeps
    `MostCapacityFirst` (mutation: re-ordering unpaid jobs too).
  - **Price 0:** a free rate parses, is a rate of `0` in the catalog and builds in `testing.runner()`
    (mutation: `is_price` refusing `0`); a free runner that answers 402 is refused unsigned, and one that
    answers 200 serves a credentialed call.
  - **Unit on 402** (`tests/unit/test_runner_call.py`): an `hour` runner that answers 200 serves a
    credentialed call; the existing `wei`/`hour` case still refuses its 402 unsigned.
  - **`LPB_DISCOVERY_URL`** (`tests/unit/test_adapters.py`): `discovery:8939/discovery`,
    `ftp://discovery/discovery`, `http:///discovery` and `http://[::1/discovery` are refused at startup.
  - **Existing tests changed:** `test_an_app_with_no_usable_rate_is_not_listed` is replaced, because the
    review reverses it. `0` left the refused prices in two tests, and three tests take the reworded error
    `price must be 0, or from 1e-300 to below 1e300`.
  - **Results:** 1560 passed, 48 skipped (one deselected, as on 29 October: a stale `dist/` wheel); every
    coverage floor met, diff coverage 100 %; ruff, mypy (strict), `check_docs.py`, `lpb gen-docs` and
    `lpb smoke` clean.

## Decision log

- 29 October (John, from the guide review and plan): the decisions in D62. Q1: keep the USD figure,
  named `price_usd`. Q2: 600 s. Q3: keep the orchestrator's stamp when present and sane.
- 29 October (John): go-livepeer compatibility is packaged as fixtures and docs, deleted together when
  go-livepeer is retired (D2). `price_usd` stays absent for go-livepeer; no engine-side conversion
  (ISS-10 derives no price).
- 29 October (John): the guide's corrections go in this branch's plan; `iss-10-rates.md` is amended after
  PR #74 merges, not on Mike's branch.
- 29 October (John): keep `price_usd` exact on this side of the gateway. Discovery JSON is parsed here
  until livepeer-python-gateway#88 parses it as `Decimal`. The gateway issue is the upstream fix.
- 29 October (John, PR #77 review): an app advertised in two units is two offerings, not one range with
  the minority unit excluded, so an unpaid unit never hides a paid one. Runners with no rate are not
  listed (superseded 30 October, below), and `rates_excluded` is gone.
- 29 October (John, PR #77 review): the engine pays exactly the ceilings livepeer/node's signer takes,
  `wei` per `seconds` or `fixed`, and the catalog says `available` for exactly those. A price no positive
  float ceiling holds is no rate, like one from `1e300`: the floor is `1e-300`, in the normal float range,
  so padding always lands above the price.
- 30 October (Mike, reviewing #77): Q1–Q3 locked as proposed, with the separate `stale` flag and the
  price-ceiling guard. Renumbered D58 → D62: #78 (ISS-12) holds D58 and its stack D59–D61.
- 30 October (Mike, reviewing `53b12d7`): no requests to go-livepeer, which is deprecated. Its
  orchestrators send no `price_usd` or `observed_at`; no upstream change is planned. The drafted request
  is removed from this plan.
- 30 October (John, from Mike's review of `53b12d7`): every app is listed. An app's runners with no rate are
  one more offering with no range and `rate_status` `missing`, because they still serve jobs and an empty
  catalog beside a working `POST /v1/jobs` misled. On a paid job the runners the signer takes a ceiling
  for are tried first, in the selection policy's order (applied after the policy, so a custom policy keeps
  its relative order); the rest are the last resort, so a runner that would be refused at its 402 is not
  failed over from on every job. A runner priced at `0` has no ceiling, so it is in the last group too.
- 30 October (John, same review): `0` is a valid free rate, never signed for. The `1e-300` floor applies
  only above `0`. The unit, like the currency, is checked only when the runner asks for payment.
- 30 October (John, same review): `HttpDiscoverySource` keeps its own exact-decimal fetch (the payment
  ceiling needs it) but is bounded like the prober: 5 s in all, 1 MiB. `utils.read_capped` is shared so
  that the prober can adopt it. Items 10(a), 10(b) and 10(d) touch only `lpb discovery`
  (`discovery/document.py`, `discovery/probe.py`) and are left for that service (tech-debt tracker).
- 30 October: an exponent `Decimal` cannot construct fails that orchestrator's probe, as `NaN` does.
  It is not re-served. A float parse would turn `1e1000000000000000000` into `inf`, keep the token, and
  every engine refresh would raise on the aggregated document.
