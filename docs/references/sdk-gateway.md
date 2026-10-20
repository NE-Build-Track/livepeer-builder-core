# Python gateway SDK (as relied on by the engine)

Baseline `60aed2e` (`releases/final`, which includes upstream [#76](https://github.com/livepeer/livepeer-python-gateway/pull/76) "Enforce TLS") plus **[livepeer-python-gateway#81](https://github.com/livepeer/livepeer-python-gateway/pull/81)** ([NE-Build-Track#3](https://github.com/NE-Build-Track/livepeer-python-gateway/pull/3)), which replaced #77. Pinned temporarily to its head `30fa1ef`, fetched from NE-Build-Track/livepeer-python-gateway; see [upstream-baseline.md](upstream-baseline.md) and tech debt T1.

| SDK surface | Engine use |
| --- | --- |
| `discovery.discover_runners(signer_url, signer_headers, discovery_url)` | `SignerDiscoverySource` |
| `live_runner.call_runner(url, runner=…, payload, method, signer_url, signer_headers, timeout)` | `SdkRunnerTransport`; the payment is per call (402 challenge → signer → retry) |
| `call_runner(..., on_payment_session=callback)` (#81 / NE#3) | Called for every new payment session obtained from the signer, before the paid request goes out. `SdkRunnerTransport` collects `session.manifest_id` into `RunnerReply.manifest_ids`, and treats any reported session as `payment_sent` (failover only before payment) |
| `LivePaymentSession.manifest_id` | The manifest ID from the 402 challenge. The SDK does not read the signer's state |
| `LivepeerHTTPError.status_code` / `.url` | Runner refusal vs signer refusal; signer 400/481 → `payment_refused` (failover) |
| `selection.reserve_session(...)` → `LiveRunnerSession(app_url, control_url)` | Persistent sessions (M4), proxied by the engine |

Signer headers are per call, so `per_actor` mode sends each actor's own key. **Deferred beyond M2:**
#70 (runner pool size and ordering) and multipart request bodies (D24).
