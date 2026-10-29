# ISS-27: Persistent application endpoints (sessions)

**Status:** proposal for discussion, 29 October 2026. Nothing here is decided until it is locked; locked
rules become decisions (D57 onward).\
**Tracker:** ISS-27 ([Network-Engineering-SPE#41](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/41)).
Design input: John's [builder-core#73](https://github.com/NE-Build-Track/livepeer-builder-core/issues/73)
(media outputs and realtime sessions), and the media work plan
([2026-10-29-media-work-plan.md](../2026-10-29-media-work-plan.md)).\
**Owner:** John (engine). **Reviewers:** Mike, John.\
**Verified against:** builder-core `releases/m3` at `d3df806`; livepeer-python-gateway `2017a80` (the pin,
also `origin/main`); livepeer/node `406b248` (the stack pin; `origin/main` is `a4b68ed`); runner-app-examples
`a526b43`.

## Done when

- Invoke supported discovered **persistent** endpoints with the agreed inputs/results contract, and keep
  owned job, result and payment references.
- Demonstrate a representative endpoint, and distinguish it from queued completion (ISS-26) and
  continuous streaming (stretch scope, `engine.md#persistent-sessions`).

**Depends on:** ISS-14 ([repeat rules](../m3-guides/iss-11-14-idempotency.md)), ISS-19 (the matrix), and
#72 ([the engine owns renewals](../m3-guides/72-payment-redirects.md)). External: H1 (SDK), H3 (node).

## #73 in brief, and where this guide differs

#73 makes realtime speech-to-text the first case and proposes: fal-style alias routes (`/v1/run`, `/v1/ws`,
`/v1/realtime`, `/v1/queue`), a realtime WebSocket the engine relays (P2), `delivery: "direct"` capability
URLs amending D9 (P3), an engine-owned reserve (P4), a Trickle client behind `MediaChannel` (P5), browser
socket tokens (P6), the `Key` scheme and CORS (P7), a `livepeer-client` wheel (P8), and an eight-step
order. Its claims check out against the code with two exceptions: `Sec-WebSocket-*` is **not** reserved
in `contracts/http.py` (only `upgrade` and `connection` are, lines 6-27), and the SDK's `run_payments`
is no longer the loop once #72 lands. This guide adopts P2 and P4 for ISS-27 and treats the rest as
later slices or open questions (Q1-Q4); it does not reject them.

## Current state (verified)

| Area | At `d3df806` |
| --- | --- |
| Contract | `contracts/reserved.py:37-58`: `SessionRequest{app, operation_ref, max_duration_s=3600, idle_timeout_s=300}`, `Session{id, job_id, …, state: opening\|open\|closed\|failed, proxy_path, opened_at, closed_at}` |
| Port | `protocols/__init__.py:382` `SessionManager.open/close`; `EngineApi.sessions` (`api.py:237`); placeholder `services/reserved.py:17` raises `NotImplementedInMilestone("persistent sessions","M4","ISS-27")` |
| REST | `POST /v1/sessions` only (`http/app.py:293`), 501 via the handler at line 445; tested in `tests/unit/test_http.py:108`. No close, get or proxy route |
| Docs | `engine.md#persistent-sessions` (line 293): SDK `reserve_session`, engine proxies all traffic, cost grows, limits, restart lets it lapse. D8, D9 (`m2-decisions.md`) |
| Discovery | `Runner.mode` (`contracts/discovery.py:29`), `Offering.modes`. **The job path ignores `mode`** (`services/jobs.py:266-283`, `services/discovery.py:80-82`) |
| Admission | `AdmissionLimiter` counts jobs globally and per app (`services/admission.py`); no per-actor or session cap |
| Funding | `adapters/runner_call.py` `start_funding` runs the SDK's `run_payments()`; its outcome is dropped (T17) |
| WebSocket | none: uvicorn is installed without `websockets`/`wsproto`, so the `[server]` extra cannot accept an upgrade |

**Defect P1 (found while verifying).** A persistent runner's discovery URL is the reserve route
(`{orchestrator}/apps/{runner_id}/session`, node `orchestrator/proxy.go:66-71`, `registry.go:419-427`).
`POST /v1/jobs` with that app and no `path` therefore **reserves a paid session**: the reply
`{session_id, app_url, control_url}` comes back inline to the caller (breaking D9), the engine stops funding
when the response ends, and the reservation lapses once its credit runs out. Fix first, test-first: the job
path skips `mode == "persistent"` runners and fails `no_offering` with "open a session" in the message.

**Defect P2.** D9 is not enforceable as written. On node `406b248` the paid `session_id` **is** the
challenge's `manifest_id` (`orchestrator/payment.go:75-122`, `reserveWithPrice(runnerID, manifest, …)` at line 103), `app_url` is `{runner.url}/{session_id}/app`
for non-proxied runners, and `POST {control_url}/stop` has no credential check (`http.go:250-256`,
`registry.go:557-573` checks the token only for runner callbacks). `Job.attempts[].runner_url` and
`sessions[].manifest_id` are on every job read (`contracts/jobs.py:107-139`), and an **auditor** reads every
job in its application (D46). Once sessions exist, a job read hands out the means to use or stop someone's
paid session. Proposed fix in [Decision](#decision-proposed) (redaction).

### How the network does it (node `406b248`, SDK `2017a80`)

- **Reserve:** `POST /apps/{runner_id}/session` answers 402 with `{payment_params, manifest_id,
  payment_url}`; the paid retry gets `{session_id, app_url, control_url}` (`payment.go:75-122`). Capacity
  503 ("runner at capacity", `registry.go:451-458`) comes **after** signing, so it is a paid refusal (D52).
- **Funding:** renewals go to `payment_url` = `…/session/{manifest}/payment` (`payment_engine.go:120`). The
  orchestrator charges whole seconds on a 1 s tick and releases the session when the balance is short
  (`ChargePaidSessions`, `payment.go:227-237`; `app.go:374`). After a release, renewals answer 404. A 480
  refresh keeps the same manifest (`RefreshChallenge`, `payment_engine.go:143-156`).
- **Traffic:** `/apps/{runner_id}/session/{session_id}/app/{path…}` is an `httputil.ReverseProxy`
  (`http.go:361-410`) that keeps HTTP, SSE and WebSocket semantics (`docs/http-api.md:20`) and adds
  `Livepeer-Session-*` headers toward the runner only. 256 proxy slots per orchestrator (`http.go:53`).
- **Stop:** `POST {control_url}/stop`; the SDK's `stop_runner_session` (`live_runner.py:1097-1126`).
- **SDK reserve:** `selection.reserve_session` runs its own discovery, pays inside `cursor.next()` →
  `call_runner` with no hook before `get_payment`, and starts `run_payments` itself (`selection.py:188-230`).
  It cannot keep D54, D52/D55 or D43, as #73 says.
- **Live and Trickle modules:** `TricklePublisher`/`TrickleSubscriber` (segment POST/GET), `Control`
  (JSON messages on a Trickle channel with a keepalive), `ChannelReader`/`JSONLReader`/`JSONLWriter`,
  `MediaOutput`/`MediaPublish` (PyAV). None sets `allow_redirects=False`. The package `__init__` imports
  `media_publish`, so PyAV already loads with the SDK.
- **Representative runners** (runner-app-examples): `realtime-transcription` (WebSocket) and `echo` (Trickle).

## Decision proposed

**A session is a job that holds one paid reservation. The engine reserves, funds, proxies and ends it;
clients only see `proxy_path`.** #73's P4 and P2 inside the reserved contract; no other new surface in M4.

1. **Open** (`SessionService.open`): policy `jobs.run` (Q3) → session admission → candidates with
   `mode == "persistent"` → write the job (`mode="session"`, Q5) and the attempt → engine-owned reserve:
   the existing `PaidRequest` 402 loop (D53/D54 intent before signing, origin check, no redirects) POSTs
   to `runner.url` and keeps funding running on return → parse `{session_id, app_url, control_url}` →
   session `open`. Refusals fail over under D52/D55 exactly as jobs do. A reply without the three fields
   after payment: best-effort stop, attempt `failed_after_payment`.
2. **Funding** is the engine loop from #72 (`fund()` → `FundingEnd`): renewals reuse the stored manifest
   and are **not** evidence writes; a refresh keeps the manifest (node `406b248`), so no new
   `SignedSession` row. `FundingEnd.released` ends the session `released`; `refused`/`redirected` end it
   `funding_stopped`. This closes T17 for sessions by construction.
3. **Traffic** (proxy delivery only in M4): `ANY /v1/sessions/{id}/app/{path}` forwards HTTP and SSE to
   `app_url + path` with the caller's body and allowed headers (the `contracts/http.py` rules, plus
   `sec-websocket-*`), streaming both ways; the same path accepts a WebSocket upgrade and relays frames
   verbatim (text as text, binary as binary) with bounded queues. Every request is authenticated and owned.
   The job id rides the `101`/response as `Livepeer-Job-Id`; the ending is a WebSocket close code and
   reason, not an injected frame (Q2).
4. **Limits and endings:** `max_duration_s`, `idle_timeout_s` (traffic through the proxy resets it),
   `LPB_MAX_SESSIONS`, `LPB_MAX_SESSIONS_PER_ACTOR`, refused with `NoCapacity` **before** payment. Endings:
   `closed`, `idle`, `max_duration`, `released`, `funding_stopped`, `engine_stopped`, `interrupted`,
   `open_failed`. Close = stop funding, `POST {control_url}/stop`, record. Job state: `succeeded` for an
   orderly ending of an opened session, `uncertain` for `interrupted` and an upstream drop after payment,
   `failed` before payment, following #73's ending table.
5. **Restart and stop:** `recover_interrupted` already makes a running paid job `uncertain`
   (`services/failover.py:59-74`); sessions add a best-effort stop from the stored control reference.
   `engine.stop()` closes open sessions after `LPB_SESSION_DRAIN_TIMEOUT_S`, as D36 does for streams.
6. **Secrets and redaction (P2):** `app_url` stays in memory; `control_url` goes to the `SecretStore`
   (needed for the stop after a restart) and never onto a record or log. Job reads by non-admins return
   session attempts with `manifest_id` replaced by a keyed hash (Q6), and the session's `runner_url` is the
   discovery URL only.
7. **Cost:** one session = one job; `JobCost` is `pending` while open and is projected like any job after.
   A refresh keeps the manifest and the signer state (the SDK keeps `_state`; Batteries `ca09e47` keys
   payment sessions by the signer's `state_id`), so a session should stay one Batteries payment session.
   **Confirm it in the live run.** If it does not, `store/matching.py` (one Batteries session per key, else
   `ambiguous`) would make every long session `uncertain`; the rule must accept several Batteries sessions
   for one `(allocation_id, manifest_id)` when the engine has a single log entry for it (Q7).

**Contracts and docs.** Move the session contracts to `contracts/sessions.py` (re-exported, so imports keep
working); add `SessionEnding`, `Session.ending`, `get`/`list`/`close` on `SessionManager`; extend
`ExecutionMode` with `session`. Rewrite `engine.md#persistent-sessions` with a mode table (immediate,
streamed D29, async ISS-26, session ISS-27, continuous media = stretch), amend D9's wording (proxy only;
owner-redacted manifests), add the session rules to `payments.md`, the sequence in
`architecture-diagrams.md` §10, and T17 (sessions closed) in the tech-debt tracker.

## Code areas

| File | Change |
| --- | --- |
| `contracts/sessions.py` (new), `contracts/reserved.py`, `contracts/jobs.py` | Session contracts and endings; `ExecutionMode` gains `session` |
| `protocols/__init__.py` | `SessionManager` gains `get`, `list_sessions`, `close`; new optional `ReservationTransport` (like `StreamingRunnerTransport`, D33): `reserve(runner, credential, recorder) -> Reservation` with `request`, `websocket`, `funding_ended`, `stop` |
| `adapters/reservation.py` (new) | Engine-owned reserve over `PaidRequest`; vendored response parsing from `selection.reserve_session` and stop from `stop_runner_session`; aiohttp HTTP and `ws_connect` to `app_url`; origin check on `app_url`/`control_url` |
| `adapters/runner_call.py` | `PaidRequest` exposes "keep funding on return" for the reserve; #72's `fund()`/`FundingEnd` |
| `services/sessions.py` (new) | `SessionService`: state machine, limit and idle watchers, endings, close, recovery, drain. `services/jobs.py` is at 493 lines: keep sessions out of it |
| `services/jobs.py`, `services/discovery.py` | P1: jobs skip persistent runners |
| `services/admission.py` | Key the limiter by any string so a second instance caps sessions per actor |
| `store/` | Migration `0009`: `lpb_sessions` (job_id, state, limits, `control_secret_ref`, opened/closed/last_activity, ending); `SessionRepo` port; matching change if Q7 |
| `http/sessions.py` (new), `http/app.py` | `POST /v1/sessions`, `GET /v1/sessions[/{id}]`, `POST /v1/sessions/{id}/close`, `ANY` + WebSocket on `/v1/sessions/{id}/app/{path}` |
| `runtime/assembly.py`, `runtime/engine.py` | Wire `SessionService` in place of `ReservedSessions`; supervise per-session tasks; drain on stop |
| `config.py` | `LPB_MAX_SESSIONS`, `LPB_MAX_SESSIONS_PER_ACTOR`, `LPB_SESSION_DRAIN_TIMEOUT_S`, ceilings on duration and idle |
| `pyproject.toml` | `[server]` gains `websockets` (or `wsproto`) so uvicorn accepts upgrades |
| `testing/fakes.py`, `testing/fake_engine.py` | `ScriptedReservations` fake; `FakeEngine` sessions with a parity test |
| `tests/structure/test_layers.py` | `aiohttp` and `livepeer_gateway` also allowed in `adapters/reservation.py` |
| `tests/unit/test_sdk_contract.py` | Fingerprint `selection.reserve_session`, `live_runner.stop_runner_session` |

## Test plan (test-driven)

**Levels.** Service tests with `ScriptedReservations` and `FixedClock` (a virtual clock for hour-long
limits); adapter tests against the loopback harness (`tests/unit/livepeer_net.py`) extended with the node's
persistent routes (402 reserve, `/payment` renewals, `/stop`, `/app/{path}` HTTP and WebSocket) so real
aiohttp meets a real server; HTTP tests with httpx for REST and Starlette's `TestClient.websocket_connect`
for the relay; `lpb_sessions` store contract tests on SQLite and PostgreSQL. One live evidence page against
`realtime-transcription` on the dev stack.

**Seams.** Clock, renewal `interval_s`, harness route statuses, signer call counter. No aiohttp mocks.

**Starter test:** a job for an app whose only runner is `persistent` fails `no_offering` and makes no
runner request (P1; red on `releases/m3`).

```text
Defects (red first)
[ ] P1 jobs.run on a persistent-only app: no_offering, no request sent, no signing
[ ] P2 a non-admin job read of a session attempt shows no raw manifest_id     [?] Q6
Open
[ ] open on a free persistent runner: Session open, job running, proxy_path set, no app_url anywhere
[ ] open stores the manifest as unknown before signing; a failed intent write signs nothing (D54)
[ ] reserve refused after signing (503 capacity) fails over to another orchestrator; failed_after_payment
[ ] paid failover stops at LPB_MAX_PAID_ATTEMPTS (D55)
[ ] signer refusal: job failed, unpaid, no failover
[ ] reply missing session_id/app_url/control_url after payment: stop sent, failed_after_payment
[ ] session admission full: NoCapacity before any request; job slots unaffected
[ ] repeated operation_ref follows the ISS-14 table                         [?] Q5
Traffic
[ ] HTTP call through proxy_path reaches app_url+path; body and allowed headers pass, Livepeer-* do not
[ ] SSE through proxy_path arrives chunk by chunk
[ ] WebSocket relay: binary and text frames both ways, unchanged; runner close code reaches the client
[ ] another actor's proxy_path is 404; a revoked key mid-session is refused on the next request
Funding and endings
[ ] renewals continue while open; no evidence write per renewal
[ ] renewal 404: ending released, job succeeded, client socket closed with the ending
[ ] renewal 403/409 or 3xx: ending funding_stopped; stop sent                [?] Q8
[ ] close: funding stopped, POST control_url/stop once, ending closed
[ ] idle_timeout and max_duration fire on the virtual clock
[ ] engine.stop closes open sessions after the drain window
[ ] restart with an open session: job uncertain, best-effort stop from the SecretStore ref
[ ] no log line or record contains app_url or control_url (structural scan)
Cost
[ ] JobCost pending while open; projected after close
[?] several Batteries payment sessions for one manifest still match           Q7
Parity
[ ] FakeEngine sessions match every row above that the facade exposes
```

**Gears.** Obvious Implementation for routes and limits; Triangulate the ending classification.

**Definition of done.** List empty or answered; `AGENTS.md` checks green (`uv run pytest`, coverage gates,
`diff-cover`, `ruff`, `mypy` strict, `scripts/check_docs.py`, `lpb gen-docs`, `lpb smoke`), store tests on
PostgreSQL; docs above updated; an evidence page in `docs/references/evidence/` showing a paid
`realtime-transcription` session with its cost reconciled.

## Upstream dependencies

| Upstream | Needed? | Why / workaround |
| --- | --- | --- |
| livepeer/node | No (pin `406b248`) | Reserve, renewals, proxy (incl. WebSocket) and stop all exist. Its gaps (unauthenticated stop, `app_url` as a bearer capability, no release reason) are contained engine-side: proxy only, secrets kept out of records, P2 redaction, "released" only from a payment 404. Recheck `a4b68ed` (`f52724d` adds `session_token` to the reserved event, runner side) before re-pinning |
| livepeer-python-gateway | No | Import `LivePaymentSession`/`LivePaymentChallenge`/`get_signer_info` as today. **Vendor** the reserve-response parsing and the stop (fingerprinted), as D53/#61 did; #72 already vendors the renewal loop. Trickle, `Control`, channel and media modules are **not** needed in M4; if the engine ever bridges Trickle, import them in `adapters/trickle.py` and note they follow redirects |
| clearinghouse-batteries | Not for the call | Cost matching may need an engine-side rule change (Q7); verify the refresh behavior at `ca09e47` first |

## Consumer impact

None breaks. The console may add a sessions view; ChatLPT, inference-gateway and the enterprise desk do
not open sessions. The Mux and media-runner teams are the first users, through the embedded `SessionManager`.

## Alternatives considered

- **Call the SDK's `reserve_session`** (today's `engine.md` text): breaks D54, D52/D55 and D43 (above).
- **`delivery: "direct"` in M4** (#73 P3): it hands out a capability equal to the reservation (P2). Later.
- **`wss /v1/realtime/{app}` opening on upgrade** (#73 P1): opens a paid session on a GET; alias later.

## Open questions to lock

- **Q1. Scope.** ISS-27 = session core + proxy (HTTP, SSE, WebSocket) + one representative endpoint
  (recommended). #73's alias routes, per-message socket, tokens, CORS, Trickle bridge, `MediaStore` and the
  client wheel become their own tickets (the wheel decided with #25).
- **Q2. Engine frames in the relay.** #73 injects `start`/`end` JSON frames; that collides with runner
  frames and writes into the stream (against D29/D37's spirit). Recommend a header plus close codes.
- **Q3. Permission.** Reuse `jobs.run` (recommended, as #73 says) or a new `sessions.open`?
- **Q4. Representative endpoint.** `realtime-transcription` (needs a GPU) for the live evidence, plus the
  loopback fake in CI (recommended); confirm with ISS-19.
- **Q5. `ExecutionMode` and repeats.** Add `session` to the job mode (recommended); a repeated
  `operation_ref` on open is 409 `OperationExists` like a job.
- **Q6. Redaction.** Keyed hash of `manifest_id` for non-admin reads (recommended), or drop sessions from
  non-admin reads?
- **Q7. Matching.** If refreshes split Batteries sessions, accept many per key with one log entry
  (recommended) instead of `ambiguous`.
- **Q8. Funding failure threshold.** End after two consecutive retryable renewal failures (the media plan's
  default), or keep #72's retry-every-cycle until the orchestrator releases?

## Delivery slices

1. P1 fix with its regression test (can ride M3 with Mike's agreement).
2. #72 (engine `fund()` and `FundingEnd`), then `adapters/reservation.py` with harness tests.
3. `SessionService`, store, limits, endings, recovery, embedded `SessionManager`, `FakeEngine` parity.
4. REST routes, HTTP/SSE proxy, P2 redaction, docs.
5. WebSocket relay, `websockets` in `[server]`, live evidence on `realtime-transcription`.
