# Discovery: livepeer/node's standalone discovery, `--nodes`, and `lpb discovery`

**Status:** review for discussion, 29 October 2026. This is the cross-component review D43 deferred
("its webhook and standalone discovery wait for a cross-component review").\
**Trackers:** ISS-09 (closed; [#45](https://github.com/NE-Build-Track/livepeer-builder-core/issues/45),
[#70](https://github.com/NE-Build-Track/livepeer-builder-core/issues/70)); ties into ISS-10 (rate age).\
**Owner:** John (engine). **Reviewers:** Mike, John.\
**Verified against:** livepeer/node `main` at `a4b68ed` (v1.0.6 plus metrics), our stacks' pin `406b248`
(v1.0.2 era); builder-core `releases/m3` at `d3df806`.

## What changed in livepeer/node

| Commit (date) | Version | Change |
| --- | --- | --- |
| `0ef907d` (16 Oct) | 1.0.3 | **Signer discovery webhook:** the signer can fetch its list of discovery URLs from a webhook (`OrchestratorWebhookFile`) |
| `e479451` (22 Oct) | 1.0.3 | **Standalone discovery** (`livepeer discovery`, `livepeer-discovery`): a service that collects and caches runner inventories from a configured list of sources. **Orchestrators stamp each runner with `observed_at`** at response time |
| `d558064` (23 Oct) | 1.0.3 | **Signer background discovery:** the signer polls its `Orchestrators` (orchestrators or discovery services) and serves `GET /discover-orchestrators` from a cache |
| `79cd2ca` (23 Oct) | 1.0.4 | Release. **Collectors (discovery and signer) reject a runner with no or bad `observed_at`**; a source whose runners all fail is a failed refresh |
| `b4ec044` (22 Oct) | — | Regression test: the signer refuses to sign for an inactive orchestrator (zero claimable reserve), answering 400 |

Our stacks pin `406b248`, before all of this. Most public orchestrators run v1.0.2 (#70).

## What the "nodes" setting means

`Nodes` (CLI `--nodes`, env `LIVEPEER_DISCOVERY_NODES`) is the standalone discovery service's **source
list**: base URLs of orchestrators **or of other discovery services**. It is the only way that service
learns who exists. There is no on-chain directory in it (no RPC; the docs say none is needed). The
related settings:

| Setting | Effect |
| --- | --- |
| `Nodes` | Sources to poll at `<base>/discovery` every `RefreshInterval` (default 5 m); 5 at a time, 3 s timeout, 1 MiB limit, no redirects, system TLS trust |
| `Grants` | Allow private or local destinations (default: public only) |
| `AdvertiseNodes` (default true) | Prepend a metadata entry `{"nodes": [{"url", "queried_at"}]}`: each source that answered and its last successful query time. It has no `address` |
| `AdvertiseRunners` (default true) | Serve the cached runners grouped by owning orchestrator |
| `MaxFailedRefreshes` (default 6) | Keep a failing source's last inventory this many refreshes, then evict |

**Federation.** Because a source can be another discovery service, discovery can be layered: a
discovery service can aggregate other discovery services. Runner records pass through whole, each keeps
the orchestrator's original `observed_at`, and duplicates resolve to the newest record and its owning
address (`discovery/shared/inventory.go`, `Merge`). The docs warn that `Nodes` must be **acyclic**: in a
cycle, peers can circulate a withdrawn runner forever, because preserved `observed_at` has no age limit.

**The signer** has its own `Orchestrators` list (static, or from the webhook) for
`GET /discover-orchestrators`. **Payment does not depend on it:** `/generate-live-payment` checks the
orchestrator on chain (a funded deposit and claimable reserve; an inactive orchestrator gets 400), not
membership in the signer's cache.

## Impact on builder-core

### The engine (`HttpDiscoverySource`, `LPB_DISCOVERY_URL`)

**Compatible.** It reads `[{"address", "runners"}]`, skips the metadata entry (no `address`), and does not
require `observed_at`, so it reads v1.0.2 and v1.0.4+ orchestrators, `lpb discovery` and node discovery
alike (run live against v1.0.6 in #70). Two follow-ups:

- **Freshness is available and unused.** Per-runner `observed_at` (v1.0.4+) and per-source `queried_at`
  give the age of a reading at the orchestrator, not just the engine's poll time. The ISS-10 guide
  ([iss-10-rates.md](iss-10-rates.md)) reads `observed_at`; `queried_at` is optional.
- **Pointing the engine at node discovery today serves an empty catalog**, because node's collector drops
  every runner from a v1.0.2 orchestrator (#70). It works once the orchestrators it reads run v1.0.4+.

### `lpb discovery` (`discovery/`)

| Behavior | Today | Consequence |
| --- | --- | --- |
| Address | Every runner in a probed answer is filed under **the URL that was probed** (`probe.py`: "an orchestrator cannot claim another's") | Correct for an orchestrator. **Wrong for a discovery service listed as a source:** all its orchestrators' runners would appear to belong to one orchestrator. Payment still works (runner URLs sit on their orchestrator's origin), but paid failover to *another* orchestrator (D52) and the `orchestrator_url` on attempts and costs would be wrong |
| `nodes` metadata entry | Skipped (no `runners`) | Fine |
| `observed_at` | Passed through untouched; never required, never stamped | Our output carries no `observed_at` for v1.0.2 orchestrators, so **node's discovery or signer cannot consume `lpb discovery`** (their collectors would drop those runners) |
| Duplicate runner URLs | Not merged | Harmless for one orchestrator; diverges from node's newest-wins rule |
| Eligibility | On-chain directory (active this round, in the AI Service Registry) or a static list; SSRF guard for on-chain URLs; TLS parity with the engine | **Node discovery has no equivalent of the on-chain directory**; its source list is operator-written |

### Our stacks

No change is forced: they pin `406b248`, the engine and `lpb discovery` read v1.0.2 orchestrators, and
the signer is not asked to discover (D43). Bumping the node pin to v1.0.6 is a separate review: it also
changes the signer (`0f29162`, fee in the signer state), redemption recovery (`c74c593`), and the
reserved event (`f52724d`).

## Options

1. **Keep `lpb discovery` as the default (recommended for M3).** It works with today's v1.0.2 fleet, has
   the on-chain directory, and matches the engine's TLS rules. Make it forward-compatible (below).
2. **Switch to node's standalone discovery.** Less code for us, and federation for free, but it serves an
   empty catalog until the fleet is v1.0.4+, and it has no on-chain eligibility. Revisit when most
   orchestrators stamp `observed_at`.
3. **Both, layered:** node discovery as the aggregator over several `lpb discovery` instances (one
   on-chain, one per enterprise list). Needs `lpb discovery` to stamp `observed_at` first.

## Proposed changes (option 1, forward-compatible)

In `discovery/` (our code; nothing upstream):

1. **Refuse aggregators as sources.** A probed answer that carries a `nodes` metadata entry, or runners
   under more than one distinct `address`, is a discovery service, not an orchestrator: exclude it with
   `reason="aggregator"` and log it. This keeps "an orchestrator cannot claim another's" true. (Q2 asks
   whether to support operator-written aggregators instead.)
2. **Stamp `observed_at` on runners that lack one,** with the probe's completion time (RFC 3339, UTC),
   and keep an orchestrator's own stamp when present. Then node's collectors accept our output, and the
   engine gets an age for every runner. Drop a stamp more than 30 s in the future, as node does.
3. **Optionally advertise `nodes`** (`LPD_ADVERTISE_NODES`, default off in M3): the probed orchestrators
   that answered and their `queried_at`, in node's format.
4. **Merge duplicate runner URLs newest-first,** matching node's rule.
5. **Docs:** `contracts/discovery.md` gains a "livepeer/node compatibility" section (formats, versions,
   the acyclic rule, aggregators refused); D43 records this review's outcome; `engine.md` notes that node
   discovery needs v1.0.4+ orchestrators.

## Test plan (test-driven)

**Level:** the existing discovery tests (`tests/discovery/`) with a loopback orchestrator returning chosen
inventories, and `FixedClock`.

```text
[ ] an answer with a nodes metadata entry is excluded as an aggregator
[ ] an answer with runners under two addresses is excluded as an aggregator
[ ] a v1.0.2 runner (no observed_at) is served with observed_at = probe time
[ ] a v1.0.4 runner keeps its own observed_at
[ ] an observed_at more than 30 s in the future is replaced by the probe time
[ ] duplicate runner URLs keep the newest record
[ ] with LPD_ADVERTISE_NODES on, the metadata entry lists answering orchestrators and queried_at
[ ] node's inventory decoder accepts our output (a fixture captured from livepeer-node's Decode rules)
[ ] the engine still reads our output, node discovery output and a single orchestrator (exists: #70 run; add a fixture test)
```

**Starter test:** the aggregator exclusion, since it guards a wrong `orchestrator_url` in failover and
costs.

## Upstream dependencies

None. Everything above is in builder-core's own discovery service and engine. Nothing in livepeer/node
needs to change; we adopt its formats. One observation for the team, not a dependency: v1.0.4's
`observed_at` requirement made v1.0.2 orchestrators invisible to v1.0.4+ collectors without a breaking-
change note (#70).

## Open questions to lock

- **Q1.** Option 1 for M3 (recommended), with option 2 or 3 revisited when the fleet is v1.0.4+?
- **Q2.** Refuse discovery services as sources (recommended), or allow operator-written aggregators and
  keep their per-entry addresses?
- **Q3.** Stamp `observed_at` on v1.0.2 runners (recommended), so node's collectors can read us?
- **Q4.** Plan the node pin bump to v1.0.6 as its own review after M3's funded run?
