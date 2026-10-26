# Discovery service: on-chain and static orchestrator directories

## Status

- **Owner:** Mike.
- **Ticket:** [#45](https://github.com/NE-Build-Track/livepeer-builder-core/issues/45), part of ISS-09 ([Network-Engineering-SPE#22](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/22)).
- **Branch:** `tasks/mz/discovery-service`, off `releases/m3` at `629f1ab`.
- **Progress:** design agreed 26 October; in progress.
- **Overlap:** it covers the problem in #27 and #30. PR #34 (`LPB_ORCHESTRATOR_LIST`) is being handled separately.
- **Decision:** D43.

## Goal

Make discovery something an operator can replace without touching the signer or the engine. Default it to the protocol itself: the bonded active set, registered for AI services.

Today the engine reads the remote signer's `GET /discover-orchestrators`. In livepeer-node `v1.0.2`, which our stacks pin:

- the signer fans out uncached across its static `Orchestrators` list on every poll (#30);
- that list is not a payment control (#27).

## Acceptance

- [ ] `OrchestratorDirectory`, `Orchestrator`, `StaticDirectory`, `OnChainDirectory` (with a JSON-RPC client), `Prober`, `Catalog`, the server, `DiscoverySettings`, the `lpb discovery` command.
- [ ] The engine has `HttpDiscoverySource`; `SignerDiscoverySource` and the signer fallback are removed.
- [ ] Tests, all written first:
  - static parsing and deduplication;
  - the on-chain pool walk with active and registered filters, the pool-size cap, and RPC errors;
  - ABI encoding and decoding, and selectors checked against Keccak-256;
  - probes with a trusted CA versus a self-signed certificate, timeouts, size limits and bad JSON;
  - catalog retention, eviction and directory changes;
  - server routes and readiness;
  - settings;
  - engine source and assembly.
- [ ] Live check of `OnChainDirectory` against Arbitrum mainnet, using a public RPC by hand (not in CI).
- [ ] Docs:
  - `docs/contracts/engine.md` (discovery);
  - `docs/RELIABILITY.md` (settings and failure modes);
  - the extension guide (writing a directory, both implementations as examples, or replacing the service);
  - `ARCHITECTURE.md` and the diagrams;
  - D43;
  - generated docs.
- [ ] Deploy:
  - the dev stack and Railway run the discovery service on the static list;
  - the engine's `LPB_DISCOVERY_URL` points at it;
  - the signer configs lose `Orchestrators` and pinned certificates;
  - the deploy workflow learns the `discovery` service;
  - the Railway service itself is created by the owner.
- [ ] ChatLPT and the console are re-pinned. Their stacks run the discovery service on the static list, and their live tests pass.

## Design

### Components

```text
OrchestratorDirectory  ──►  Prober (/discovery, TLS, timeouts)  ──►  Catalog (cache, eviction)  ──►  GET /discovery
  static | on-chain | yours                                                                      (livepeer-node's format)
                                                                                                        │
engine: HttpDiscoverySource(LPB_DISCOVERY_URL) ◄───────────────────────────────────────────────────────┘
```

- **`OrchestratorDirectory`** (the port) answers one question: which orchestrators are eligible.
  ```python
  async def orchestrators(self) -> tuple[Orchestrator, ...]
  ```
  An `Orchestrator` is a base URL plus an optional Ethereum address.
  - **`StaticDirectory`** reads a configured list of base URLs.
  - **`OnChainDirectory`** walks the `BondingManager` transcoder pool on Arbitrum
    (`getFirstTranscoderInPool` and `getNextTranscoderInPool`, capped at the pool size). It keeps
    orchestrators where `isActiveTranscoder(addr)` is true for the current round and
    `AIServiceRegistry.getServiceURI(addr)` is non-empty.
  - Both are complete examples of the port. An enterprise writes its own directory (an approved list, a
    private fleet) and reuses the rest.
- **`Prober`** fetches each orchestrator's `GET /discovery`:
  - at most 16 requests at a time;
  - a 5 s timeout and a 1 MiB response limit;
  - TLS verified against the system trust store (`ssl.create_default_context()`), so self-signed or
    untrusted certificates are excluded and logged with a reason;
  - each response is reduced to one entry per orchestrator: `{"address": <probed base URL>, "runners": [...]}`.
    Runner records are kept as they are.
- **`Catalog`** holds the directory and each orchestrator's last good inventory:
  - it reloads the directory every `directory_interval_s` and probes every `probe_interval_s`;
  - a failed probe keeps the last inventory until `max_failed_refreshes` consecutive failures, then
    evicts it (livepeer-node's semantics);
  - a failed directory load keeps the previous directory;
  - an orchestrator that leaves the directory drops out at once.
- **Server** (Starlette):
  - `GET /discovery` returns the merged inventory.
  - `GET /healthz` reports that the process is up.
  - `GET /readyz` returns 200 once a directory has loaded and a probe pass has finished, otherwise 503,
    with counts and the last error.
  - It runs as its own process with `lpb discovery`, the way `lpb serve` runs the REST service.

### The contract

The contract is livepeer-node's inventory format, the one a `v1.0.2` orchestrator serves at `/discovery`:

```json
[{"address": "https://orch.example.com:8935", "runners": [{"url": "...", "app": "...", "capacity": 4, "price_info": {...}}]}]
```

The engine already parses it: `runners_from_entries`, through the SDK's `discover_runners(discovery_url=…)`. So `LPB_DISCOVERY_URL` can point at:

- this service;
- a single orchestrator's `/discovery`;
- later, livepeer-node's own standalone discovery (`v1.0.3`).

There is no app filtering in the service. The engine filters per request, and ChatLPT through its `FilteredDiscovery`.

### Engine change, with no backwards compatibility

- `SignerDiscoverySource` becomes `HttpDiscoverySource(discovery_url)`, which sends no headers.
- Assembly uses it when `LPB_DISCOVERY_URL` is set. Otherwise it uses the empty static source:
  `LPB_SIGNER_URL` alone no longer discovers anything.
- The signer only signs.

### Settings

The service's settings use the `LPD_` prefix, so they never collide with the engine's
`LPB_DISCOVERY_URL` and `LPB_DISCOVERY_INTERVAL_S`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `LPD_DIRECTORY` | `static` | `static` or `onchain` |
| `LPD_ORCHESTRATORS` | unset | Static: comma-separated base URLs, or a path to a file with one per line |
| `LPD_ARBITRUM_RPC_URL` | unset | On-chain: required |
| `LPD_BONDING_MANAGER`, `LPD_AI_SERVICE_REGISTRY` | Arbitrum mainnet addresses | On-chain contracts. The `BondingManager` proxy survives upgrades; a redeployed registry is a settings change |
| `LPD_DIRECTORY_INTERVAL_S` | `3600` | Directory reload; the active set changes per round |
| `LPD_PROBE_INTERVAL_S` | `30` | Inventory refresh |
| `LPD_MAX_FAILED_REFRESHES` | `6` | Consecutive failed probes before eviction (`0` evicts at once) |
| `LPD_HTTP_HOST`, `LPD_HTTP_PORT` | `127.0.0.1`, `8939` | Listener (livepeer-node discovery's port) |
| `LPD_LOG_JSON` | `true` | Logging |

### On-chain details

These come from `livepeer/protocol`, branch `delta`, `deployments/arbitrumMainnet`:

| Contract | Address |
| --- | --- |
| `BondingManager` proxy | `0x35Bcf3c30594191d53231E4FF333E8A770453e40` |
| `AIServiceRegistry` | `0x04C0b249740175999E5BF5c9ac1dA92431EF34C5` |

The calls are plain JSON-RPC `eth_call`, with no `web3` dependency. Each selector is hard-coded next to its signature and checked in tests by an independent Keccak-256:

| Selector | Signature |
| --- | --- |
| `0x88a6c749` | `getFirstTranscoderInPool()` |
| `0x235c9603` | `getNextTranscoderInPool(address)` |
| `0x2a4e0d55` | `getTranscoderPoolSize()` |
| `0x08802374` | `isActiveTranscoder(address)` |
| `0x214c2a4b` | `getServiceURI(address)` |

### Layering

`discovery/` is a top-level layer, like `http/`:

- it imports only `utils` from the package and depends on none of the engine;
- `httpx` is allowed there for probing and RPC (and stays confined to `adapters/batteries.py` elsewhere);
- `starlette` and `uvicorn` are allowed in `discovery/server.py` only;
- it ships in the `server` extra.

## Out of scope

- livepeer-node `v1.0.3`. Its webhook format (`[{"address": …}]`) and standalone discovery come after a cross-component review.
- App filtering in the service.
- Prometheus metrics; `/readyz` carries the counts.
- Persisting the cache.

## Progress

- 26 October: issue #45 opened, plan written, and branch started.

## Decision log

- **26 October.**
  - Separate process, livepeer-node's `/discovery` format, one directory port with on-chain and static implementations.
  - Exclude untrusted TLS.
  - Static list everywhere first; on-chain is deployed once an RPC provider is chosen.
  - livepeer-node stays at `v1.0.2`.
  - No backwards compatibility for signer discovery.
- **26 October, TLS.** I checked every orchestrator on the current lists against the system trust store: 8 trusted, 21 self-signed, 2 unreachable. ChatLPT and the console already depend only on trusted hosts. Chat runs on `hky.eliteencoder.net` and images on `ai1.eliteencoder.net`, because the SDK verifies TLS and calls to self-signed hosts already fail before payment. Excluding them changes nothing that works today.
- **26 October, Railway list.** Railway's signer list moves to the discovery service unchanged (`deploy/railway/discovery/orchestrators.txt`). The `signer.pymthouse.com` URL in it appears only in a comment naming where the list was extracted from; it is not an entry.
