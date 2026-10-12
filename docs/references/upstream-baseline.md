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

`pyproject.toml` pins `livepeer-gateway` by direct reference to fork commit
`3250c08cb6424d339614f1e9095064fbe7e6da6e`, the head of SDK #71 (`auth_ids` and `payment_sent` on
results and errors). PyPI `livepeer-gateway` 1.0.0 lacks `auth_ids`, so installing from PyPI would
break job cost attribution. That defect was found in the prototype on 12 October. Exit condition:
tech debt T1.

## Licenses

| Repository | License | Use |
| --- | --- | --- |
| livepeer-python-gateway | MIT | Dependency |
| node | MIT | Run as a service (signer, orchestrator) |
| clearinghouse-batteries | None published | Run as a service; no code copied |
| console, PymtHouse | None published | Out of scope; no code copied |
