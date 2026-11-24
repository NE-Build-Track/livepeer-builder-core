# Runtime evidence: the payment clearinghouse's paid proofs on the direct signer, 23 November 2026

Proofs (a) and (b) of ISS-31's last ticket (#169), run again on the design in which an enterprise's
engine signs at the clearinghouse's published signer itself, with the key the clearinghouse handed over
(D85 point 1 and D87 points 1 and 3, as amended on 19 and 23 November 2026). The run of 19 November used
the withdrawn design; what of it still stands is at the top of
[its record](2026-11-19-payment-clearinghouse-proofs.md).

The run followed the proof runbook in [deploy/README.md](../../../deploy/README.md#proof-runbook), steps
0 to 6, on one host, from new volumes. It ran from 00:26 to 00:36 UTC on 24 November 2026, which is the
evening of 23 November where it was run.

| Component | Identity |
| --- | --- |
| Engine | this branch at `978eace`; one image built from it ran both engines |
| Clearinghouse | `deploy/clearinghouse`: the engine in its `clearinghouse` role with two listeners, Batteries `03bc9fb` with its on-chain listener on, node signer and discovery `eee09c4`, and the relay that publishes the signer |
| Enterprise | `deploy/enterprise`: the engine in its `work` role, with the clearinghouse's address, a tenant admin key, the published signer's address and its own secret key. No Batteries |
| Network | real orchestrators on Arbitrum One, and one funded wallet. One signer used it |
| Spend | $0.001228 charged to one allocation |

No address, credential or tenant id is recorded here. Every access credential stayed in a git-ignored
file, the wallet's files were mounted read-only and never copied, and no allocation's key was printed or
kept by a command.

## Before any job

| Check | Result | Seen |
| --- | --- | --- |
| The clearinghouse project's services and published ports | pass | Batteries healthy; the signer, the relay, discovery and the engine up; two ports on the host's loopback, the engine's public listener and the signer, and no other |
| `lpb payments check` on the clearinghouse | pass | role `clearinghouse`; three probes ok: the signer, Batteries, the grants credential; exit 0 |
| A job route and a signer path on the clearinghouse's engine | pass | `POST /v1/jobs` is 404, and `POST /sign-orchestrator-info` is 404: the engine signs for nobody |
| The published signer | pass | `GET /sign-orchestrator-info` at the published address answered 405, as a signer does, and its own readiness port, published nowhere, answered 200 |
| The wallet report, on the private listener | pass | one escrow account with its deposit and reserve; `signer.ready` true, read through the transport that cannot pay |
| Two tenants onboarded on the private listener, $0.50 each | pass | each `total` 0.5, `unallocated` 0.5, `spent` 0 |
| The clearinghouse admin's key on the public listener's tenant list | pass | 404 |
| Tenant two allots its own actor on the public listener, $0.25 | pass | `active`, `credential_custody` `work_engine`, and the answer handed a key over |
| The same allotment repeated | pass | the same allocation, no key, `replayed` true |
| What the clearinghouse stores after it | pass | 0 secrets; its record says the work engine keeps that actor's key |
| `lpb payments check` on the enterprise's engine, as tenant one | pass | role `work`; four probes ok: the grant, the usage, the signer at the published address, the runner list; exit 0 |
| A clearinghouse route on the enterprise's engine | pass | 404 |
| The enterprise's engine reads offerings from the clearinghouse's runner list | pass | 25 offerings with rates, none stale |
| The enterprise allots its actor, $0.25 | pass | `active`, the key kept by `secret_store` |
| **Where that key is** | pass | the enterprise's store holds 1 secret and its record says `secret_store`; the clearinghouse's store holds 0 and its record says `work_engine`; the read of the allocation at the clearinghouse carries no key |

## Proofs (a) and (b): jobs signed at the published signer

Every job was run by tenant one's actor on the enterprise's engine, which signed each payment at the
published signer with the key it stored at allotment. The clearinghouse's engine serves no signer path,
so nothing else could have signed them.

| Job | Price | Ran | Result | Payment | Cost after sync |
| --- | --- | --- | --- | --- | --- |
| A media read, fixed price | $0.000001 | 1.7 s | `failed`, `refused`: the runner answered 404 | sent; 1 payment row | $0.000001, `observed`, not billable |
| A chat completion | by the second | 8.7 s | `succeeded` | sent; 1 payment row | $0.000195, `observed`, billable |
| A chat completion | by the second | 3.3 s | `succeeded` | sent; 1 payment row | $0.000195, `observed`, billable |
| A chat completion | by the second | 30.1 s | `succeeded` | sent; 9 payment rows, one each cycle | $0.000838, `observed`, billable |

| Check after the jobs | Result | Seen |
| --- | --- | --- |
| Tenant one's payment records at the clearinghouse | pass | 12 rows, $0.001228 |
| The actor's allowance on the enterprise's engine | pass | `spent` $0.001228 of $0.25 |
| Reconciliation for the actor on the enterprise's engine | pass | `balanced` |
| Tenant two's payment records | pass | 0 rows |
| Tenant two's actor | pass | `spent` 0 of $0.25 |
| The tenant list, on the private listener | pass | tenant one `spent` $0.001228, tenant two `spent` 0 |
| Tenant one's admin on five of tenant two's routes | pass | 403 on each: the grant, the actors, the payment records, one allocation, the access keys |
| The clearinghouse admin's credential on the public listener | pass | 403 |
| The wallet report after the jobs | pass | the deposit and the reserve as before; unspent grants $0.998772 over 2 grants, which is each grant's unallocated balance and what its allocations still hold |
| Both engines' `lpb payments check` after the jobs | pass | exit 0 on each |

## An empty allocation is refused by the signer

| Check | Result | Seen |
| --- | --- | --- |
| The enterprise allots a second actor with $0 | pass | `exhausted`, the key kept by `secret_store` |
| That actor's job | pass | HTTP 402; `failed` with `payment` / `allowance_exhausted`, status 402, `payment_sent` false |
| Tenant one's payment records after it | pass | 12 rows, as before: nothing was signed |
| What each engine stores at the end | pass | the enterprise 2 secrets, both `secret_store`; the clearinghouse 0, with all three actors `work_engine` |

The signer's Batteries authorization webhook is the only check at signing, and it refused.

## What the run found

- **`docker compose run` restarted the clearinghouse's engine.** The runbook's helper for the private
  listener ran `docker compose run private`, and `private` shares the engine's network namespace.
  Building the enterprise project had made the engine image again under the tag both projects use, so
  the helper's next call recreated the clearinghouse's engine: for a few seconds the private listener
  refused connections and the public one dropped them. Nothing was lost: the tenants, the allocations
  and the payment records were as before, and the checks that had failed passed when they were repeated.
  The helper now runs with `--no-deps`, which never touches the engine; with it the same calls left the
  engine's container as it was.
- **The first payment of a job priced by the second covers ten seconds.** Each of the two short chat
  jobs was one payment row of ten times the rate. The 30-second job was nine rows.
- **A fixed-price job that succeeds is still not shown.** The media runner refused the request with a
  404 after the payment was sent, as on 19 November: its path is not known. The fixed-price payment is
  shown: one signature at the published signer, a charge to the right allocation, and a cost the engine
  accounts and does not bill. The $0.02 image runner was not tried again.
- **Proof (c) was not run again.** It stands as run on 19 November.
