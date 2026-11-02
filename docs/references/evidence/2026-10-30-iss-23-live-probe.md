# Runtime evidence: the ISS-23 live probe on the console's stack, 30 October 2026

The first run of `scripts/live_probe.py` (D61, Q1 and Q3) against a real payment stack. It marks the
L rows of the [M3 traceability spec](../../product-specs/m3-traceability.md) `live`. The funded proof is
ISS-25.

| Component | Identity |
| --- | --- |
| Engine | `tasks/mz/iss-23-verification` at `ade673f` (#83, stacked on #82, #81 and #78), image built from that commit; `/readyz` reports `0.0.1.dev1+gade673f02` |
| Stack | builder-core-console's `deploy/` (the M3 dev stack, ISS-13): Batteries `ca09e47`, node signer `406b248`, the discovery service, `per_actor` mode, SQLite |
| Fund actor | `live-d57-1793312007-x`, provisioned in application `default` by the 29 October D57 live check |
| Command | `LPB_PROBE_URL=http://127.0.0.1:8092 LPB_PROBE_ADMIN_KEY=<admin> LPB_PROBE_FUND_ACTOR=live-d57-1793312007-x uv run --frozen python scripts/live_probe.py --yes` |
| Spend | One fund of 0.01 USD of allowance, repeated once under the same `operation_ref` and applied once. No paid job |

## Probe output

```text
PASS  the admin key is accepted: 200, expected 200
PASS  issue a key (actor): 201
PASS  issue a key (actor): 201
PASS  issue a key (auditor): 201
PASS  no credential: GET /v1/jobs: 401, expected 401
PASS  no credential: GET /v1/admin/keys: 401, expected 401
PASS  no credential: POST /v1/admin/costs/sync: 401, expected 401
PASS  actor key: GET /v1/admin/keys: 403, expected 403
PASS  actor key: GET /v1/admin/actors: 403, expected 403
PASS  actor key: POST /v1/admin/costs/sync: 403, expected 403
PASS  auditor key reads: GET /v1/admin/keys: 200, expected 200
PASS  auditor key: POST /v1/admin/costs/sync: 403, expected 403
PASS  auditor key: POST /v1/admin/keys: 403, expected 403
PASS  a job is recorded without payment: 920477e6-7ceb-49cb-b0e6-1f4112f307f9
PASS  another actor's operation_ref is a new job: 6c2ce105-513c-4b97-b4aa-5ac7162be4ef
PASS  the owner reads the job: 200, expected 200
PASS  another actor: GET the job: 404, expected 404
PASS  another actor: GET its cost: 404, expected 404
PASS  another actor's job list leaves it out: 200
PASS  fund: 200, expected 200
PASS  the repeat is a replay (Idempotent-Replayed: true): 200
PASS  a fund repeated under one operation_ref applies once: granted grew by 0.01, expected 0.01
PASS  revoke the actor key: 204, expected 204
PASS  a revoked key: GET /v1/jobs: 401, expected 401
PASS  revoke the probe's other keys
live probe lpb-probe-1793372128-6cc3ac against http://127.0.0.1:8092: 25 passed, 0 failed, 0 skipped
```

## Also observed on the same engine

Not part of the probe; run by hand to see the 30 October review changes on the real stack.

| Request | Answer | Decision |
| --- | --- | --- |
| Revoke a probe key a second time | `204`, as the first | D61 (6) |
| Revoke an unknown key | `400` | D61 (6) |
| `POST /v1/jobs` with a 17 MiB body and no credential | `413` `{"error": "the request body exceeds 16777216 bytes", "code": "request_too_large", "limit_bytes": 16777216}` | D59 (4b) |
| `GET /v1/jobs?before=2026-10-30T10:00:00` (no offset) | `422` | D59 |
| `GET /v1/jobs?before=2026-10-30T10:00:00+02:00` | `200` | D59 |
| `GET /v1/costs/summary?actor_id=live-d57-1793312007-x` | `by_app` is `{"blueclaw/chat/qwen3.8-27b": {"delivered": …}}`, kept by class | D60 |

## Left on the stack

Three keys for `lpb-probe-1793372128-6cc3ac` actors, all revoked; two failed `no_offering` jobs with no
payment; 0.01 USD more allowance granted to `live-d57-1793312007-x`.
