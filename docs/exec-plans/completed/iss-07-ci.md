# ISS-07: Add development builds and continuous checks

## Status

Owners: John, Mike. GitHub: #13. **Accepted by Mike on 13 October.** CI is green on `releases/m2`.

## Goal

Versioned development packages, and automated test, lint/type and install/start checks for the M2
components: the framework later service and application components extend.

## Acceptance

- [x] `.github/workflows/ci.yml`: ruff, strict mypy, docs check, generated-docs drift, unit, store and
      structure tests on SQLite
- [x] The same store suite on a PostgreSQL 16 service container
- [x] Versioned dev build (hatch-vcs: `0.x.devN+g<sha>`) uploaded as a CI artifact; nothing published
- [x] Clean-install job: install the wheel in a fresh venv, then `lpb smoke` and a packaged-migrations check
- [x] Branch-coverage gates: 95% overall, per-layer floors, 90% of changed lines on pull requests
- [x] Engine container image built and smoke-tested (job `container`): non-root, `lpb smoke`, `/readyz`, 401 without a credential
- [x] Green on the pushed `releases/m2` branch
- [x] Mike's review (13 October)

## Progress

- [x] 2026-10-12: Workflow scaffolded.
- [x] 2026-10-12: Coverage gates added (98% branch coverage at the time).
- [x] 2026-10-13: Scaffold merged to `releases/m2`; CI green there. Container smoke job added; the
      image runs as non-root and `.dockerignore` keeps local secrets out of the build context.
      The dev stack's image registry is configurable (`IMAGE_REGISTRY`).

## Decision log

- 2026-10-12: Minimal CI in M2 (D5). Release-grade artifacts stay with ISS-38. Builds are CI artifacts
  only (the baseline doesn't authorize publishing).
- 2026-10-13: The engine container smoke test is in M2, not ISS-38 (Mike). Release-grade images and
  publishing stay with ISS-38.
