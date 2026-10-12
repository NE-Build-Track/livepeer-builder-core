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
| CI | B | Workflow written | Not yet run (nothing pushed) |
| Docs | B | `scripts/check_docs.py` | The doc-gardening routine isn't automated yet |
