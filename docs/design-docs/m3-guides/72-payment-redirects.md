# #72: Payment renewals and refreshes must not follow redirects

**Status:** superseded, 29 October 2026. John closed #72 as *not planned*: renewal and refresh transport
belongs to the SDK, and the fix is tracked upstream as
[livepeer-python-gateway#87](https://github.com/livepeer/livepeer-python-gateway/issues/87) (no redirects on
renewal and refresh, stop the funding loop on a 3xx). builder-core consumes it through a future SDK pin
bump and does not own a replacement payment loop. The engine-side design below was built and passed a
live run (PR [#75](https://github.com/NE-Build-Track/livepeer-builder-core/pull/75), closed; branch
`tasks/mz/72-payment-redirects`); it is kept for reference only.\
**Issue:** [#72](https://github.com/NE-Build-Track/livepeer-builder-core/issues/72) (found by John). Affects
ISS-12 and ISS-13, and the ISS-25 funded run: the representative chat job is priced per second, so it is
metered and uses the renewal loop.\
**Owner:** John (engine). **Reviewers:** Mike, John.\
**Verified against:** builder-core `releases/m3` at `d3df806`; livepeer-python-gateway `2017a80` (the pin);
livepeer/node `e75e5b6`.\
**Upstream:** a private security report was filed with livepeer-python-gateway on 29 October. We follow it
but do not depend on it (see [Upstream dependencies](#upstream-dependencies)).

## The defect

The engine's paid runner call (`adapters/runner_call.py`, D53) sends the first request with
`allow_redirects=False` and fails any status of 300 or more. For a metered runner it then starts the SDK's
`LivePaymentSession.run_payments()`, which renews every `PAYMENT_INTERVAL_S` (3 s). Two of the SDK's
requests in that loop follow redirects, because the SDK turns them off only for `_StreamState` payloads
(`http._request_session`, `http._request_failed`):

| Request | SDK path | What a redirect does |
| --- | --- | --- |
| **Renewal** | `send_payment()` → `_post_empty(payment_url)` | aiohttp follows 301/302/303/307/308 and keeps `Livepeer-Payment` and `Livepeer-Segment` (it strips only `Authorization`, `Cookie` and `Proxy-Authorization` across origins). A 2xx from the target counts as delivered, and the loop mints another ticket next cycle |
| **Refresh** | `get_payment()` → `_refresh_payment_params()` → `post_json({payment_url origin}/refresh-payment)` | The target's `payment_params` replace the orchestrator's, including `TicketParams.Recipient`. The signer signs every later ticket against them (livepeer/node `signer/payment.go:93`) and does not check the recipient against the orchestrator (`signer/service.go:345`) |

`PaidRequest._challenge` checks that `payment_url` belongs to the runner's or the orchestrator's origin,
but only on the first hop. A redirect sidesteps that check.

**Exposure.** TLS is enforced, so the redirect must come from a host the engine already trusts: a
misconfigured proxy (HTTP to HTTPS, canonical host, trailing slash), an open redirect, or a compromised or
malicious orchestrator. The refresh path is the one with money at risk.

## Decision proposed

**The engine owns every request in the payment plane: the first paid request (already, D53), renewals and
refreshes.** Each one goes to an origin the challenge proved, never follows a redirect, treats any 3xx as
a failure, and is never retried automatically by the HTTP client. This is option 3 from the 29 October
analysis; option 1 (fix upstream and re-pin) is excluded by the no-upstream rule, and option 2 (override
the two requests but keep the SDK's loop) keeps minting tickets every 3 s against a redirecting endpoint,
because the SDK's loop retries anything that is not a 4xx.

### Shape of the change (`adapters/runner_call.py`)

1. **`EnginePaymentSession(LivePaymentSession)`**, constructed by `PaidRequest._payment_session` in place
   of the SDK class. It overrides:
   - `send_payment()`: gets the payment through the inherited `get_payment()` and posts it with the
     engine's payment client (below);
   - `_refresh_payment_params()`: posts `{sender, manifest_id}` to `{payment origin}/refresh-payment` with
     the engine's payment client, validates `payment_params` as the SDK does, and replaces the challenge's
     params. Without the override, `get_payment()` would still call the SDK's redirect-following version.
     A 3xx on either request raises `PaymentRedirected` (a `PaymentError`), which the funding loop maps to
     `redirected` and stops on; the SDK's loop would have retried a `PaymentError` next cycle.
   It keeps the SDK's signer requests (`get_payment`, `_payment_request`): they go to the operator's
   configured signer, not to a host the orchestrator names.
2. **An engine funding loop** replaces `run_payments()`: `fund(session, *, interval_s) -> FundingEnd`.
   Same cadence and the same 4xx rules as the SDK (404 means the runner released the session; other 4xx
   except 408 and 429 stop; 5xx, 408, 429 and transport errors retry next cycle), plus **a 3xx stops the
   loop**: a redirect will not change on retry, so no further tickets are minted. `FundingEnd` is a small
   value (`cancelled`, `released`, `refused`, `redirected`) recorded on `PaymentEvents` and logged as
   `runner.funding_stopped` with the reason. T17 can then surface `released` on the stream with a few
   lines; T17 itself is not in this change.
3. **One payment client per funding task**: an `aiohttp.ClientSession` with `allow_redirects=False` on
   every request and `_retry_connection = False` (as `PaidRequest._session` does), reused across renewals
   instead of the SDK's new session and TLS handshake every 3 s. Every request checks that its URL's
   origin is in the request's `_payment_origins` (the same set `_challenge` uses). The client is closed by
   `stop_funding`.
4. `start_funding()` starts `fund(...)` instead of `payment.run_payments()`; `stop_funding()` is unchanged.
5. `interval_s` defaults to the SDK's `PAYMENT_INTERVAL_S` (imported, not copied), and tests pass a small
   value.

Nothing changes in `services/`, the contracts or the REST surface. Failure kinds are unchanged: a funding
loop that stops does not fail the call by itself (the runner ends the session when funding stops, as
today).

### Why a subclass and not a full copy

The SDK's signer side (`get_payment`, `_payment_request`, the signer state) is not affected and stays the
SDK's. Overriding two methods and replacing the loop is the smallest copy that closes both paths, in line
with John's approach in #61 (copy only what must change, import the rest). The cost is that
`_refresh_payment_params` is a private method: the fingerprint test (below) makes any SDK change to it, or
to `get_payment` which calls it, fail CI until someone re-reviews the override.

## Code areas

| File | Change |
| --- | --- |
| `adapters/runner_call.py` | `EnginePaymentSession`, `fund()`, `FundingEnd`, the payment client; `_payment_session` and `start_funding` use them; MIT notice already present (the overrides adapt `remote_signer.py` at `2017a80`) |
| `tests/unit/test_sdk_contract.py` | Add `remote_signer` to `_MODULES`; fingerprint `LivePaymentSession.send_payment`, `.run_payments`, `._refresh_payment_params`, `.get_payment` (support dotted `Class.method` names in the lookup) |
| `tests/unit/livepeer_net.py` | Loopback harness: let the renewal and refresh routes answer a chosen 3xx with `Location` to a second loopback server that records every request it receives |
| `tests/unit/test_native_runner_call.py` (or a new `test_payment_redirects.py`) | The tests below |
| `docs/references/sdk-gateway.md` | `run_payments`, `send_payment` and `_refresh_payment_params`: "not called since #72"; why |
| `docs/exec-plans/tech-debt-tracker.md` | Widen T18 (the engine owns renewal and refresh); note in T20 that the renewal loop is now the engine's, so the T20 fix is a few lines when needed; add a row for signer requests following redirects (below) |
| `docs/contracts/payments.md` | One rule under the paid call: no request in the payment plane follows a redirect |
| `docs/design-docs/m3-decisions.md` | The decision, numbered when locked |

## Test plan (test-driven)

**Level.** In-process against the loopback runner, signer and a second "redirect target" server
(`tests/unit/livepeer_net.py`), as the native runner-call tests already do. No mocks of aiohttp: the real
client meets a real server, which is the only way to prove that a redirect is not followed and that the
headers do not travel. The SDK-fingerprint check stays a structure-level unit test.

**Seams.** `interval_s` on the funding loop; the harness's per-route status; a request log on the target
server; the signer's call counter (each `/generate-live-payment` mints a ticket).

**Starter test.** A renewal answered `307` with `Location` on the target server: the target receives no
request, and the signer is asked for no further ticket. It is red on `releases/m3`.

```text
Defect (red on releases/m3 first)
[ ] renewal 307 to another origin: the target receives nothing; no further ticket is minted
[ ] renewal 301, 302, 303, 308: same (parametrized)
[ ] a redirected renewal never carries Livepeer-Payment or Livepeer-Segment to the target
[ ] refresh-payment 3xx: the target receives nothing; the signer never signs with refreshed params
[ ] a same-origin redirect is refused too (no 3xx is followed, whatever the Location)

Parity with the SDK loop (each should be [=] or nearly)
[ ] a 2xx renewal keeps funding at the interval
[ ] 404 ends funding as released
[ ] 403 and 409 end funding as refused; 408, 429 and 503 retry next cycle
[ ] a transport error retries next cycle
[ ] a 480 during renewal refreshes once and renews with the new params
[ ] cancelling the call stops funding and closes the payment client (no unclosed-session warning)

Guards
[ ] the funding end reason is recorded and logged (runner.funding_stopped)
[ ] a renewal URL outside the payment origins is refused without a request
[ ] the SDK fingerprints fail when any overridden or relied-on SDK method changes
```

**Gears.** Obvious Implementation for the client settings; Triangulate the loop's status rules against
the SDK's with the parity tests before deleting the call to `run_payments`.

**Definition of done.** The list is empty; the checks in `AGENTS.md` pass; the docs above are updated; a
metered paid job on the console's dev stack renews normally (a streamed chat longer than 3 s), recorded in
the PR; #72 closed with a link to the PR.

## Upstream dependencies

| Upstream | Needed? | Notes |
| --- | --- | --- |
| livepeer-python-gateway | **Ideally yes; vendored instead.** | The right fix is in the SDK: `allow_redirects=False` and 3xx-as-failure for `_post_empty`, `post_json` to orchestrator URLs and `call_runner`'s paid request, and stopping `run_payments()` on a 3xx. Reported privately on 29 October. The engine overrides the two methods and owns the loop, so it does not wait. **Exit:** when the SDK ships the fix and we re-pin, delete the overrides and call `run_payments()` again, unless T17 or T20 still need the engine's loop |
| livepeer/node | No | The signer's missing recipient check makes the refresh path dangerous, but closing the redirect closes the path. Not vendorable (a server), and not needed |
| clearinghouse-batteries | No | Not in this path |

## Out of scope, recorded

- **Signer requests follow redirects too** (`/generate-live-payment`, `/sign-orchestrator-info`, through
  the SDK's `post_json`), carrying `signer_headers`. The signer URL is the operator's own configuration,
  so the exposure is lower. New tech-debt row; same fix pattern if wanted later.
- **T17** (a released metered stream can end `succeeded`) and **T20** (stale signer state after a blank
  `segCreds`): both get easier once the loop is the engine's, but each stays its own change.

## Open questions to lock

**Moot:** #72 was closed as not planned before these were settled into a decision.

- **Q1.** On a redirected renewal, stop funding only (recommended), or also fail the in-flight call as
  `payment` so the caller sees it at once? Stopping funding lets the orchestrator end the session as it
  does today when funding stops.
- **Q2.** Include the signer-request hardening now, or leave it as the new tech-debt row (recommended)?
- **Q3.** Ship before the ISS-25 dry run (13 November): agreed?
