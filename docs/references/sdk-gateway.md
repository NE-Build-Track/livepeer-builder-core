# Python gateway SDK (as relied on by the engine)

Baseline `44df061`. Pinned temporarily to `3238731`, the head of NE-Build-Track/livepeer-python-gateway `dev/bytestreaming` (**[NE livepeer-python-gateway#5](https://github.com/NE-Build-Track/livepeer-python-gateway/pull/5)**): upstream `main` plus the payment session callback from [livepeer-python-gateway#77](https://github.com/livepeer/livepeer-python-gateway/pull/77) (which replaced livepeer #71) and streamable runner bodies. See [upstream-baseline.md](upstream-baseline.md) and tech debt T1.

| SDK surface | Engine use |
| --- | --- |
| `discovery.discover_runners(signer_url, signer_headers, discovery_url)` | `SignerDiscoverySource` |
| `live_runner.call_runner(url, runner=…, payload, method, signer_url, signer_headers, timeout)` | `SdkRunnerTransport`; the payment is per call (402 challenge → signer → retry). `JobRequest.payload` is a dict or `None`; since `3238731`, `None` sends an empty body instead of `{}` |
| `call_runner(..., on_payment_session=callback)` (#77) | Called once for each new session the signer signs, after the SDK accepts the signer's reply (non-empty `payment` and `segCreds`) and before the paid request goes out. `SdkRunnerTransport` collects `session.manifest_id` into `RunnerReply.manifest_ids`, and treats any signed session as `payment_sent` (failover only before payment) |
| `LivePaymentSession.manifest_id` | The manifest ID from the 402 challenge. The SDK does not read the signer's state |
| `LivepeerHTTPError.status_code` / `.url` | Runner refusal vs signer refusal; signer 400/481 → `payment_refused` (failover) |
| `selection.reserve_session(...)` → `LiveRunnerSession(app_url, control_url)` | Persistent sessions (M4), proxied by the engine |

Signer headers are per call, so `per_actor` mode sends each actor's own key. **Deferred beyond M2:**
#70 (runner pool size and ordering) and multipart request bodies (D24).
