# Orchestrator discovery: an engine-owned list, verified TLS

## Status

Owner: John. Tickets: [#27](https://github.com/NE-Build-Track/livepeer-builder-core/issues/27) and
[#30](https://github.com/NE-Build-Track/livepeer-builder-core/issues/30) Phase 1 (M3; related to ISS-09
dynamic capability discovery). Branch `tasks/m3/orchestrator-list` off `releases/m3`. **Revised 22
October: `LPB_DISCOVERY_URL` (a `/discover-orchestrators` document) is the production source;
`LPB_ORCHESTRATOR_LIST` is probed once, at startup. Awaiting review. Not merged.** #30 Phase 2 (a Go discovery service outside this repository) is a
follow-up, not part of this plan.

## Goal

Move runner discovery off the signer; the signer is unmodified and only pays.

- **Production: `LPB_DISCOVERY_URL`.** It serves the JSON list `livepeer/node` returns from
  `GET /discover-orchestrators` (`DiscoveredOrchestrator`), re-read every interval.
- **Development and demos: `LPB_ORCHESTRATOR_LIST`.** It lists orchestrator addresses, inline or in a
  file. At startup the engine calls `GET /discovery` on each one once. Capacity from that probe is
  unknown, and price stays as probed (T16).

Every path parses runners with the SDK's `discover_runners` filter and then the
`DiscoveredOrchestrator` contract, one orchestrator at a time. The discovery URL, the probe and the
paid call verify TLS with the process trust store, and no deployment installs extra certificates.

## Acceptance

- [x] `LPB_ORCHESTRATOR_LIST` selects `OrchestratorListDiscoverySource`, which probes once on its first fetch,
      even with `LPB_SIGNER_URL` set. `LPB_DISCOVERY_URL` is re-read every interval. With both settings,
      `BuilderEngine.from_settings` raises `InvalidRequest`; an empty list clears an image default
      (`tests/unit/test_runtime.py`).
- [x] The list is inline (comma-separated) or orchWebhook JSON in a file.
  - Each address is probed with `GET /discovery` and redirects are not followed. No signer headers.
    At most 16 probes run at once.
  - Failures and untrusted certificates are left out and logged.
  - Probed runners have unknown `capacity_available`.
  - A list that names no orchestrator, or where every probe fails, raises.
  - Later fetches reuse the first good snapshot (`tests/unit/test_adapter_edges.py`,
    `tests/unit/test_discovery_tls.py`).
- [x] One bad orchestrator or runner does not drop the rest of a document.
  - The bad entry is skipped (`discovery.orchestrator_invalid` for an orchestrator), and unknown fields
    are ignored.
  - The fetch raises only when no named orchestrator parses.
  - `tests/fixtures/discover-orchestrators.json` goes through the real SDK filter for a discovery URL,
    and matches the SDK's own parser on URL, app, runner ID, mode, orchestrator and price
    (`tests/unit/test_adapter_edges.py`).
- [x] The Railway engine image copies `orchestrators.json` (orchWebhook addresses) and sets
      `LPB_ORCHESTRATOR_LIST`. The dev stack mounts the same file. The Railway signer sources are
      unchanged (`tests/structure/test_deploy.py`).
- [x] The dev-stack signer neither mounts nor points at pinned certificates; the pinning script is gone.
- [ ] `uv run pytest` with the coverage gates and `diff-cover` against `origin/releases/m3`, ruff, mypy,
      `scripts/check_docs.py`, `lpb gen-docs` with no drift, and `lpb smoke` pass in CI.
- [x] `docker compose -f deploy/railway/compose.yaml config` succeeded for the 21 October probe design
      ([evidence](../../references/evidence/2026-10-21-orchestrator-list.md)). Re-check before deploy.

## Progress

- [x] 21 October: `OrchestratorListDiscoverySource` (concurrent probes, at most 16 at once; merge;
      exclusion by certificate or failure, logged when it changes; duplicate runners removed),
      `EngineSettings.orchestrator_list`, engine wiring and the conflict check.
- [x] 21 October: unit, real-TLS and structure tests.
- [x] 21 October: Railway engine image carries `orchestrators.json` (the 28 signer addresses); the dev
      stack drops its pinned certificates and mounts `config/orchestrators.json` into the engine.
- [x] 21 October: docs (D42, D27 note, T12 resolved, RELIABILITY, SECURITY, contracts, references).
- [x] 21 October: live run against the Railway list, from a file and from an https URL
      ([evidence](../../references/evidence/2026-10-21-orchestrator-list.md)). That run probed
      `GET /discovery`; the 22 October revision does not.
- [x] 22 October: `LPB_ORCHESTRATOR_LIST` reads a `/discover-orchestrators` document. The per-orchestrator
      probe, exclusion log, and address-only deploy lists are gone. Railway and the dev stack use
      `deploy/railway/engine/orchestrators.json`.
- [x] 22 October (after review): the list is probed once, on the first fetch (`OrchestratorListDiscoverySource`;
      unknown capacity). `LPB_DISCOVERY_URL` is the production `/discover-orchestrators` source. Parsing
      skips a bad orchestrator or runner instead of failing the refresh. The demonstration catalog moved
      to `tests/fixtures/discover-orchestrators.json`. `orchestrators.json` is the address list again,
      and the real-TLS discovery test is back.
- [ ] Before the Railway deploy: confirm no Railway environment sets `LPB_DISCOVERY_URL` on the engine
      (the engine would refuse to start).

## Decision log

- 21 October: the list **replaces** signer discovery rather than merging with it (#27 over #30's
  additive proposal; John). Setting both `LPB_ORCHESTRATOR_LIST` and `LPB_DISCOVERY_URL` is a
  configuration error. Recorded as D42.
- 21 October: TLS stays on everywhere; orchestrators with untrusted certificates are excluded rather
  than pinned, and the dev stack's pinned certificates are removed (accepted TLS direction on #27,
  resolving the TLS concern on PR #31; retires T12). Exclusion by probe was dropped on 22 October and
  **restored after review**, now at startup only.
- 21 October: #30 Phase 2, an on-chain discovery service (a new Go service on `livepeer/node` packages,
  or an extension of `pymthouse/discovery-service` with verified TLS), is a follow-up. It needs no
  engine change: it serves a `/discover-orchestrators` document to `LPB_DISCOVERY_URL`.
