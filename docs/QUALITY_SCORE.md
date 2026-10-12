# Quality score

Grades: **A** done and tested · **B** works, with known gaps · **C** defined only · **D** missing.
Update this file in the same change that moves a grade.

| Domain / layer | Grade | Evidence | Gap |
| --- | --- | --- | --- |
| Contracts | A | Frozen models; structural money/float check | — |
| Access | B | `test_access.py` (policy, keys, chain, bootstrap) | JWT and trusted backend are C (M4) |
| Discovery | B | `test_adapters.py`, unit jobs tests | Capability resolution (T3) |
| Jobs (immediate) | A | `test_jobs.py`: failover, uncertain, write ordering, recovery, admission, idempotency | Idempotency finalized by John (T2) |
| Jobs (async), sessions | C | Contracts and 501 tests | M4 |
| Payments | A | `test_payments.py`: explicit provisioning, idempotency, custody, rotation | Mode B (M4) |
| Costs | A | `test_costs.py`: classification, corrected, reconciliation, cursor | — |
| Events | C | Contract and 501 test | M3 |
| Store | A | Store suite on SQLite and PostgreSQL; migrations, drift, restart, backup, retention | — |
| HTTP | B | `test_http.py` | Metrics endpoint (T4) |
| CI | A | Green on PRs; coverage, per-layer and diff-coverage gates | — |
| Docs | B | `scripts/check_docs.py` | The doc-gardening routine isn't automated yet |

## Test coverage

Branch coverage is measured on every CI run. Three gates hold it, and any one can fail the build:

| Gate | Floor | Where it lives |
| --- | --- | --- |
| Whole package | 95% | `[tool.coverage.report] fail_under` in `pyproject.toml` |
| `services`, `store`, `contracts` | 95% | `FLOORS` in `scripts/check_coverage.py` |
| `adapters`, `runtime`, `http` | 90% | `FLOORS` in `scripts/check_coverage.py` |
| Lines changed by a pull request | 90% | `diff-cover` step in `.github/workflows/ci.yml` |

The baseline when the gates went in was 98% overall, and at least 97% in every layer.
Floors only go up. Raise one when a layer has stayed well above it, and never lower one to make a change pass.
Cover the gap, or explain in the pull request why the line can't be reached.

How the suite is built:

- **Fakes, not mocks, at the seams.** `livepeer_builder.testing` fakes the transport, the usage feed,
  the payment manager and the clock. Tests drive real services through the engine.
- **Real storage.** Store tests run against SQLite files and, in CI, PostgreSQL 16. In-memory
  SQLite is only used where persistence is not under test.
- **Wire formats at the boundary.** Batteries and the SDK are exercised through `httpx.MockTransport`
  and monkeypatched SDK entry points. Tests assert on the requests themselves: route, body and
  `Idempotency-Key`.
- **Properties for money.** Hypothesis checks that fees add up however usage is split, that every
  attributed charge is counted once, that missing usage is `pending` and never zero, and that money
  survives the store exactly.
- **No sleeps for ordering.** Concurrency tests coordinate with `asyncio.Event`. Background-loop
  tests use short intervals and assert on outcomes.
- **Coverage follows async code.** `concurrency = ["greenlet", "thread"]` is set because SQLAlchemy runs
  queries in greenlets.
- `src/livepeer_builder/store/migrations/env.py` is omitted. Alembic loads it by file path; the
  migration tests exercise it.
