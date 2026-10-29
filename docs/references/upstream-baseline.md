# Upstream baseline

**Decided 12 October 2026 (D1, D2).** Each repository's `main` at that date is the baseline. M2 work
goes on a `releases/m2` branch in each. The SDK, node and Batteries forks matched their
`livepeer/*` upstream `main` exactly on 12 October.

| Component | Repository | Baseline commit | Used by |
| --- | --- | --- | --- |
| Python gateway SDK | [livepeer/livepeer-python-gateway](https://github.com/livepeer/livepeer-python-gateway) | `44df06157fcdb864e37d971e8caba86b2a7dc92e` (12 October baseline); pinned now to `2017a80` on `main` (26 October: [#76](https://github.com/livepeer/livepeer-python-gateway/pull/76), [#80](https://github.com/livepeer/livepeer-python-gateway/pull/80), [#81](https://github.com/livepeer/livepeer-python-gateway/pull/81), [#83](https://github.com/livepeer/livepeer-python-gateway/pull/83); was `76a1a1c`, and `3238731` on the NE fork before that) | `adapters/sdk.py` |
| Signer and orchestrator | [livepeer/node](https://github.com/livepeer/node) | `406b248f6cb8728e6e7eba1809901f589060bc6b` (v1.0.2, 19 October; was `f970be6`, and `1432123` before that). It includes Josh's fix for T10. Built from upstream since 28 October; the NE-Build-Track fork has no commits of its own at this pin | dev stack and Railway; [node-signer.md](node-signer.md) |
| Clearinghouse Batteries | [livepeer/clearinghouse-batteries](https://github.com/livepeer/clearinghouse-batteries) | `ca09e47c067682fb48c3f15425546fb6aa5b6659`. Built from upstream since 28 October, at the same commit: the NE-Build-Track fork has no commits of its own on any branch | `adapters/batteries.py`; [batteries-api.md](batteries-api.md) |
| Prototype (reference only) | [eliteprox/livepeer-core](https://github.com/eliteprox/livepeer-core) | `bbe1253d9bb84d33914cdb19864aba1e32eb8202` | Starting point for this repository (D4) |

## The SDK pin

`pyproject.toml` pins `livepeer-gateway` by direct reference to
`2017a80c740af5fcb0fef4ee84659af9b07fb90c`, the head of
[livepeer/livepeer-python-gateway](https://github.com/livepeer/livepeer-python-gateway) `main`
on 26 October. That commit includes:

- "Enforce TLS" ([#76](https://github.com/livepeer/livepeer-python-gateway/pull/76), `fc47903`);
- streamable runner request bodies ([#80](https://github.com/livepeer/livepeer-python-gateway/pull/80),
  `77ba2ce`): `payload` may be bytes, a file, an async byte iterator or a `StreamPayload`, with
  `content_type`. The engine does not use these yet;
- the payment session callback ([#81](https://github.com/livepeer/livepeer-python-gateway/pull/81);
  supersedes [#77](https://github.com/livepeer/livepeer-python-gateway/pull/77), whose head `e6d243b`
  was an earlier pin);
- the runner-side session stop ([#83](https://github.com/livepeer/livepeer-python-gateway/pull/83),
  `6106141`): `LiveRunnerRegistration.stop_session()` and `LiveRunnerSessionEvent.session_token`.
  They belong to the runner, and the engine does not use them. What they mean for the caller is in
  [sdk-gateway.md](sdk-gateway.md#runner-session-stop-83);
- Ruff typing modernisation ([#84](https://github.com/livepeer/livepeer-python-gateway/pull/84)) and
  the SDK's own lockfile ([#85](https://github.com/livepeer/livepeer-python-gateway/pull/85)), with no
  change to the calls the engine makes.

The 22 October pin was `76a1a1c` (#81). `call_runner`, `discover_runners`, `open_stream` and
`run_payments` are identical in `76a1a1c` and `2017a80`. The 21 October pin `3238731` was the head of NE-Build-Track `dev/bytestreaming`
([NE livepeer-python-gateway#5](https://github.com/NE-Build-Track/livepeer-python-gateway/pull/5)).
#80 and #81 are on upstream `main`, so that fork pin is dropped.

**What the engine uses (D53).** The engine no longer calls `call_runner`. Its own runner call
(`adapters/runner_call.py`) is adapted from `call_runner` at `2017a80` (MIT): it sends the runner
request, answers the 402, and signs through the SDK's signer client (`get_signer_info`,
`LivePaymentSession.get_payment()`; for metered prices, the session's renewal, refresh and loop are the
engine's own since #72, D58, in `adapters/payment_funding.py`). That lets it record each
session's `manifest_id` before the signer is asked, which no SDK callback allows.
`LivePaymentSession.manifest_id` is the manifest from the 402 challenge. The SDK does not decode
signer state. `tests/unit/test_sdk_contract.py` holds the copy to the pinned SDK: it fails when
`call_runner` or a helper the copy follows changes.

- PyPI `livepeer-gateway` 1.0.0 predates `LivePaymentSession.manifest_id`, so the engine still pins a
  commit (T1).
- Exit condition: tech debt T1 (re-pin to a release that includes #81).

## Licenses

| Repository | License | Use |
| --- | --- | --- |
| livepeer-python-gateway | MIT | Dependency |
| node | MIT | Run as a service (signer, orchestrator) |
| clearinghouse-batteries | None published | Run as a service; no code copied |
| console, PymtHouse | None published | Out of scope; no code copied |
