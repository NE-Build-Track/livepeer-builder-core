# Upstream baseline

**Decided 12 October 2026 (D1, D2).** Each repository's `main` at that date is the baseline. M2 work
goes on a `releases/m2` branch in each. The SDK, node and Batteries forks matched their
`livepeer/*` upstream `main` exactly on 12 October.

| Component | Repository | Baseline commit | Used by |
| --- | --- | --- | --- |
| Python gateway SDK | [NE-Build-Track/livepeer-python-gateway](https://github.com/NE-Build-Track/livepeer-python-gateway) | `44df06157fcdb864e37d971e8caba86b2a7dc92e` (12 October baseline); pinned to `202721c7664d0fdd1f4bc5324216d6a816cfaf0f` (27 October, payment-intent hooks on upstream `2017a80`; [PR #7](https://github.com/NE-Build-Track/livepeer-python-gateway/pull/7)) | `adapters/sdk.py` |
| Signer and orchestrator | [NE-Build-Track/node](https://github.com/NE-Build-Track/node) | `406b248f6cb8728e6e7eba1809901f589060bc6b` (v1.0.2, 19 October; was `f970be6`, and `1432123` before that). It includes Josh's fix for T10 | dev stack and Railway; [node-signer.md](node-signer.md) |
| Clearinghouse Batteries | [NE-Build-Track/clearinghouse-batteries](https://github.com/NE-Build-Track/clearinghouse-batteries) | `ca09e47c067682fb48c3f15425546fb6aa5b6659` | `adapters/batteries.py`; [batteries-api.md](batteries-api.md) |
| Prototype (reference only) | [eliteprox/livepeer-core](https://github.com/eliteprox/livepeer-core) | `bbe1253d9bb84d33914cdb19864aba1e32eb8202` | Starting point for this repository (D4) |

## The SDK pin

`pyproject.toml` and `uv.lock` pin `livepeer-gateway` to
`202721c7664d0fdd1f4bc5324216d6a816cfaf0f` on
[NE-Build-Track/livepeer-python-gateway](https://github.com/NE-Build-Track/livepeer-python-gateway).
[PR #7](https://github.com/NE-Build-Track/livepeer-python-gateway/pull/7) targets `releases/m3`;
the pin is immutable and does not depend on when that PR is merged.

The companion starts from published `releases/final` / upstream `2017a80` and reconciles the old
M3 fork ancestry. It includes TLS enforcement (#76), streamable request bodies (#80), the upstream
payment-session callback (#81), and subsequent upstream session-stop and dependency updates.
The new hooks add durable intent without restoring the old fork's cancellation drain:

- `on_payment_attempt(session)` is awaited before signing; a failure prevents the signing request.
- `on_payment_error(session, error)` receives signing/validation exceptions before a refresh retry.
  Cancellation propagates without invoking it, leaving previously recorded intent unresolved.
- `on_payment_session(session)` retains upstream validated-success behavior before paid dispatch.
  Missing success notification is not proof of no charge. D45 supersedes that part of D41.
- Hooks apply to new sessions; metered renewals use the existing session. The SDK does not decode
  signer state. Persistence and paid/unpaid/unknown policy belong to builder-core.

Earlier pins were upstream `76a1a1c` (22 October), NE-Build-Track `dev/bytestreaming` `3238731`
(21 October), and fork `e6d243b`. The original fork's early notification and cancellation drain
were superseded deliberately; the [execution plan](../exec-plans/active/payment-intent.md)
records the consumer-side reconciliation and validation.

PyPI `livepeer-gateway` 1.0.0 lacks the required callbacks. T1 ends when a published release
contains the session callback and both intent/error hooks.

## Licenses

| Repository | License | Use |
| --- | --- | --- |
| livepeer-python-gateway | MIT | Dependency |
| node | MIT | Run as a service (signer, orchestrator) |
| clearinghouse-batteries | None published | Run as a service; no code copied |
| console, PymtHouse | None published | Out of scope; no code copied |
