# ISS-31: the payment clearinghouse

## Status

- **Owners:** Mike decides and reviews. The engine work is built ticket by ticket, one pull request
  each.
- **Ticket:** ISS-31
  ([Network-Engineering-SPE#45](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/45)), amended
  on 18 November 2026. Its work is eight builder-core issues, #162 to #169 (P1 to P8).
- **Guide:** [iss-31-payment-configurations.md](../../design-docs/m4-guides/iss-31-payment-configurations.md).
- **Decisions:** D82 to D87 in [m4-decisions.md](../../design-docs/m4-decisions.md).
- **Branches:** a stack. P1 targets `releases/m4`, and each later ticket targets the branch before it.
  Merges are merge commits, so the stack needs no rebase.
- **Progress:** P1, P2 and P3 are built and in review. P4 to P8 follow in order.

## Goal

builder-core lets an enterprise app buy network work in two ways. It runs Batteries, the signer and the
wallet itself (self-hosted payments), or it uses a payment clearinghouse that another party runs. A
clearinghouse is an engine in front of Batteries and the signer. It does payments only, serves many
tenants, and is the only thing published. The enterprise app keeps its own engine, which runs its jobs
and sends them straight to orchestrators.

## Acceptance

From ISS-31 as amended on 18 November 2026.

- [ ] A payment clearinghouse serves at least two tenants, one for each enterprise app. Each enterprise
  app runs its jobs in its own engine, as an embedded engine or an engine server, and reaches the
  clearinghouse only for signing, allotment, allowance and usage.
- [ ] One tenant's admin cannot read or change another tenant. The attempt is refused.
- [ ] Self-hosted payments still work, as one deployment with one tenant.
- [ ] The clearinghouse admin onboards a tenant and tops up its grant by hand, on a private listener.
- [ ] The arrangement and its limits are documented, and nothing implies an ongoing public service.

Funded evidence for both configurations is ISS-36, not this plan.

## Progress

- [x] **18 November 2026.** The design is locked (D82 to D86), and the guides and decision log are merged
  ([#173](https://github.com/NE-Build-Track/livepeer-builder-core/pull/173)).
- [x] **18 November 2026.** The open points are settled (D87).
- [ ] **P1 (#162).** `per_actor` is the only allocation mode. Built and in review:
  [#174](https://github.com/NE-Build-Track/livepeer-builder-core/pull/174).
- [ ] **P2 (#163).** Rename routes, classes and settings to the domain language. Built and in review:
  [#177](https://github.com/NE-Build-Track/livepeer-builder-core/pull/177).
- [ ] **P3 (#164).** Separate the work routes from the clearinghouse routes.
- [ ] **P4 (#165).** Tenants at the clearinghouse.
- [ ] **P5 (#166).** The routes an enterprise's engine calls: payments, usage and discovery.
- [ ] **P6 (#167).** A public and a private listener.
- [ ] **P7 (#168).** Remote adapters for the five payment ports, and `lpb payments check`.
- [ ] **P8 (#169).** The engine's role setting, and the two Compose projects that prove it.

Each pull request ticks its own line here in the same change.

## Decision log

- **18 November 2026, D82.** The second payment configuration is a payment clearinghouse. It replaces
  D74, which is not built. `per_actor` is the only allocation mode. Source: Mike.
- **18 November 2026, D83.** A tenant and its grant share one id. The API says tenant, with the grant
  nested under it. Names follow the domain language. Source: Mike.
- **18 November 2026, D84.** The engine splits at its five payment ports into a work half and a
  clearinghouse half, and stays one program with a role setting. The clearinghouse has a public and a
  private listener. Source: Mike.
- **18 November 2026, D85.** Batteries and the signer are reachable only by the clearinghouse's engine.
  The clearinghouse accepts keys only in the first version. Source: Mike.
- **18 November 2026, D86.** The console runs twice at a clearinghouse, top-up is manual, and the work is
  inside M4. Source: Mike.
- **18 November 2026, D87.** The open points are settled: a payment key for each actor, the
  clearinghouse's runner list by default, the eight route paths, the Compose projects in `deploy/`,
  `lpb payments check` kept, the realm's role mapping, and how a clearinghouse failure is classified.
  Source: Mike.
- **18 November 2026.** The sample enterprise apps and the console break when they next move their pin.
  That is accepted, and they are fixed after this work lands (#116). Source: Mike.
