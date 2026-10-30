# ISS-23: Verification of access, repeated commands and accounting

**Status:** proposal for discussion, 29 October 2026.\
**Tracker:** ISS-23 ([Network-Engineering-SPE#36](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/36)).\
**Owner:** John (engine). **Reviewers:** Mike, John.\
**Verified against:** builder-core `releases/m3` at `d3df806` (1,350 tests collected; the mapped files pass,
with the PostgreSQL store runs skipped locally and run in CI).

## Done when (from the 21 October M3 plan)

Automated checks cover cross-actor access, revoked keys, duplicate submissions and admin commands,
authorization denial, and late or duplicate cost observations, **each marked as fixture-based or live**.
Many exist from M2; this maps them to the list and fills the gaps. Funded-path evidence is ISS-25;
broader recovery is ISS-35.

## Current state (verified)

**Every automated check is fixture-based.** The one pytest marker is `postgres` (store tests on both
backends). `tests/unit/livepeer_net.py` is a loopback orchestrator and signer: it drives the engine's
real runner-call code but no real network. Live evidence exists only outside the suite: builder-core's
`docs/references/evidence/` (M2 paid runs) and the console's run log (`console-m3.md`: 21 October paid
build tests, the 26 October engine probe 22/22, a revoked key answering 401), all before `d3df806` and
none committed as a runnable check.

**No document maps the checks.** `m2-traceability.md` is M2-only and lists ISS-23 as carried forward.

### Seed mapping (fixture-based, file:line at `d3df806`)

Levels: U unit (fakes, in-memory SQLite), S store contract (SQLite and PostgreSQL), H HTTP route, N
loopback network, P property test.

| Category | Existing checks |
| --- | --- |
| Cross-actor access | U `test_jobs.py:514`, `test_ownership.py:75,105,124`, `test_access.py:31,310`, `test_payments.py:140,328`, `test_fake_engine_contract.py:193`; S `test_contract.py:66,244`; H `test_http_routes.py:259` |
| Revoked credentials | U `test_access.py:59,141,162`, `test_payments.py:209,220`, `test_service_edges.py:88`, `test_jwt.py:183,224,551,638`; S `test_contract.py:88,118`; H `test_http_routes.py:42,103,225` |
| Duplicate job submissions | U `test_jobs.py:448`, `test_streaming.py:293`, `test_fake_engine_contract.py:285`; P `test_properties.py:114`; S `test_contract.py:66`, `test_edges.py:59`; H `test_http_routes.py:149` |
| Duplicate admin commands | U `test_payments.py:63,100,163`, `test_service_edges.py:88`, `test_adapters.py:37`; S `test_contract.py:118`; H `test_http_routes.py:225` |
| Authorization denial | U `test_access.py:31,50,77,100,120,162,263–297`, `test_ownership.py:54,65,97`, `test_fake_engine_contract.py:150,183`, `test_jwt.py:165,173,492,869`; H `test_http.py:31,63`, `test_http_routes.py:78,92,259,317` |
| Late or duplicate cost observations | U `test_costs.py:74,116,134,222,316,342,405,436–676`, `test_service_edges.py:195`, `test_jobs.py:530`, `test_payment_evidence.py:76–207`; N `test_signing_intent.py:172,228`; P `test_properties.py:61,80`; S `test_contract.py:139,271,311,364,413,458,487–580` |

## Decision proposed

**Publish the mapping as a traceability spec with test names, guard it with a doc test, fill the gaps
with fixture-based tests, and add one live check that runs against a real stack.**

### The traceability spec

`docs/product-specs/m3-traceability.md`, mirroring the M2 file, linked from `product-specs/index.md`:

| Column | Content |
| --- | --- |
| Category | One of the six above |
| Behavior | One line, in the builder's or operator's words |
| Check | `tests/...::test_name` (names, not line numbers, so it survives edits) |
| Level | U, S, H, N, P, or **L** (live) |
| Evidence | `fixture` or `live` |
| Ticket | ISS-23, or the guide that owns it (ISS-11/14, ISS-12), ISS-25 for funded proof, ISS-35 for recovery |

**Guard:** a check in `tests/structure/test_docs.py` (or `scripts/check_docs.py`) that every
`tests/...::name` cited in a traceability file exists. Nothing validates test names in docs today.

### Gap tests (fixture-based)

Rows owned by other guides are cited, not repeated: B1–B4, repeated jobs and admin commands
([iss-11-14-idempotency.md](iss-11-14-idempotency.md)); the three payment refusals and the leak checks
([iss-12-payment-failures.md](iss-12-payment-failures.md)).

```text
Cross-actor
[ ] Bob reuses Alice's operation_ref: his own job runs; Alice's job id never appears (engine and HTTP)
[ ] cost summary, recent costs and allowance exclude another actor's fees
Revoked
[ ] a job after revoke-allocation fails not_provisioned with no transport call
[ ] a revoked admin key and a revoked auditor key get 401 on admin routes
Duplicate admin commands
[ ] repeated set-status, revoke-allocation and revoke-key are no-ops (service and HTTP)
Authorization
[ ] route sweep: every /v1/admin/* write answers 403 to actor and auditor keys and 401 with no credential
Cost observations
[ ] Batteries rows with status duplicate or ignored are never counted
[ ] a re-delivered row id with a different fee is ignored and logged     [?] Q2
[ ] a late row for a job purged by retention stays unattributed
```

### One live check

Commit the console's engine probe (today a manual run) as a script in builder-core,
`scripts/live_probe.py`, run against a stack named by environment variables and skipped otherwise. It
issues an actor key, checks cross-actor 404s and admin 403s, revokes the key and expects 401, and repeats
a fund under one reference. It spends at most one funding move of a few cents and no paid jobs (paid
jobs are ISS-25). Its output is recorded as the `live` evidence rows (Q1).

## Test plan notes

**Levels and seams** follow the existing suite: real `SqlStore` on SQLite (PostgreSQL in CI),
`FakeAllocationManager`, `FixedClock`, the loopback network for signer behavior. The route sweep iterates
the FastAPI route table, so a new admin route is covered without editing the test.

**Starter test:** the route sweep. It is cheap, it covers a whole category, and it fails loudly if a
future admin route forgets its permission check.

**Definition of done.** `m3-traceability.md` lists every category with at least one check each, every
row marked fixture or live; the doc guard passes; the gap tests are green; the live probe has run once
against the console's stack with its output recorded; the checks in `AGENTS.md` pass.

## Upstream dependencies

None needed. Some fixture-based checks stand in for upstream behavior: `FakeAllocationManager` copies
Batteries' replay rules, usage-row statuses and key refusals are Batteries', and the loopback signer
imitates livepeer/node's 402 challenge and refusals. The live probe is what checks those against the
real services at the pinned commits.

## Open questions to lock

- **Q1.** Add the live probe to builder-core (recommended), or keep live checks in the console repo?
- **Q2.** A re-delivered usage row id with a different fee: keep ignoring it (log it), or treat it as a
  correction?
- **Q3.** Is one live probe enough for "marked live", with ISS-25 as the funded proof?
