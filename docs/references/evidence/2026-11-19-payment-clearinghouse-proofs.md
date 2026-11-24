# Runtime evidence: the payment clearinghouse's paid proofs, 19 November 2026

## What still stands (marked 23 November 2026)

This run used a design that was withdrawn the day it was run. Then, the clearinghouse's engine served
the signer's two paths, issued each actor a payment key of its own (`lpp_`), and signed for the
enterprise's engine with the Batteries key it kept. Now, the clearinghouse hands each allocation's own
Batteries key to the enterprise's engine once and stores none, and the enterprise's engine signs at the
clearinghouse's published signer itself (D85 point 1 and D87 points 1 and 3, as amended on 19 and 23
November 2026). Nothing below this section is changed: it is the record of what was run.

Each result is judged by what it exercised.

| Result | Status | Why |
| --- | --- | --- |
| Proofs (a) and (b): the jobs paid through the clearinghouse, and their costs | **Superseded. Run again on 23 November 2026.** | Every payment was signed at the clearinghouse engine's two signer paths with an `lpp_` key. Neither exists now |
| Tenant one's and tenant two's payment records, allowances and `spent` after those jobs | **Superseded. Run again on 23 November 2026.** | They follow from those payments |
| Tenant two allots its own actor, "with a payment key issued once" | **Superseded. Run again on 23 November 2026.** | The answer now hands over the allocation's own key, and the clearinghouse stores none |
| The enterprise allots its actor through the clearinghouse | **Superseded. Run again on 23 November 2026.** | The enterprise's engine now stores the key it is handed |
| `lpb payments check` on the enterprise's engine, with "the signer path 405" at the clearinghouse | **Superseded. Run again on 23 November 2026.** | The check now asks the published signer, at `LPB_SIGNER_URL`, and never the clearinghouse's engine |
| An empty allocation refused by the signer | **Not run then. Run on 23 November 2026.** | The clearinghouse's engine is no longer in the signing path, so the signer's Batteries authorization webhook is the only check at signing |
| Proof (c): an engine in its `both` role pays by itself | **Stands as run.** | The engine signed at its own signer with the allocation's Batteries key from its own store. That path did not use the signer proxy or an `lpp_` key, and the direct-signer change did not alter it. It was not run again on the code after the change |
| `lpb payments check` on the clearinghouse, and the signer probe against a real signer (405) | **Stands as run.** | The probe is the same `GET` at the same address. Its words for a missing address changed; its requests did not |
| The wallet report: the deposit, the reserve and `signer.ready` | **Stands as run** for what it reads. | The report makes the same identity call. An engine in its `clearinghouse` role now makes it through a transport that cannot pay, which no live run has exercised |
| The routes each role serves, the 403s between tenants, and the clearinghouse admin's 403 on the public listener | **Stand as run.** | None of them touched signing. The runbook repeats them at no cost |
| The offerings read from the clearinghouse's runner list | **Stands as run.** | Unchanged |
| What the run found: the password file's newline, discovery's owner-only config, a refused request still charged, the deposit that did not move | **Stands.** | Facts about the signer, discovery and the network |

