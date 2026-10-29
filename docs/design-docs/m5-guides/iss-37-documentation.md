# ISS-37: Installation, extension and operating documentation

**Status:** proposal for discussion, 29 October 2026. Nothing here is decided until it is locked in
review with Mike and John.\
**Tracker:** ISS-37, a draft item on [Project 13](https://github.com/orgs/Cloud-SPE/projects/13) (no issue
yet); [planning reference](https://github.com/Cloud-SPE/Network-Engineering-SPE/blob/eea61ba3ddec7326934d0218cb5503f16ffc5286/docs/design-docs/build-track-december-2026-issue-proposal-draft.md#iss-37-complete-installation-extension-and-operating-documentation).\
**Owner:** unassigned (draft item; M5 owner Mike). **Reviewers:** Mike, John.\
**Verified against:** builder-core `releases/m3` at `d3df806`; the console (`builder-core-mgmt-ui`) at
`938db3e`; Network-Engineering-SPE at `3c365a4`.

## Done when

From the tracker, condensed:

- Version-matched guidance for both integration modes and both payment configurations, enterprise
  authentication replacement, jobs and reporting, configuration, upgrades, retention, backup and
  restore, and recovery.
- Links to the reference app's instructions and to the supported interface examples. The docs
  explain secrets, funding prerequisites, logs and correlation, and limitations.
- The docs match the release candidate and are **usable without their authors**.

The source task (L1 in the task breakdown) adds two things: the limits of the supplied auth adapters, and
logs and correlation references that support an investigation without exposing secrets.

**Depends on:** ISS-24, ISS-31, ISS-32, ISS-33, ISS-34, ISS-35. The plan allows documentation to start in
M4. M5 runs from 21 to 31 December 2026.

## Current state (verified)

### What exists

| Area | Files |
| --- | --- |
| Entry points | `README.md` (77 lines), `AGENTS.md` (a map), `ARCHITECTURE.md` (layers, providers, integration modes), `docs/index.md` |
| Guides | `docs/guides/index.md` lists one guide, `embedded-mode.md` (239 lines) |
| Contracts (normative) | `docs/contracts/`: `access.md` (339 lines, JWT bearer and an authorization-server appendix), `engine.md` (378), `payments.md` (182), `persistence.md`, `discovery.md`, `events.md` (defined only) |
| Operations | `docs/RELIABILITY.md` (configuration table, `lpb` commands, backup and restore, failure behavior), `docs/SECURITY.md` (secrets, trust boundaries), `docs/QUALITY_SCORE.md` |
| Extension | `docs/design-docs/extending-the-engine.md`; `docs/generated/interfaces.md` |
| Generated | `docs/generated/`: `openapi.json`, `db-schema.md`, `interfaces.md`, `contracts.md`, written by `lpb gen-docs` (`src/livepeer_builder/docs_gen.py`, `docs_diagrams.py`). CI runs `lpb gen-docs --check` |
| Deploy | `deploy/dev-stack/README.md` (payer prerequisites), `deploy/railway/README.md` |
| References | `docs/references/`: pins, licenses, wire formats, runtime evidence |
| Reference app (console) | `README.md`, `docs/guides/m3-journey.md` (the ISS-24 guide), `deploy/README.md`, `deploy/keycloak/README.md`, `docs/references/payment-stack.md` |

### What keeps docs honest today

- `scripts/check_docs.py`, run by `tests/structure/test_docs.py` and by CI, checks the following:
  links and anchors resolve, every doc under `docs/` is reachable from `AGENTS.md` or `docs/index.md`,
  exec plans have the right sections, `docs/…md` references in `src/` exist, Mermaid blocks parse, and
  every `<!-- example: file#region -->` block equals its tested region.
- **Tested examples:** one in `README.md` (`tests/unit/test_readme_example.py`) and three in
  `extending-the-engine.md`. **None** of the five Python blocks in `embedded-mode.md` is tested, and
  neither is the one in `access.md`.
- **Generated references:** the OpenAPI spec, the DB schema, the protocols and the contracts.
- **Configuration:** all 47 `EngineSettings` fields appear as `LPB_*` rows in `RELIABILITY.md` (checked by
  script on 29 October). **No test keeps the table complete.** `tests/structure/test_settings.py` only
  checks that `config.py` is the only module that reads the environment.

### Acceptance items mapped to docs

| Item | Covered by | Gap |
| --- | --- | --- |
| Embedded mode | `guides/embedded-mode.md`; README "Embed it" | Untested blocks. Its install line, `pip install livepeer-builder`, fails: the name is not on PyPI (404 on 29 October) |
| Service mode | README "Run the service"; `ARCHITECTURE.md#integration-modes`; `extending-the-engine.md#service-mode` | No guide. The README steps never set `LPB_DISCOVERY_URL`, so the engine starts with empty static discovery and an empty catalog |
| Both payment configurations | `payments.md` (allocation modes; Mode B "deferred to M4") | No setup guide. `per_application` needs `LPB_SHARED_ALLOCATION_ID` set by hand (T5). The second configuration is ISS-31's output |
| Enterprise auth replacement | `extending-the-engine.md`; `access.md` (JWT bearer and the appendix); console `deploy/keycloak/README.md` | No guide. #51 (public and confidential OIDC clients) is open. `QUALITY_SCORE.md` still grades Access B with "JWT and trusted backend are C (M4)", although JWT bearer is built (T7, D45) |
| Jobs and reporting | `engine.md`, `payments.md#cost-classification` | Contract prose only, with no task path ("run a job, read result and cost, reconcile"). Async and persistent modes arrive with ISS-26 and ISS-27 |
| Configuration | `RELIABILITY.md#configuration`; `discovery.md#settings` (`LPD_*`) | Unguarded (above) |
| Upgrades | `persistence.md#migrations` (`lpb migrate`, `lpb revision`, drift test) | No procedure (stop, back up, migrate, start, verify), no release notes, no statement on downgrades |
| Retention | `persistence.md#retention` | Adequate |
| Backup and restore | `RELIABILITY.md#backup-and-restore`; drill in `tests/store/test_durability.py` | Adequate for SQLite. PostgreSQL gets one line (`pg_dump`), and PostgreSQL is stretch scope |
| Recovery | `RELIABILITY.md#failure-behavior`; `engine.md#deployment-performance-and-recovery-assumptions` | A table of behaviors, not a runbook: nothing on what an operator does about `uncertain` jobs, a persistent `cost_sync_error` or a lost `LPB_SECRET_KEY` |
| Secrets | `SECURITY.md#secrets` | Adequate. It needs a backup checklist that pairs the database with the key |
| Funding prerequisites | `deploy/dev-stack/README.md#prerequisites`; console `m3-journey.md` "What you need" | Nothing in the builder-core `docs/` tree. The console guide assumes a copy of ChatLPT's `deploy/secrets/` |
| Logs and correlation | `RELIABILITY.md#operations` (one sentence: JSON lines with an `event` field); `payments.md#correlation`; the `Livepeer-Job-Id` header | No event catalog. A grep finds at least 27 distinct `event` names. A finished immediate job writes no event line: `services/jobs.py` logs only `jobs.streams_closed` and `jobs.close_failed` |
| Limitations | README status note, `engine.md`, the tech-debt tracker, the console's "Known limitations (M3)" | Scattered across files. No page lists the limits per release |
| Reference app | Console `docs/guides/m3-journey.md` | It links builder-core **PR #26** rather than `docs/guides/embedded-mode.md`. It clones `builder-core-mgmt-ui`, but the repository is now `builder-core-console` (GitHub redirects) |
| Interface examples | README, `extending-the-engine.md`, `generated/openapi.json` | No REST walkthrough with `curl`. MCP is ISS-30 |

Stale statements an outside reader would trip on: the README status ("M3 in progress"), `AGENTS.md`
("milestone M3") and the Access row in `QUALITY_SCORE.md`.

## Decision proposed

### 1. Organize the doc set by the reader's task

Contracts stay normative, and the guides become procedures that link to them. Every new guide lives in
`docs/guides/`, and `docs/guides/index.md` becomes the "start here" for people who are not developers.

| New or changed file | Reader | Content |
| --- | --- | --- |
| `docs/guides/install.md` (new) | Operator | Prerequisites (Python 3.12, and `git` while the SDK is a direct reference, T1). Install from the ISS-38 release (wheel or image, by version and digest). First start, `lpb smoke`, `bootstrap-admin`, the discovery source |
| `docs/guides/service-mode.md` (new) | Integrator | The seven outcomes over REST with `curl`: key, offerings, rate, run, stream, result, cost, allowance. Errors and the `Livepeer-Job-Id` header |
| `docs/guides/embedded-mode.md` | Integrator | Every Python block becomes a tested example region |
| `docs/guides/payment-operation.md` (new) | Operator | Both configurations from ISS-31: self-operated (signer and Batteries you run; payer keystore, TicketBroker deposit and reserve, Arbitrum RPC) and separately operated. `per_actor` against `per_application`. The three terms from the ISS-11 guide: allowance change, signer funding, spending guarantee |
| `docs/guides/enterprise-auth.md` (new) | Integrator | Replacing access: JWT bearer behind an OIDC server (folds in #51), a custom `Authenticator`, identity mapping in embedded mode. The limits of each supplied adapter. Links ISS-33's console configuration |
| `docs/guides/jobs-and-reporting.md` (new) | Integrator | Immediate, streamed, async (ISS-26) and persistent (ISS-27) jobs; results and their retention; cost states; reconciliation; events, if ISS-18's feed ships |
| `docs/guides/operations.md` (new) | Operator | Runbook: upgrade procedure, backup and restore, retention, recovery actions, health. **Event catalog** (every `event` name, level, fields, action). **Investigate a job:** job id → attempts → `manifest_id` → `payment_session_id` → Batteries usage row, without printing a secret |
| `docs/guides/limitations.md` (new) | Everyone | Known limits and external dependencies for this release (T1, Mode B, the held events feed, T13/T16 attribution limits). ISS-38 copies it into the release notes |
| `RELIABILITY.md`, `SECURITY.md` | Reference | Kept as the configuration and secrets reference. Add the backup checklist and the cross-links |
| README, `AGENTS.md`, `QUALITY_SCORE.md` | — | Fix the stale lines. The README's "Install" points to `install.md` and the release, not to PyPI |

The console keeps its own journey guide (APP home). builder-core links to it **by tag**, and the
console links builder-core's guides by tag rather than by PR.

### 2. Keep docs version-matched by mechanism, not by memory

The docs ship with the code: same repository, same tag, and the sdist includes `docs/`
(`pyproject.toml`, `[tool.hatch.build.targets.sdist]`). Version-matching therefore means **the docs at
tag `vX` describe the code at tag `vX`**. Tests enforce what can drift:

1. **Completeness guards** (`tests/structure/test_doc_coverage.py`, new). Every `EngineSettings` and
   `DiscoverySettings` field has a configuration row. Every `lpb` subcommand (`cli._parser()`) has a row in
   the operations table. Every `"event": "<name>"` literal in `src/` has a row in the event catalog, and
   every catalog row names a real event.
2. **Tested examples:** the `<!-- example: -->` mechanism extends to every Python block in `docs/guides/`.
   A block that is deliberately illustrative carries `<!-- illustrative -->`, which the check accepts.
3. **Pins named in docs** equal the pins in code: the SDK commit in `pyproject.toml`, and the node and
   Batteries commits in `deploy/*/compose.yaml` and `upstream-baseline.md`. This mirrors the console's
   `pins.test.ts`.
4. **No PR or branch links as documentation.** A guide may not link
   `github.com/NE-Build-Track/*/pull/…` or `/tree/<branch>`. Links to other repositories name a tag or a
   commit.
5. **An install-guide CI job.** The marked shell steps in `install.md` run against the built wheel and the
   image: migrate, `bootstrap-admin`, serve, `/readyz` 200, an unauthenticated job 401. This extends the
   existing `build-install` and `container` jobs.
6. **A release gate (ISS-38).** The release checklist requires `limitations.md` and the release notes for
   that version, and a green fresh-reviewer record (below) for the release candidate.

A generated configuration reference was considered (see Q2). The table's prose says more than the
`Field` descriptions do, so the proposal keeps the prose and has a test guard its completeness.

### 3. Prove the docs work without their authors: a fresh-reviewer check

This is a rehearsal for ISS-39, run on each release candidate. The reviewer is a fresh AI agent session
with no repository history, or a person who wrote none of the docs. They get only the release artifacts
and the published docs at the tag, a clean VM, and a script of tasks:

1. Install the service from the release image, and install embedded from the wheel.
2. Point discovery at a source, and connect to the provided payment stack.
3. Issue a key, run a job, and read its result and cost.
4. Back up, restore, and upgrade from the previous release candidate.
5. Find one job's evidence from its id.
6. Swap in JWT auth.

Every question asked and every stall is logged as a doc defect (label `docs-fresh-reviewer`). The check
passes when every task is done and no blocking defect is open. The record joins the ISS-37 evidence.

## Code areas

| File | Change |
| --- | --- |
| `docs/guides/*.md` | The new guides above; `index.md` maps each acceptance item to a section |
| `docs/RELIABILITY.md`, `docs/SECURITY.md`, `README.md`, `AGENTS.md`, `docs/QUALITY_SCORE.md` | Cross-links, backup checklist, stale lines |
| `scripts/check_docs.py` | Extend `check_examples` to accept `<!-- illustrative -->`; add the PR-link rule |
| `tests/structure/test_doc_coverage.py` (new) | Settings, CLI, event and pin guards |
| `tests/unit/test_embedded_guide_examples.py` (new) | Regions for the embedded guide's blocks, run against `FakeEngine` |
| `.github/workflows/ci.yml` | An `install-guide` step in `build-install` and in `container` |
| `src/livepeer_builder/services/jobs.py` | Optional (Q4): a `jobs.finished` info event carrying `job_id`, state and outcome, so a log search can find any job |
| Console `docs/guides/m3-journey.md` | Link builder-core by tag; replace "copy ChatLPT's secrets" with the fresh `init-secrets.py` path |

## Test plan (test-driven)

**Levels.** Structural tests (pure, fast, in `tests/structure/`) for every guard. Example tests run against
`FakeEngine`. One CI job runs the install guide against real artifacts. The fresh-reviewer check is a
manual acceptance test with a written record. No test calls the network.

**Seams.** The guards read source files through `ast` (as `test_settings.py` does) and read
`cli._parser()`. Example regions use `livepeer_builder.testing`.

**Starter test:** every `event` literal in `src/` has a row in the operations event catalog. It is red
today because no catalog exists, and it forces the catalog's table format.

```text
Guards
[ ] every "event" literal in src/ has a catalog row; every row names a real event
[ ] every EngineSettings field has a configuration row          [=] holds today; prove it fails by deleting a row
[ ] every DiscoverySettings field has a configuration row
[ ] every lpb subcommand has a row in the operations table
[ ] SDK, node and Batteries commits named in docs equal pyproject and deploy compose files
[ ] a guide linking a PR or a branch of this repository fails check_docs
[ ] every python block in docs/guides is a tested region or marked illustrative
Examples
[ ] embedded-mode blocks run against FakeEngine: construct, provision, run, cost
[ ] service-mode curl steps match routes in generated/openapi.json (paths and methods exist)
Install path (CI)
[ ] install.md steps on the built wheel in a clean venv: migrate, bootstrap-admin, serve, /readyz 200, 401
[ ] the same steps on the release image
Content and acceptance
[ ] guides/index.md maps all 16 rows of the acceptance map to a section; none empty
[ ] a finished immediate job is findable in logs by job_id                 [?] Q4
[ ] fresh-reviewer rehearsal on the release candidate: all tasks done, zero blocking defects
```

**Definition of done.** Every item is `[x]`, or `[=]` or `[-]` with a reason, and every `[?]` is answered.
The checks in `AGENTS.md` are green: `uv run pytest`, coverage gates, `ruff`, `mypy` strict,
`scripts/check_docs.py`, `lpb gen-docs --check`. The fresh-reviewer record is attached to the tracker.
The docs at the release candidate tag pass the same checks.

## Upstream dependencies

| Upstream | Needed? | Why |
| --- | --- | --- |
| livepeer/node | No | The docs describe the signer at the pinned `406b248` and link node's own docs by commit |
| livepeer-python-gateway | No | Install docs must say that the SDK is a git direct reference until a PyPI release exists (T1), so `git` is a prerequisite. That is a documentation fact, not a change |
| clearinghouse-batteries | No | Operators build it from source at `ca09e47` (no published images, T8). Its licensing is ISS-38's concern |

## Consumer impact

| Consumer | Change |
| --- | --- |
| Console | `m3-journey.md` becomes the reference-app instructions (after ISS-34), linked by tag. It drops the ChatLPT secrets dependency for an outside reader |
| ChatLPT, inference-gateway | Their READMEs link builder-core's guides at the tag they pin |
| Enterprise desk | None |

## Alternatives considered

- **A docs site (MkDocs or similar).** Deferred. Markdown in the repository is versioned with the code, and
  `check_docs.py` already guards it. A site can render the same files later.
- **Generate every reference table.** Rejected for prose-heavy tables. Generate where the source is
  already structured (OpenAPI, the schema); guard completeness elsewhere.
- **A separate docs repository.** Rejected, because docs and code could no longer share a tag.

## Open questions to lock

- **Q1.** Operator material in `docs/guides/` (recommended, one index) or a new `docs/operations/`?
- **Q2.** Should a completeness test guard the configuration reference (recommended), or should
  `lpb gen-docs` generate it from `Field` descriptions?
- **Q3.** Should the version match rest on the tag (recommended) with version stamps only on
  `limitations.md` and the release notes, or should every guide carry a "written for vX" line?
- **Q4.** Add a `jobs.finished` event with `job_id` (recommended: log search is how operators
  investigate), or document that jobs are investigated through `GET /v1/jobs/{id}` only?
- **Q5.** Who runs the fresh-reviewer rehearsal? Recommended: a fresh agent session on each release
  candidate, and the ISS-39 reviewer only on the final one.

## Delivery slices

1. **Guards and stale fixes** (can start in M4): the event catalog, configuration and CLI completeness,
   pins, the PR-link rule; fix the README status, `QUALITY_SCORE.md` and the README discovery step.
2. **Install and service mode:** `install.md`, `service-mode.md`, tested embedded blocks, the CI
   install-guide step. Needs ISS-38's artifact names.
3. **Payment operation and enterprise auth**, after ISS-31, ISS-32 and ISS-33.
4. **Jobs and reporting, the operations runbook and limitations**, after ISS-34 and ISS-35.
5. **Fresh-reviewer rehearsal** on the release candidate; fix the defects it finds.
