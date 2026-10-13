# Build Track baseline (accepted architecture)

**Source:** Cloud-SPE/Network-Engineering-SPE, `docs/design-docs/self-sovereign-open-builder-stack-draft.md`
and `open-builder-architecture-and-sequences.md`, both accepted effective 30 September 2026. This is a
digest for agents. When it disagrees with a later decision, [m2-decisions.md](../design-docs/m2-decisions.md) wins.

## The deliverable

A reusable, **noncommercial builder engine** with installable packages and runnable REST and MCP
services. Enterprises import and extend its public interfaces, or call it as a deployed service,
**without modifying core source**. REST and MCP share core behavior. A reference application
demonstrates embedding, calling the service, enterprise authentication, custom endpoints and tools,
allowance administration, and mock commerce.

**Self-sovereign** means independent installation, operation, data control, credential administration,
upgrades and recovery. Operators can run Batteries and the signer themselves (funding the signer
wallet), or delegate payment operations to a hosted operator. Neither mode requires PymtHouse, a
proprietary identity provider, or commerce.

## The commercial boundary

> **The engine reports wholesale network costs; each enterprise decides what to charge its customers.**

| Layer | Owns |
| --- | --- |
| Enterprise applications | Product experience, customer accounts, specialized tools, retail pricing and billing |
| Builder engine (this repository) | Access controls, discovery, job management, results, usage and network-cost reporting; REST and MCP |
| Network and payment components | SDK, remote signer, Batteries (authorization and accounting), orchestrators and Live Runners |

### Core versus enterprise responsibility

| Capability | Core responsibility | Enterprise or example responsibility |
| --- | --- | --- |
| Access | Validated actor context, ownership checks, credential lifecycle, service trust | Customer identities, Google login/SSO, entitlement policy |
| Discovery and prices | Supported capabilities, input descriptions, network rates, units, freshness | Catalog presentation, retail prices, markup |
| Execution | SDK integration, jobs and attempts, status, result references, understandable failures | Application workflows, additional tools |
| MCP and REST | Two interfaces to shared core services and authorization rules | Extra endpoints and tools; enterprise authentication, without forking |
| Usage and cost | Execution measurements; correlated network-cost projections with uncertainty | Retail metering, subscriptions, invoices, refunds, margin |
| Payments | Adapter to authorized provisioning, reporting and signing | Choose a provider or self-operate; fund the signer separately from customer billing |
| Persistence | Engine-owned records behind a supported interface | Customer and commerce stores, analytics, external-ID mappings |

No CRM, retail wallet, subscription engine or asset-management product enters core. Enterprise code
must not rewrite engine tables or Batteries' ledger. No plugin marketplace or runtime code loading.

## Integration modes

- **Variant A, import and extend:** the enterprise backend and MCP host import the released packages
  in-process and add routes, tools and authentication.
- **Variant B, call the deployed service:** the enterprise backend calls the REST/MCP service over
  authenticated HTTP.

Both run the same core release. They are alternatives, not duplicate backends. Exposing MCP needs the
selected authentication boundary; there is no anonymous public access. A frontend reaches the engine
through a backend or trusted access layer, and provider and admin credentials stay server-side.

## Payment operation modes

