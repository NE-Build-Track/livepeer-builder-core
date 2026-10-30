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

## M3 (open)

| Ticket | Plan | Owners |
| --- | --- | --- |
| Streaming jobs (to be filed) | [active/streaming-jobs.md](exec-plans/active/streaming-jobs.md) | Mike |
| Dependency-injection refactor (internal, before ISS-32) | [active/dependency-injection-refactor.md](exec-plans/active/dependency-injection-refactor.md) | Mike |
| Discovery service, [#45](https://github.com/NE-Build-Track/livepeer-builder-core/issues/45) (ISS-09) | [active/discovery-service.md](exec-plans/active/discovery-service.md) | Mike |
| ISS-08 Standalone credentials and ownership controls | [active/iss-08-standalone-credentials.md](exec-plans/active/iss-08-standalone-credentials.md) | Mike (John reviews) |
| JWT bearer, chosen by `LPB_AUTH` (#49, ISS-33 in M4) | [completed/jwt-bearer.md](exec-plans/completed/jwt-bearer.md) | Mike (John reviews) |
| Neon Postgres, with a database branch per pull request | [active/neon-preview-branches.md](exec-plans/active/neon-preview-branches.md) | John |
| Actor collection and read-only admin (#52) | [active/admin-read-and-actors.md](exec-plans/active/admin-read-and-actors.md) | Mike (John reviews) |
| ISS-11 + ISS-14 Repeat-safe commands (idempotency) | [active/iss-11-14-idempotency.md](exec-plans/active/iss-11-14-idempotency.md) | John (Mike reviews) |
| ISS-12 Distinct payment failures | [active/iss-12-payment-failures.md](exec-plans/active/iss-12-payment-failures.md) | John (Mike reviews) |
| ISS-15 Owned job history and basic results | [active/iss-15-history-results.md](exec-plans/active/iss-15-history-results.md) | John (Mike reviews) |

Nothing closes without Mike's review. Josh reviews engine code.

- [Native HTTP transport](exec-plans/active/native-http-transport.md): bounded raw bodies,
  operation identity and stream headers; remaining SDK dependencies are explicit.
