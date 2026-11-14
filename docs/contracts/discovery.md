# Discovery

- **Decision:** D63. Supersedes D43 except that the signer only signs. Amends D62 (1).
- **Plan:** [discovery-service](../exec-plans/completed/discovery-service.md) (built 26 October to 5 November; removed here).
- **Code:** `adapters/sdk.py` (`HttpDiscoverySource`), `adapters/discovery_inventory.py`, `contracts/discovery.py`.
- **Status:** the engine reads livepeer/node's `/discovery` contract through one HTTP source. It does not know which process serves that URL.

## What the engine reads

`LPB_DISCOVERY_URL` is required. The engine does not start without it, and it does not fall back to a built-in list. `HttpDiscoverySource` sends `GET` to that URL with no credential and does not follow redirects. The URL may carry no userinfo, query or fragment, and the engine refuses one at startup: a failed refresh's error quotes the URL, and it goes to the log (`discovery.failed`). `/readyz` serves only a code as `discovery_error`, `unavailable` or `invalid_answer`, never the text (#113, C7; [RELIABILITY.md](../RELIABILITY.md#operations)). A 3xx is a failed refresh. Each poll has 5 seconds in all.

The body is a JSON array, the document livepeer/node's `livepeer-discovery` serves. `[]` is an empty inventory. A body that is not a JSON array fails the refresh, and the last good snapshot stays.

A document with records and no readable runner fails the refresh too (#114, C28). It is an answer the engine could not read, not an empty inventory: the reader skipped at least one entry or runner and read none. The last good snapshot stays and ages as after any failed refresh, `discovery_error` is `invalid_answer`, and the log carries the counts, in `discovery.inventory_skipped` and in the text of `discovery.failed`. Before, one bad publish emptied the catalog for every application. A truly empty answer still replaces the snapshot: `[]`, a document of metadata entries only, or orchestrator entries whose `runners` lists are empty. So does an answer with any readable runner, whatever share of its records was skipped; there is no threshold. A supplied `DiscoverySource` that returns no runners is an empty answer as before.

The dev stack points the URL at livepeer/node's `livepeer-discovery`, run in static mode (`deploy/dev-stack/config/discovery.toml`). The node commit that stack builds is recorded in [upstream-baseline.md](../references/upstream-baseline.md).

## How a document is read

The reader takes the fields it stores. An entry is read when `address` is a URL the engine can call and `runners` is a list. A metadata entry (`nodes`, no `address`, and no `runners` or an empty one) yields no runners. An entry with `nodes`, no `address` and records under `runners` is not metadata: no address can read those records, so it is a skipped entry (#114, C28). A runner is kept when `url` is a URL the engine can call, `app` is valid text of 1 to 200 characters, and `observed_at` is an RFC 3339 timestamp that converts to UTC.

Valid text is a JSON string of valid Unicode with no control character (#114, C12). In an optional field (`mode`, `version`, `metadata`, a price's `currency` and `unit`, a GPU's `id` and `name`) a tab, a newline and a carriage return are allowed: they can be stored and served, and metadata is often JSON written over several lines. `app` and `url` allow none. A lone surrogate is not valid Unicode: JSON can carry one as `\ud800`, and the catalog could then not be written as JSON, so `GET /v1/offerings` answered 500 for every application. A control character is U+0000 to U+001F or U+007F to U+009F; PostgreSQL refuses the first of them, NUL, in text. A runner whose `app` or `url` is not valid text, or whose `app` is longer than 200 characters (the longest app a job can name and the store can keep), is skipped and counted like any other bad record. Any other text field that is not valid text reads as absent, and the runner stays: `mode`, `version`, `metadata`, the `currency` and `unit` of `price_info`, and the `id` and `name` of `gpu`. A price whose currency or unit is absent cannot be paid. Nothing is rewritten: text is kept exactly as advertised or not read.

A URL the engine can call is `http` or `https`, has an ASCII host the engine's HTTP client connects to as written and, if any, a port from 1 to 65535, and holds no whitespace, control character or lone surrogate, which the HTTP client cannot send. aiohttp folds a non-ASCII host, so `https://①27.0.0.1` would reach `127.0.0.1` under a name the payment-origin check never sees; a host written in punycode (`xn--`) is read. It has no userinfo, query or fragment: the engine appends each request's path to a runner URL, an `address` whose origin matches that URL becomes `orchestrator_url` for the payment-origin check, and error text quotes both. Node's schema allows those three on a runner URL; the engine does not. One bad record is skipped on its own. The skip is logged as `discovery.inventory_skipped` with a count of skipped entries and a count of skipped runners. The rest of the document is kept. When nothing is left to keep, the refresh fails ([above](#what-the-engine-reads)).

There is no size limit, no stamp-age filter, and no de-duplication. An integer field reads any whole JSON number node accepts (`3`, `3.0`, `3e0`) that fits 64 bits. A price string, a fractional capacity, or any other optional field of the wrong type is absent, and the runner stays. `capacity_available` is the exception: a missing one is unknown, which selection treats as one free slot, but a value that is present and not an integer reads as 0, so that runner is not chosen.

Numbers are read as exact decimal tokens ([RFC 8259](https://www.rfc-editor.org/rfc/rfc8259)), never as binary floats. A token `Decimal` cannot hold still counts as a number: the runner is kept, that field is absent, and the payment layer reports that the price cannot be paid.

## Schema fields and `Runner`

`address` on an orchestrator entry becomes `orchestrator_url` only when its origin equals the runner URL's origin. A claimed address on another host is dropped, so `PaidRequest` will not sign a challenge whose `payment_url` is that host and `orchestrator_of` will not exclude it as a failover group. The pinned dump uses the same host for `address` and every runner URL, so those runners keep their address. `price_info` is kept as advertised: `price`, `price_usd`, `currency` and `unit`, all optional. `price` and `price_usd` are exact decimals when the token fits.

| Schema | `Runner` |
| --- | --- |
| entry `address` | `orchestrator_url` when its origin matches `url`; otherwise absent |
| `url`, `app`, `mode` | the same names. `app` is at most 200 characters |
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

The reader is tested against the node commit [upstream-baseline.md](../references/upstream-baseline.md) pins, without a copy of node's schema. `tests/unit/node_discovery_f45d83f.json` is that commit's `livepeer-discovery --dump` for ai1 and xodeapp, and every runner in it must read with its exact price. Node's own `discovery/schema_test.go` cases for the fields the engine needs are kept by name in `tests/unit/test_discovery_reader.py`. What the engine does with text it cannot keep is in `tests/unit/test_runner_text.py`, written against the catalog, the runner list and the log, not the reader (#114). When the pin moves, capture the document again with that commit's `livepeer-discovery --dump`.

`FakeDiscoveryService` (`livepeer_builder.testing`, test-only) serves a node-format document, including `observed_at` by default, through an in-memory HTTP client into the real `HttpDiscoverySource`. `make_engine(runners=…)` keeps that signature and is built on it, so a test runs the same reader as production.

## `observed_at`

The reader keeps a runner when `observed_at` is an [RFC 3339](https://www.rfc-editor.org/rfc/rfc3339) timestamp, `T` and `Z` in either case, that it can convert to UTC. Nanoseconds truncate to microseconds. A runner that omits the stamp, uses another date format, or has a stamp the clock cannot hold (a leap second) does not appear. That includes orchestrators on livepeer/node `v1.0.2`. The catalog still treats a stamp it cannot use, from a source other than this reader, as the poll time (D62 (3)).
