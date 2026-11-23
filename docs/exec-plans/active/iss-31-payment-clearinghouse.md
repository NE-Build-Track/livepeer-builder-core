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
- **Progress:** P1 to P5 are merged into `releases/m4`. P6 is built and in review. P7 and P8 follow in
  order.

## Goal

builder-core lets an enterprise app buy network work in two ways. It runs Batteries, the signer and the
wallet itself (self-hosted payments), or it uses a payment clearinghouse that another party runs. A
clearinghouse is an engine in front of Batteries, with a signer beside it that enterprise engines reach
directly. It does payments only and serves many tenants. The enterprise app keeps its own engine, which
runs its jobs, sends them straight to orchestrators and signs their payments at the clearinghouse's
signer.

## Acceptance

From ISS-31 as amended on 18 November 2026. The first item follows Mike's amendment of 19 November: the
enterprise's engine signs at the signer itself.

- [ ] A payment clearinghouse serves at least two tenants, one for each enterprise app. Each enterprise
  app runs its jobs in its own engine, as an embedded engine or an engine server. That engine reaches
  the clearinghouse for allotment, allowance and usage, and signs at the clearinghouse's signer.
- [ ] One tenant's admin cannot read or change another tenant. The attempt is refused.
- [ ] Self-hosted payments still work, as one deployment with one tenant.
- [ ] The clearinghouse admin onboards a tenant and tops up its grant by hand, on a private listener.
- [ ] The arrangement and its limits are documented, and nothing implies an ongoing public service.

Funded evidence for both configurations is ISS-36, not this plan.

## Progress

