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
| `GET /readyz` | `200` once a directory has loaded, a probe pass has finished, and the refresh loop is still running; otherwise `503`, so a stopped refresh, which would serve a frozen inventory, gets the process restarted. The body is `ready`, `refreshing`, `orchestrators` (in the directory), `serving` (with an inventory), `directory_loaded_at`, `probed_at`, and `directory_error`: the last failed directory load as a stable reason (the error's type, and its message only when this service wrote it), or `null`. The endpoint is unauthenticated, so it never carries another system's error text |
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
| `OnChainDirectory` | Orchestrators in the `BondingManager` transcoder pool on Arbitrum that are active in the current round (`isActiveTranscoder`) and registered in a service registry (non-empty `getServiceURI`): the AI Service Registry unless overridden | `LPD_DIRECTORY=onchain`, `LPD_ARBITRUM_RPC_URL` |

**Which contracts.** The on-chain directory looks up the `BondingManager` in the protocol's `Controller`
(`getContract`) on every load, so a protocol upgrade is followed.
- **The registry it reads** is `LPD_SERVICE_REGISTRY`, which defaults to the AI Service Registry.
- **Why that is an override:** the AI Service Registry was deployed beside the Controller and never
  registered in it (`getContract(keccak256("AIServiceRegistry"))` is zero on Arbitrum). The Controller's
  own `ServiceRegistry` is the protocol's registry for transcoding.
- **To read it instead,** set `LPD_SERVICE_REGISTRY=controller`.

**A registration is not operator input.** Anyone with enough stake can register any URI, so the directory
treats each one as untrusted:
- the URI must be an `https` base URL;
- the orchestrator is marked `public_only` for the prober (see [Probing](#probing-and-caching));
- one orchestrator whose reads fail (an RPC error, a revert, a URI that is not UTF-8) is skipped and
  logged as `discovery.orchestrator_unreadable`, and the rest are returned. Only the Controller lookups
  or the pool itself failing fail the load;
- the pool walk reads no further than the pool's reported size and never more than 1000 (logged as
  `discovery.pool_truncated`), so a bad linked list or a bad size cannot stall it.

It leaves out three kinds of orchestrator:
- **Unregistered:** active but with no URI in the registry it reads.
- **Unusable URI:** a URI that isn't an https base URL. It is logged as `discovery.service_uri_invalid`.
- **Network-modules manifest:** a URI ending in `/.well-known/livepeer-registry.json`. That is the
  Cloud-SPE `livepeer-network-modules` protocol, which this engine cannot pay. It is logged as
  `discovery.registry_manifest_skipped`.

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
- **a URL the operator did not write** (`public_only`, from the chain) is resolved once and contacted
  only if every address is public: not private, loopback, link-local, shared (CGNAT), reserved or
  multicast. The checked address is the one connected to, with TLS still verified against the name, so
  a registration cannot reach the service's own network or a metadata endpoint, nor rebind between the
  check and the connection. Otherwise `reason="private_address"`;
- an inventory whose `runners` is not a list (a number, a string, an object) is a failed probe, never
  an empty inventory that replaces the last good one. `null` means no runners;
- any other failure, of any kind, is `reason="failed"` for that orchestrator alone: it never ends the
  pass;
- an empty inventory counts as a success.

**Retention.** A failed probe keeps the orchestrator's last inventory until `LPD_MAX_FAILED_REFRESHES`
consecutive failures, then evicts it. `0` evicts on the first failure. A success resets the count.

**Directory reloads.** The directory reloads every `LPD_DIRECTORY_INTERVAL_S`, and on every probe tick
until it has loaded once:
- a load that takes longer than `LPD_DIRECTORY_TIMEOUT_S` is abandoned, so probing goes on;
- a failed load keeps the previous directory and sets `directory_error` (its full text is logged as
  `discovery.directory_failed`);
- an orchestrator that leaves the directory drops out at once, and comes back only after it answers again.

**The loop survives its own errors.** A pass that fails is logged as `discovery.refresh_failed` and
the next tick runs. If the loop ever stops, `discovery.refresh_stopped` is logged and `/readyz` fails.

**Logs.** The service's logging keeps `httpx` at WARNING: its per-request INFO line carries the URL,
and `LPD_ARBITRUM_RPC_URL` usually carries the provider's key. A JSON-RPC error is reported by its code,
never with the provider's text.

The cache is not persisted.

## Settings

The prefix is `LPD_`, so these never collide with the engine's `LPB_*` settings.

| Variable | Default | Meaning |
| --- | --- | --- |
| `LPD_DIRECTORY` | `static` | `static` or `onchain` |
| `LPD_ORCHESTRATORS` | unset | Static: base URLs or a file. Required for `static` |
| `LPD_ARBITRUM_RPC_URL` | unset | On-chain: JSON-RPC endpoint, often carrying the provider key. Required for `onchain`; never logged |
| `LPD_CONTROLLER` | `0xD8E8328501E9645d16Cf49539efC04f734606ee4` | The protocol's `Controller` on Arbitrum mainnet; the `BondingManager` (and, with `controller` below, the `ServiceRegistry`) is looked up in it |
| `LPD_SERVICE_REGISTRY` | `0x04C0b249740175999E5BF5c9ac1dA92431EF34C5` | The registry read for service URIs: the AI Service Registry, an override of the Controller's. `controller` reads the Controller's protocol `ServiceRegistry` |
| `LPD_DIRECTORY_INTERVAL_S` | `3600` | Directory reload. The active set changes per round |
| `LPD_DIRECTORY_TIMEOUT_S` | `300` | The most one directory load may take before it is abandoned |
| `LPD_PROBE_INTERVAL_S`, `LPD_PROBE_TIMEOUT_S` | `30`, `5` | Inventory refresh and the time allowed for each probe |
| `LPD_MAX_FAILED_REFRESHES` | `6` | Consecutive failed probes before eviction |
| `LPD_HTTP_HOST`, `LPD_HTTP_PORT` | `127.0.0.1`, `8939` | Listener. `8939` is livepeer-node discovery's port |
| `LPD_LOG_JSON` | `true` | JSON-lines logs |
