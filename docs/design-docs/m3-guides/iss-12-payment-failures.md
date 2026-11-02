# ISS-12: Distinct payment failures and no payment key on builder responses

**Status:** proposal for discussion, 29 October 2026.\
**Tracker:** ISS-12 ([Network-Engineering-SPE#25](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/25)).\
**Owner:** John (engine). **Reviewers:** Mike, John.\
**Verified against:** builder-core `releases/m3` at `d3df806`; livepeer/node `406b248` (the console stack's
pin) and `e75e5b6`; Clearinghouse Batteries `ca09e47`; livepeer-python-gateway `2017a80`.

## Done when (from the 21 October M3 plan)

Tests show "not authorized", "allowance exhausted" and "signer unavailable" as distinct failures, and no
builder-facing response carries a payment key. (Paid execution has worked since 14 October; this is
checking and tests. Funded proof is ISS-25.)

## Current state (verified)

**Upstream decides the status, the engine flattens it.** Batteries answers the signer's auth webhook
with a decision: 401 (invalid or revoked key), 403 (grant or allocation inactive, binding mismatch), 402
(allocation exhausted) (`internal/store/auth.go`). The node signer passes that status through as its own,
with the body "signer auth rejected request with status N" (`signer/auth.go:115`), and answers 502 when
the webhook is unreachable and 503 when busy. The engine then maps:

| Case | Engine today | Builder sees |
| --- | --- | --- |
| Not authorized (signer 401/403) | `Failure.kind = payment`, manifest `unpaid`, no failover | 503, `Retry-After: 10` |
| Allowance exhausted (signer 402) | same as above | **same** 503, `Retry-After: 10` |
| Signer 5xx or lost reply **while signing** | `payment_unknown`, manifest `unknown`, job `uncertain` | 503, no `Retry-After` |
| Signer refuses connections at `sign-orchestrator-info` (before anything is signed) | `unreachable`, **fails over to every runner** | 503, `Retry-After: 5` |
| Signer 5xx at `sign-orchestrator-info` | `payment` | 503, `Retry-After: 10` (same as 402) |
| Engine's own `not_provisioned` (no or revoked allocation) | `not_provisioned` | **402** |

So the three cases are not distinguishable by kind or status: 401, 402 and 403 differ only in
`failure.status_code`, which the contract documents as "the runner's own status". `Retry-After` invites
a retry that cannot succeed for 401, 402 and 403. "Signer unavailable" splits three ways, and one of them
fails over across every runner although the runners are not the problem.

**Leaks.** `Failure.message` is `str(LivepeerHTTPError)`, which carries the signer URL and up to 2,000
characters of the signer's body, and reaches builders on the job problem and `GET /v1/jobs/{id}`.
`signer_url` is an unvalidated string (`config.py`), so userinfo or a token in it would leak.
`GET /v1/allowance` maps a Batteries error to `ProviderError` with Batteries' raw text. Guards that
exist: no `lpg_` key on provision and rotate responses, keys encrypted at rest, signer logs carry only the
error type.

**Tests.** 402 and 403 are tested at the adapter and job level; **401 has no test**; no test covers a
signer 502 or a 5xx at `sign-orchestrator-info`; no route test asserts the REST shape of any signer
refusal; no test asserts that builder-facing bodies carry no signer URL or upstream text.

## Decision proposed

**Keep `Failure.kind` for behavior (failover, cost) and add `Failure.reason` for explanation.** The three
refusals behave the same (no failover, unpaid), so they stay `kind = payment`; what differs is what the
builder should do, which a reason states.

### Contract (`contracts/jobs.py`)

`Failure.reason: PaymentReason | None`, set only for payment kinds:

| `reason` | From | `kind` | Job | REST | `Retry-After` |
| --- | --- | --- | --- | --- | --- |
| `not_authorized` | signer 401 or 403 | `payment` | failed | **403** | none |
| `allowance_exhausted` | signer 402 | `payment` | failed | **402** | none |
| `signer_unavailable` | signer unreachable or 5xx **before** a manifest is stored (`sign-orchestrator-info`, or connection refused before `get_payment`) | `payment` | failed | 503 | 10 |
| `signer_unknown` | signer 5xx, lost reply or cancel **while** signing | `payment_unknown` | uncertain | 503 | none |

`not_provisioned` keeps 402 with no reason; its `kind` tells it apart from `allowance_exhausted`.

A paused allocation surfaces as `not_authorized`: Batteries answers 403 and the signer does not forward
the reason. Documented as an upstream limit, not fixed here.

### Engine changes

- `adapters/runner_call.py` `signer_error`: map 401 and 403 to `not_authorized`, 402 to
  `allowance_exhausted`; wrap `get_signer_info` (today outside `_sign`) so a connection error or 5xx there
  becomes `payment` / `signer_unavailable`, **with no failover** (every runner shares the signer).
- `services/failover.py`: `signer_unavailable` does not fail over (same rule as `payment`).
- **Engine-written messages for payment failures.** `Failure.message` says what happened in the engine's
  words ("the signer refused payment: allowance exhausted (HTTP 402)"). The upstream text goes only to
  the log, as today. `Failure.body` stays the runner's own words and is never set from the signer.
- `http/app.py`: status and `Retry-After` by reason (table above).
- `config.py`: refuse a `signer_url` with userinfo, a query or a fragment at startup.
- `GET /v1/allowance` (and any builder-facing route): map `ProviderError` to a fixed message with the
  provider's status; the provider text goes to the log.
- `testing/fake_engine.py`: the same reasons; contract test parity.

### Docs

- `engine.md#failures`: add the `reason` column and a builder-facing "what it means / what to do" column
  (shared with ISS-15); fix `payment` ("402 or 403" omits 401 and the unstored-manifest case).
- `payments.md`: who answers what (Batteries → signer → engine), the paused-allocation limit, and that
  no builder-facing body carries a payment key, a signer URL or upstream text.

## Test plan (test-driven)

**Levels.** Adapter tests against the loopback signer (`tests/unit/livepeer_net.py`) for the status
mapping; job-level tests for failover and state; one HTTP route test per reason for status,
`Retry-After` and body; a leak test that scans every builder-facing body. No mocks of aiohttp.

**Starter test.** A signer 401 gives `kind=payment`, `reason=not_authorized`, unpaid, no failover (there is
no 401 test today).

```text
[ ] signer 401 → payment / not_authorized, unpaid, no failover
[ ] signer 403 → payment / not_authorized
[ ] signer 402 → payment / allowance_exhausted
[ ] signer refuses connections at sign-orchestrator-info → payment / signer_unavailable, no failover
[ ] signer 502 at sign-orchestrator-info → payment / signer_unavailable
[ ] signer 502 while signing → payment_unknown / signer_unknown, job uncertain (exists: keep)
[ ] REST: not_authorized 403, allowance_exhausted 402, signer_unavailable 503 + Retry-After, signer_unknown 503 without
[ ] REST: not_provisioned stays 402 with no reason, distinct from allowance_exhausted
[ ] no builder-facing body contains lpg_, an Authorization value, the signer URL or signer body text
[ ] a signer_url with userinfo or a query is refused at startup
[ ] /v1/allowance with a Batteries error returns a fixed message, no provider text
[ ] FakeEngine reasons match
[ ] the docs' failure table lists every kind and reason (doc test)
```

**Definition of done.** The list is empty; the checks in `AGENTS.md` pass; `engine.md`, `payments.md` and
OpenAPI updated; console and enterprise desk updated to show the reason.

## Upstream dependencies

None needed. The distinction relies on behavior the pins already have: Batteries' 401/402/403 decisions
and the signer passing the status through (`signer/auth.go:115`, same at `406b248` and `e75e5b6`). Two
upstream limits are documented, not fixed: the signer drops Batteries' reason (paused and invalid-binding
both read 403), and Batteries being down (502) looks like any signer 5xx. Neither is vendorable (both are
servers).

## Consumer impact

The new `reason` field and the 402/403 statuses are additive in the body but change status codes for
signer refusals (503 → 402/403). The console's failure view, ChatLPT and the enterprise desk should branch
on `reason`. Embedded callers see the same `JobFailed` with a richer `Failure`.

## Open questions to lock

**Locked by Mike, 29 October 2026:** Q1 `Failure.reason`; Q2 status by reason (403 / 402 / 503); Q3 a
refused connection (connect phase only) is `signer_unavailable`. The rules become
[D58](../m3-decisions.md).

- **Q1.** `Failure.reason` (recommended) or new `Failure.kind` values?
- **Q2.** REST status by reason (403 / 402 / 503), or keep 503 for every payment failure and rely on the
  body?
- **Q3.** Treat "connection refused before `get_payment`" as `signer_unavailable` (nothing could be
  signed), instead of today's conservative `payment_unknown`?