**Proven on the new path on 23 November 2026** ([the evidence](2026-11-23-payment-clearinghouse-proofs.md)), from the runbook in [deploy/README.md](../../../deploy/README.md#proof-runbook):
a tenant onboarded on the private listener; an allotment from the enterprise's engine, with the key
stored there and none at the clearinghouse; a fixed paid job and a live one from the enterprise's
engine, signed at the published signer; the tenant's usage at the clearinghouse, and the second tenant
untouched; and an empty allocation refused by the signer.

## The run of 19 November 2026

The three paid proofs that ISS-31's last ticket asks for (#169), run on one host with the Compose
projects in [deploy/](../../../deploy/README.md) and its proof runbook: an engine in its `work` role
pays for jobs through an engine in its `clearinghouse` role that holds two tenants, and an engine in
its `both` role still pays by itself.

| Component | Identity |
| --- | --- |
| Engine | this branch at `9296469` with the two Compose fixes of `943cf3f`; one image built from it ran all three engines |
| Clearinghouse | `deploy/clearinghouse`: the engine in its `clearinghouse` role with two listeners, Batteries `03bc9fb` with its on-chain listener on, node signer and discovery `eee09c4` |
| Enterprise | `deploy/enterprise`: the engine in its `work` role, with no Batteries and no signer |
| Self-hosted | `deploy/dev-stack` under a project name of its own, with new volumes: the engine in its `both` role |
| Network | real orchestrators on Arbitrum One, and one funded wallet. One signer used it at a time |
| Spend | about $0.021 charged to allocations: $0.0208 through the clearinghouse and $0.0002 on the self-hosted engine |

No address, credential or tenant id is recorded here. Every access credential stayed in a git-ignored
file, and the wallet's files were mounted read-only and never copied.

## Before any job

| Check | Result | Seen |
| --- | --- | --- |
| `lpb payments check` on the clearinghouse | pass | role `clearinghouse`; the signer 405, Batteries 200, the grants credential 200; exit 0 |
| The signer probe against a real signer | pass | `GET /sign-orchestrator-info` answered 405, as read from the node's code |
| The wallet report, on the private listener | pass | one escrow account on chain 42161 with its deposit and reserve; `signer.ready` true |
| A job route on the clearinghouse | pass | `POST /v1/jobs` is 404 |
| Two tenants onboarded on the private listener, $0.50 each | pass | each `total` 0.5, `unallocated` 0.5, `spent` 0 |
| Tenant two allots its own actor on the public listener, $0.25 | pass | `active`, with a payment key issued once |
| `lpb payments check` on the enterprise's engine, as tenant one | pass | role `work`; the grant 200, the usage 200, the signer path 405, the runner list 200; exit 0 |
| A clearinghouse route on the enterprise's engine | pass | 404 |
| The enterprise's engine reads offerings from the clearinghouse's runner list | pass | 26 offerings with rates, none stale |
| The enterprise allots its actor through the clearinghouse, $0.25 | pass | `active`; tenant one's `unallocated` fell to 0.25 |

## Proofs (a) and (b): jobs paid through the clearinghouse

Every job was run by tenant one's actor on the enterprise's engine. Each payment was signed at the
clearinghouse's two signer paths with that actor's payment key.

| Job | Price | Result | Payment | Cost after sync |
| --- | --- | --- | --- | --- |
| A chat completion, 1.8 s | by the second | `succeeded` | sent; 1 payment row | $0.000194, `observed`, billable |
| A chat completion, 16.4 s | by the second | `succeeded` | sent; 6 payment rows, one each cycle | $0.000583, `observed`, billable |
| An image, fixed price | $0.02 | `failed`, `runner_rejected`: the runner answered 400 | sent; 1 payment row | $0.020043, `observed`, not billable |
| A media read, fixed price, at two paths | $0.000001 each | `failed`, `refused`: the runner answered 404 | sent; 1 payment row each | accounted |

| Check after the jobs | Result | Seen |
| --- | --- | --- |
| Tenant one's payment records | pass | 10 rows, $0.020821 |
| The actor's allowance on the enterprise's engine | pass | `spent` $0.020821 of $0.25 |
| Tenant two's payment records | pass | 0 rows |
| Tenant two's actor | pass | `spent` 0 of $0.25 |
| The tenant list, on the private listener | pass | tenant one `spent` $0.020821, tenant two `spent` 0 |
| Tenant one's admin on five of tenant two's routes | pass | 403 on each: the grant, the actors, the payment records, one allocation, the access keys |
| The clearinghouse admin's credential on the public listener | pass | 403 |
| The wallet report after the jobs | pass | the deposit and the reserve as before; unspent grants $0.979 over 2 grants |

**What is not shown: a fixed-price job that succeeds.** Both fixed-price runners that were tried
refused the request after the payment was sent. The image runner refused a request it had drawn the day
before, and the path of the media runner is not known. The fixed-price payment itself is shown three
times: one signature through the clearinghouse, a charge to the right allocation, and a cost the
engine accounts and does not bill.

## Proof (c): an engine in its `both` role pays by itself

The clearinghouse's signer was stopped first.

| Check | Result | Seen |
| --- | --- | --- |
| `lpb payments check` | pass | role `both`; the signer 405, Batteries 200, the grants credential 200; exit 0 |
| One tenant onboarded with $0.10, one actor allotted $0.05 | pass | `active` |
| A chat completion, 2.0 s | pass | `succeeded`, payment sent |
| Its cost after sync | pass | $0.000194, `observed`, billable |
| Reconciliation for the actor | pass | `balanced`; delivered $0.000194, nothing unattributed |

## What the run found

- **The signer could not open the wallet.** It reads the password file as exact bytes, and the
  wallet's password file ends in a newline. The clearinghouse project now passes the password on
  without it, from the signer container's memory.
- **Discovery could not read its config,** which the runbook's `umask 077` makes owner-only. It now
  runs as root, as the signer does.
- **A refused request is still charged.** The runner is paid before it reads the request. The engine
  records such a job as failed after payment and does not bill it.
- **The deposit did not move.** Payments are probabilistic tickets, so a few cents of work rarely
  redeems one.

All three stacks were stopped afterwards and no signer was left running.
