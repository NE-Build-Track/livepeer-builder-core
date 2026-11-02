# ISS-18: Usage and cost reports (events feed held)

**Status:** proposal for discussion, 29 October 2026.\
**Tracker:** ISS-18 ([Network-Engineering-SPE#31](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/31)).
Closes as **partial**: the versioned events feed is held by the M3 decision of 21 October (item 4).\
**Owner:** John (engine). **Reviewers:** Mike, John.\
**Verified against:** builder-core `releases/m3` at `d3df806`.

## Done when (from the 21 October M3 plan)

Job-level and aggregate cost reports are checked against the criteria. The events feed is held, so the
task stays open as partial until the hold is lifted.

## Criteria check (verified)

| Criterion | Status | Evidence |
| --- | --- | --- |
| Job-level usage and observed network cost | **Met** | `GET /v1/jobs/{id}/cost` (store only, never Batteries); `POST /v1/jobs/{id}/usage` (`app_reported`, once, owner only, succeeded jobs); `test_jobs.py`, `test_costs.py` |
| Aggregate report with units, attribution and uncertainty | **Partial** | `GET /v1/costs/summary` gives `by_class` (`Fees`: `fee_eth`, `fee_usd`, `row_count`), `jobs_with_cost`, `jobs_pending`. Uncertainty: yes (classes; pending never folded in). Units: yes (ETH and USD fields). **Attribution: only by actor scope**; no breakdown by app or orchestrator; no usage totals; `unattributed` is admin-only (reconciliation). **Since D60:** `by_app`, per app and class |
| Stable IDs | **Met** | Job UUID, attempt `(job_id, number)`, `operation_ref`, `manifest_id`, Batteries `payment_session_id`, `UsageRow.id` |
| Versioned feed with replay and de-duplication | **Held** | `events.md` defines it; `GET /v1/events` answers 501 |
| Reporting failures preserve execution state | **Met by design, partly tested** | Job completion never calls cost code; cost sync runs in its own loop and reports errors in health (`test_runtime.py`); no test with Batteries down end to end |
| Network cost, settlement and retail distinguishable | **Met** | `payments.md` (cost = expected value at signing; settlement is operator-level), core beliefs, the M3 decision (item 6). The ISS-10 guide adds the "Wholesale and retail" section |

**Stale text.** `events.md`, `product-specs/builder-outcomes.md` and `milestones.md` still say the feed
lands in M3, and `services/reserved.py` raises `NotImplementedInMilestone(..., "M3", ...)`.
`references/batteries-api.md` says Batteries has no grant filter on usage; it has one at `ca09e47`.

## Decision proposed

**Close the reporting criteria that M3 can close, record the hold everywhere it shows, and add the
smallest attribution the aggregate report lacks.**

### Code

- **`CostSummary.by_app`** (proposed as `dict[str, Fees]`; built as `dict[str, dict[CostClass, Fees]]`,
  D60, so a final fee and a `pending` one are never one figure): fees per runner app in the same window and scope. Built
  from the job records the summary already reads; no new store query shape beyond a group-by. This is
  the attribution a builder needs ("what did each app cost me") and the console's Usage page already
  shows it from Batteries.
- `by_orchestrator` and usage totals are **not** added in M3 (Q1): orchestrator spend is an operator
  view (reconciliation), and usage units differ per capability, so a total has no single unit.
- `services/reserved.py`: the events placeholder says the feed is held (decision 2026-10-21, item 4),
  not "M3".

### Docs

- `payments.md`: an "Attribution" paragraph: how a cost reaches a job (`JobCost` → job → attempts →
  `allocation_id`, `orchestrator_url`), the units on every field, and what `by_app` adds.
- `events.md`, `builder-outcomes.md`, `milestones.md`: mark the feed held, with the decision link.
- `references/batteries-api.md`: fix the grant-filter note.
- ISS-18 tracker comment: the criteria table above, with the feed held.

## Test plan (test-driven)

```text
[ ] Batteries down: a job still ends succeeded, its cost reads pending, health shows the sync error, the job record is unchanged
[ ] a refused usage report (wrong owner, failed job, second report) leaves the job's state and usage unchanged
[ ] summary by_class totals across delivered, failed_after_payment, uncertain and pending, with a failover job
[ ] summary scope: an actor sees its own; an admin sees the application's, or one actor with actor_id
[ ] summary by_app adds up, class by class, to by_class for the same window and scope
[ ] pending jobs are counted in jobs_pending and never in a class total (exists: keep)
[ ] GET /v1/events answers 501 naming the hold
[ ] FakeEngine summary matches (by_app)
```

**Starter test:** Batteries down during sync leaves a succeeded job succeeded with its cost pending.

**Definition of done.** The list is empty; the checks in `AGENTS.md` pass; docs and OpenAPI updated; the
tracker carries the criteria table and stays open as partial for the feed.

## Upstream dependencies

None. Reports come from the engine's own store, filled from Batteries' usage feed, which already has
what is needed (including grant and allocation filters at `ca09e47`). The feed, if the hold lifts, is
engine-side too.

## Open questions to lock

**Locked by Mike, 30 October 2026:** Q1 `by_app` only (no `by_orchestrator`, no usage totals in M3); Q2
the events feed stays held for M3 (the 21 October decision, item 4), and ISS-18 stays open as partial.
The rules become [D60](../m3-decisions.md).

- **Q1.** Add `by_app` only (recommended), also `by_orchestrator`, or neither in M3?
- **Q2.** Leave the events feed held for M3 (the decision), or lift the hold if the other M3 work lands
  early?
