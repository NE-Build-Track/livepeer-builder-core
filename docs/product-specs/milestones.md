# Milestones and tickets

**Source:** Cloud-SPE/Network-Engineering-SPE, `docs/design-docs/build-track-december-2026-issue-proposal-draft.md`
(accepted delivery mapping), and the GitHub project Cloud-SPE/projects/13. A milestone passes when its
issues and gate evidence are accepted. Nothing closes without Mike's review.

## Gates

| Milestone | Gate | Target |
| --- | --- | --- |
| M1: Define and approve the architecture | September planning evidence; architecture and executive summary approved | Done (30 Sep) |
| **M2: Foundation and integration contracts** | **Source baseline, shared/access/payment contracts, importable core, SQLite evidence, and development build checks are reviewable** | **16 Oct 2026** |
| M3: Complete builder journey | One minimum-app journey demonstrates all seven outcomes, with funded execution and correlated network-cost evidence | — |
| M4: Reuse and reliability | Remaining execution modes, REST/MCP and embedded/service coverage, enterprise auth replacement, both payment configurations, failure/recovery evidence | — |
| M5: Independent installation and release | Final artifacts, documentation, clean-environment reproduction, acceptance evidence | — |

## M2 acceptance criteria (verbatim) and where each is met

| Ticket | Acceptance | Met by |
| --- | --- | --- |
| ISS-01 Baseline | Record selected Console, Python SDK, Batteries, and go-livepeer revisions and recheck required behavior; distinguish source evidence from runtime proof. Record reusable material, permissions, compatibility gaps, and dispositions; review offered private source if available; the engine stays independently buildable without it | [completed/iss-01-baseline.md](../exec-plans/completed/iss-01-baseline.md); [upstream-baseline.md](../references/upstream-baseline.md) (Console out of scope; node replaced go-livepeer, D2–D3) |
| ISS-02 Engine contracts | Document access context, capabilities/rates, jobs/attempts, results, measured usage, cost states, and extension boundaries with examples to implement and test them. Cover immediate, asynchronous and persistent-endpoint execution; identify upstream contracts; explicitly dispose of deployment, performance and recovery assumptions. New APIs, HA, financial recourse and extra streaming are not assumed | [contracts/engine.md](../contracts/engine.md), [events.md](../contracts/events.md), [ARCHITECTURE.md](../../ARCHITECTURE.md#providers) |
| ISS-03 Access | Define how trusted standalone and enterprise adapters produce validated actor identity, ownership scope and permitted operations across imported and service modes. Document browser and administrator trust boundaries and credential-lifecycle responsibilities. Enterprises choose enrollment and authentication without core patches or an additional user-facing engine key; unchecked request fields cannot establish identity | [contracts/access.md](../contracts/access.md) |
| ISS-04 Payments | Record credential/allocation mapping, permitted management operations, reporting access, units, stable references, and repeat-command semantics. For each required interface, record implementation evidence or an agreed gap disposition from the affected maintainer; link proposed upstream changes; contract agreement isn't implemented compatibility | [contracts/payments.md](../contracts/payments.md), [references/](../references/index.md), [tech debt T1, T13](../exec-plans/tech-debt-tracker.md) |
| ISS-05 Core packages | The shared core imports and starts independently of the reference application, proprietary identity, PymtHouse, and private Inc source. Document supported interfaces for persistence, execution/payment adapters, and enterprise extension. Provide an import/start smoke check using a versioned development build | `lpb smoke`; [ARCHITECTURE.md](../../ARCHITECTURE.md); CI `build-install` job |
| ISS-06 Records | Persist access mappings, jobs/attempts, result references, measured usage and cost projections behind the storage interface. Exercise restart, schema migration and restore with representative records; document configurable retention. Preserve the provider ledger as the network-accounting authority | `store/`; `tests/store/`; [contracts/persistence.md](../contracts/persistence.md) |
| ISS-07 Builds and CI | Produce versioned development packages and automate test, lint/type and install/start checks for the components available. Provide the build/test framework later components extend; final artifacts are ISS-38 | `.github/workflows/ci.yml` |

The detailed status per ticket is in [m2-traceability.md](m2-traceability.md).

## Later tickets this repository already prepares for

| Ticket | Milestone | Prepared here |
| --- | --- | --- |
| ISS-08 Standalone credentials and ownership controls | M3 | `ApiKeyAuthenticator`, `KeyService`, `DefaultPolicy` (implemented) |
| ISS-09 Dynamic capability discovery | M3 | `DiscoveryService` (implemented); capability resolution is T3 |
| ISS-10 Network rates and pricing assumptions | M3 | `Offering.rate_low/high` (implemented) |
| ISS-11 Scoped allocation provisioning and management | M3 | `PaymentsService` (implemented) |
| ISS-12 SDK payment authorization and remote signing | M3 | `SdkRunnerTransport` (implemented); funded proof on public orchestrators, 14 October ([evidence](../references/evidence/2026-10-14-dev-stack-public-orchestrators.md)) |
| ISS-13 First funded payment configuration | M3 | `deploy/dev-stack` |
| ISS-14 Durable submission and immediate execution | M3 | `JobService.run` (implemented) |
| ISS-15 Owned job history and basic results | M3 | `GET /v1/jobs`, `ResultRef` (implemented) |
| ISS-16 Capture usage; correlate jobs with payment evidence | M3 | `report_usage`; `auth_id` correlation (implemented) |
| ISS-17 Replayable network-cost projections | M3 | `CostService` projections and `corrected` (implemented) |
| ISS-18 Scoped usage, cost reports and integration events | M3 | Cost reads (implemented); events feed **defined** ([events.md](../contracts/events.md)) |
| ISS-19 Representative jobs and acceptance matrix | M3 | Open |
| ISS-20 REST for the first journey | M3 | `http/app.py` (minimal, implemented) |
| ISS-21–25 Reference app, journey guide, funded seven-outcome proof | M3 | Out of this repository (reference app); evidence plans in `docs/exec-plans/` |
| ISS-26 Async jobs and durable recovery handles | M4 | **Defined:** `mode="async"`, `ResultRef.storage="stored"` → 501 |
| ISS-27 Persistent application endpoints | M4 | **Defined:** `SessionManager`, engine-proxied (D9) → 501 |
| ISS-28 Failure classification and recovery across modes | M4 | Failure taxonomy and recovery for immediate jobs (implemented) |
| ISS-29 REST across all execution modes | M4 | Reserved routes return 501 |
| ISS-30 The same journey through MCP | M4 | Not started; MCP auth defined in [access.md](../contracts/access.md#modes) |
| ISS-31 Both payment configurations | M4 | Mode A implemented; Mode B defined as a gap |
| ISS-32 Embedding, service consumption, enterprise extensions | M4 | Protocols and both integration modes |
| ISS-33 Replace the reference app's access flow | M4 | `Authenticator` / `AccessPolicy` protocols; JWT bearer defined |
| ISS-34–36 Reference-app coverage, isolation and replay, funded matrix | M4 | Store and cost tests are the foundation |
| ISS-37–39 Docs, release artifacts, independent installation | M5 | `docs/`, CI dev builds, `lpb smoke` |

## Upstream interfaces (H1–H6)

| Ref | Interface | Status (14 October) |
| --- | --- | --- |
| H1 | Python gateway SDK | Pinned to [livepeer-python-gateway#77](https://github.com/livepeer/livepeer-python-gateway/pull/77) `e6d243b` (`on_payment_session` reports `manifest_id`; no signer-state decoding). **Open:** merge into `main`, then re-pin (T1). #70 and multipart deferred |
| H2 | Batteries management, authorization and accounting | Delivered upstream (#5, #6, #7) at `ca09e47`. Mode B scoping deferred to M4 |
| H3 | Remote signer and orchestrator/Live Runner | livepeer/node `f970be6`, which accepts a ticket recipient that differs from the node address (T10, resolved upstream). Orchestrators are to serve CA-signed certificates (D27, T12) |
| H4 | Funded acceptance environment | The dev stack plus the `cloud-spe-gateway` payer is funded; paid jobs on public orchestrators reconciled on 14 October ([evidence](../references/evidence/2026-10-14-dev-stack-public-orchestrators.md)) |
| H5 | Source access and permitted reuse | Done (ISS-01): simple-infra reviewed; nothing private reused |
| H6 | Scope and final acceptance | Mike's review per ticket; procedure open for M5 |
