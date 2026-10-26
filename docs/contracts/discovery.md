# Discovery service contract

- **Ticket:** [#45](https://github.com/NE-Build-Track/livepeer-builder-core/issues/45), part of ISS-09.
- **Plan:** [discovery-service](../exec-plans/active/discovery-service.md).
- **Code:** `discovery/`.
- **Status:** implemented. It runs as its own process with `lpb discovery`, from the `[server]` extra.

The discovery service answers which runners exist. It finds eligible orchestrators through an
`OrchestratorDirectory`, reads each one's inventory, and serves the merged result in livepeer-node's
format. The engine reads it through `LPB_DISCOVERY_URL`
([engine.md#discovery](engine.md#discovery)), and nothing in the engine depends on the service.

## HTTP

| Route | Answer |
| --- | --- |
| `GET /discovery` | `200` with livepeer-node's inventory: `[{"address": "<orchestrator base URL>", "runners": [...]}]`, one entry per orchestrator that is serving, in directory order. `address` is the URL that was probed, never one an orchestrator claims for itself. Runner records are passed through as the orchestrator advertised them |
| `GET /readyz` | `200` once a directory has loaded and a probe pass has finished, otherwise `503`. The body is `ready`, `orchestrators` (in the directory), `serving` (with an inventory), `directory_loaded_at`, `probed_at`, and `directory_error` (the last failed directory load, or `null`) |
| `GET /healthz` | `200` while the process runs |

This format is what a livepeer-node `v1.0.2` orchestrator serves at `/discovery`. So
`LPB_DISCOVERY_URL` can point at this service, at a single orchestrator, or at livepeer-node's own
standalone discovery service (added in `v1.0.3`).

There is no app filtering: consumers filter. The engine filters per request, and ChatLPT through its
discovery wrapper.

## Directories

`OrchestratorDirectory.orchestrators()` returns the eligible `Orchestrator`s (a base URL, plus an
optional Ethereum address). It raises when it cannot answer. Everything after the directory is
shared: probing, TLS, caching and serving.

| Directory | Who is eligible | Setting |
| --- | --- | --- |
| `StaticDirectory` | A configured list | `LPD_DIRECTORY=static`, `LPD_ORCHESTRATORS` (comma-separated base URLs, or a file with one per line; `#` starts a comment) |
| `OnChainDirectory` | Orchestrators in the `BondingManager` transcoder pool on Arbitrum that are active in the current round (`isActiveTranscoder`) and registered in the `AIServiceRegistry` (non-empty `getServiceURI`) | `LPD_DIRECTORY=onchain`, `LPD_ARBITRUM_RPC_URL` |

The on-chain directory leaves out three kinds of orchestrator:
- **Unregistered:** active but with no AI service URI, so it offers transcoding only.
- **Unusable URI:** a URI that isn't an http(s) base URL. It is logged as `discovery.service_uri_invalid`.
- **Network-modules manifest:** a URI ending in `/.well-known/livepeer-registry.json`. That is the
  Cloud-SPE `livepeer-network-modules` protocol, which this engine cannot pay. It is logged as
  `discovery.registry_manifest_skipped`.

The pool walk reads no further than the pool's reported size, so a bad linked list cannot make it
loop.

To change who is discovered, write a directory and run `serve(settings, directory=...)`: an approved
list, your own fleet, a configuration database. To change everything, serve the same `GET /discovery`
format from any system and point `LPB_DISCOVERY_URL` at it.

## Probing and caching

**Probes.** Each orchestrator's `GET <base URL>/discovery` is read every `LPD_PROBE_INTERVAL_S`:
- at most 16 at a time, with an `LPD_PROBE_TIMEOUT_S` timeout and a 1 MiB limit, and no redirects;
- TLS is verified against the deployment's trust store: the system roots, as OpenSSL loads them, which
  `SSL_CERT_FILE` and `SSL_CERT_DIR` extend. A deployment that pins orchestrator certificates for its
  engine (inference-gateway's `runner-trust`) mounts the same pins and sets the same variable here;
- an orchestrator whose certificate that store does not accept is left out, logged as
  `discovery.orchestrator_excluded` with `reason="untrusted_certificate"`. The engine calls runners
  over verified TLS with the same kind of store, so it could never use one;
- any other failure is `reason="failed"`;
- an empty inventory counts as a success.

**Retention.** A failed probe keeps the orchestrator's last inventory until `LPD_MAX_FAILED_REFRESHES`
consecutive failures, then evicts it. `0` evicts on the first failure. A success resets the count.

**Directory reloads.** The directory reloads every `LPD_DIRECTORY_INTERVAL_S`, and on every probe tick
until it has loaded once:
- a failed load keeps the previous directory and sets `directory_error`;
- an orchestrator that leaves the directory drops out at once, and comes back only after it answers again.

The cache is not persisted.

## Settings

The prefix is `LPD_`, so these never collide with the engine's `LPB_*` settings.

| Variable | Default | Meaning |
| --- | --- | --- |
| `LPD_DIRECTORY` | `static` | `static` or `onchain` |
| `LPD_ORCHESTRATORS` | unset | Static: base URLs or a file. Required for `static` |
| `LPD_ARBITRUM_RPC_URL` | unset | On-chain: JSON-RPC endpoint, often carrying the provider key. Required for `onchain`; never logged |
| `LPD_BONDING_MANAGER` | `0x35Bcf3c30594191d53231E4FF333E8A770453e40` | `BondingManager` proxy, Arbitrum mainnet |
| `LPD_AI_SERVICE_REGISTRY` | `0x04C0b249740175999E5BF5c9ac1dA92431EF34C5` | `AIServiceRegistry`, Arbitrum mainnet |
| `LPD_DIRECTORY_INTERVAL_S` | `3600` | Directory reload. The active set changes per round |
| `LPD_PROBE_INTERVAL_S`, `LPD_PROBE_TIMEOUT_S` | `30`, `5` | Inventory refresh and the time allowed for each probe |
| `LPD_MAX_FAILED_REFRESHES` | `6` | Consecutive failed probes before eviction |
| `LPD_HTTP_HOST`, `LPD_HTTP_PORT` | `127.0.0.1`, `8939` | Listener. `8939` is livepeer-node discovery's port |
| `LPD_LOG_JSON` | `true` | JSON-lines logs |
