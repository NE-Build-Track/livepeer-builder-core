# Discovery

- **Decision:** D63. Supersedes D43 except that the signer only signs. Amends D62 (1).
- **Plan:** [discovery-service](../exec-plans/completed/discovery-service.md) (built 26 October to 5 November; removed here).
- **Code:** `adapters/sdk.py` (`HttpDiscoverySource`), `adapters/discovery_inventory.py`, `contracts/discovery.py`, `contracts/discovery.schema.json`.
- **Status:** the engine reads livepeer/node's `/discovery` contract through one HTTP source. It does not know which process serves that URL.

## What the engine reads

`LPB_DISCOVERY_URL` is required. The engine does not start without it, and it does not fall back to a built-in list. `HttpDiscoverySource` sends `GET` to that URL with no credential and does not follow redirects. The URL may carry no userinfo, query or fragment, and the engine refuses one at startup: a failed refresh's error quotes the URL, and `/readyz` serves it as `discovery_error`. A 3xx is a failed refresh. Each poll has 5 seconds in all.

The body is a JSON array, the document livepeer/node's discovery schema describes. `[]` is an empty inventory. A body that is not a JSON array fails the refresh, and the last good snapshot stays.

The dev stack points the URL at livepeer/node's `livepeer-discovery`, run in static mode (`deploy/dev-stack/config/discovery.toml`).

## The contract and its pin

The contract is livepeer/node's `docs/discovery.schema.json` at `f45d83fd68f73d195dffe8b45f6cac296d023c49`. The package vendors that file and a pin record beside it (`contracts/discovery.schema.pin.json`): repository `livepeer/node`, path `docs/discovery.schema.json`, that commit, and sha256 `9abe0cf90b2e21f8ddfdf215616a1a71f969ddaf69281b92719d3b60014475c4`. [upstream-baseline.md](../references/upstream-baseline.md) records the same pin. `/readyz` reports `discovery_contract` as `livepeer/node@f45d83f`.

A bump replaces the vendored file and the pin together, updates the baseline row and `DISCOVERY_CONTRACT`, and re-runs the three guards: the offline sha256 check, node's `TestDiscoverySchema` fixtures, and the differential test. A weekly workflow compares the schema at livepeer/node's `main`, and at its latest release tag once that tag contains the pinned commit, with the pin. A different schema, or none, on a compared ref opens an issue in this repository. Any other GitHub error fails the job and opens nothing. It does not run on pull requests.

## How a document is read

`jsonschema` validates each entry and each runner against the schema's definitions, with formats asserted, the way livepeer/node's `compiler.AssertFormat()` does. The rules are the file's. The reader adds none: no size limit, no stamp-age filter, and no de-duplication.

A record that fails is skipped on its own. The skip is logged as `discovery.inventory_skipped` with a count per schema keyword. The rest of the document is kept. A metadata entry (`nodes`, and no `address`) is valid and yields no runners.

Numbers are read as exact decimal tokens ([RFC 8259](https://www.rfc-editor.org/rfc/rfc8259)), never as binary floats. A token `Decimal` cannot hold still counts as a number: the runner is kept, that field is absent, and the payment layer reports that the price cannot be paid.

## Schema fields and `Runner`

`address` on an orchestrator entry becomes `orchestrator_url`. `price_info` is kept as advertised: `price`, `price_usd`, `currency` and `unit`, all optional. `price` and `price_usd` are exact decimals when the token fits.

| Schema | `Runner` |
| --- | --- |
| entry `address` | `orchestrator_url` |
| `url`, `app`, `mode` | the same names |
| `observed_at` | `observed_at`, as UTC. A stamp the clock cannot convert is kept as `None` |
| `capacity`, `capacity_used`, `capacity_available` | the same names. There is no `session_ids` fallback |
| `price_info` | `price_info`, as advertised |
| `version`, `metadata`, `gpu` | the same names. The engine never interprets `metadata` |

`runner_id` is not in the schema and is not on `Runner`.

## Rules that apply after discovery

| Rule | Where |
| --- | --- |
| Whether a price can be paid: the float bounds, a positive price, wei per `seconds` or `fixed`, and the ceiling | the payment layer: `payable_rate` and the price rules in `contracts/payments.py`, then `max_price` and the signer. The engine never signs without a ceiling |
| Rate age and `stale` | the catalog (D62 (3), `LPB_RATE_MAX_AGE_S`) |
| Which apps an enterprise will call | a `decorate` wrapper around `HttpDiscoverySource` |

## Sources

Any process that serves this contract can sit behind `LPB_DISCOVERY_URL`. One orchestrator's own `/discovery` is such a process. The reference implementation these docs name is livepeer/node's `livepeer-discovery`. The dev stack runs it in static mode (`deploy/dev-stack/config/discovery.toml`).

That process GETs each listed orchestrator at startup and every `RefreshInterval` (30s in the dev stack; the process default is 5 minutes) and verifies TLS with the system trust store. A failed refresh keeps the last successful snapshot until `MaxFailedRefreshes` consecutive failures. The default is 6, and the dev stack does not override it. A TLS failure, a non-200, or a document `Decode` rejects counts as one failure. A source that has never succeeded stays out of the inventory. A runner with no `observed_at`, or one more than 30 seconds ahead, is skipped inside `Decode`. That skip does not drop the source when another runner in the same document is valid.

## Testing

`FakeDiscoveryService` (`livepeer_builder.testing`, test-only) serves a node-format document, including `observed_at` by default, through an in-memory HTTP client into the real `HttpDiscoverySource`. `make_engine(runners=…)` keeps that signature and is built on it, so a test runs the same reader and the same schema checks as production.

## `observed_at`

The schema requires `observed_at`, an [RFC 3339](https://www.rfc-editor.org/rfc/rfc3339) timestamp. A runner that does not stamp it fails the schema and does not appear. That includes orchestrators on livepeer/node `v1.0.2`. The catalog still treats a stamp it cannot use, from a source other than this reader, as the poll time (D62 (3)).
