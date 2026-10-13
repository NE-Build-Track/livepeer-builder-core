# Runtime evidence: real-node attribution run, 7 October 2026

> **Provenance:** copied from `eliteprox/livepeer-core` `docs/evidence/2026-10-07-arbitrum-run.md` at `bbe1253` (John). It predates the 12 October baseline: go-livepeer v0.9.3 and Batteries `9312212`, not livepeer/node and `ca09e47`. The re-run is tracked in [m2-attribution-rerun](../../exec-plans/active/m2-attribution-rerun.md).


Arbitrum One. The signer and orchestrator are `livepeer/go-livepeer:v0.9.3`. Batteries is a local build of `livepeer/clearinghouse-batteries` at `9312212`. The proof scripts import `livepeer-gateway` at `3250c08` (`eliteprox/feat/call-runner-manifest-id`). The account `0x0074780FefF1FD0277FAD6ccdb5a29908df6051F` had a broadcaster deposit of 0.014424 ETH and a reserve of 0.0496 ETH. It is not an active orchestrator in round 4363, so the orchestrator rejected tickets after the signer had already published them. Batteries still applied those charges. The public RPC refused archive `eth_getLogs` calls; signing did not depend on that.

## What was compared

Spend for a user is the sum of `applied` usage rows whose `allocation_id` was stored when that user's API key was created. A job's cost is the `applied` rows whose `payment_session_id` is an `auth_id` the gateway recorded for that job, and whose `allocation_id` is that user's allocation. `manifest_id` is stored and reported, and it is not used as the billing key.

`prove_allocation.py` records the manifest and deliberately stores no `auth_id`. `prove_job.py` and `prove_forged_manifest.py` record the `auth_id` decoded from the signer's returned state.

## Result

Batteries `spent_eth` matched the Postgres sum for every actor:

| Actor | Applied rows | Postgres `fee_eth` | Batteries `spent_eth` |
| --- | --- | --- | --- |
| alice | 5 | 0.000000195142198485 | 0.000000195142198485 |
| bob | 2 | 0.000000078056879394 | 0.000000078056879394 |
| shared | 3 | 0.000000117085319091 | 0.000000117085319091 |

Jobs stored with an empty `auth_ids` list stayed `pending` even though those calls had already increased the actor's spend. Jobs stored with the signer `auth_id` became `observed`. One session signed twice (`AaEXySjtf0-ouWJfXyd2nA`, sequence 0 and 1) and the job cost was two applied rows, `7.8056879394e-8` ETH.

A client-chosen manifest `forged-by-client` was signed under alice's API key. The signer log shows `manifest_id=forged-by-client` while `sessionID=8aac965a`. The applied usage row is:

- `allocation_id` `AaEXxtxjc_q5fBXorpUwgA` (alice)
- `payment_session_id` `AaEXyNVVd4OEK6yPeRkDpg` (the `auth_id` decoded from the signer state)
- `manifest_id` `forged-by-client`
- `status` `applied`
- `computed_fee_eth` `3.9028439697e-8`

The job that recorded that `auth_id` is `observed` for that one row. Summing by the manifest also finds it, on alice's allocation only, because the API key selected the allocation. The manifest string did not.

Ignored rows have a null allocation and a null manifest and are not spend.

A checkpoint set to `not-a-cursor` made `GET /v1/usage` return 400 `invalid cursor`. The worker cleared it, synced from the start, and a second poll re-read 20 rows with `rows_new=0`.

## Recommendation

Key observed cost by `(allocation_id, payment_session_id)`. Keep `manifest_id` as a label. One allocation per actor is what makes a user's spend equal Batteries' own `spent_eth`, and it is what lets the signer webhook refuse a user who is over the allowance. A shared allocation, which is what a single Batteries user does today, still charges correctly in total, and it cannot say which enterprise user spent what unless the gateway records `auth_id` at sign time. `manifest_id` cannot do that job: this run put an arbitrary string on an applied charge.

## Stack notes

The example hello-world runner calls `register_runner(price_unit=...)`, which `3250c08` does not accept, so `deploy/runners/hello` registers the same `/hello` behavior with `unit="fixed"`. The example echo runner registers at price 0, which this orchestrator rejects. The priced `hold` runner registered, and a metered sign asked for 109 tickets, over the signer's limit of 100, so the two-sign proof used a fixed-price session instead. That is the same state reuse a reserved session uses.
