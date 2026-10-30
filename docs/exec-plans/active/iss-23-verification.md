# ISS-23: Verification of access, repeated commands and accounting

## Status

- **Owner:** John (engine), per the guide. **Reviewers:** Mike, John. Test-first, one pull request.
- **Ticket:** ISS-23 ([Network-Engineering-SPE#36](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/36)) (M3).
- **Branch:** `tasks/mz/iss-23-verification`, at `56ccb27` (`releases/m3` plus PR #78, ISS-12, D58); to
  be stacked on the ISS-15 and ISS-18 branches.
- **Design:** the [verification guide](../../design-docs/m3-guides/iss-23-verification.md) (29 October).
  Q1–Q3 locked by Mike on 30 October.
- **Progress:** the spec, the doc guard, the gap tests and the live probe are built and checked, and
  the probe has run live (25 passed); in review as #83.
- **Decision:** D61.

## Goal

Automated checks cover cross-actor access, revoked keys, duplicate job submissions and admin commands,
authorization denial, and late or duplicate cost observations, each marked fixture-based or live, in
one traceability spec whose cited test names a doc check keeps true. The gaps the guide lists get
fixture tests; one live probe checks the same rules against a real stack.

## Acceptance

From the ticket and the 21 October M3 plan, as the guide's definition of done:

- [x] [m3-traceability.md](../../product-specs/m3-traceability.md) lists every category with at least one
  check, every row marked fixture or live.
- [x] The doc guard passes: `scripts/check_docs.py` fails on a cited test that does not exist
  (`tests/structure/test_traceability.py`).
- [x] The gap tests are green (`test_admin_route_sweep.py`, `test_verification_access.py`,
  `test_cost_observations.py`).
- [x] The live probe has run once against the console's stack, with its output recorded under
  `docs/references/evidence/` and linked from the spec
  ([30 October](../../references/evidence/2026-10-30-iss-23-live-probe.md): 25 passed, engine `ade673f`).
- [x] The `AGENTS.md` checks are green.

### Test list

- [=] route sweep: every `/v1/admin/*` route answers 401 with no credential and 403 to an actor key, and
  every admin write 403 to an auditor key, from FastAPI's route table (the starter; proved by mutation:
  removing the policy check from `set_status` fails it for both keys)
- [x] doc guard: a cited test or file that does not exist is reported; existing async and parametrized
  tests are not
- [=] Bob reuses Alice's `operation_ref`: his own job runs, Alice's job never appears (engine and REST)
- [=] cost summary, recent costs and allowance leave out another actor's fees
- [=] a job after revoke-allocation fails `not_provisioned` with no runner call
- [=] a revoked admin key and a revoked auditor key get 401 on admin routes
- [=] repeated set-status, revoke-allocation and revoke-key change nothing more (engine and REST)
- [=] Batteries rows with status `duplicate` or `ignored` are never counted
- [x] a re-delivered row id with another fee is ignored and logged (Q2): ignored already, the log was
  missing
- [=] a late row for a job retention purged stays unattributed
- [x] the live probe without a network: every check passes against the engine in-process; nothing is
  spent without `--yes` or above the cap; a wrong answer fails it; no stack, exit 0

## Progress

- 2026-10-30: The M3 live suite ran on the console's stack with this branch merged with #77 (`9676260`): 41
  engine checks green across payment failures, access, secrets, a discovery outage, streams with an
  engine restart, Batteries down and sync replay ([evidence](../../references/evidence/2026-10-30-m3-live-suite.md)).
  The traceability spec gains the L rows it proves, a live payment-failure section (ISS-12), and the
  console's D57 checks; the dev-stack README notes that restarting clearinghouse needs the signer
  recreated.

- 2026-10-30: Cursor's second review of #83. The first probe job could itself be paid: `POST /v1/jobs`
  returns after the runner call, and the shared key pays in `per_application` mode. The jobs now name the
  run's own app (`lpb-probe/<run>`), the probe checks `GET /v1/offerings?app=` first and sends none if it
  is listed, and both jobs must end `no_offering` unpaid. The test that claimed this is rebuilt in
  `per_application` with a runner for the probe's app: no runner call, no job. 26 checks now.

- 2026-10-30: Cursor's review of #83. (1) `live_probe.py` reads how its first job ended and sends the second
  only after a `no_offering` failure with nothing paid: if some runner advertised the probe's app, the
  shared key could have paid in `per_application` mode. (2) Its keys are revoked in a `finally`, one
  failed revoke not stopping the next, and a body that is not JSON is a FAIL, not a crash. (3) A 201
  without a key id is a failed issue, never the previous key. (4) A new usage row id twice on one page
  with another fee is logged like a later re-delivery (D61 (3)).

- 2026-10-30: Live run on the console's stack with the engine built from `ade673f`: 25 passed, 0 failed,
  one 0.01 USD fund applied once. The review changes (repeat revoke 204, 413 over 16 MiB, `before`
  422 without an offset, `by_app` by class) seen on the same engine.

- 2026-10-30: Baseline at `56ccb27`: 1578 passed, 49 skipped. Built test-first in one session.
- 2026-10-30: The only engine change: `SqlStore.apply_usage` reads the stored fees of row ids it has
  seen and logs `costs.usage_redelivered` (WARNING) when a re-delivered row's fee differs. The re-read
  of the last page on every sync is the same row with the same fee, so it logs nothing.
- 2026-10-30: `scripts/live_probe.py` added to the strict mypy file list.
- 2026-10-30: Checks: 1609 passed, 49 skipped (31 new tests; PostgreSQL store tests skipped locally).
  Layer coverage floors pass (store 98.16%); diff coverage against `56ccb27` 100%. Ruff, format, mypy
  (strict), `check_docs.py`, `lpb gen-docs --check` and `lpb smoke` pass. The probe has not been run
  against any live stack.

## Decision log

- 2026-10-30: Q1 the live probe in builder-core, REST only, not in CI; Q2 ignore and log; Q3 one live
  run marks the rows live (Mike). Recorded as D61.
- 2026-10-30: The guard is check 7 of `scripts/check_docs.py` (beside the example check, which also
  ties docs to code) and applies to `docs/product-specs/*traceability*.md`; `test_docs.py` runs it in
  pytest and CI.
- 2026-10-30: The probe's cross-actor job asks for an app no runner advertises, so it fails
  `no_offering` before any credential or payment and still records a job owned by the first actor. Its
  denial checks use only harmless routes (`POST /v1/admin/costs/sync`, a key issue that is revoked if it
  wrongly succeeds), so a wrong "allow" on a live stack moves no money. The fund is capped per currency.
- 2026-10-30: Repeats of the commands that take no reference change nothing, but over REST the repeat
  answers differently from the first: revoke-allocation 404 (no longer provisioned) and revoke-key 400
  (no active key). Pinned as they are; whether a lost-answer retry should get the first answer is
  left to Mike.
- 2026-10-30: Mike's review: **a repeat of a finished revoke answers as the first did** (D61 (6)).
  Revoke-allocation returns the revoked actor without calling the provider; revoke-key succeeds
  without a second audit line. An unknown actor or key keeps its 404 or 400. One cut short (Batteries
  revoked, the actor not yet marked) is still active and runs again, Batteries' revoke being
  repeat-safe. The tests now pin the new answers.
- 2026-10-30: `services/payments.py` was at 499 lines, so the revoke change crossed the 500-line cap.
  `GrantDesk`, `idempotency_key` and `GRANT_TAG` moved to `services/grants.py` unchanged (a tidy split
  by responsibility: grants fund every application, payments one actor's allocation); `payments.py`
  imports them, so `services.payments.idempotency_key` still resolves.
