# Discovery

- **Ticket:** [#45](https://github.com/NE-Build-Track/livepeer-builder-core/issues/45), part of ISS-09.
- **Plan:** [discovery-service](../exec-plans/active/discovery-service.md).
- **Code:** [NE-Build-Track/livepeer-discovery](https://github.com/NE-Build-Track/livepeer-discovery) `737e520`, process `livepeer-onchain-discovery`. This repository no longer ships the process.
- **Status:** the engine reads a `GET /discovery` inventory. The process that builds it is the on-chain discovery service.

The engine does not walk the chain and does not probe orchestrators. It polls `LPB_DISCOVERY_URL`
([engine.md#discovery](engine.md#discovery)). That URL is the on-chain discovery service, livepeer-node's
own discovery, or one orchestrator. Nothing in the engine imports the discovery process.

## HTTP

The service answers livepeer-node's inventory. Railway and the dev stack run
`livepeer-onchain-discovery` and keep the service name `discovery`, so the engine's URL stays
`http://discovery:8939/discovery` (on Railway, `http://discovery.railway.internal:8939/discovery`).

| Route | Answer |
| --- | --- |
| `GET /discovery` | `200` with a JSON array. A metadata entry `{"nodes": [...]}` has no `address` and the engine skips it. Each other entry is `{"address": "<orchestrator base URL>", "runners": [...]}`. Runner records are passed through |
| `GET /readyz` | `200` once a directory has loaded, a probe pass has finished, and the loops are running; otherwise `503`. The body is `ready`, `refreshing`, `orchestrators`, `serving`, `directory_loaded_at`, `probed_at`, and `directory_error`. `directory_error` is a stable reason this process wrote, never a provider's text or the RPC URL |
| `GET /healthz` | `200` while the process runs |

There is no app filtering. The engine filters per request.

## Who is probed

The process unions two sources:

- **Chain.** With `LIVEPEER_ONCHAIN_DISCOVERY_RPC_URL` set, it walks the Arbitrum bonded pool through the protocol `Controller` (`0xD8E8328501E9645d16Cf49539efC04f734606ee4` unless overridden) and reads service URIs from the AI Service Registry (`0x04C0b249740175999E5BF5c9ac1dA92431EF34C5`). `LIVEPEER_ONCHAIN_DISCOVERY_SERVICE_REGISTRY=controller` reads the Controller's protocol `ServiceRegistry` instead. An orchestrator is eligible when it is active this round and its URI is an `https` base URL. A URI ending in `/.well-known/livepeer-registry.json` is skipped. One reverting orchestrator is skipped. A transient RPC failure is retried, then the whole load fails and the previous directory is kept.
- **File.** `LIVEPEER_ONCHAIN_DISCOVERY_NODES` is a file of base URLs, one per line, `#` starting a comment, or a comma-separated list. Railway bakes in `deploy/railway/discovery/orchestrators.txt`. The dev stack mounts `deploy/dev-stack/config/orchestrators.txt`.

The RPC URL is required. The process exits before listening when it is empty, and it never prints the value. A mode-0600 file may be named with `LIVEPEER_ONCHAIN_DISCOVERY_RPC_URL_FILE`; the entrypoint reads it into the variable. The dev stack does that and runs the container as root so the file is readable. Railway sets the variable and runs as uid `10001`.

Settings use the prefix `LIVEPEER_ONCHAIN_DISCOVERY_`. The listen address is `[::]:8939` on Railway and `0.0.0.0:8939` on the dev stack. Probes run every 30 seconds in both deploys. A source keeps its last inventory until 6 consecutive failed probes, then it is evicted. A failed directory load keeps the previous directory. The chain directory reloads every hour.

## Known limit

The collector drops a runner with no `observed_at`, or one more than 30 seconds in the future
([livepeer/node#51](https://github.com/livepeer/node/issues/51)). Orchestrators that still speak the
v1.0.2 inventory do not appear. The engine is looser: a runner that reaches it with no stamp is kept,
stamped with the poll time. A runner the collector already dropped never reaches the engine.

## Replacing it

Serve the same `GET /discovery` document from another process and point `LPB_DISCOVERY_URL` at it.
To change who the engine calls in-process, supply another `DiscoverySource`
([extending the engine](../design-docs/extending-the-engine.md)).
