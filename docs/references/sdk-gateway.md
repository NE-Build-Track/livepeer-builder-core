# Python gateway SDK (as relied on by the engine)

Native HTTP review pin: **NE-Build-Track/livepeer-python-gateway `f500ecd`**, from
[PR #6](https://github.com/NE-Build-Track/livepeer-python-gateway/pull/6), supplies validated
runner headers and opt-in error streams. This supersedes the historical pin below for
this branch. Publication stays in NE-Build-Track forks.


Baseline `44df061`. Pinned to `76a1a1c`, the head of [livepeer/livepeer-python-gateway](https://github.com/livepeer/livepeer-python-gateway) `main` on 22 October: "Enforce TLS" ([#76](https://github.com/livepeer/livepeer-python-gateway/pull/76)), streamable runner bodies ([#80](https://github.com/livepeer/livepeer-python-gateway/pull/80)) and the payment session callback ([#81](https://github.com/livepeer/livepeer-python-gateway/pull/81), which superseded [#77](https://github.com/livepeer/livepeer-python-gateway/pull/77) and livepeer #71). The 21 October pin was NE-Build-Track `dev/bytestreaming` `3238731`. See [upstream-baseline.md](upstream-baseline.md) and tech debt T1.

| SDK surface | Engine use |
| --- | --- |
| `discovery.discover_runners(discovery_url=…)` | `HttpDiscoverySource` reads `LPB_DISCOVERY_URL`, with no signer and no credential (#45) |
| `live_runner.call_runner(url, runner=…, payload, method, signer_url, signer_headers, timeout, max_payment_challenge_retries)` | `SdkRunnerTransport`; the payment is per call (402 challenge → signer → retry). `JobRequest.payload` is a dict or `None`; at `76a1a1c` the SDK sends `None` as an empty body, so the adapter passes `{}` instead, as earlier pins sent (D41, T15) |
| `call_runner(..., on_payment_session=callback)` (#81) | Called once for each new session the signer signs, after the SDK validates the signer's reply (non-empty `payment` and `segCreds`) and before the paid request goes out. `SdkRunnerTransport` awaits the engine's `on_signed(session.manifest_id)` there, so the evidence is stored before the paid request (the SDK awaits an async callback; D42), collects `session.manifest_id` into `RunnerReply.manifest_ids` and derives `payment_sent` solely from whether `on_payment_session` ran: if signing fails or times out before the callback runs, `payment_sent` is `False` and the job fails (D41, T14) |
| `call_runner(..., stream=True)` → `LiveRunnerCallStream` | `SdkRunnerTransport.open` (`jobs.streaming`); the 402 is paid before the first byte, and the engine bounds the wait for the runner's headers (`first_byte_timeout_s`, D35) |
| `LivePaymentSession.manifest_id` | The manifest ID from the 402 challenge. The SDK does not read the signer's state |
| `LivepeerHTTPError.status_code` / `.url` | Runner refusal vs signer refusal; signer 400/481 → `payment_refused` (failover) |
| `selection.reserve_session(...)` → `LiveRunnerSession(app_url, control_url)` | Persistent sessions (M4), proxied by the engine |

Signer headers are per call, so `per_actor` mode sends each actor's own key. **Deferred beyond M2:**
#70 (runner pool size and ordering) and multipart request bodies (D24).
