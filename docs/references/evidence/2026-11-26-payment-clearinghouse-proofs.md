# Runtime evidence: the payment clearinghouse's paid proofs on `releases/m4`, 26 November 2026

The three paid proofs of ISS-31 (#169), run once more with every pull request of ISS-31 merged: a
clearinghouse with two tenants, **each tenant's app in an engine of its own**, and an engine in its
`both` role that pays by itself. This run adds what the earlier ones did not show: a second enterprise
engine, a tenant's routes in the form they have since #185, a grant topped up by hand, and proof (c) on
the same commit as the other two. What the run of 23 November showed and this one did not is under
[What the run found](#what-the-run-found).

The run followed the proof runbook in [deploy/README.md](../../../deploy/README.md#proof-runbook), steps
0 to 7, on one host, from new volumes, from 21:26 to 21:33 UTC on 26 November 2026.

| Component | Identity |
| --- | --- |
| Engine | `releases/m4` at `4a5ceba`; every engine of the run was built from it |
| Clearinghouse | `deploy/clearinghouse`: the engine in its `clearinghouse` role with two listeners, Batteries `03bc9fb` with its on-chain listener on, node signer and discovery `eee09c4`, and the relay that publishes the signer |
| Enterprise one and two | `deploy/enterprise`, started twice under two project names: an engine in its `work` role for tenant one and another for tenant two, each with its own tenant admin key, its own secret key, its own database and its own published port. No Batteries in either |
| Self-hosted | `deploy/dev-stack`: one engine in its `both` role with its own Batteries and signer, and one tenant |
| Network | real orchestrators on Arbitrum One, and one funded wallet. One signer used it at a time: the clearinghouse's was stopped before the dev stack's started |
| Spend | $0.001110 charged to three allocations: $0.000555 to tenant one's actor, $0.000277 to tenant two's, and $0.000278 to the self-hosted engine's |

No address, credential or tenant id is recorded here. Every access credential stayed in a git-ignored
file, the wallet's files were mounted read-only and never copied, and no allocation's key was printed or
kept by a command. The projects ran under names of their own, so the volumes of 23 November are as they
were.

## ISS-31's acceptance, and what shows each item

| Acceptance item | Shown by |
| --- | --- |
| A clearinghouse serves at least two tenants, one for each enterprise app; each app runs its jobs in its own engine, which reaches the clearinghouse for allotment, allowance and usage and signs at its signer | [Two engines, two tenants](#two-engines-two-tenants): each engine allotted its own actor, ran a paid job and read its own usage |
| One tenant's admin cannot read or change another tenant | [One tenant cannot reach another](#one-tenant-cannot-reach-another) |
| Self-hosted payments still work, as one deployment with one tenant | [Proof (c)](#proof-c-an-engine-in-its-both-role-pays-by-itself) |
| The clearinghouse admin onboards a tenant and tops up its grant by hand, on a private listener | [Before any job](#before-any-job): the onboarding and the top-up rows |
| The arrangement and its limits are documented | Not a runtime check: [deploy/README.md](../../../deploy/README.md#limits) |

## Before any job

| Check | Result | Seen |
| --- | --- | --- |
| The clearinghouse project's services and published ports | pass | Batteries healthy; the signer, the relay, discovery and the engine up; two ports on the host's loopback, the engine's public listener and the signer, and no other |
| `lpb payments check` on the clearinghouse | pass | role `clearinghouse`; three probes ok: the signer, Batteries, the grants credential; exit 0 |
| A job route and a signer path on the clearinghouse's engine | pass | `POST /v1/jobs` is 404, and `POST /sign-orchestrator-info` is 404: the engine signs for nobody |
| The published signer | pass | `GET /sign-orchestrator-info` at the published address answered 405, as a signer does, and its own readiness port, published nowhere, answered 200 |
| The wallet report, on the private listener | pass | one escrow account with a deposit; `signer.ready` true |
| Two tenants onboarded on the private listener, $0.50 each | pass | each `total` 0.5, `unallocated` 0.5, `spent` 0 |
| The clearinghouse admin's key on the public listener's tenant list | pass | 404 |
| Tenant two allots an actor by a direct call on the public listener, $0.25, at the path that names no tenant | pass | `active`, `credential_custody` `work_engine`, and the answer handed a key over |
| The same allotment repeated | pass | the same allocation, no key, `replayed` true |
| **The clearinghouse admin tops up tenant two's grant by $0.10, on the private listener** | pass | 204; the tenant list shows `total` 0.6 for tenant two and 0.5 for tenant one |
| The same top-up repeated under the same reference | pass | 204, and the total is still 0.6 |
| Tenant two tops up its own grant | pass | refused: 404 on the public listener, at the path that names its tenant and at one that names none, and 403 on the private listener |
| `lpb payments check` on each enterprise engine | pass | role `work` on both; four probes ok on each: the grant, the usage, the signer at the published address, the runner list; exit 0 |
| A clearinghouse route on each enterprise engine | pass | 404 on both |
| Engine one reads offerings from the clearinghouse's runner list | pass | 27 offerings with rates, none stale. Engine two's runner-list probe passed, and its job below found its runner |

## Two engines, two tenants

Engine one is tenant one's and engine two is tenant two's. Each allotted one actor of its own with
`enterprise/allot.py`, which calls the clearinghouse with that engine's tenant admin key and stores the
key that comes back.

| Check | Result | Seen |
| --- | --- | --- |
| Engine one allots its actor, $0.25 | pass | `active`, the key kept by `secret_store` |
| Engine two allots its actor, $0.25 | pass | `active`, the key kept by `secret_store` |
| **Where the keys are** | pass | engine one's store holds 1 secret, its own actor's; engine two's holds 1, its own actor's; the clearinghouse's holds 0 and its record says `work_engine` for all three actors. The read of an allocation at the clearinghouse carries no key |
| The grants after the allotments | pass | tenant one `unallocated` 0.25 of 0.5; tenant two 0.10 of 0.6, after its two actors |

Every job below was signed at the published signer by the engine that ran it, with the key that engine
stored at allotment. The clearinghouse's engine serves no signer path, so nothing else could have signed
them.

| Job | Engine | Price | Ran | Result | Payment | Cost after sync |
| --- | --- | --- | --- | --- | --- | --- |
| A media read, fixed price | one | $0.000001 | 0.9 s | `failed`, `refused`: the runner answered 404 | sent; 1 payment row | $0.000001, `observed`, not billable |
| Speech from a sentence | one | by the second | 0.8 s | `succeeded`: an MP3 | sent; 1 payment row | $0.000277, `observed`, billable |
| Speech from 3,852 characters | one | by the second | 1.9 s | `succeeded`: an MP3 of 3 MB | sent; 1 payment row | $0.000277, `observed`, billable |
| Speech from a sentence | two | by the second | 0.5 s | `succeeded`: an MP3 | sent; 1 payment row | $0.000277, `observed`, billable |

| Check after the jobs | Result | Seen |
| --- | --- | --- |
| Each tenant's payment records at the clearinghouse | pass | tenant one 3 rows, tenant two 1 row: each tenant's own jobs and no other's |
| Each actor's allowance on its own engine | pass | engine one's actor `spent` $0.000555 of $0.25; engine two's $0.000277 of $0.25 |
| Reconciliation for each actor on its own engine | pass | `balanced` on both |
| Tenant two's other actor, allotted by the direct call | pass | `spent` 0 of $0.25 |
| The tenant list, on the private listener | pass | tenant one `spent` $0.000555, tenant two `spent` $0.000277 |
| The wallet report after the jobs | pass | `signer.ready` true; unspent grants $1.099167 over 2 grants, which is each grant's unallocated balance and what its allocations still hold. The two spends above are rounded: unrounded they are $0.00055546 and $0.00027723, and $1.10 less both is that figure |
| All three engines' `lpb payments check` after the jobs | pass | exit 0 on each |

## One tenant cannot reach another

On the routes as they are since #185: a tenant's own routes on the public listener name no tenant, and
the paths that name a tenant are served on the private listener alone.

| Check | Result | Seen |
| --- | --- | --- |
| Each tenant admin's key reads its own grant | pass | tenant one's key answers with tenant one's id, tenant two's with tenant two's |
| Each tenant's actor list | pass | tenant one lists its one actor; tenant two lists its two; neither lists the other's |
| Tenant one reads tenant two's actors by name on its own routes, and tenant two reads tenant one's | pass | 404 on each: the routes reach the caller's tenant alone |
| Tenant one tops up, and revokes, tenant two's actor on its own routes | pass | 404 on each |
| Tenant one's key at five paths that name tenant two, on the public listener: the grant, the actors, the payment records, one allocation, the access keys | pass | 404 on each |
| Tenant one's key at the paths that name its own tenant, on the public listener | pass | 404 on each: no path that names a tenant is served there |
| Tenant one's key on the private listener, at tenant two's grant and at its own | pass | 403 on both |
| The clearinghouse admin reads tenant two's actor on the private listener, by naming the tenant | pass | answered |
| The clearinghouse admin's key at a tenant's own routes on the public listener; no credential there | pass | 403; 401 |
| An access key of engine one's actor on engine two, and the reverse | pass | 401 on both: each engine knows its own keys alone |

## An empty allocation is refused by the signer

| Check | Result | Seen |
| --- | --- | --- |
| Engine one allots a second actor with $0 | pass | `exhausted`, the key kept by `secret_store` |
| That actor's job | pass | HTTP 402; `failed` with `payment` / `allowance_exhausted`, status 402, `payment_sent` false |
| Tenant one's payment records after it | pass | 3 rows, as before: nothing was signed |

The signer's Batteries authorization webhook is the only check at signing, and it refused.

## Proof (c): an engine in its `both` role pays by itself

The clearinghouse's signer and its relay were stopped first. The dev stack ran under a project name of
its own, with an override file kept outside the repository that mounts the wallet's folder read-only
into its signer.

| Check | Result | Seen |
| --- | --- | --- |
| The signer's own readiness | pass | 200 |
| `lpb payments check` | pass | role `both`; three probes ok: the signer, Batteries, the grants credential; exit 0 |
| One tenant onboarded with $0.50, one actor allotted $0.25, on the engine's one listener | pass | `active`, `credential_custody` `secret_store`, no key in the answer; the engine's store holds 1 secret |
| A paid job: speech from a sentence, priced by the second | pass | `succeeded` in 0.7 s; a payment was sent |
| The job's cost after `POST /v1/costs/sync` | pass | $0.000278, `observed`, billable, 1 payment row |
| Reconciliation for the actor | pass | `balanced` |
| The tenant list | pass | one tenant, `spent` $0.000278 of a grant of $0.50 |

## What the run found

- **No job ran for more than one payment cycle.** No chat runner was listed on this day, so the jobs
  priced by the second were speech, and the longest ran for 1.9 seconds. Each was one payment row that
  covers ten seconds. A job of several cycles was not shown on this commit: the one of
  [23 November](2026-11-23-payment-clearinghouse-proofs.md), nine rows over 30 seconds, is the record of
  that.
- **No fixed-price job succeeded.** The media runner refused the request with a 404 after the payment
  was sent, as on 19 and 23 November: its path is not known. The engine accounts that payment and does
  not bill it. The fixed-price job that succeeded and was billed is the image of 23 November, which was
  left out here to keep the spend small.
- **A grant's top-up answers 204 with no body.** The new total is read from the tenant list or from the
  tenant's own grant.
- **An engine in its `work` role issues an access key that names any tenant id.** Engine two's admin
  issued a key for an actor under tenant one's id. That key's job was refused with 402,
  `not_provisioned`, and no payment, because the engine holds no allocation for that actor. The key
  gives no reach into tenant one. The clearinghouse takes the tenant from the engine's tenant admin
  key, and `enterprise/allot.py` sends the engine's own tenant id, `LPB_CLEARINGHOUSE_TENANT_ID`, and
  not the id an access key names. Nothing of tenant one was read or charged.
- **The dev stack's discovery could not read the clearinghouse project's orchestrator list.** The run
  gave the dev stack the same git-ignored list the clearinghouse project uses, bound from that project's
  folder. The file's mode is 0600 and the dev stack's discovery does not run as root, so it stopped at
  start. The override file ran that one service as root, for this run alone. The dev stack's own
  `config/discovery.toml` needs no such step.
- **The projects were given their settings by environment variables.** Other project names made new
  volumes, and the wallet's and each tenant's settings were exported in the shell, which Compose reads
  before a project's `.env`. The `.env` files and the volumes of the 23 November run were not touched.
