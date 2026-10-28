# Upstream baseline

**Decided 12 October 2026 (D1, D2).** Each repository's `main` at that date is the baseline. M2 work
goes on a `releases/m2` branch in each. The SDK, node and Batteries forks matched their
`livepeer/*` upstream `main` exactly on 12 October.

| Component | Repository | Baseline commit | Used by |
| --- | --- | --- | --- |
| Python gateway SDK | [livepeer/livepeer-python-gateway](https://github.com/livepeer/livepeer-python-gateway) | `44df06157fcdb864e37d971e8caba86b2a7dc92e` (12 October baseline); pinned now to `76a1a1c` on `main` (22 October: [#76](https://github.com/livepeer/livepeer-python-gateway/pull/76), [#80](https://github.com/livepeer/livepeer-python-gateway/pull/80), [#81](https://github.com/livepeer/livepeer-python-gateway/pull/81); was `3238731` on the NE fork) | `adapters/sdk.py` |
| Signer and orchestrator | [livepeer/node](https://github.com/livepeer/node) | `406b248f6cb8728e6e7eba1809901f589060bc6b` (v1.0.2, 19 October; was `f970be6`, and `1432123` before that). It includes Josh's fix for T10. Built from upstream since 28 October; the NE-Build-Track fork has no commits of its own at this pin | dev stack and Railway; [node-signer.md](node-signer.md) |
| Clearinghouse Batteries | [NE-Build-Track/clearinghouse-batteries](https://github.com/NE-Build-Track/clearinghouse-batteries) | `ca09e47c067682fb48c3f15425546fb6aa5b6659` | `adapters/batteries.py`; [batteries-api.md](batteries-api.md) |
| Prototype (reference only) | [eliteprox/livepeer-core](https://github.com/eliteprox/livepeer-core) | `bbe1253d9bb84d33914cdb19864aba1e32eb8202` | Starting point for this repository (D4) |

## The SDK pin

`pyproject.toml` pins `livepeer-gateway` by direct reference to
`76a1a1c58656ef2ad0c28542ea5f99086b76db69`, the head of
[livepeer/livepeer-python-gateway](https://github.com/livepeer/livepeer-python-gateway) `main`
on 22 October ("Add payment session callback for runner calls"). That commit includes:

- "Enforce TLS" ([#76](https://github.com/livepeer/livepeer-python-gateway/pull/76), `fc47903`);
- streamable runner request bodies ([#80](https://github.com/livepeer/livepeer-python-gateway/pull/80),
  `77ba2ce`): `payload` may be bytes, a file, an async byte iterator or a `StreamPayload`, with
  `content_type`. The engine does not use these yet;
- the payment session callback ([#81](https://github.com/livepeer/livepeer-python-gateway/pull/81);
  supersedes [#77](https://github.com/livepeer/livepeer-python-gateway/pull/77), whose head `e6d243b`
  was an earlier pin).

The 21 October pin `3238731` was the head of NE-Build-Track `dev/bytestreaming`
([NE livepeer-python-gateway#5](https://github.com/NE-Build-Track/livepeer-python-gateway/pull/5)).
#80 and #81 are on upstream `main`, so that fork pin is dropped.

`call_runner(on_payment_session=...)` is called once for each new session the signer signs, after
`LivePaymentSession.get_payment()` validates the signer's reply and before the paid request goes out
to the runner. If signing fails or is cancelled before `on_payment_session` runs, no callback fires,
`payment_sent` is `False`, and the job fails. `LivePaymentSession.manifest_id` is the manifest from
the 402 challenge. The SDK does not decode signer state.

- PyPI `livepeer-gateway` 1.0.0 has no payment-session reporting, so installing from PyPI would break
  job cost attribution. That defect was found in the prototype on 12 October.
- A callback error, including `SignerRefreshRequired`, belongs to the caller and does not restart
  the payment challenge. The engine records each `manifest_id` on its session log.
- Exit condition: tech debt T1 (re-pin to a release that includes #81).

## Licenses

| Repository | License | Use |
| --- | --- | --- |
| livepeer-python-gateway | MIT | Dependency |
| node | MIT | Run as a service (signer, orchestrator) |
| clearinghouse-batteries | None published | Run as a service; no code copied |
| console, PymtHouse | None published | Out of scope; no code copied |
