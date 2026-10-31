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

- [x] `OrchestratorDirectory`, `Orchestrator`, `StaticDirectory`, `OnChainDirectory` (with a JSON-RPC client), `Prober`, `Catalog`, the server, `DiscoverySettings`, the `lpb discovery` command.
- [x] The engine has `HttpDiscoverySource`; `SignerDiscoverySource` and the signer fallback are removed.
- [x] Tests, all written first (each rule also checked by a mutation that the tests catch):
  - static parsing and deduplication;
  - the on-chain pool walk with active and registered filters, the pool-size cap, and RPC errors;
  - ABI encoding and decoding, and selectors checked against Keccak-256;
  - probes with a trusted CA versus a self-signed certificate, timeouts, size limits and bad JSON;
  - catalog retention, eviction and directory changes;
  - server routes and readiness;
  - settings;
  - engine source and assembly.
- [x] Live check of `OnChainDirectory` against Arbitrum mainnet, using a public RPC by hand (not in CI): a pool of 100, 53 active and registered, in 5.5 s.
- [x] Docs:
  - `docs/contracts/engine.md` (discovery);
  - `docs/RELIABILITY.md` (settings and failure modes);
  - the extension guide (writing a directory, both implementations as examples, or replacing the service);
  - `ARCHITECTURE.md` and the diagrams;
  - D43;
  - generated docs.
- [ ] Deploy (all in the branch except the last item, which waits for the owner):
  - the dev stack and Railway run the discovery service on the static list;
  - the engine's `LPB_DISCOVERY_URL` points at it;
  - the signer configs lose `Orchestrators` and pinned certificates;
  - the deploy workflow learns the `discovery` service;
  - the Railway service itself is created by the owner.
- [x] ChatLPT, the console and inference-gateway are pinned to the branch. Their stacks run the discovery service on the static list, and their live tests pass (re-pin to the merge on `releases/m3`).

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
| `LPD_CONTROLLER`, `LPD_SERVICE_REGISTRY` | the Arbitrum `Controller`; the AI Service Registry | The `BondingManager` is looked up in the `Controller`. The registry is an override of the Controller's `ServiceRegistry` (`controller` reads that one). Replaced `LPD_BONDING_MANAGER` and `LPD_AI_SERVICE_REGISTRY` after the PR #46 review |
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
- 26 October: the discovery service, both directories, the engine change, docs and deploy are on the
  branch (`985f23d`). Local CI is green, and the store suite passes on PostgreSQL 16.
- 26 October: live on all three consumers.
  - **Console:** discovery serves the two trusted orchestrators and excludes three self-signed ones. The
    endpoint sweep (42/42), the console API test (33/33), the cost probe (7/7) and the disconnect test pass,
    and the REST spec is unchanged.
  - **ChatLPT:** 23/23.
  - **inference-gateway:** with its `runner-trust` mounted, discovery also keeps the pinned open-pool. The
    paid provider test passed 11/11 on its later runs.
  - **Gateway failures in its first runs:**
    - two signer timeouts at `/generate-live-payment`, both before payment;
    - one stream end that missed the 10 s finalize limit;
    - one SQLite `OperationalError` at job creation.
  - None involved discovery, and a 16-worker stress run on a SQLite file (1,103 buffered and streamed jobs,
    cost reads, usage reports, cost sync) reproduced none of them.

- 26 October: PR #46 review (Cursor's five threads and John's comment), each fixed test-first:
  - **One orchestrator never breaks the pass:** a non-list `runners` is that orchestrator's failure, and
    so is any unexpected error. It never raises through the pass, and never reads as an empty inventory.
  - **The refresh loop:** logs a failed pass and keeps ticking; a directory load is time-limited; `/readyz`
    fails once the loop has stopped.
  - **Secrets:** the RPC key stays out of the log (`httpx` at WARNING) and out of `/readyz`
    (`directory_error` is a stable reason; JSON-RPC errors are reported by code).
  - **On-chain loads:** one unreadable orchestrator is skipped, not the whole load.
  - **Registrations are untrusted:** https only; resolved, checked public, and pinned by the prober; the
    walk is capped at 1000.
  - **John's comment:** contracts come through the `Controller`, with the AI Service Registry as the
    default override (checked on Arbitrum: the Controller resolves `ServiceRegistry` and `BondingManager`;
    `AIServiceRegistry` is unregistered).
  - **Proof:** I reproduced the probe, refresh-loop and log-leak defects on `00ed0f4` first, and the new
    tests were also checked by mutations.
