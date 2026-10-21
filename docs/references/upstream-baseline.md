# Upstream baseline

**Decided 12 October 2026 (D1, D2).** Each repository's `main` at that date is the baseline. M2 work
goes on a `releases/m2` branch in each. The SDK, node and Batteries forks matched their
`livepeer/*` upstream `main` exactly on 12 October.

| Component | Repository | Baseline commit | Used by |
| --- | --- | --- | --- |
| Python gateway SDK | [livepeer/livepeer-python-gateway](https://github.com/livepeer/livepeer-python-gateway) | `44df06157fcdb864e37d971e8caba86b2a7dc92e`; pinned now to `3238731` on [NE-Build-Track/livepeer-python-gateway](https://github.com/NE-Build-Track/livepeer-python-gateway) (21 October; was `e6d243b`, the head of [#77](https://github.com/livepeer/livepeer-python-gateway/pull/77)) | `adapters/sdk.py` |
| Signer and orchestrator | [NE-Build-Track/node](https://github.com/NE-Build-Track/node) | `406b248f6cb8728e6e7eba1809901f589060bc6b` (v1.0.2, 19 October; was `f970be6`, and `1432123` before that). It includes Josh's fix for T10 | dev stack and Railway; [node-signer.md](node-signer.md) |
| Clearinghouse Batteries | [NE-Build-Track/clearinghouse-batteries](https://github.com/NE-Build-Track/clearinghouse-batteries) | `ca09e47c067682fb48c3f15425546fb6aa5b6659` | `adapters/batteries.py`; [batteries-api.md](batteries-api.md) |
| Prototype (reference only) | [eliteprox/livepeer-core](https://github.com/eliteprox/livepeer-core) | `bbe1253d9bb84d33914cdb19864aba1e32eb8202` | Starting point for this repository (D4) |

## The M2 SDK exception

`pyproject.toml` pins `livepeer-gateway` by direct reference to
`3238731d319638ec7b5303fdbb0697c90ff7400b` on
[NE-Build-Track/livepeer-python-gateway](https://github.com/NE-Build-Track/livepeer-python-gateway), the head of
`dev/bytestreaming`
(**[NE livepeer-python-gateway#5](https://github.com/NE-Build-Track/livepeer-python-gateway/pull/5)**,
draft into `releases/final`). It merges two branches onto upstream `main` `fc47903`, which includes
"Enforce TLS" (#76):

- the payment session callback, first proposed upstream as
  [#77](https://github.com/livepeer/livepeer-python-gateway/pull/77) (closed unmerged; the previous
  pin was its head, `e6d243b`);
- streamable runner request bodies (`payload` may be bytes, a file, an async byte iterator or a
  `StreamPayload`, with `content_type`). The engine does not use these yet.

`call_runner(on_payment_session=...)` is called once for each new session the signer signs, after
the SDK accepts the signer's reply and before the paid request goes out.
`LivePaymentSession.manifest_id` is the manifest from the 402 challenge. The SDK does not decode
signer state.

- PyPI `livepeer-gateway` 1.0.0 has no payment-session reporting, so installing from PyPI would break
  job cost attribution. That defect was found in the prototype on 12 October.
- A callback error, including `SignerRefreshRequired`, belongs to the caller and does not restart
  the payment challenge. The engine records each `manifest_id` on its session log.
- Exit condition: tech debt T1 (re-pin to NE #5's merge commit, then a release).

## Licenses

| Repository | License | Use |
| --- | --- | --- |
| livepeer-python-gateway | MIT | Dependency |
| node | MIT | Run as a service (signer, orchestrator) |
| clearinghouse-batteries | None published | Run as a service; no code copied |
| console, PymtHouse | None published | Out of scope; no code copied |