| Mode | Who operates | The engine holds |
| --- | --- | --- |
| **A: self-operated** | The operator runs Batteries, the signer and a funded wallet | A management credential for its own Batteries |
| **B: hosted payment operator** | A third party runs Batteries, the signer and a provider-funded wallet for many builders | Scoped credentials and reports; **the contract is still to be established** (deferred to M4, [payments.md#mode-b](../contracts/payments.md#mode-b)) |

The payment operator owns the funds, wallet operations, ledger and event ingestion in both modes.
Provisioning an allowance is distinct from funding the wallet. Switching providers is not
automatically lossless.

## Execution scope

- **Required:** immediate results, jobs that complete asynchronously, and requests to persistent
  application endpoints.
- **Stretch:** incremental text streaming and continuous live audio/video.
- **Acceptance** verifies the supported modes, failure handling and recovery, **without unintended
  duplicate jobs or charges**.

## Records and accounting authority

| Record | Authority | Meaning |
| --- | --- | --- |
| Job inputs/outputs, status, measured work | Engine / SDK execution path | What was attempted and observed |
| Allocations; ticket expected-value accounting | Batteries / payment provider | Network payment permission and observed ticket cost |
| Winning-ticket redemption | Payment operator settlement evidence | On-chain settlement; not a per-job receipt |
| Customer bill and commercial balance | Enterprise | Retail price and policy |

- **Engine reporting is a projection, not a second ledger.** Missing or delayed evidence is
  pending/unknown, never zero.
- **Allocations do not fund signer escrow.** Strict spend reservations and hard ceilings are not
  established.
- **Revoking access** does not reverse issued tickets or remove late fees.

## Required versus stretch

- **Required (accepted baseline):**
  - a new backend repository with installable packages and runnable services;
  - imported and HTTP integration;
  - a noncommercial core with mocked example commerce;
  - the pinned execution baseline;
  - **SQLite persistence** with migrations and backup/restore;
  - interim ownership and release preparation.
- **Stretch:** PostgreSQL (implemented here as an optional adapter, decision D5), additional
  streaming, and guaranteed hard spending limits.
- **Out of scope:** a full Console port (Console itself is out of scope, decision D3), Stripe/real
  commerce, HA, SLAs, and financial recourse.

## Service assurance

The 2 September stakeholder feedback asked what builders can know about performance, and what happens
when a long job fails after incurring cost. Failure records and cost classification answer part of
it. **No SLA, automatic refund or recovery guarantee** follows from the design. See
[engine.md#deployment-performance-and-recovery-assumptions](../contracts/engine.md#deployment-performance-and-recovery-assumptions).

## Acceptance evidence the baseline expects (by M5)

- A pinned seven-outcome journey ([builder-outcomes.md](builder-outcomes.md)) with no mandatory
  PymtHouse or commerce service.
- The pinned execution baseline through the Python SDK: success, failure, queued recovery and restart.
- The same core release in standalone and imported enterprise modes: shared REST/MCP behavior,
  ownership isolation, and enterprise features added without modifying core source.
- Mock commerce disconnected from standalone operation; retry-safe commands; late and unmatched costs;
  duplicate events; replay without double application.
- **SQLite persistence, migrations and backup/restore.** PostgreSQL contract checks if it's included,
  without implying HA.
- Self-operated and hosted payment configurations, documented, with upstream API gaps and provider
  obligations identified rather than invented.
- Package and container builds, install/start smoke checks, automated tests, lint/type checks,
  versioned release artifacts, and release documentation.

## Ownership and governance

| Topic | Arrangement |
| --- | --- |
| Engine repository | Mike Zupper is the delivery lead and interim owner. The intended transfer to the Livepeer GitHub organization needs agreement |
| Upstream components | The existing maintainers own the SDK, node and Batteries. Josh coordinates network, runtime and payment-core dependencies |
| Review | Nothing closes without Mike's review. Josh reviews engine code (D6) |
| Releases | Controlled credentials and explicit release triggers. This baseline authorizes no publishing |

## Decisions the baseline left open, and where they stand

| Open item (baseline §Decision boundaries) | Status |
| --- | --- |
| Exact access/OAuth profile | **Decided:** D12–D16 ([access.md](../contracts/access.md)) |
| Payment credential/allocation granularity | **Decided:** D17 (`per_actor` default) |
| Provider provisioning/reporting contracts | **Decided:** D18–D22 ([payments.md](../contracts/payments.md)) |
| Example feature selection; reference app placement | Open (M3, ISS-21/22) |
| Execution-process packaging | **Decided for M2:** one distribution with extras; in-process SDK adapter |
| Representative acceptance capabilities | Open (ISS-19) |
| Supported deployment guarantees; service-assurance and recourse boundaries | **Decided for M2:** no HA, no exactly-once, no SLA or recourse ([engine.md](../contracts/engine.md#deployment-performance-and-recovery-assumptions)) |
| Ongoing owners | Open (repository transfer to the Livepeer organization) |
| Funded acceptance environment | Partly evidenced: [dev-stack paid run](../references/evidence/2026-10-12-dev-stack-paid-run.md). Blocked by T9 and T10 |
| Final-release acceptance procedure | Open (M5, ISS-39) |
