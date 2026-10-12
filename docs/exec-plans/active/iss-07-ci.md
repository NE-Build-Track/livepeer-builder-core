# ISS-07: Add development builds and continuous checks

## Status

Owners: John, Mike. GitHub: #13. **The workflow is written in this scaffold; it has not run yet,
because nothing is pushed.**

## Goal

Versioned development packages, and automated test, lint/type and install/start checks for the M2
components: the framework later service and application components extend.

## Acceptance

- [x] `.github/workflows/ci.yml`: ruff, strict mypy, docs check, generated-docs drift, unit, store and
      structure tests on SQLite
- [x] The same store suite on a PostgreSQL 16 service container
- [x] Versioned dev build (hatch-vcs: `0.x.devN+g<sha>`) uploaded as a CI artifact; nothing published
- [x] Clean-install job: install the wheel in a fresh venv, then `lpb smoke` and a packaged-migrations check
- [ ] The first green run on the pushed `releases/m2` branch
- [ ] Mike's review

## Progress

- [x] 2026-10-12: Workflow scaffolded.

## Decision log

- 2026-10-12: Minimal CI in M2 (D5). Release-grade artifacts stay with ISS-38. Builds are CI artifacts
  only (the baseline doesn't authorize publishing).
