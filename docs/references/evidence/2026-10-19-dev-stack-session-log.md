# Runtime evidence: session-log attribution on NE-Build-Track pins, 19 October 2026

The 14 October run ([evidence](2026-10-14-dev-stack-public-orchestrators.md)) matched usage to jobs
by `auth_id`. On 16 October, #9 replaced that with the engine's session log: the SDK reports each
signed `manifest_id` (livepeer-python-gateway#77), and cost sync resolves it to a Batteries
`payment_session_id` (D20). This run is the live check that PR #9's test plan left open, on the pins
in `releases/final`.

| Component | Revision / identity |
| --- | --- |
| Engine | this repository, branch `tasks/mz/ne-build-track-pins` from `releases/final` `c7d136a`; schema `0004` |
| Signer | NE-Build-Track/node `406b248` (v1.0.2) |
| Batteries | NE-Build-Track/clearinghouse-batteries `ca09e47`, embedded Kafka |
| SDK | NE-Build-Track/livepeer-python-gateway `e6d243b` (head of livepeer-python-gateway#77) |
| Orchestrators | speedybird, open-pool eu-central, hky.eliteencoder; certificates pinned (D27, T12) |
| Actor | `alice`, allocation `AaFU0jUbf5uTT2LYrVohTg` (USD), funded with $1 from grant `AaFU0hc3fCGCGyo_t3a8bA` |

The dev-stack volumes had been removed since 14 October, so this run started from an empty Batteries
ledger, signer state and engine database. The engine applied migrations `0001`–`0004` on start.

## Results

1. **Discovery.** The signer discovered three orchestrators. xode's `/discovery` listed no runners
   at the time, so the signer left it out.
2. **Grant funding (D28).** Provisioning alice with the grant on the request created the allocation
   and funded it through `grants.fund` on the engine credential (201).
3. **Paid jobs.** Three jobs went engine → SDK → signer → Batteries webhook → orchestrator → runner,
   all on speedybird, and each returned 200 with a real result:

   | Job | App | Manifest | `payment_session_id` | Session log | Cost |
   | --- | --- | --- | --- | --- | --- |
   | `6b002539` | `blueclaw/embeddings/ollama` | `c8c70a1d` | `AaFU0m3efym5UdJnMfoEdQ` | `matched` | $0.0000277 |
   | `77d9d55a` | `blueclaw/chat/qwen3.8-27b` | `c7346619` | `AaFU0nneet29l16lfWV7oA` | `matched` | $0.000111 |
   | `f8944f1e` | `blueclaw/chat/qwen3.8-27b` | `d7a8f3d8` | `AaFU0oWscXS0Z8SN7sQjlw` | `matched` | $0.000111 |

4. **Attribution through the session log.** Each attempt logged one manifest. Cost sync resolved
   each entry to exactly one Batteries payment session and marked it `matched`. All three jobs are
   `observed`, and each attempt is `delivered`.
5. **Reconciliation balanced** for alice:
   - Batteries `spent` = **$0.000249372050891031**;
   - delivered = **$0.000249372050891031** (3 rows);
   - unattributed, failed after payment, uncertain and pending = 0;
   - `balanced: true`.
6. **A duplicate usage row is ignored.** Batteries recorded a fourth usage row with status
   `duplicate` and no payment session (a Kafka redelivery). Only `applied` rows count, so it changed
   no cost.

## Not covered

- A forged `manifest_id` and a rejected-ticket charge (open items in
  [m2-attribution-rerun](../../exec-plans/active/m2-attribution-rerun.md)).
- An ambiguous manifest (T13): no manifest mapped to more than one session in this run.
- xode, open-pool and hky: no job was routed to them.