- [x] **18 November 2026.** The design is locked (D82 to D86), and the guides and decision log are merged
  ([#173](https://github.com/NE-Build-Track/livepeer-builder-core/pull/173)).
- [x] **18 November 2026.** The open points are settled (D87).
- [x] **P1 (#162).** `per_actor` is the only allocation mode. Merged into `releases/m4` on 20 November
  2026: [#174](https://github.com/NE-Build-Track/livepeer-builder-core/pull/174).
- [x] **P2 (#163).** Rename routes, classes and settings to the domain language. Merged into
  `releases/m4` on 20 November 2026:
  [#177](https://github.com/NE-Build-Track/livepeer-builder-core/pull/177).
- [x] **P3 (#164).** Separate the work routes from the clearinghouse routes. Merged into `releases/m4`
  on 20 November 2026: [#178](https://github.com/NE-Build-Track/livepeer-builder-core/pull/178).
- [x] **P4 (#165).** Tenants at the clearinghouse. Merged into `releases/m4` on 20 November 2026:
  [#179](https://github.com/NE-Build-Track/livepeer-builder-core/pull/179).
- [x] **P5 (#166).** What an enterprise's engine needs from the clearinghouse: each actor's payment key,
  handed over once, the tenant's usage and the runner list. Merged into `releases/m4` on 23 November
  2026: [#180](https://github.com/NE-Build-Track/livepeer-builder-core/pull/180), with
  [#186](https://github.com/NE-Build-Track/livepeer-builder-core/pull/186).
- [ ] **P6 (#167).** A public and a private listener. Built and in review:
  [#181](https://github.com/NE-Build-Track/livepeer-builder-core/pull/181).
- [ ] **P7 (#168).** Remote adapters for the five payment ports, and `lpb payments check`. Built and in
  review: [#182](https://github.com/NE-Build-Track/livepeer-builder-core/pull/182).
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
  The clearinghouse accepts keys only in the first version. Source: Mike. Point 1 was amended on 19
  November (below).
- **18 November 2026, D86.** The console runs twice at a clearinghouse, top-up is manual, and the work is
  inside M4. Source: Mike.
- **18 November 2026, D87.** The open points are settled: a payment key for each actor, the
  clearinghouse's runner list by default, the eight route paths, the Compose projects in `deploy/`,
  `lpb payments check` kept, the realm's role mapping, and how a clearinghouse failure is classified.
  Source: Mike. Points 1 and 3 were amended on 19 and 23 November (below).
- **18 November 2026.** The sample enterprise apps and the console break when they next move their pin.
  That is accepted, and they are fixed after this work lands (#116). Source: Mike.
- **18 November 2026, P4.** A record written before a tenant's id was its grant's id is not re-keyed:
  #165 rewrites no row. An existing allocation stays on the grant its record names, and nothing new is
  allotted to a tenant whose id is not a grant's id. Whether to re-key existing deployments is open.
  Source: Mike (not to rewrite rows in #165); the rest follows from it.
- **19 November 2026, D87 point 3 amended.** The payments path is the signer's two paths at the root of a
  clearinghouse's public listener, `POST /sign-orchestrator-info` and `POST /generate-live-payment`, each
  called with a payment key. The gateway SDK drops any path in a signer address, so
  `POST /v1/clearinghouse/payments` could not be reached by it unmodified. Source: Mike. Replaced the
  same day (next entry).
- **19 November 2026, D85 point 1 and D87 points 1 and 3 amended.** After review of #180, the
  clearinghouse has no signer proxy and issues no second key. Allotment and rotation at the clearinghouse
  hand the allocation's own Batteries key to the enterprise's engine once, the clearinghouse keeps no
  allocation key, and the enterprise's engine signs at the published signer itself. The signer is
  reachable by enterprise engines and keeps its Batteries authorization webhook configured, which is now
  the only check at signing. There is no payments route. This replaces the same day's earlier amendment
  of D87 point 3. Source: Mike.
- **19 November 2026, P6.** Two listeners are a deployment's choice until the role setting (#169): with
  `LPB_HTTP_PRIVATE_PORT` unset one listener serves everything, so self-hosted payments is unchanged.
  The private listener takes a clearinghouse admin credential only, as the guide's routes say ("for a
  `clearinghouse_admin` only"): a tenant's admin is refused there. The wallet report converts nothing
  and computes no margin. "Unspent" is each grant's total less what its allocations spent, summed for
  each currency over every grant; the deposit and reserve are Batteries' own on-chain listener's, which
  a deployment must turn on; "ready" is the signer's identity call. Source: D82, D84 and the guide; where
  they are silent the stricter or the plainer reading was built, for Mike's review.
- **19 November 2026, P7, first part.** Everything of #168 that does not depend on the payments route
  is built, and signing is left as one seam. Where the guide is silent: the port names an allocation
  and the routes an actor, so an allocation manager may be asked for one actor (`AllocationsByActor`),
  with no member of the port changed; the engine's idempotency key is the route's `operation_ref`; a
  payment key whose answer was lost is put right by a rotation; the tenant's id is a third setting,
  `LPB_CLEARINGHOUSE_TENANT_ID`, until `GET /v1/me` exists; the allowance is read through
  `GET /v1/allowance`, since the allocation route answers no amounts (T36); and `lpb payments check`
  asks the signer `GET /sign-orchestrator-info`, whose 405 says the route is there. Source: D84, D87 and
  the guide's "The remote adapters: each port and its route"; the rest for Mike's review.
- **19 November 2026, P7, second part.** Mike decided the two points the first part left. (1) D87
  point 3 is amended: a clearinghouse serves the signer's two paths at its root, so an enterprise's
  engine signs with the unmodified SDK's transport pointed at the clearinghouse, with the actor's payment
  key, and `LPB_CLEARINGHOUSE_URL` is a scheme, a host and a port. (2) The allocation route answers the
  allocation's amounts, and the remote adapters read the allowance there (T36, closed). Built where
  those are silent: a trailing slash on the address is taken off; the route's `allowance` is `null`
  when the provider cannot be read, and the record is still answered; only the clearinghouse's own 503
  with `signer_not_reached` is read as "nothing was sent", and a self-hosted engine reads no code (T39,
  closed); an engine that uses a clearinghouse serves neither the signer's two paths nor a wallet
  report; an allocation an actor no longer holds still cannot be read through a clearinghouse (T40).
  Source: Mike (the two decisions, as dated notes on #168); the rest for his review.
- **23 November 2026, D87 point 1.** A resumed allotment or rotation hands the key over whenever
  Batteries returns it again: Batteries replays the original success, the secret included, for a
  matching idempotency key, and never expires it. A repeat of a finished command answers without the key
  (`replayed`), and a lost key is then replaced by a rotation. Source: Mike, after review of #186.
- **23 November 2026, P5.** An allotment or a payment key rotation left unfinished is resumed only by
  the kind of command that began it, the one that stores the key or the one that hands it over. A resume
  by the other kind is refused before any provider call (409 `operation_conflict`), and the record stays
  resumable by the first. Source: Mike, after review of #180.
- **23 November 2026, P5.** The unreleased migration that created `lpb_payment_keys` is deleted, not
  dropped by a later migration. A database that ran this stack before is recreated. Source: Mike.
- **23 November 2026, P5.** [#186](https://github.com/NE-Build-Track/livepeer-builder-core/pull/186),
  John's code change for the amendment of 19 November, is merged into P5's branch. The hand-written
  documents and the comments in code follow it in one sweep.
- **23 November 2026, P6.** Neither listener carries a signer path: a clearinghouse serves none (#167,
  as Mike amended it on 19 November 2026). Every listener's app keeps each allocation's key with the
  engine (`deliver_allocation_keys=False`), on one listener or two: there is no role setting yet, and
  the engine behind a listener runs jobs. Handing keys to the caller is the `clearinghouse` role's
  (#169). Source: the amended ticket; the `False` on both listeners is for Mike's review.
