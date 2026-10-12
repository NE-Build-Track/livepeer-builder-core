# Python gateway SDK (as relied on by the engine)

Baseline `44df061` plus SDK #71 (pinned at fork commit `3250c08`; see
[upstream-baseline.md](upstream-baseline.md)).

| SDK surface | Engine use |
| --- | --- |
| `discovery.discover_runners(signer_url, signer_headers, discovery_url)` | `SignerDiscoverySource` |
| `live_runner.call_runner(url, runner=…, payload, method, signer_url, signer_headers, timeout)` | `SdkRunnerTransport`; the payment is per call (402 challenge → signer → retry) |
| `LiveRunnerCallResult.auth_ids` / `session_id` (#71) | `Attempt.auth_ids`, the cost key; `manifest_id` label |
| `LivepeerGatewayError.payment_sent` / `.auth_ids` (#71) | Failover only before payment; charges on failed attempts |
| `selection.reserve_session(...)` → `LiveRunnerSession(app_url, control_url)` | Persistent sessions (M4), proxied by the engine |

**Known gap (T9):** `auth_id_from_signer_state` only reads go-livepeer's `"State"` key; livepeer/node returns `"state"`, so `auth_ids` come back empty until #71 is fixed.

Signer headers are per call, so `per_actor` mode sends each actor's own key. **Deferred beyond M2:**
#70 (runner pool size and ordering) and multipart request bodies (D24).
