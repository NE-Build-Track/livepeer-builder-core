# Runtime evidence: paid jobs on public orchestrators, 14 October 2026

The 12 October run ([evidence](2026-10-12-dev-stack-paid-run.md#public-orchestrators)) couldn't pay
any public orchestrator: the node signer required `OrchestratorInfo.address` to equal
`TicketParams.recipient` (T10). Mike filed [livepeer/node#1](https://github.com/livepeer/node/issues/1),
and Josh fixed it in `f970be6`. This run repeats the public-orchestrator test on that commit, with
no shims.

| Component | Revision / identity |
| --- | --- |
| Engine | this repository at `23d8987` (`releases/m2`), dev-stack image |
| Signer | livepeer/node `f970be644c55e41f77ef3ee8f085f516074f4cc8` |
| Batteries | `ca09e47`, embedded Kafka |
| SDK | eliteprox/livepeer-python-gateway PR 5 head `dca9cd69` (T1) |
| Orchestrators | xode us-central, speedybird, open-pool eu-central, hky.eliteencoder; certificates pinned (D27) |
| Actor | `alice`, allocation `AaEx8zzgcXmF_CeYUZiSDA` (USD), from the 12 October run |

## Results

1. **Discovery.** The signer discovered all four orchestrators: 7 runners, all metered per second.
2. **Paid jobs succeed on public orchestrators.** Three jobs went engine → SDK → signer → Batteries
   webhook → orchestrator → runner, and each returned 200 with a real result:

   | Job | App | Orchestrator | `auth_id` | Manifest | Usage rows | Cost |
   | --- | --- | --- | --- | --- | --- | --- |
   | `4889174d` | `blueclaw/embeddings/ollama` | speedybird | `AaE6ZxRNf_mw9z_cu2xSDQ` | `6caec59f` | 1 | $0.0000277 |
   | `e822f790` | `blueclaw/chat/qwen3.8-27b` | xode | `AaE6ZxgZfVS7hRCMvqr_5g` | `324b35d5` | 3 | $0.000199 |
   | `1737a4a3` | `blueclaw/chat/qwen3.8-27b` | xode | `AaE6Z0JMdD2mK0sR6yd74A` | `eba2c1a3` | 4 | $0.000244 |

   On 12 October, the same embeddings call failed with 400 `incomplete or expired orchestrator
   payment params`.
3. **Attribution without the T9 shim.** Each attempt recorded its `auth_id` from the SDK's
   `on_payment_session` callback. After cost sync, all three jobs were `observed` and `delivered`.
   The chat jobs paid in several signing rounds (3 and 4 rows), all under one `auth_id`.
4. **Reconciliation balanced** for alice:
   - Batteries `spent` = **$0.003470427534712312**;
   - delivered = **$0.002470427534710618** (10 rows: 2 from 12 October, 8 from this run);
   - unattributed = **$0.001000000000001694** (job 1 of 12 October, which recorded no `auth_id`);
   - `balanced: true`.
5. **Manifest and `auth_id` line up.** Every usage row carries both `payment_session_id` (the
   `auth_id`) and `manifest_id`. In this run they map one-to-one: each job has one manifest and one
   `auth_id`, and no manifest appears on another job. Joining on `(allocation_id, manifest_id)`
   would have produced the same costs. This bears on T11, but three jobs don't establish that a
   manifest is unique to one payment challenge.

## Not covered

- A forged `manifest_id` and a rejected-ticket charge (open items in
  [m2-attribution-rerun](../../exec-plans/active/m2-attribution-rerun.md)).
- The hky and open-pool orchestrators: the engine chose xode for chat, and no job used open-pool's
  audio apps.