- 22 October: retire the per-orchestrator `GET /discovery` fan-out (John). `LPB_ORCHESTRATOR_LIST` is
  either a local file or a URL of the signer's `/discover-orchestrators` JSON, the same document
  `livepeer-python-gateway` already reads. The engine parses it with `DiscoveredOrchestrator`.
  Deployments ship a short catalog of real orchestrators until the Go discovery service exists.
  Recorded as the revision of D42. **Superseded after review the same day:** see the last entry.
- 21 October: a TLS handshake against the system trust store found 7 of the 28 Railway orchestrators
  with publicly trusted certificates (19 self-signed, 2 unreachable). The address list was
  briefly replaced by a catalog on 22 October and is back after review; the startup probe excludes
  the untrusted ones.
- 21 October: the issue's "D29 in m2-decisions.md" became D42 in m3-decisions.md, because D29 is the
  streaming decision.
- 22 October, after review: the baked catalog froze capacity and price, which admission and the
  payment ceiling act on, and one bad entry failed the whole refresh (John).
  - `LPB_ORCHESTRATOR_LIST` goes back to an address list, probed once at startup, and its capacity is
    treated as unknown.
  - `LPB_DISCOVERY_URL` is the production `/discover-orchestrators` source.
  - The `DiscoveredOrchestrator` JSON stays as the schema contract, with a test fixture.
  - Parsing is per orchestrator and per runner and ignores unknown fields.
  - Discovery code is the SDK's where it is public (`discover_runners`, `orchestrator_discovery_urls`);
    its private parser is only checked for parity in a test.
  - Price after the probe is tracked as T16.
