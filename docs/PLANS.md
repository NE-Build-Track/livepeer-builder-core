# Plans

Plans are versioned artifacts. Small changes use a short note in the PR. Anything that spans
sessions, people or milestones gets an **execution plan** in `exec-plans/active/`. An agent can
resume it from the repository alone.

## Exec plan shape

`scripts/check_docs.py` enforces these sections, in order:

1. `## Status`: owners, ticket, current state
2. `## Goal`: one paragraph
3. `## Acceptance`: checklist, taken from the ticket's acceptance criteria
4. `## Progress`: dated checklist; update it in the same change as the work
5. `## Decision log`: dated decisions with their source

When every acceptance item is evidenced and Mike has reviewed it, move the plan to
`exec-plans/completed/`. Shortcuts and known gaps go in
[exec-plans/tech-debt-tracker.md](exec-plans/tech-debt-tracker.md).

## M2: Foundation and integration contracts (closed 19 October 2026)

| Ticket | Plan | Owners |
| --- | --- | --- |
| ISS-01 Baseline | [completed/iss-01-baseline.md](exec-plans/completed/iss-01-baseline.md) | John, Mike |
| ISS-02 Engine contracts | [completed/iss-02-engine-contracts.md](exec-plans/completed/iss-02-engine-contracts.md) | John, Mike |
| ISS-03 Access | [completed/iss-03-access.md](exec-plans/completed/iss-03-access.md) | John, Mike |
| ISS-04 Payments | [completed/iss-04-payments.md](exec-plans/completed/iss-04-payments.md) | John, Mike |
| ISS-05 Core packages | [completed/iss-05-core-packages.md](exec-plans/completed/iss-05-core-packages.md) | John, Mike |
| ISS-06 SQLite records | [completed/iss-06-records.md](exec-plans/completed/iss-06-records.md) | John, Mike |
| ISS-07 Builds and CI | [completed/iss-07-ci.md](exec-plans/completed/iss-07-ci.md) | John, Mike |
| Evidence re-run | [completed/m2-attribution-rerun.md](exec-plans/completed/m2-attribution-rerun.md) | John |

## M3: Complete builder journey (closed 9 November 2026)

All 18 M3 tickets (ISS-08 to ISS-25) and the milestone tracker are closed. The tickets with no plan here
were delivered in the reference app or without an exec plan:
[milestones.md](product-specs/milestones.md#m3-tickets-and-where-each-is-met) lists every ticket and where
it is met.

| Ticket | Plan | Owners |
| --- | --- | --- |
| Streaming jobs (ISS-14, ISS-20; [#28](https://github.com/NE-Build-Track/livepeer-builder-core/pull/28)) | [completed/streaming-jobs.md](exec-plans/completed/streaming-jobs.md) | Mike |
| Dependency-injection refactor (internal, before ISS-32) | [completed/dependency-injection-refactor.md](exec-plans/completed/dependency-injection-refactor.md) | Mike |
| Discovery service, [#45](https://github.com/NE-Build-Track/livepeer-builder-core/issues/45) (ISS-09) | [completed/discovery-service.md](exec-plans/completed/discovery-service.md) | Mike |
| ISS-08 Standalone credentials and ownership controls | [completed/iss-08-standalone-credentials.md](exec-plans/completed/iss-08-standalone-credentials.md) | Mike (John reviews) |
| JWT bearer, chosen by `LPB_AUTH` (#49, ISS-33 in M4) | [completed/jwt-bearer.md](exec-plans/completed/jwt-bearer.md) | Mike (John reviews) |
| Hosted database and preview databases (deployment left this repository, #95) | [completed/neon-preview-branches.md](exec-plans/completed/neon-preview-branches.md) | John |
| Actor collection and read-only admin (#52) | [completed/admin-read-and-actors.md](exec-plans/completed/admin-read-and-actors.md) | Mike (John reviews) |
| ISS-10 Network rates and pricing assumptions | [completed/iss-10-rates.md](exec-plans/completed/iss-10-rates.md) | John (Mike reviews) |
| ISS-11 + ISS-14 Repeat-safe commands (idempotency) | [completed/iss-11-14-idempotency.md](exec-plans/completed/iss-11-14-idempotency.md) | John (Mike reviews) |
| ISS-12 Distinct payment failures | [completed/iss-12-payment-failures.md](exec-plans/completed/iss-12-payment-failures.md) | John (Mike reviews) |
| ISS-15 Owned job history and basic results | [completed/iss-15-history-results.md](exec-plans/completed/iss-15-history-results.md) | John (Mike reviews) |
| ISS-18 Cost reports (the events feed moved to M4 as ISS-46) | [completed/iss-18-cost-reports.md](exec-plans/completed/iss-18-cost-reports.md) | John (Mike reviews) |
| ISS-23 Verification of access, repeated commands and accounting | [completed/iss-23-verification.md](exec-plans/completed/iss-23-verification.md) | John (Mike reviews) |
| Native HTTP transport: bounded raw bodies, operation identity and stream headers ([#55](https://github.com/NE-Build-Track/livepeer-builder-core/pull/55)) | [completed/native-http-transport.md](exec-plans/completed/native-http-transport.md) | Mike |

## M4: Reuse and reliability (open)

No exec plan is active yet. The implementation guides are in
[design-docs/m4-guides/](design-docs/m4-guides/index.md), and what M3 left open is in
[milestones.md](product-specs/milestones.md#carried-forward-to-m4). Add a plan to `exec-plans/active/`
when work on a ticket starts.

Nothing closes without Mike's review. Josh reviews engine code.
