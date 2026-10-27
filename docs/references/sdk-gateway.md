# Python gateway SDK (as relied on by the engine)

Baseline `44df061`. Pinned to NE-Build-Track `202721c`, the published payment-intent companion
([PR #7](https://github.com/NE-Build-Track/livepeer-python-gateway/pull/7)), based on upstream
`2017a80`. It retains upstream #81 validation and prompt cancellation, and adds pre-sign intent
and signing-error hooks. See [upstream-baseline.md](upstream-baseline.md) and tech debt T1.

| SDK surface | Engine use |
| --- | --- |
| `discovery.discover_runners(discovery_url=…)` | `HttpDiscoverySource` reads `LPB_DISCOVERY_URL`, with no signer and no credential (#45) |
| `live_runner.call_runner(url, runner=…, payload, method, signer_url, signer_headers, timeout, max_payment_challenge_retries)` | `SdkRunnerTransport`; the payment is per call (402 challenge → signer → retry). `JobRequest.payload` is a dict or `None`; at `76a1a1c` the SDK sends `None` as an empty body, so the adapter passes `{}` instead, as earlier pins sent (D41, T15) |
| `call_runner(..., on_payment_attempt=callback)` | Awaited before signing. The engine records `unknown` intent durably; failure aborts signing (D45) |
| `call_runner(..., on_payment_error=callback)` | Receives the session and original signing/validation exception before any SDK refresh retry. The adapter records `unpaid` for explicit signer rejection (400/401/402/403/480/481/482); transport failures, 5xx and malformed success remain `unknown`. Cancellation bypasses this hook and leaves intent unresolved |
| `call_runner(..., on_payment_session=callback)` | After validated signer success, before paid dispatch. The adapter awaits `on_signed(manifest_id)` and records `paid`; it preserves upstream #81 behavior. Missing callback alone does not establish refusal (D45 supersedes D41) |
| `call_runner(..., stream=True)` → `LiveRunnerCallStream` | `SdkRunnerTransport.open` (`jobs.streaming`); the 402 is paid before the first byte, and the engine bounds the wait for the runner's headers (`first_byte_timeout_s`, D35) |
| `LivePaymentSession.manifest_id` | The manifest ID from the 402 challenge. The SDK does not read the signer's state |
| `LivepeerHTTPError.status_code` / `.url` | Runner refusal vs signer refusal; signer 400/481 → `payment_refused` (failover) |
| `selection.reserve_session(...)` → `LiveRunnerSession(app_url, control_url)` | Persistent sessions (M4), proxied by the engine |

Signer headers are per call, so `per_actor` mode sends each actor's own key. **Deferred beyond M2:**
#70 (runner pool size and ordering) and multipart request bodies (D24).
