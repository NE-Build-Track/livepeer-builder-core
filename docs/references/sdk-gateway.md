# Python gateway SDK (as relied on by the engine)

Baseline `44df061`. Pinned to `2017a80`, the head of [livepeer/livepeer-python-gateway](https://github.com/livepeer/livepeer-python-gateway) `main` on 26 October: "Enforce TLS" ([#76](https://github.com/livepeer/livepeer-python-gateway/pull/76)), streamable runner bodies ([#80](https://github.com/livepeer/livepeer-python-gateway/pull/80)), the payment session callback ([#81](https://github.com/livepeer/livepeer-python-gateway/pull/81), which superseded [#77](https://github.com/livepeer/livepeer-python-gateway/pull/77) and livepeer #71) and the runner-side session stop ([#83](https://github.com/livepeer/livepeer-python-gateway/pull/83), [below](#runner-session-stop-83)). The 22 October pin was `76a1a1c`; every call the engine makes is the same in both. The 21 October pin was NE-Build-Track `dev/bytestreaming` `3238731`. See [upstream-baseline.md](upstream-baseline.md) and tech debt T1.

The engine's paid runner call is its own (`adapters/runner_call.py`, D53), adapted from
`live_runner.call_runner` at `2017a80` (MIT). It sends the runner request over aiohttp, answers the 402
itself and signs through the SDK's signer client. `tests/unit/test_sdk_contract.py` fails when the
pinned SDK's `call_runner` path changes, so a pin bump reviews the copy.

| SDK surface | Engine use |
| --- | --- |
| `discovery.discover_runners(discovery_url=…)` | `HttpDiscoverySource` reads `LPB_DISCOVERY_URL`, with no signer and no credential (#45) |
| `remote_signer.get_signer_info(signer_url, headers)` | The payer address for the runner request (`Livepeer-Payer-Address`). The SDK caches it per signer and bearer |
| `remote_signer.LivePaymentSession(signer_url, signer_headers=…, type, challenge, app, max_price)` | One per 402 challenge. `type` comes from the runner's price unit (`fixed`, or `live` for `seconds`/`hour`) and `max_price` is the advertised price padded by 1.2%, as `call_runner` does |
| `LivePaymentSession.get_payment()` | Signs the session. The transport reports the `manifest_id` as `unknown` (`on_payment_state`) before it, `unpaid` when the signer refused (400, 401, 402, 403, 481, a 480 refresh), and signed (`on_signed`, `payment_sent`) once it returns, before the paid request goes out (D41, D42, D53). A lost or malformed reply, a 5xx or a cancel leaves it `unknown` |
| `LivePaymentSession.run_payments()` | Metered funding while the work runs: from the paid request until the buffered call returns or the stream closes. Renewals are not new sessions and are not reported |
| `LivePaymentSession.manifest_id` | The manifest ID from the 402 challenge. The SDK does not read the signer's state |
| `LivepeerHTTPError.status_code` | Signer refusals: 400/481 → `payment_refused` (failover, paid or not: D26, D52), others → `payment`. The runner's own refusals are mapped by the engine, which knows they came from the runner URL |
| `call_runner(...)` | **Not called** since D53; the copy follows it at `2017a80`. A missing `payload` is still sent as `{}` (T15) |
| `selection.reserve_session(...)` → `LiveRunnerSession(app_url, control_url)` | Persistent sessions (M4), proxied by the engine |

## Runner session stop (#83)

[#83](https://github.com/livepeer/livepeer-python-gateway/pull/83) adds
`LiveRunnerRegistration.stop_session()` and `LiveRunnerSessionEvent.session_token`. Both are on the
runner side. The engine does not call them, and the token never reaches the caller: the orchestrator
sends it to the runner on the `reserved` event and as the `Livepeer-Session-Token` header of the
request it proxies.

The session the runner may now stop is the one the engine paid for. go-livepeer
(`server/ai_http.go`, `reservePaidLiveRunnerSession`) reserves the runner session under the payment's
`ManifestID`, so the orchestrator's session ID is the `manifest_id` the engine records. The
reservation happens on the paid retry, after the signer's payment was validated, so every refusal on that
path arrives with `payment_sent=True`. Only a 502 can come after the app started:

| Status from the runner URL | Cause |
| --- | --- |
| 404 `runner not found` | The runner is not ready, or it deregistered |
| 404 `runner session not found` | The runner stopped the session before the request was proxied (#83) |
| 409 `session already exists` | The manifest is already a live session on that runner |
| 503 `no capacity available for runner` | Capacity was taken between discovery and reservation |
| 400 | The orchestrator rejected the payment or the price |
| 502 | The proxy lost the runner, before or during its answer |

The engine fails these over to a runner on another orchestrator, except a 400, which would be
charged on every orchestrator ([engine.md](../contracts/engine.md#behavior-the-contract-fixes), D52).
The charge stays on the attempt as `failed_after_payment`.

The orchestrator mints a fresh manifest for each 402 challenge (`core.RandomManifestID()`): 4 random
bytes from `math/rand`, written as 8 hex characters. It rejects a duplicate only while the first
session is live (409). See T13.

Signer headers are per call, so `per_actor` mode sends each actor's own key. **Deferred beyond M2:**
#70 (runner pool size and ordering) and multipart request bodies (D24).
