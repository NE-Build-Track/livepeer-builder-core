# Orchestrator discovery: an engine-owned list, verified TLS

## Status

Owner: John. Tickets: [#27](https://github.com/NE-Build-Track/livepeer-builder-core/issues/27) and
[#30](https://github.com/NE-Build-Track/livepeer-builder-core/issues/30) Phase 1 (M3; related to ISS-09
dynamic capability discovery). Branch `tasks/m3/orchestrator-list` off `releases/m3`. **Implemented on
21 October and shown live the same day; awaiting Mike's review and Josh's review of the engine code.
Not merged.** #30 Phase 2 (an on-chain discovery service) is a follow-up, not part of this plan.

## Goal

Move runner discovery off the signer. The engine reads its own orchestrator list
(`LPB_ORCHESTRATOR_LIST`, go-livepeer `-orchWebhookUrl` JSON from a file or an http(s) URL), calls
`GET /discovery` on every listed orchestrator concurrently each interval, and merges the runners that
answer into the snapshot `DiscoveryService` already caches. The signer is unmodified and only pays.
Discovery and the paid call verify TLS with the process trust store, so an orchestrator whose
certificate that store does not accept is left out at discovery and never called, and no deployment
installs extra certificates.

## Acceptance

- [x] `LPB_ORCHESTRATOR_LIST` selects `OrchestratorListDiscoverySource`, even with `LPB_SIGNER_URL` set;
      with `LPB_DISCOVERY_URL` as well, `BuilderEngine.from_settings` raises `InvalidRequest`. The engine
      does not discover through the signer (`tests/unit/test_runtime.py`).
- [x] Discovery and `call_runner` verify TLS; no source path disables it or installs a CA
      (`tests/structure/test_tls.py`, `tests/structure/test_deploy.py`).
- [x] Startup excludes a self-signed orchestrator and keeps one whose certificate the process trust
      store accepts, over real local HTTPS servers (`tests/unit/test_discovery_tls.py`).
- [x] A call-time TLS failure records `unreachable` with no payment, buffered and streamed
      (`tests/unit/test_discovery_tls.py`).
- [x] Unit tests cover the file and URL sources, skipped empty addresses, a non-list, an empty list, an
      invalid URL, a partial failure that still merges, a certificate failure that excludes that
      orchestrator, and a total failure that raises the first error (`tests/unit/test_adapter_edges.py`).
- [x] Structure tests require `deploy/railway/engine/orchestrators.json` to be a non-empty list of
      `address` entries and the engine Dockerfile to set `LPB_ORCHESTRATOR_LIST`. The Railway signer
      sources are unchanged.
- [x] The dev-stack signer neither mounts nor points at pinned certificates; the pinning script is gone.
- [ ] `uv run pytest` with the coverage gates and `diff-cover` against `origin/releases/m3`, ruff, mypy,
      `scripts/check_docs.py`, `lpb gen-docs` with no drift, and `lpb smoke` pass in CI.
- [x] `docker compose -f deploy/railway/compose.yaml config` succeeds and the engine image builds
      ([evidence](../../references/evidence/2026-10-21-orchestrator-list.md)).
- [x] A local engine with `LPB_ORCHESTRATOR_LIST` pointed at the Railway JSON returns runners from
      several trusted orchestrators on `GET /v1/runners`, `/readyz` shows no `discovery_error`, and the
      same holds with the list served from an https URL
      ([evidence](../../references/evidence/2026-10-21-orchestrator-list.md)).

## Progress

- [x] 21 October: `OrchestratorListDiscoverySource` (concurrent probes, at most 16 at once; merge;
      exclusion by certificate or failure, logged when it changes; duplicate runners removed),
      `EngineSettings.orchestrator_list`, engine wiring and the conflict check.
- [x] 21 October: unit, real-TLS and structure tests.
- [x] 21 October: Railway engine image carries `orchestrators.json` (the 28 signer addresses); the dev
      stack drops its pinned certificates and mounts `config/orchestrators.json` into the engine.
- [x] 21 October: docs (D42, D27 note, T12 resolved, RELIABILITY, SECURITY, contracts, references).
- [x] 21 October: live run against the Railway list, from a file and from an https URL
      ([evidence](../../references/evidence/2026-10-21-orchestrator-list.md)).
- [ ] Before the Railway deploy: confirm no Railway environment sets `LPB_DISCOVERY_URL` on the engine
      (the engine would refuse to start).

## Decision log

- 21 October: the list **replaces** signer discovery rather than merging with it (#27 over #30's
  additive proposal; John). Setting both `LPB_ORCHESTRATOR_LIST` and `LPB_DISCOVERY_URL` is a
  configuration error. Recorded as D42.
- 21 October: TLS stays on everywhere; orchestrators with untrusted certificates are excluded rather
  than pinned, and the dev stack's pinned certificates are removed (accepted TLS direction on #27,
  resolving the TLS concern on PR #31; retires T12).
- 21 October: #30 Phase 2, an on-chain discovery service (a new Go service on `livepeer/node` packages,
  or an extension of `pymthouse/discovery-service` with verified TLS), is a follow-up. It needs no
  engine change: it serves runner entries to `LPB_DISCOVERY_URL`, or an orchestrator list to
  `LPB_ORCHESTRATOR_LIST`.
- 21 October: a TLS handshake against the system trust store found 7 of the 28 Railway orchestrators
  with publicly trusted certificates (19 self-signed, 2 unreachable). The Railway list keeps all 28, so
  an orchestrator is picked up as soon as it serves a trusted certificate; the dev-stack list holds the
  7 trusted ones.
- 21 October: the issue's "D29 in m2-decisions.md" became D42 in m3-decisions.md, because D29 is the
  streaming decision.