- 26 October: live checks of `lpb discovery` after the review fixes.
  - **On-chain, through a free public RPC:** 19 of 56 orchestrators were lost to HTTP 429 under the first
    per-address skip. That led to the transient-error rule: retry, then fail the whole load and keep the
    previous directory. Re-run on the same RPC, the persistent 429s now failed the load cleanly:
    `directory_error` showed `ChainError: RPC returned HTTP 429`, and `/readyz` answered 503.
  - **On-chain, through a keyed provider:** ready in 28 s, with 47 orchestrators and no failed reads. Six
    network-modules manifests and one unusable URI were skipped. Neither the URL nor its key appeared in
    the log.
  - **Probes:** of those 47, one answers `/discovery`, with no runners. 41 fail and 5 have untrusted
    certificates. On-chain AI orchestrators mostly don't run livepeer-node's `/discovery` yet, which is
    why the rollout is static first.
  - **Static, Railway's list:** 28 orchestrators, 5 serving 89 runners; 19 untrusted, 4 failed.
- 30 October: runner records are kept as the JSON text each orchestrator sent (`discovery/document.py`),
  and a number `Decimal` cannot build fails that orchestrator's probe. Moved here from #77 (ISS-10) with
  #84's fix, so the discovery service is reviewed on its own. See the decision log, "30 October, runner
  records as received".

## Decision log

- **26 October.**
  - Separate process, livepeer-node's `/discovery` format, one directory port with on-chain and static implementations.
  - Exclude untrusted TLS.
  - Static list everywhere first; on-chain is deployed once an RPC provider is chosen.
  - livepeer-node stays at `v1.0.2`.
  - No backwards compatibility for signer discovery.
- **26 October, TLS.** I checked every orchestrator on the current lists against the system trust store: 8 trusted, 21 self-signed, 2 unreachable. ChatLPT and the console already depend only on trusted hosts. Chat runs on `hky.eliteencoder.net` and images on `ai1.eliteencoder.net`, because the SDK verifies TLS and calls to self-signed hosts already fail before payment. Excluding them changes nothing that works today.
- **26 October, pinned certificates.** inference-gateway trusts pinned orchestrator certificates in its
  engine container (`SSL_CERT_DIR`, `runner-trust`). The prober's default context is OpenSSL's default,
  which honours `SSL_CERT_FILE` / `SSL_CERT_DIR`, so such a deployment mounts the same pins in the
  discovery container and those orchestrators stay discoverable. "Untrusted" means untrusted by the
  deployment's store. Pinned by `test_the_default_trust_store_is_the_deployments_so_pinned_certificates_count`.
- **26 October, Railway list.** Railway's signer list moves to the discovery service unchanged (`deploy/railway/discovery/orchestrators.txt`). The `signer.pymthouse.com` URL in it appears only in a comment naming where the list was extracted from; it is not an entry.
- **26 October, PR #46 review (Mike).**
  - **Fixes:** all five of Cursor's threads.
  - **Registry:** the on-chain contracts come through the protocol's `Controller`, and
    `LPD_SERVICE_REGISTRY` defaults to the AI Service Registry as an override of the Controller's
    `ServiceRegistry`. The Controller does not know the AI registry. This keeps the 26 October rule
    (active, and registered for AI), and an operator can read the protocol registry with `controller`.
  - **Exposure:** the service is public on Railway (John, `00ed0f4`), so `/readyz` carries only stable
    reasons.
- **30 October, runner records as received ([#87](https://github.com/NE-Build-Track/livepeer-builder-core/pull/87), Mike's review).**
  - **As received:** each runner object is kept as the JSON text the orchestrator sent and re-served
    unchanged, because livepeer-node's `price_usd` has more digits than a float keeps. Only the envelope
    (the list, each entry's `runners`) is parsed.
  - **Unbuildable numbers:** a number `Decimal` cannot build fails that orchestrator's probe alone, as
    `NaN` does. A float parser would keep it, and re-serving it would fail every consumer's refresh.
  - **Order with #77:** the digits reach the engine only once ISS-10
    ([#77](https://github.com/NE-Build-Track/livepeer-builder-core/pull/77)) is in. Before that, the SDK
    reads `price_usd` as a float. Merged back to back, this does not matter.
  - **The limit is the platform's:** on 64-bit CPython, an adjusted exponent above `999999999999999999`
    or an exponent below `-1999999999999999997`. 32-bit builds refuse sooner, and `_pydecimal` builds
    both. This is stated in `utils.decimal_json_number`, not enforced as a bound of our own: a bound at
    the 64-bit limits never triggers on CI, a tighter one would refuse numbers kept on purpose, and the
    engine reads an unbuildable number per field (#77).
  - **Typed entry:** `Probe.entry` and `Catalog.inventory()` are `InventoryEntry` (`address`,
    `runners: tuple[RawJson, ...]`), not `dict[str, Any]`. A parsed runner is refused when the `Probe`
    is built and flagged by mypy, so it can no longer make every `GET /discovery` a 500.
  - **`Probes` retired:** the directory is the service's one port, so the prober is no longer
    replaceable. `Catalog` takes a `Prober`, and `serve()` has no `prober=` argument.
