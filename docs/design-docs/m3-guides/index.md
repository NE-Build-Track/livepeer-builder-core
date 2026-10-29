# M3 implementation guides

One guide per remaining M3 engine change: the architecture, the code areas, the test plan, the upstream
check and the open questions, so that whoever picks up the ticket can drive it to completion. Each is a
proposal until it is locked in review; locked rules move into [m3-decisions.md](../m3-decisions.md).

All guides were verified against builder-core `releases/m3` at `d3df806` on 29 October 2026. **None of
them needs a change to livepeer/node, livepeer-python-gateway or clearinghouse-batteries.** #72 is the
exception that moved upstream: it was closed as not planned, and its fix is
[livepeer-python-gateway#87](https://github.com/livepeer/livepeer-python-gateway/issues/87), consumed by a
future SDK pin bump.

## Review agenda (Mike and John)

In the order the work should land. Each row lists the questions to lock.

| # | Guide | Trackers | What it decides | Questions |
| --- | --- | --- | --- | --- |
| 1 | [iss-11-14-idempotency.md](iss-11-14-idempotency.md) | ISS-11, ISS-14 | Final repeat rules for jobs; an operation ledger for admin commands; four defects (B1: a repeated payment-key rotation revokes the live key) | Q1–Q5 |
| 2 | [72-payment-redirects.md](72-payment-redirects.md) | #72 (ISS-12, ISS-13, ISS-25) | **Superseded:** #72 closed as not planned; tracked upstream as livepeer-python-gateway#87. Kept for reference | — |
| 3 | [iss-10-rates.md](iss-10-rates.md) | ISS-10 | Rates as advertised (wei, unit, the runner's USD quote) with source and age; stale and missing marked; "Wholesale and retail" and pricing assumptions in `payments.md` | Q1–Q3 |
| 4 | [iss-12-payment-failures.md](iss-12-payment-failures.md) | ISS-12 | `Failure.reason` for not authorized, allowance exhausted and signer unavailable; REST status by reason; no signer URL or upstream text on builder responses | Q1–Q3 |
| 5 | [iss-15-history-and-results.md](iss-15-history-and-results.md) | ISS-15 | How inputs and results are accessed in M3; expired provider assets; history paging | Q1–Q3 |
| 6 | [iss-18-cost-reports.md](iss-18-cost-reports.md) | ISS-18 (partial) | Criteria check; `by_app` on the summary; the held feed recorded everywhere | Q1–Q2 |
| 7 | [iss-23-verification.md](iss-23-verification.md) | ISS-23 | A traceability spec with a doc guard; gap tests; one live probe | Q1–Q3 |
| 8 | [discovery-node-v1.0.4.md](discovery-node-v1.0.4.md) | ISS-09 follow-up (D43 review) | livepeer/node's standalone discovery and `--nodes`; keep `lpb discovery`, refuse aggregators as sources, stamp `observed_at` | Q1–Q4 |

ISS-13, ISS-20 and ISS-24 are the console's (Mike) and need review, not engine guides. ISS-25 (the funded
run) depends on 1 and 4 landing first; its dry run is due 13 November. Until the SDK pin includes #87, its
metered renewals still follow redirects; the paid orchestrators come from the stack's own static list.
