# AGENTS.md

This file is a **map**, not a manual. Read the linked documents when a task touches them.

## What this repository is

`livepeer-builder` is the shared, noncommercial **builder engine** for Livepeer. Applications either
import it (in-process) or run it as a REST service. It does discovery, paid job execution, access
control and network-cost reporting. Retail pricing, customers and billing stay in each enterprise
application. Delivery track: Cloud SPE Build Track, milestone **M2** (target 16 October 2026).

## Start here

| You need | Read |
| --- | --- |
| The layers, and what may import what | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Component, sequence, state and ER diagrams | [docs/design-docs/architecture-diagrams.md](docs/design-docs/architecture-diagrams.md) |
| How an enterprise replaces the defaults (interfaces, class diagrams) | [docs/design-docs/extending-the-engine.md](docs/design-docs/extending-the-engine.md) |
| How to embed the engine in a Python backend | [docs/guides/embedded-mode.md](docs/guides/embedded-mode.md) |
| What we are building and why (scope, boundary) | [docs/product-specs/build-track-baseline.md](docs/product-specs/build-track-baseline.md) |
| Milestones, tickets, and status for each M2 ticket | [docs/product-specs/milestones.md](docs/product-specs/milestones.md), [m2-traceability.md](docs/product-specs/m2-traceability.md) |
| The rules every change follows | [docs/design-docs/core-beliefs.md](docs/design-docs/core-beliefs.md) |
| What a public contract promises | [docs/contracts/index.md](docs/contracts/index.md) |
| Why something was decided | [docs/design-docs/m2-decisions.md](docs/design-docs/m2-decisions.md) |
| What to work on, and its acceptance | [docs/exec-plans/active/](docs/exec-plans/active/) and [docs/PLANS.md](docs/PLANS.md) |
| Known gaps and shortcuts | [docs/exec-plans/tech-debt-tracker.md](docs/exec-plans/tech-debt-tracker.md) |
| Upstream repos, pins, wire formats, glossary | [docs/references/index.md](docs/references/index.md) |
| Operating, configuring, backing up | [docs/RELIABILITY.md](docs/RELIABILITY.md) |
| Secrets, credentials, trust boundaries | [docs/SECURITY.md](docs/SECURITY.md) |
| How healthy each area is | [docs/QUALITY_SCORE.md](docs/QUALITY_SCORE.md) |
| The database schema and REST schema | [docs/generated/](docs/generated/) (generated; do not edit) |

## Commands

```sh
uv sync --all-extras                     # install (Python 3.12, uv)
uv run pytest                            # unit, store (SQLite) and structural tests
uv run pytest --cov --cov-report=json && uv run python scripts/check_coverage.py   # coverage gates
uv run diff-cover coverage.xml --compare-branch=origin/releases/m2 --fail-under=90  # needs --cov-report=xml
LPB_TEST_POSTGRES_URL=postgresql+asyncpg://... uv run pytest tests/store   # also on Postgres
uv run ruff check . && uv run ruff format --check .
uv run mypy                              # strict
uv run python scripts/check_docs.py      # links, anchors, indexes, exec-plan shape
uv run lpb gen-docs                      # regenerate docs/generated after schema/API changes
uv run lpb smoke                         # import, migrate, start, health, stop
```

CI (`.github/workflows/ci.yml`) runs all of these plus a clean-install check of the built wheel.

## Source map

```text
src/livepeer_builder/
  contracts/   types layer: pydantic models that cross the boundary (imports nothing internal)
  errors.py    engine errors, one HTTP status each
  protocols/   providers: every extension interface (Authenticator, AccessPolicy, SecretStore, ...)
  config.py    EngineSettings from LPB_* environment variables
  services/    business rules: policy, keys, discovery, jobs, payments, costs, reserved features
  store/       SQLAlchemy Core + packaged Alembic migrations, backup, retention
  adapters/    SDK, Batteries, auth, secrets, clock implementations of the protocols
  runtime/     BuilderEngine wiring and structured logging
  http/        FastAPI REST service ([server] extra)
  testing/     fakes shipped for builders' own tests
  cli.py       `lpb` operator commands
tests/unit  tests/store  tests/structure
deploy/dev-stack/   optional payment stack (Batteries + node signer + engine); orchestrators come from the network
deploy/railway/     Railway Batteries, signer, and engine; branch pushes deploy the engine, production and a manual run deploy the selected services
```

## Working agreements

- **Enforce invariants, not style.** If a rule matters, encode it in `tests/structure/` with a
  failure message that says how to fix it. Do not add prose-only rules.
- **Parse at the boundary.** Contracts are frozen pydantic models; adapters convert wire formats.
- **Money is `Decimal`.** Never float, never "missing = 0" (missing cost is `pending`).
- **Secrets never ride on records.** Payment keys live in a `SecretStore`; records hold a `SecretRef`.
- **Reserved features fail loudly** with `NotImplementedInMilestone` (HTTP 501), never silently.
- **Schema change ⇒ migration + `lpb gen-docs`.** `tests/store/test_durability.py` catches drift.
- **Plans are artifacts.** Update the exec plan's progress and decision log in the same change.
- **Git:** never commit or push without a human review. No `Co-Authored-By` trailers or other
  attribution lines in commits or PR descriptions.

## Escalate to a human when

- a change would alter an accepted decision in `docs/design-docs/m2-decisions.md`;
- an upstream repository (SDK, node, Batteries) needs a change;
- money could move: provisioning, funding, revocation semantics.
