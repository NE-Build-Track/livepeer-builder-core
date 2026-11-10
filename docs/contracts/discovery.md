# Discovery

- **Decision:** D63. Supersedes D43 except that the signer only signs. Amends D62 (1).
- **Plan:** [discovery-service](../exec-plans/completed/discovery-service.md) (built 26 October to 5 November; removed here).
- **Code:** `adapters/sdk.py` (`HttpDiscoverySource`), `adapters/discovery_inventory.py`, `contracts/discovery.py`.
- **Status:** the engine reads livepeer/node's `/discovery` contract through one HTTP source. It does not know which process serves that URL.

## What the engine reads

`LPB_DISCOVERY_URL` is required. The engine does not start without it, and it does not fall back to a built-in list. `HttpDiscoverySource` sends `GET` to that URL with no credential and does not follow redirects. The URL may carry no userinfo, query or fragment, and the engine refuses one at startup: a failed refresh's error quotes the URL, and `/readyz` serves it as `discovery_error`. A 3xx is a failed refresh. Each poll has 5 seconds in all.

The body is a JSON array, the document livepeer/node's `livepeer-discovery` serves. `[]` is an empty inventory. A body that is not a JSON array fails the refresh, and the last good snapshot stays.

The dev stack points the URL at livepeer/node's `livepeer-discovery`, run in static mode (`deploy/dev-stack/config/discovery.toml`). The node commit that stack builds is recorded in [upstream-baseline.md](../references/upstream-baseline.md).

## How a document is read

The reader takes the fields it stores. An entry is read when `address` is an `http` or `https` URL with a host and `runners` is a list. A metadata entry (`nodes`, and no `address`) yields no runners. A runner is kept when `url` is an `http` or `https` URL with a host, `app` is a non-empty string, and `observed_at` converts to UTC. One bad record is skipped on its own. The skip is logged as `discovery.inventory_skipped` with a count of skipped entries and a count of skipped runners. The rest of the document is kept.

There is no size limit, no stamp-age filter, and no de-duplication. A price string, a non-integer capacity, or any other optional field of the wrong type is absent, and the runner stays.

Numbers are read as exact decimal tokens ([RFC 8259](https://www.rfc-editor.org/rfc/rfc8259)), never as binary floats. A token `Decimal` cannot hold still counts as a number: the runner is kept, that field is absent, and the payment layer reports that the price cannot be paid. An integer field (`capacity`, `capacity_used`, `capacity_available`, `gpu.vram_mb`) holds a signed 64-bit value; a larger integer still passes the schema, so the runner is kept and that field is absent.

## Schema fields and `Runner`

`address` on an orchestrator entry becomes `orchestrator_url`. `price_info` is kept as advertised: `price`, `price_usd`, `currency` and `unit`, all optional. `price` and `price_usd` are exact decimals when the token fits.

| Schema | `Runner` |
| --- | --- |
| entry `address` | `orchestrator_url` |
| `url`, `app`, `mode` | the same names |
| `observed_at` | `observed_at`, as UTC. A missing stamp, or one the clock cannot convert, skips the runner |
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

`FakeDiscoveryService` (`livepeer_builder.testing`, test-only) serves a node-format document, including `observed_at` by default, through an in-memory HTTP client into the real `HttpDiscoverySource`. `make_engine(runners=…)` keeps that signature and is built on it, so a test runs the same reader as production.

## `observed_at`

The reader keeps a runner when `observed_at` is an [RFC 3339](https://www.rfc-editor.org/rfc/rfc3339) timestamp it can convert to UTC. Nanoseconds truncate to microseconds. A runner that omits the stamp, or whose stamp does not convert, does not appear. That includes orchestrators on livepeer/node `v1.0.2`. The catalog still treats a stamp it cannot use, from a source other than this reader, as the poll time (D62 (3)).
