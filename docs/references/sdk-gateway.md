# Python gateway SDK (as relied on by the engine)

Baseline `44df061` plus **eliteprox/livepeer-python-gateway PR 5**, which replaced livepeer #71. Pinned temporarily to its head `dca9cd69`; see [upstream-baseline.md](upstream-baseline.md) and tech debt T1.

| SDK surface | Engine use |
| --- | --- |
| `discovery.discover_runners(signer_url, signer_headers, discovery_url)` | `SignerDiscoverySource` |
| `live_runner.call_runner(url, runner=…, payload, method, signer_url, signer_headers, timeout)` | `SdkRunnerTransport`; the payment is per call (402 challenge → signer → retry) |
| `call_runner(..., on_payment_session=callback)` (PR 5) | Called for every session the signer signs, before the paid request goes out. `SdkRunnerTransport` collects `session.auth_id` into `Attempt.auth_ids` (the cost key), and treats any signed session as `payment_sent` (failover only before payment) |
| `LiveRunnerCallResult.session_id` | The `manifest_id` label |
| `LivepeerHTTPError.status_code` / `.url` | Runner refusal vs signer refusal; signer 400/481 → `payment_refused` (failover) |
| `selection.reserve_session(...)` → `LiveRunnerSession(app_url, control_url)` | Persistent sessions (M4), proxied by the engine |

**T9 (resolved in `84ec542`):** `LivePaymentSession.auth_id` reads go-livepeer's `"State"` or livepeer/node's `"state"`. Before the fix it only read `"State"`; livepeer/node returns `"state"`, so `auth_ids` come back empty until #71 is fixed.

Signer headers are per call, so `per_actor` mode sends each actor's own key. **Deferred beyond M2:**
#70 (runner pool size and ordering) and multipart request bodies (D24).
