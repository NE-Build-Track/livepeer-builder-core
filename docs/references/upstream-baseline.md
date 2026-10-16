# Upstream baseline

**Decided 12 October 2026 (D1, D2).** Each repository's `main` at that date is the baseline. M2 work
goes on a `releases/m2` branch in each. The SDK, node and Batteries forks matched their
`livepeer/*` upstream `main` exactly on 12 October.

| Component | Repository | Baseline commit | Used by |
| --- | --- | --- | --- |
| Python gateway SDK | [livepeer/livepeer-python-gateway](https://github.com/livepeer/livepeer-python-gateway) | `44df06157fcdb864e37d971e8caba86b2a7dc92e` (plus [#77](https://github.com/livepeer/livepeer-python-gateway/pull/77) at `e6d243b`) | `adapters/sdk.py` |
| Signer and orchestrator | [livepeer/node](https://github.com/livepeer/node) | `f970be644c55e41f77ef3ee8f085f516074f4cc8` (14 October; was `1432123`). It adds Josh's fix for T10 | dev stack; [node-signer.md](node-signer.md) |
| Clearinghouse Batteries | [eliteprox/clearinghouse-batteries](https://github.com/eliteprox/clearinghouse-batteries) | `ca09e47c067682fb48c3f15425546fb6aa5b6659` | `adapters/batteries.py`; [batteries-api.md](batteries-api.md) |
| Prototype (reference only) | [eliteprox/livepeer-core](https://github.com/eliteprox/livepeer-core) | `bbe1253d9bb84d33914cdb19864aba1e32eb8202` | Starting point for this repository (D4) |

## The M2 SDK exception

`pyproject.toml` pins `livepeer-gateway` by direct reference to
`e6d243b1d8bcbc8c1f0262ddbc3239a9f5b83cf4` on
[livepeer/livepeer-python-gateway](https://github.com/livepeer/livepeer-python-gateway).
That is the head of
**[livepeer-python-gateway#77](https://github.com/livepeer/livepeer-python-gateway/pull/77)**
("Report each signed payment session from call_runner", against `main`).

#77 adds `call_runner(on_payment_session=...)`. The callback receives each signed session.
`LivePaymentSession.manifest_id` is the manifest from the 402 challenge. The SDK does not decode
signer state.

- PyPI `livepeer-gateway` 1.0.0 has no payment-session reporting, so installing from PyPI would break
  job cost attribution. That defect was found in the prototype on 12 October.
- A callback error, including `SignerRefreshRequired`, belongs to the caller and does not restart
  the payment challenge. The engine records each `manifest_id` on its session log.
- Exit condition: tech debt T1 (re-pin to the merge commit on `main`, then a release).

## Licenses

| Repository | License | Use |
| --- | --- | --- |
| livepeer-python-gateway | MIT | Dependency |
| node | MIT | Run as a service (signer, orchestrator) |
| clearinghouse-batteries | None published | Run as a service; no code copied |
| console, PymtHouse | None published | Out of scope; no code copied |
