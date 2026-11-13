# Milestones and tickets

**Source:** Cloud-SPE/Network-Engineering-SPE, `docs/design-docs/build-track-december-2026-issue-proposal-draft.md`
(accepted delivery mapping), and the GitHub project Cloud-SPE/projects/13. A milestone passes when its
issues and gate evidence are accepted. Nothing closes without Mike's review.

## Gates

| Milestone | Gate | Target |
| --- | --- | --- |
| M1: Define and approve the architecture | September planning evidence; architecture and executive summary approved | Done (30 Sep) |
| M2: Foundation and integration contracts | Source baseline, shared/access/payment contracts, importable core, SQLite evidence, and development build checks are reviewable | Done (19 Oct; target 16 Oct). Carried-forward items: [m2-traceability](m2-traceability.md#carried-forward-to-m3) |
| M3: Complete builder journey | One minimum-app journey demonstrates all seven outcomes, with funded execution and correlated network-cost evidence | Done (9 Nov; target 20 Nov). [Tickets](#m3-tickets-and-where-each-is-met); carried-forward items: [below](#carried-forward-to-m4) |
| **M4: Reuse and reliability** | **Remaining execution modes, REST/MCP and embedded/service coverage, enterprise auth replacement, both payment configurations, failure/recovery evidence** | **Next** |
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

## M3 tickets and where each is met

M3 closed on 9 November 2026: the tracker ([#20](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/20)) and all 18 tickets are closed on the
[project board](https://github.com/orgs/Cloud-SPE/projects/13). "Console" is the reference app,
`NE-Build-Track/builder-core-console`. The checks behind the access, repeat and accounting rows are in
[m3-traceability.md](m3-traceability.md).

| Ticket | Closed | Met by |
| --- | --- | --- |
| ISS-08 Standalone credentials and ownership controls ([#21](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/21)) | 27 Oct | Inspect and rotate keys, 403 before 404, the key audit log: [#48](https://github.com/NE-Build-Track/livepeer-builder-core/pull/48) (D44); [plan](../exec-plans/completed/iss-08-standalone-credentials.md); [access.md](../contracts/access.md) |
| ISS-09 Dynamic capability discovery ([#22](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/22)) | 28 Oct | The discovery service, [#46](https://github.com/NE-Build-Track/livepeer-builder-core/pull/46) (D43). Replaced on 10 November by D63 ([#95](https://github.com/NE-Build-Track/livepeer-builder-core/pull/95), [#96](https://github.com/NE-Build-Track/livepeer-builder-core/pull/96)): the engine reads livepeer/node's `/discovery` contract; [discovery.md](../contracts/discovery.md) |
| ISS-10 Network rates and pricing assumptions ([#23](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/23)) | 2 Nov | Rates as advertised, with source and age, never signed for without a ceiling: [#77](https://github.com/NE-Build-Track/livepeer-builder-core/pull/77) (D62); [plan](../exec-plans/completed/iss-10-rates.md); [payments.md](../contracts/payments.md#pricing-assumptions) |
| ISS-11 Scoped allocation provisioning and management ([#24](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/24)) | 30 Oct | The operation ledger and required references on admin commands: [#76](https://github.com/NE-Build-Track/livepeer-builder-core/pull/76) (D57); [plan](../exec-plans/completed/iss-11-14-idempotency.md); [payments.md](../contracts/payments.md#repeat-safe-commands) |
| ISS-12 SDK payment authorization and remote signing ([#25](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/25)) | 2 Nov | The engine-owned paid call ([#61](https://github.com/NE-Build-Track/livepeer-builder-core/pull/61), [#64](https://github.com/NE-Build-Track/livepeer-builder-core/pull/64); D52 to D54) and distinct payment failures ([#78](https://github.com/NE-Build-Track/livepeer-builder-core/pull/78), D58); [plan](../exec-plans/completed/iss-12-payment-failures.md) |
| ISS-13 First funded payment configuration ([#26](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/26)) | 2 Nov | `deploy/dev-stack`; console `docs/references/m3-environment.md` |
| ISS-14 Durable submission and immediate execution ([#27](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/27)) | 30 Oct | The final repeat rules for jobs, [#76](https://github.com/NE-Build-Track/livepeer-builder-core/pull/76) (D57), and streamed jobs, [#28](https://github.com/NE-Build-Track/livepeer-builder-core/pull/28); [engine.md](../contracts/engine.md#idempotency) |
| ISS-15 Owned job history and basic results ([#28](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/28)) | 2 Nov | Paged history and the M3 access model for inputs and results: [#81](https://github.com/NE-Build-Track/livepeer-builder-core/pull/81) (D59); [plan](../exec-plans/completed/iss-15-history-results.md); [rest-builder.md](../guides/rest-builder.md) |
| ISS-16 Capture usage; correlate jobs with payment evidence ([#29](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/29)) | 24 Oct | Payment evidence stored when the signer signs, before the paid request: [#44](https://github.com/NE-Build-Track/livepeer-builder-core/pull/44); [payments.md](../contracts/payments.md#attribution) |
| ISS-17 Replayable network-cost projections ([#30](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/30)) | 24 Oct | `CostService` projections; a paid job never reads as `none`: [#43](https://github.com/NE-Build-Track/livepeer-builder-core/pull/43) |
| ISS-18 Scoped usage, cost reports and integration events ([#31](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/31)) | 2 Nov | Cost per app and class on the summary: [#82](https://github.com/NE-Build-Track/livepeer-builder-core/pull/82) (D60); [plan](../exec-plans/completed/iss-18-cost-reports.md). The events feed was held and is ISS-46 in M4 |
| ISS-19 Representative jobs and acceptance matrix ([#32](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/32)) | 27 Oct | Console `docs/product-specs/m3-journey.md` |
| ISS-20 REST for the first journey ([#33](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/33)) | 2 Nov | `http/app.py` and [the generated OpenAPI document](../generated/openapi.json); console `docs/references/m3-rest-operations.md` |
| ISS-21 Reference app: access and live catalog ([#34](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/34)) | 27 Oct | The console |
| ISS-22 Reference app: first execution and reporting journey ([#35](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/35)) | 27 Oct | The console |
| ISS-23 Verification of access, repeated commands and accounting ([#36](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/36)) | 2 Nov | The traceability spec, its doc guard and the live probe: [#83](https://github.com/NE-Build-Track/livepeer-builder-core/pull/83) (D61); [m3-traceability.md](m3-traceability.md); [plan](../exec-plans/completed/iss-23-verification.md) |
| ISS-24 First runnable installation and journey guide ([#37](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/37)) | 2 Nov | Console `docs/guides/m3-journey.md` |
| ISS-25 Funded seven-outcome journey ([#38](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/38)) | 9 Nov | The final run of 9 November and its evidence, console `docs/references/evidence/2026-11-09-iss-25-funded-journey.md` |

The release is the merge of `releases/m3` into `releases/final`
([#32](https://github.com/NE-Build-Track/livepeer-builder-core/pull/32), 11 November 2026), tagged `v0.2.0` and
`m3-complete-builder-journey`. M4 work lands on `releases/m4`, which starts at that tag.

## Carried forward to M4

| Item | Source | Where it goes |
| --- | --- | --- |
| The versioned events feed, held through M3 | D60; [events.md](../contracts/events.md) | ISS-46 ([#51](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/51)); a delivery design is proposed in [#86](https://github.com/NE-Build-Track/livepeer-builder-core/issues/86) |
| Josh's code review of the engine | D6; the [streaming](../exec-plans/completed/streaming-jobs.md) and [ISS-10](../exec-plans/completed/iss-10-rates.md) plans | Not assigned to a ticket |
| The enterprise apps send operation references and branch on a payment failure's `reason` | D57, D58; the [ISS-11/14](../exec-plans/completed/iss-11-14-idempotency.md) and [ISS-12](../exec-plans/completed/iss-12-payment-failures.md) plans | Each app's repository |
| The console, ChatLPT, the inference gateway and the enterprise UI follow the discovery change: `lpb discovery` is removed, and `Runner` has no `runner_id`, `rate` or `rate_error` | D63, [#96](https://github.com/NE-Build-Track/livepeer-builder-core/pull/96) | Each app's repository |
| Paid requests wait on sequential chain calls in the signer and the orchestrator. The engine records each step's time ([#90](https://github.com/NE-Build-Track/livepeer-builder-core/pull/90)) | [#89](https://github.com/NE-Build-Track/livepeer-builder-core/issues/89) | #89 |
| Metered payment renewals and refreshes follow redirects (accepted risk, 10 November) | T27 | Revisit on an SDK release that refuses them |
| Every other open shortcut and known gap | [tech-debt-tracker.md](../exec-plans/tech-debt-tracker.md) | Per row |

## Later tickets this repository already prepares for

| Ticket | Milestone | Prepared here |
| --- | --- | --- |
| ISS-26 Async jobs and durable recovery handles | M4 | **Defined:** `mode="async"`, `ResultRef.storage="stored"` → 501 |
| ISS-27 Persistent application endpoints | M4 | **Defined:** `SessionManager`, engine-proxied (D9) → 501 |
| ISS-28 Failure classification and recovery across modes | M4 | Failure taxonomy and recovery for immediate jobs (implemented) |
| ISS-29 REST across all execution modes | M4 | Reserved routes return 501 |
| ISS-30 The same journey through MCP | M4 | Not started; MCP auth defined in [access.md](../contracts/access.md#modes) |
| ISS-31 Both payment configurations | M4 | Mode A implemented; Mode B defined as a gap |
| ISS-32 Embedding, service consumption, enterprise extensions | M4 | Protocols and both integration modes |
| ISS-33 Replace the reference app's access flow | M4 | `Authenticator` / `AccessPolicy` protocols; JWT bearer implemented (D45, #49) |
| ISS-34–36 Reference-app coverage, isolation and replay, funded matrix | M4 | Store and cost tests are the foundation |
| ISS-37–39 Docs, release artifacts, independent installation | M5 | `docs/`, CI dev builds, `lpb smoke` |

## Upstream interfaces (H1–H6)

| Ref | Interface | Status (19 October) |
| --- | --- | --- |
| H1 | Python gateway SDK | Pinned (26 October) to livepeer/livepeer-python-gateway `main` `2017a80` (was `76a1a1c`): "Enforce TLS" ([#76](https://github.com/livepeer/livepeer-python-gateway/pull/76)), streamable runner bodies ([#80](https://github.com/livepeer/livepeer-python-gateway/pull/80)) and `on_payment_session` ([#81](https://github.com/livepeer/livepeer-python-gateway/pull/81), reporting `manifest_id` with no signer-state decoding). The runner-side session stop ([#83](https://github.com/livepeer/livepeer-python-gateway/pull/83)) is unused. The NE `dev/bytestreaming` pin is dropped. **Open:** a PyPI release, then re-pin (T1). #70 and multipart deferred |
| H2 | Batteries management, authorization and accounting | Delivered upstream (#5, #6, #7) at `ca09e47`. Mode B scoping deferred to M4 |
| H3 | Remote signer and orchestrator/Live Runner | livepeer/node `f45d83f` (main, 10 commits after v1.0.6; was `406b248` / v1.0.2) in the dev stack. It includes `f970be6`, which accepts a ticket recipient that differs from the node address (T10, resolved upstream). Orchestrators are to serve CA-signed certificates (D27, T12) |
| H4 | Funded acceptance environment | The dev stack plus the `cloud-spe-gateway` payer is funded; paid jobs on public orchestrators reconciled on 14 October ([evidence](../references/evidence/2026-10-14-dev-stack-public-orchestrators.md)) |
| H5 | Source access and permitted reuse | Done (ISS-01): simple-infra reviewed; nothing private reused |
| H6 | Scope and final acceptance | Mike's review per ticket; procedure open for M5 |
