# ISS-12: Distinct payment failures

## Status

- **Owner:** John (engine), per the guide. **Reviewers:** Mike, John. Test-first, one pull request.
- **Ticket:** ISS-12 ([Network-Engineering-SPE#25](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/25)) (M3).
- **Branch:** `tasks/mz/iss-12-payment-failures`, stacked on `tasks/mz/iss-11-14-idempotency` at `a41beef`
  (PR #76, D57, not merged yet).
- **Design:** the payment-failures guide, `docs/design-docs/m3-guides/iss-12-payment-failures.md`
  (29 October; not on this branch yet). Q1–Q3 locked by Mike on 29 October.
- **Progress:** engine built and checked; awaiting review. The consumer changes (console, ChatLPT,
  inference gateway, the enterprise UIs) are to do.
- **Decision:** D58.

## Goal

A builder can tell "not authorized", "allowance exhausted" and "signer unavailable" apart, from the
failure and from the REST status, and no builder-facing response carries a payment key, the signer URL
or the signer's or provider's own words. `Failure.kind` keeps deciding behavior; the new
`Failure.reason` says what the builder can do.

## Acceptance

From the ticket and the 21 October M3 plan:

- [x] Tests show "not authorized", "allowance exhausted" and "signer unavailable" as distinct failures
  (`tests/unit/test_payment_failures.py`, `tests/unit/test_payment_failures_http.py`).
- [x] No builder-facing response carries a payment key, the signer URL or upstream text (the leak scan
  in `test_payment_failures_http.py`).
- [x] `engine.md#failures`, `payments.md` and the OpenAPI schema are updated.
- [x] Every test in the guide's list is `[x]` or `[=]` with a reason; the `AGENTS.md` checks are green.
- [ ] The console and the enterprise desks branch on `reason` (separate pull requests in those repos).

### Test list

- [x] signer 401 → `payment` / `not_authorized`, unpaid, no failover (the starter)
- [=] signer 403 → `payment` / `not_authorized` (the same rule as 401)
- [x] signer 402 → `payment` / `allowance_exhausted`
- [x] what the SDK's `post_json` chains for a refused connection, a DNS failure, a timeout, a dropped
  connection and a 5xx; only the first two never reached the signer
- [x] signer refuses connections at `sign-orchestrator-info` → `payment` / `signer_unavailable`, no failover
- [=] signer 502, 503 or a dropped connection at `sign-orchestrator-info` → `signer_unavailable`
  (the rule above; proved by mutation)
- [x] signer refuses the connection to sign → `payment` / `signer_unavailable`, manifest `unpaid`, `failed`
- [x] signer 502, a dropped or a malformed reply while signing → `payment_unknown` / `signer_unknown`
- [x] the failure message is the engine's; the signer's words and URL go to the log
- [+] the contract refuses a reason its kind cannot have (`reason_fits`)
- [+] a custom transport's reason that does not fit its kind is dropped, not an engine error
- [+] the SDK's text and the signer URL reach the `runner.signer_error` log as `detail`
- [x] REST: 403, 402, 503 + `Retry-After: 10`, 503 without, buffered and streamed
- [=] REST: `not_provisioned` stays 402 with no reason
- [=] no builder-facing body carries `lpg_`, the signer URL or its text (proved by mutation)
- [x] a `signer_url` with userinfo, a query or a fragment is refused at startup
- [x] `/v1/allowance` with a provider error answers a fixed message and the provider's status
- [x] `FakeEngine` carries the same reasons (contract test)
- [=] a repeat after `allowance_exhausted`, `not_authorized` or `signer_unavailable` runs again; after
  `signer_unknown` it is 409 (D57's list; proved by mutation)
- [x] the failure table in `engine.md` lists every kind and reason (doc test)

## Progress

- 2026-10-29: Baseline at `a41beef`: 1489 passed, 49 skipped. Built test-first in one session. One
  existing assertion changed: `test_runner_call.py::test_unreachable_runner_or_signer_is_unpaid`
  expected a signer that refuses connections at `sign-orchestrator-info` to be `unreachable`; under D58
  it is `payment` / `signer_unavailable`. The loopback harness gained a scriptable
  `sign-orchestrator-info`, a hook before each challenge and a signer that can go down, and its signer
  words became distinctive (`UPSTREAM-…`) so the leak scan can find them. The signer-failure reading
  moved out of `runner_call.py` into `adapters/signer_failures.py` (the 500-line cap), which joins
  `runner_call.py` on the `aiohttp` and `livepeer_gateway` allowlist.
- 2026-10-29: Checks: 1577 passed, 49 skipped (PostgreSQL store tests skipped locally; the store is
  unchanged). Layer coverage floors pass (adapters 99.10%); diff coverage against
  `origin/tasks/mz/iss-11-14-idempotency` 99%. Ruff, format, mypy (strict), `check_docs.py`,
  `lpb gen-docs --check` and `lpb smoke` pass. One run under coverage tripped the 40 ms bound in
  `test_stream_lifecycle.py::test_aclose_never_waits_for_the_runner[1.0]` (timing; passes alone and on
  rerun; not on the signer path).

## Decision log

- 2026-10-29: Q1 `Failure.reason` beside `kind`; Q2 REST status by reason; Q3 a connect-phase failure
  during `get_payment` is `signer_unavailable` (Mike). Recorded as D58.
- 2026-10-29: Any transport failure at `sign-orchestrator-info` (also a timeout or a dropped connection)
  is `signer_unavailable`: that call signs nothing, so nothing can be charged. The locked text names a
  refused connection and a 5xx; the guide says "unreachable or 5xx before a manifest is stored".
- 2026-10-29: A `payment` failure with no reason remains: a failed intent write, every runner
  `payment_refused`, another signer status, a malformed identity reply. 503 with `Retry-After: 10`, as
  before.
- 2026-10-29: `ProviderError` keeps its `provider HTTP {status}: …` form for the fixed allowance
  message. Only `/v1/allowance` reaches the provider among builder-facing routes; admin routes keep the
  provider's text for operators.
