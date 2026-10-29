# Payment renewals and refreshes must not follow redirects (#72)

## Status

Owner: John (engine). Reviewers: Mike, John. Issue
[#72](https://github.com/NE-Build-Track/livepeer-builder-core/issues/72), found by John; affects ISS-12,
ISS-13 and the ISS-25 funded run, whose chat job is metered. Implemented on `tasks/mz/72-payment-redirects`
from `releases/m3` `d3df806`, test-first; awaiting review. Decision D58. Nothing is committed or closed
before Mike's review.

## Goal

The engine owns every request in the payment plane: the first paid request (D53), metered renewals and
`/refresh-payment`. Each goes only to an origin the 402 challenge proved, never follows a redirect, treats
any 3xx as a failure and is never resent by the HTTP client, so a misconfigured proxy, an open redirect or
a hostile orchestrator cannot replay a ticket to another host or swap the ticket recipient. The SDK pin
stays upstream `2017a80`; nothing is sent upstream.

## Acceptance

- [x] A renewal answered 301, 302, 303, 307 or 308 reaches no other host, and no further ticket is minted.
- [x] A refresh answered 3xx reaches no other host, and nothing is signed with another host's params.
- [x] A redirect to the payment origin itself is not followed either.
- [x] Funding keeps the SDK's cadence and 4xx rules: 404 `released`; 403, 409 `refused`; 408, 429, 503
      and lost connections retried; a signer 480 refreshes once; a 482 skips the cycle.
- [x] Stopping the funding closes its payment client.
- [x] Why funding stopped is recorded on the call and logged (`runner.funding_stopped`); a redirect does
      not fail the in-flight call (Q1).
- [x] A payment URL off the challenge's origins is refused before a ticket or a request.
- [x] `tests/unit/test_sdk_contract.py` fingerprints every SDK method the override replaces or relies on.
- [x] Docs: D58, payments.md, sdk-gateway.md, upstream-baseline.md, tech debt T17, T18, T20 and T23.
- [x] A metered paid job on the dev stack renews normally (a streamed chat longer than 3 s), recorded in
      the PR (29 October, below).
- [ ] #72 closed with a link to the PR, after Mike's review.

## Progress

- 2026-10-29: Baseline on `d3df806`: 1318 passed, 32 skipped. The starter test (a renewal answered 307
  to a second loopback origin) was red: the SDK followed it eight times in ten intervals, with
  `Livepeer-Payment` and `Livepeer-Segment`. The redirected-refresh test was red against the SDK's
  `_refresh_payment_params` after the renewal fix. Every parity test that passed at once was shown to
  fail under a deliberate mutation of the loop.
- 2026-10-29: `EnginePaymentSession`, `fund` and `FundingEnd` live in `adapters/payment_funding.py`;
  `adapters/runner_call.py` would have passed the 500-line limit (`tests/structure/test_taste.py`), so
  the new module joins `runner_call.py` in the `livepeer_gateway` and `aiohttp` boundary
  (`tests/structure/test_layers.py`). Full suite, coverage gates, diff coverage, lint, types, docs and
  `lpb smoke` pass locally (numbers in the PR). PostgreSQL was not run: no schema change.
- 2026-10-29: Live on `deploy/dev-stack` (Batteries `ca09e47`, signer `406b248`, Arbitrum One), the
  engine image built from this branch with a fresh database. A streamed `blueclaw/chat/qwen3.8-27b`
  chat on hky.eliteencoder.net, priced 4,728,005,481 wei per second: 14.9 s, 892 chunks, job
  `succeeded`, funding ended `cancelled` when the call ended, no renewal failure logged. Cost sync
  matched **4 usage rows** to the one paid session, class `delivered`, $0.000244; reconciliation for
  the actor `balanced`. So renewals on the engine's payment client are accepted by a real orchestrator
  and attribute as before.

## Decision log

- 2026-10-29: Mike accepted the guide's option 3 (subclass the SDK session, own the loop); option 1
  (fix upstream and re-pin) is excluded by the no-upstream rule, option 2 keeps minting tickets against a
  redirecting endpoint. Q1: a redirected renewal stops funding only. Q2: signer requests stay as they
  are, as T23. Q3: ships before the ISS-25 dry run on 13 November. Recorded as D58.
- 2026-10-29: `fund` reads the SDK's `PAYMENT_INTERVAL_S` each cycle when no `interval_s` is given, as
  `run_payments` does, rather than binding it as a default argument, so the interval stays the SDK's.
- 2026-10-29: The client is created on the first renewal and closed when `fund` ends, which
  `stop_funding` waits for; `stop_funding` itself is unchanged. T17 and T20 stay out of scope.
