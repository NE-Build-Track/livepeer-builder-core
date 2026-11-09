# Discovery

- **Ticket:** [#45](https://github.com/NE-Build-Track/livepeer-builder-core/issues/45), part of ISS-09.
- **Plan:** [discovery-service](../exec-plans/active/discovery-service.md).
- **Code:** the engine polls `LPB_DISCOVERY_URL`. This repository does not ship a discovery process.
- **Status:** the engine reads a `GET /discovery` inventory. Another process builds it.

The engine does not walk the chain and does not probe orchestrators. It polls `LPB_DISCOVERY_URL`
([engine.md#discovery](engine.md#discovery)). That URL may be livepeer-node's `livepeer-discovery`
or one orchestrator. The dev stack runs `livepeer-discovery` at `f45d83f` in static mode
(`deploy/dev-stack/config/discovery.toml`). Nothing in the engine imports a discovery process.

## HTTP

The engine accepts livepeer-node's inventory. In the dev stack the URL is
`http://discovery:8939/discovery`.

| Route | What the engine reads |
| --- | --- |
| `GET /discovery` | `200` with a JSON array. A metadata entry `{"nodes": [...]}` has no `address` and the engine skips it. Each other entry is `{"address": "<orchestrator base URL>", "runners": [...]}`. Runner records are passed through |

There is no app filtering. The engine filters per request.

The dev stack's collector GETs each listed URL at startup and every `RefreshInterval`, and it
verifies TLS with the system trust store. A runner without `observed_at`, or one stamped more than
30 seconds ahead, is dropped, and a TLS failure never enters the catalog. `/readyz` on that
process is on the metrics port and is ready when the process listens. The engine is looser: a
runner that reaches it with no stamp is kept, stamped with the poll time. A runner the collector
already dropped never reaches the engine.

## Replacing it

Serve the same `GET /discovery` document from another process and point `LPB_DISCOVERY_URL` at it.
To change who the engine calls in-process, supply another `DiscoverySource`
([extending the engine](../design-docs/extending-the-engine.md)).
