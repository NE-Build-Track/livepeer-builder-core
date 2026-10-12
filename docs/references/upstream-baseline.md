# Upstream baseline

**Decided 12 October 2026 (D1, D2).** Each repository's `main` at that date is the baseline. M2 work
goes on a `releases/m2` branch in each. The SDK, node and Batteries forks matched their
`livepeer/*` upstream `main` exactly on 12 October.

| Component | Repository | Baseline commit | Used by |
| --- | --- | --- | --- |
| Python gateway SDK | [eliteprox/livepeer-python-gateway](https://github.com/eliteprox/livepeer-python-gateway) | `44df06157fcdb864e37d971e8caba86b2a7dc92e` (plus SDK #71 on `releases/m2`) | `adapters/sdk.py` |
| Signer and orchestrator | [eliteprox/node](https://github.com/eliteprox/node) | `1432123688e7e4ad8149c1989865890854cdf37a` | dev stack; [node-signer.md](node-signer.md) |
| Clearinghouse Batteries | [eliteprox/clearinghouse-batteries](https://github.com/eliteprox/clearinghouse-batteries) | `ca09e47c067682fb48c3f15425546fb6aa5b6659` | `adapters/batteries.py`; [batteries-api.md](batteries-api.md) |
| Prototype (reference only) | [eliteprox/livepeer-core](https://github.com/eliteprox/livepeer-core) | `bbe1253d9bb84d33914cdb19864aba1e32eb8202` | Starting point for this repository (D4) |

## The M2 SDK exception

`pyproject.toml` pins `livepeer-gateway` by direct reference to
`84ec5425102f5b27899eed069b29c901f4e41fd3`. That is the head of
**eliteprox/livepeer-python-gateway PR 5** ("Report each signed payment session from call_runner",
moved from livepeer #71, targeting `releases/m2`).

PR 5 adds `call_runner(on_payment_session=...)`. It drops the earlier `auth_ids`/`payment_sent`
fields on results and errors, so the engine derives both from the callback.

- PyPI `livepeer-gateway` 1.0.0 has no payment-session reporting, so installing from PyPI would break
  job cost attribution. That defect was found in the prototype on 12 October.
- `84ec542` also reads livepeer/node's lowercase `"state"` key, which resolves T9.
- Exit condition: tech debt T1 (re-pin to the merge commit).

## Licenses

| Repository | License | Use |
| --- | --- | --- |
| livepeer-python-gateway | MIT | Dependency |
| node | MIT | Run as a service (signer, orchestrator) |
| clearinghouse-batteries | None published | Run as a service; no code copied |
| console, PymtHouse | None published | Out of scope; no code copied |
