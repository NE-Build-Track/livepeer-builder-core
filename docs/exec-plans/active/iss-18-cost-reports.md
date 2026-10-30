# ISS-18: Scoped usage, cost reports and integration events (M3 part)

## Status

- **Owner:** John (engine), per the guide. **Reviewers:** Mike, John. Test-first, one pull request.
- **Ticket:** ISS-18 ([Network-Engineering-SPE#31](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/31)) (M3).
  Stays open as **partial**: the versioned events feed is held.
- **Branch:** `tasks/mz/iss-18-cost-reports`, on `tasks/mz/iss-12-payment-failures` at `56ccb27` (PR #78,
  D58, not merged yet). ISS-15 (D59) is built alongside and will be stacked under this branch.
- **Design:** the cost-reports guide, [iss-18-cost-reports.md](../../design-docs/m3-guides/iss-18-cost-reports.md).
  Q1–Q2 locked by Mike on 30 October.
- **Progress:** engine built and checked; awaiting review. The tracker comment (the criteria table, the
  feed held) is drafted, not posted.
- **Decision:** D60.

## Goal

A builder can read what each runner app cost in a window, from the same records and in the same scope as
the class totals, and can rely on cost reporting never changing a job's state. Everywhere the events feed
shows, it says the feed is held, with the decision.

## Acceptance

From the ticket, the 21 October M3 plan and the guide's criteria check:

- [x] Job-level usage and observed network cost: unchanged (`GET /v1/jobs/{id}/cost`,
  `POST /v1/jobs/{id}/usage`); the refused-report test pins that a refusal changes nothing.
- [x] Aggregate report with units, attribution and uncertainty: `CostSummary.by_app`; the
  [Attribution](../../contracts/payments.md#attribution) section gives the path from a cost to its job,
  attempts, allocation and orchestrator, and the unit of every field.
- [x] Reporting failures preserve execution state: tested end to end with Batteries down.
- [x] The feed is held everywhere it shows: `services/reserved.py` (501 naming the decision),
  `events.md`, `builder-outcomes.md`, `milestones.md`, and the one-line statuses that said M3.
- [x] `references/batteries-api.md`: the usage feed's filters at `ca09e47` (`grant_id`, `allocation_id`,
  `manifest_id`), and that the engine sends none.
- [x] OpenAPI regenerated; every test in the guide's list is `[x]` or `[=]` with a reason; the
  `AGENTS.md` checks are green.
- [ ] The tracker carries the criteria table and stays open as partial (drafted; Mike posts it).
- [ ] Versioned feed with replay and de-duplication: **held** (the 21 October decision, item 4; D60).

### Test list

`tests/unit/test_cost_reports.py` unless named.

- [=] Batteries down: a job still ends succeeded, its cost reads pending, health shows the sync error,
  the job record is unchanged (the starter; met by design, passed at once)
- [=] a refused usage report (wrong owner, failed job, second report) leaves the job's state and usage
  unchanged (passed at once)
- [=] summary `by_class` totals across delivered, failed_after_payment, uncertain and pending, with a
  failover job (passed at once)
- [=] summary scope: an actor sees its own; an admin sees the application's, or one actor with
  `actor_id`; an actor cannot widen it (passed at once)
- [x] summary `by_app` adds up, class by class, to `by_class` for the same window and scope (per class since Mike's review)
- [=] pending jobs are counted in `jobs_pending` and never in a class total (exists in `test_costs.py`:
  kept)
- [x] `GET /v1/events` answers 501 naming the hold
- [x] `FakeEngine` summary matches (`by_app`) (`test_fake_engine_contract.py`, both sides)
- [+] REST returns `by_app` and the OpenAPI schema declares it (passed at once: the contract field)
- [+] the store's window read returns each projection with its job's app (`tests/store/test_contract.py`)
- [+] a scripted `FakeEngine` summary still wins over the jobs it ran (`test_fake_engine.py`)

## Progress

- 2026-10-30: Baseline at `56ccb27`: 1578 passed, 49 skipped. Built test-first in one session. The four
  criteria the guide marks met passed as soon as they were written; they stay as documentation tests.
  `by_app` was red on the missing field, the `FakeEngine` side on its unscripted `NotFound`, and the
  events test on `milestone == "M3"`.
- 2026-10-30: `CostSnapshot.projections_and_unprojected` now returns each stored projection with its
  job's runner app (`select` adds `lpb_jobs.app` to the join it already made); the unprojected jobs
  carry `app` already. The service groups in Python: the fees are exact decimal strings, and the
  jobs no projection covers are computed live, so a SQL `GROUP BY` could not total them. No migration.
- 2026-10-30: The summary arithmetic moved into `services.costs.summarize`, shared by `CostService` and
  `FakeEngine`, so the fake cannot drift from the engine.
- 2026-10-30: Checks: 1588 passed, 49 skipped (PostgreSQL store tests skipped locally; the changed store
  read is in the shared store contract suite). Layer coverage floors pass (total 97.79%); diff coverage
  against `origin/tasks/mz/iss-12-payment-failures` 100%. Ruff, format, mypy (strict), `check_docs.py`,
  `lpb gen-docs --check` and `lpb smoke` pass. One run under coverage tripped the 40 ms bound in
  `test_stream_lifecycle.py::test_aclose_never_waits_for_the_runner[1.0]` (timing, under a parallel
  load; passes alone and on the rerun; not on the cost path), as in the ISS-12 plan.

## Decision log

- 2026-10-30: Q1 `by_app` only; Q2 the feed stays held for M3 (Mike). Recorded as D60.
- 2026-10-30: ~~`by_app` folds every class, `pending` included~~ (superseded the same day by Mike's
  review below: kept by class). An app with only unpaid jobs does not appear, as such jobs have no cost.
- 2026-10-30: The held feed answers `milestone: "held"`, `ticket: "ISS-18"`: it has no milestone yet.
  `NotImplementedInMilestone` gained a keyword `held_by` for the message; existing callers are unchanged.
- 2026-10-30: An unscripted `FakeEngine` summary is computed from the jobs it ran, like its unscripted
  `for_job`; before, it raised `NotFound`. A builder's test that expected that error must script one.
- 2026-10-30: Mike's review. (1) The `CostSnapshot` return-type change is kept and stated in D60 (5):
  a custom store must return `(app, JobCost)` pairs; no consumer supplies one. (2) The computed
  `FakeEngine` summary is kept; no consumer test relied on `NotFound`. (3) `by_app` is totalled in the
  service, not SQL, for exact decimals and the live-computed jobs. (4) **`by_app` is kept by class**,
  `{app: {class: Fees}}`: the guide's flat total folded `pending`, which can still grow, into a figure
  that read as final. The field is new and unused, so its shape is chosen now; the check is per class.
