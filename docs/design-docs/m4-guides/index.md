# M4 implementation guides

One guide per M4 tracker (M4: reuse and reliability): the architecture, the code areas, the test plan,
the upstream check and the open questions. Each is a proposal until it is locked in review with Mike and
John; locked rules move into a decision record. Written 29 October 2026 and verified against
builder-core `releases/m3` at `d3df806`, livepeer/node `406b248` (the pin then; `f45d83f` since D63) and `a4b68ed`, the SDK at
`2017a80`, and Clearinghouse Batteries `ca09e47`. The ISS-46 guide was added on 2 November and verified
against `releases/m3` at `1514212`.

**Every question was locked on 12 and 13 November 2026.** Each guide now ends with a "Questions locked"
section that holds Mike's answers, and it says where an answer replaces the guide's recommendation. The
answers that did:

- **ISS-27:** the engine sends `start` and `end` frames on a relayed socket, as #73 proposes, and a
  repeated open returns the existing session.
- **ISS-29:** RFC 9457 problem bodies, an opaque paging cursor, and the grant funding route renamed with no
  alias. All three are breaking changes for REST clients.
- **ISS-46:** any event retention is allowed, with no required relation to job retention.
- **ISS-32:** the trusted-backend authenticator is not built in M4; the commerce fixture consumes the events
  feed from the start; the router split (#25) lands before ISS-26.
- **ISS-33:** admin comes from a dedicated token claim, and the scope denylist goes.
- **ISS-35:** the wrong-match defect is fixed, and a total of purged fees is kept. A test on 13 November
  confirmed that reconciliation otherwise stops balancing.

**Related tickets, all builder-core issues on the board under M4.** The hardening and upkeep tickets, #112
to #117, come from the review of the `v0.2.0` release, and the hardening fixes merge before any feature
work. The stretch tickets, #118 to #124, hold the parts of #73 and #86 that the ISS-27 and ISS-46 guides
left out; any of them may move to M5.

**The runner standard.** ISS-47 (runner contract v1) is not vetted yet. ISS-48, its adoption in the engine,
is the last feature of M4 and may move to M5, and it has no guide here yet. ISS-26 to ISS-30 no longer
depend on it: they route by app ID, as today, and each guide notes where the standard plugs in later.

**No M4 guide needs an upstream change.** ISS-27 vendors two SDK functions (the reserve-response parser
and `stop_runner_session`), fingerprinted as D53 did. Per-actor allocations under a separately operated
payment service would need grant-scoped Batteries credentials, which cannot be vendored; ISS-31 works
around it.

## Review agenda

In dependency order. M4 builds on the M3 guides ([../m3-guides/index.md](../m3-guides/index.md)),
especially idempotency (ISS-11/14) and payment failures (ISS-12). #72 moved upstream
([livepeer-python-gateway#87](https://github.com/livepeer/livepeer-python-gateway/issues/87)): renewals stay
the SDK's `run_payments()`, and ISS-27 and ISS-28 read its result instead of owning a loop.

| # | Guide | Owner | Depends on | What it decides | Questions |
| --- | --- | --- | --- | --- | --- |
| 1 | [iss-26-async-jobs.md](iss-26-async-jobs.md) | John | ISS-14, ISS-15, ISS-19 | A database-backed queue and an in-process worker; stored results with their own retention; polling, no webhooks yet; restart rules | Q1–Q7 |
| 2 | [iss-27-persistent-endpoints.md](iss-27-persistent-endpoints.md) | John | ISS-14, ISS-19 | Sessions as jobs holding one paid reservation; #73's reserve and relay, narrowed; the SDK funding loop's result read, not dropped | Q1–Q8 |
| 3 | [iss-28-failure-recovery.md](iss-28-failure-recovery.md) | John | ISS-15, ISS-26, ISS-27 | `Failure.kind` stays the fact; derived `category` and `recovery`; one table across all four modes; T17 | Q1–Q4 |
| 4 | [iss-31-payment-configurations.md](iss-31-payment-configurations.md) | John | ISS-13 | The second configuration is a separately operated payment service (`per_application`, read-only operator credential, no money moves from the engine) | Q1–Q4 |
| 5 | [iss-29-rest-coverage.md](iss-29-rest-coverage.md) | John | ISS-18, ISS-20, ISS-28 | A `code` on every error; a REST contract with a coverage test; keyset paging; a compatibility check against the last tag | Q1–Q5 |
| 6 | [iss-30-mcp.md](iss-30-mcp.md) | John | ISS-29 | An MCP adapter beside `http/`, streamable HTTP, mounted by `lpb serve` under an `[mcp]` extra; the same authorization as REST | Q1–Q7 |
| 7 | [iss-32-embedding-and-extensions.md](iss-32-embedding-and-extensions.md) | John | ISS-07, ISS-29, ISS-30 | "Proved" = a release-candidate tag, examples installed from its wheel in CI, every consumer pinned to it; the trusted-backend authenticator | Q1–Q6 |
| 8 | [iss-33-access-flow-replacement.md](iss-33-access-flow-replacement.md) | Mike | ISS-21, ISS-32 | The console's OIDC replacement is built; commit a repeatable probe and show a second application | Q1–Q4 |
| 9 | [iss-34-reference-app-coverage.md](iss-34-reference-app-coverage.md) | Mike | ISS-22, ISS-28, ISS-29 | The console adds async, sessions, failure and "result unavailable" views; one committed browser smoke test | Q1–Q4 |
| 10 | [iss-35-isolation-recovery-replay.md](iss-35-isolation-recovery-replay.md) | John | ISS-06, ISS-23, ISS-28–32 | Isolation sweep, crash recovery, accounting replay (`lpb costs verify`), migration and restore | Q1–Q3 |
| 11 | [iss-36-funded-matrix.md](iss-36-funded-matrix.md) | Mike | ISS-19, ISS-25, ISS-31–35 | Seven matrix rows, one evidence record per row, a spend cap, the ISS-25 runbook | Q1–Q4 |
| 12 | [iss-46-events-feed.md](iss-46-events-feed.md) | John | ISS-18 | One append-only `lpb_events` table written in the change's own transaction; sequence order equals commit order; the pull feed is the acceptance, push a later slice | Q1–Q8 |

## Findings to raise in review

Found while writing these guides and checked against the code; each is handled in the guide named.

- **A job sent to a persistent app reserves a paid session and hands the caller `app_url` and
  `control_url`**, against D9. The engine never filters runners by `mode`, and the orchestrator's reserve
  answer carries both URLs; its client stop route needs no credential (`406b248`). ISS-27, bug P1.
- **A job whose final save fails after a successful runner answer is billed `delivered`**: the cost class
  follows the attempt, not the job. `finish_job` has no state guard. ISS-28.
- **FakeEngine runs every job as `immediate`**, and `JobService.submit` is not on the public facade. ISS-26.
- **The docs disagree on the second payment configuration**: the console calls it `per_application`; the
  plan (P4) means a separately operated payment service. ISS-31.
- **Two retention risks to test first:** purging usage rows can turn an ambiguous match into a wrong one,
  and purging a job moves its delivered fees to `unattributed`. ISS-35.
- **Nothing reads `Livepeer-Actor`**, so a service credential with `actors.assert` does nothing yet. ISS-32.
- **The console decides state by matching error text** ("no allocation"). ISS-29's `code` field.
- **The events feed moved to M4 as ISS-46** (2 November; ISS-18 closed for M3 on its cost reports). Until it lands,
  `ReservedEventFeed` still answers 501 naming ISS-18 (D60). ISS-26, ISS-29 and ISS-32 each have a row that waits for it.
