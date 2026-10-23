# Python gateway SDK (as relied on by the engine)

Baseline `44df061`. Pinned to `76a1a1c`, the head of [livepeer/livepeer-python-gateway](https://github.com/livepeer/livepeer-python-gateway) `main` on 22 October: "Enforce TLS" ([#76](https://github.com/livepeer/livepeer-python-gateway/pull/76)), streamable runner bodies ([#80](https://github.com/livepeer/livepeer-python-gateway/pull/80)) and the payment session callback ([#81](https://github.com/livepeer/livepeer-python-gateway/pull/81), which superseded [#77](https://github.com/livepeer/livepeer-python-gateway/pull/77) and livepeer #71). The 21 October pin was NE-Build-Track `dev/bytestreaming` `3238731`. See [upstream-baseline.md](upstream-baseline.md) and tech debt T1.

| SDK surface | Engine use |
| --- | --- |
| `discovery.discover_runners(signer_url, signer_headers, discovery_url)` | Every discovery path, so the SDK's entry filter (a `runners` list; a runner needs a URL and an app) applies to all of them. `SignerDiscoverySource` calls it for the signer or `LPB_DISCOVERY_URL`, a `GET /discover-orchestrators` document; it sends no signer headers to a discovery URL. `OrchestratorListDiscoverySource` calls it with only `discovery_url=<address>/discovery`, once per listed orchestrator, at startup (D42) |
| `discovery.orchestrator_discovery_urls(addresses)` | `OrchestratorListDiscoverySource`: splits an inline comma list, validates each address and builds its `/discovery` URL |
| `selection._runner_candidates_from_discovery` (private) | Not used at runtime. `tests/unit/test_adapter_edges.py` checks that `runners_from_entries` agrees with it on URL, app, runner ID, mode, orchestrator and price. A blank price `unit` stays blank, so `call_runner` picks its own payment type. Not used: `discover_orchestrator_runners`, which returns the first orchestrator that answers rather than merging them |
| `live_runner.call_runner(url, runner=…, payload, method, signer_url, signer_headers, timeout)` | `SdkRunnerTransport`; the payment is per call (402 challenge → signer → retry). `JobRequest.payload` is a dict or `None`; at `76a1a1c` the SDK sends `None` as an empty body, so the adapter passes `{}` instead, as earlier pins sent (D41, T15) |
| `call_runner(..., on_payment_session=callback)` (#81) | Called once for each new session the signer signs, after the SDK validates the signer's reply (non-empty `payment` and `segCreds`) and before the paid request goes out. `SdkRunnerTransport` collects `session.manifest_id` into `RunnerReply.manifest_ids` and derives `payment_sent` solely from whether `on_payment_session` ran: if signing fails or times out before the callback runs, `payment_sent` is `False` and the job fails (D41, T14) |
| `LivePaymentSession.manifest_id` | The manifest ID from the 402 challenge. The SDK does not read the signer's state |
| `LivepeerHTTPError.status_code` / `.url` | Runner refusal vs signer refusal; signer 400/481 → `payment_refused` (failover) |
| `selection.reserve_session(...)` → `LiveRunnerSession(app_url, control_url)` | Persistent sessions (M4), proxied by the engine |

**TLS.** At `76a1a1c` every SDK request verifies TLS with aiohttp's default context, the process
trust store ([livepeer-python-gateway#76](https://github.com/livepeer/livepeer-python-gateway/pull/76)).
The engine never turns it off and installs no CA. The discovery URL and the startup probe use that default. On the paid
call a certificate the trust store rejects fails the unpaid first request, before any 402, so the
attempt is `unreachable` with no payment (D42).

Signer headers are per call, so `per_actor` mode sends each actor's own key. **Deferred beyond M2:**
#70 (runner pool size and ordering) and multipart request bodies (D24).
