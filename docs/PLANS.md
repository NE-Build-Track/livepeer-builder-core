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

## M2: Foundation and integration contracts (target 16 October 2026)

| Ticket | Plan | Owners |
| --- | --- | --- |
| ISS-01 Baseline | [completed/iss-01-baseline.md](exec-plans/completed/iss-01-baseline.md) | John, Mike |
| ISS-02 Engine contracts | [active/iss-02-engine-contracts.md](exec-plans/active/iss-02-engine-contracts.md) | John, Mike |
| ISS-03 Access | [completed/iss-03-access.md](exec-plans/completed/iss-03-access.md) | John, Mike |
| ISS-04 Payments | [active/iss-04-payments.md](exec-plans/active/iss-04-payments.md) | John, Mike |
| ISS-05 Core packages | [active/iss-05-core-packages.md](exec-plans/active/iss-05-core-packages.md) | John, Mike |
| ISS-06 SQLite records | [completed/iss-06-records.md](exec-plans/completed/iss-06-records.md) | John, Mike |
| ISS-07 Builds and CI | [completed/iss-07-ci.md](exec-plans/completed/iss-07-ci.md) | John, Mike |
| Evidence re-run | [active/m2-attribution-rerun.md](exec-plans/active/m2-attribution-rerun.md) | John |

Nothing closes without Mike's review. Josh reviews engine code.
