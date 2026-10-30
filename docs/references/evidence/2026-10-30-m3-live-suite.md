# Runtime evidence: the M3 live suite on the console's stack, 30 October 2026

Every engine-side M3 behavior that can run on one local stack, run live: payment failures by reason,
access and secrets, a discovery outage, streams (including an engine restart mid-stream), Batteries
down, and cost-sync replay. It backs the L rows of the [M3 traceability spec](../../product-specs/m3-traceability.md)
that cite it. The console's own runs (the Builder journey in a browser, Operator actions, sessions,
Sources and gaps) are recorded in builder-core-console.

| Component | Identity |
| --- | --- |
| Engine | a local merge of this branch at `4e9715e` (#78 → #81 → #82 → #83) and #77 at `53b12d7`, commit `9676260`, image built from it; the engine and the discovery service ran it |
| Stack | builder-core-console's `deploy/` (ISS-13): Batteries `ca09e47`, node signer `406b248`, `per_actor`, SQLite, real orchestrators on Arbitrum One |
| Runner | a script that drives the engine's REST API and the stack's containers (stop, start, restart); not committed, since it stops containers |
| Spend | under $0.01 of network cost for the suite (every paid job is a short chat; the day's total was $0.011) |

Each run below was green. Three checks first failed because of the runner, not the engine, and were
re-run: restarting Batteries left the signer on Batteries' old network namespace (the signer runs with
`network_mode: service:clearinghouse`, so it must be recreated, see the dev-stack README); `/readyz`
was read before the 30-second sync loop had run; and one stream ended before a fixed-delay restart.

## Run A: Payment failures by reason (ISS-12, D58)

| Check | Result | Seen |
| --- | --- | --- |
| allowance exhausted: 402, reason allowance_exhausted, no Retry-After | pass | (200, None), (200, None), (402, 'allowance_exhausted') |
| allowance exhausted: job failed, nothing paid | pass | failed |
| after the operator funds it, the same operation_ref runs again (D57 closed list) | pass | 200 new job f7c6a805-ea16-42bc-abd9-f079c5d5cc06 |
| paused allocation: 403, reason not_authorized, no Retry-After | pass | 403 not_authorized the signer refused payment: not authorized (HTTP 403) |
| revoked allocation: 402 not_provisioned, no reason | pass | 402 not_provisioned None |
| signer stopped (buffered): 503, signer_unavailable, Retry-After 10 | pass | 503 signer_unavailable retry-after=10 msg=the signer is unavailable: it could not be reached; nothing was signed |
| signer stopped: job failed, unpaid, no failover | pass | attempts 1 |
| signer stopped (streamed): 503, signer_unavailable | pass | 503 signer_unavailable |
| no response carries a payment key or the signer URL | pass | 0 bodies |

## Run B: Access: scoped keys, other actors, revoked auditor (ISS-08, ISS-23)

| Check | Result | Seen |
| --- | --- | --- |
| a key limited to discover is issued | pass | 201 |
| discover-only: GET /v1/runners is allowed | pass |  |
| discover-only: POST /v1/jobs is 403 | pass | 403 |
| discover-only: GET /v1/jobs is 403 | pass | 403 |
| discover-only: GET /v1/costs/summary is 403 | pass | 403 |
| discover-only: GET /v1/allowance is 403 | pass | 403 |
| actor b1 records a job (no runner, unpaid) | pass | 404 |
| another actor: GET /v1/jobs/{id} is 404 | pass | 404 |
| another actor: GET /v1/jobs/{id}/result is 404 | pass | 404 |
| another actor: GET /v1/jobs/{id}/cost is 404 | pass | 404 |
| another actor: POST /v1/jobs/{id}/usage is 404 | pass | 404 |
| auditor key reads admin keys | pass |  |
| a revoked auditor key is 401 on admin reads | pass |  |

## Run C: Secrets in responses and logs (ISS-08, ISS-12)

| Check | Result | Seen |
| --- | --- | --- |
| GET /v1/admin/keys and /keys/{id} carry no token | pass | 0 leaked |
| GET /v1/admin/actors carries no payment key (lpg_) | pass | 200 |
| engine and discovery logs carry no issued token and no payment key | pass | tokens in logs: []; lpg_ in logs: False |

## Run D: Discovery outage and rate provenance (ISS-09, ISS-10)

| Check | Result | Seen |
| --- | --- | --- |
| price_usd: the orchestrator does not send it (not dropped by the engine) | pass | runner price_usd [None]; orchestrator sends price_usd: False |
| discovery stopped: offerings go stale | pass | stale=True |
| discovery stopped: /readyz says discovery is not fresh | pass | {"discovery_fresh": false, "discovery_error": "[Errno -3] Temporary failure in name resolution"} |
| discovery back: fresh again, offering not stale | pass | fresh=True |

## Run E: Streams: pending mid-stream, several cost rows, a client leaving, an engine restart (ISS-14, ISS-16)

| Check | Result | Seen |
| --- | --- | --- |
| a long stream is read to the end | pass | 405 events |
| mid-stream the cost reads pending | pass | pending |
| the long stream settles observed with several cost rows | pass | observed rows=3 $0.000200097461755189 |
| a client that leaves the stream: the job ends abandoned (uncertain when paid) | pass | uncertain abandoned |
| an engine restart mid-stream: the job is resolved interrupted, uncertain when paid | pass | uncertain interrupted after 583 events |
| the interrupted paid stream's cost still settles | pass | observed $0.000244563564367453 |

## Run F: Batteries down after a paid job (ISS-17, ISS-18)

| Check | Result | Seen |
| --- | --- | --- |
| a paid chat succeeds before Batteries stops | pass | 200 |
| Batteries down: job still succeeded, cost pending | pass | succeeded pending |
| Batteries down: /readyz names the cost-sync error | pass | Batteries unreachable: [Errno -3] Temporary failure in name resolution |
| Batteries back: the cost settles observed | pass | observed $0.000111165256530661 |
| Batteries down: a manual cost sync is refused, not silent | pass | 503 {"error":"Batteries unreachable: [Errno -3] Temporary failure in name resolution"} |

## Run G: Cost sync replay: repeated syncs and a restart (ISS-17)

| Check | Result | Seen |
| --- | --- | --- |
| three syncs and a restart leave every total and reconciliation unchanged | pass | {"live-d57-1793312007-x": {"balanced": true, "by_class": {"delivered": {"fee_eth": "0.000002953829301810", "fee_usd": "0.006573332202967040", "row_count": 8}, " |
