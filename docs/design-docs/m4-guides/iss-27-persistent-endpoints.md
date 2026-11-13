# ISS-27: Persistent application endpoints (sessions)

> **Questions locked on 12 and 13 November 2026.** Mike answered every open question; see [Questions
> locked](#questions-locked). Where an answer differs from the proposal below, the answer wins, and that
> section says which part it replaces. Decision numbers are assigned in the M4 decision record.

**Status:** proposal for discussion, 29 October 2026. Nothing here is decided until it is locked; locked
rules become decisions, numbered when they are locked.\
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

**Depends on:** ISS-14 ([repeat rules](../m3-guides/iss-11-14-idempotency.md)) and ISS-19 (the matrix).
External: H1 (SDK), H3 (node). Renewals stay the SDK's `run_payments()`: #72 was closed as not planned and
its redirect fix is upstream ([livepeer-python-gateway#87](https://github.com/livepeer/livepeer-python-gateway/issues/87)),
so builder-core owns no funding loop (revised 29 October).

## #73 in brief, and where this guide differs

#73 makes realtime speech-to-text the first case and proposes: fal-style alias routes (`/v1/run`, `/v1/ws`,
`/v1/realtime`, `/v1/queue`), a realtime WebSocket the engine relays (P2), `delivery: "direct"` capability
URLs amending D9 (P3), an engine-owned reserve (P4), a Trickle client behind `MediaChannel` (P5), browser
socket tokens (P6), the `Key` scheme and CORS (P7), a `livepeer-client` wheel (P8), and an eight-step
order. Its claims check out against the code with two exceptions: `Sec-WebSocket-*` is **not** reserved
in `contracts/http.py` (only `upgrade` and `connection` are, lines 6-27), and #73 assumed an
engine-owned funding loop, which builder-core will not own (#72 closed; the loop stays the SDK's). This guide adopts P2 and P4 for ISS-27 and treats the rest as
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
2. **Funding** is the SDK's `run_payments()`, started as jobs start it today, with its result **read
   instead of dropped**: `True` (a renewal answered 404) ends the session `released`; `False` (any other
   4xx but 408 and 429, and a 3xx once the pin includes #87) ends it `funding_stopped`; a cancel is the
   engine's own close. 5xx and network errors are retried every cycle until the orchestrator releases
   the session (Q8). Renewals reuse the stored manifest and are **not** evidence writes; a refresh keeps
   the manifest (node `406b248`), so no new `SignedSession` row. Reading the result closes T17 for
   sessions. Until the pin includes #87, renewals and refreshes follow redirects (accepted risk).
3. **Traffic** (proxy delivery only in M4): `ANY /v1/sessions/{id}/app/{path}` forwards HTTP and SSE to
   `app_url + path` with the caller's body and allowed headers (the `contracts/http.py` rules, plus
   `sec-websocket-*`), streaming both ways; the same path accepts a WebSocket upgrade and relays frames
   verbatim (text as text, binary as binary) with bounded queues. Every request is authenticated and owned.
   The engine sends a first frame `{type: "start", job_id, session_id}` and a last frame
   `{type: "end", job_id, reason}`, as #73 proposes (Q2, locked). Every other frame is the runner's.
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
| `adapters/runner_call.py` | `PaidRequest` exposes "keep funding on return" for the reserve, and the funding task's `run_payments()` result |
| `services/sessions.py` (new) | `SessionService`: state machine, limit and idle watchers, endings, close, recovery, drain. `services/jobs.py` is at 493 lines: keep sessions out of it |
| `services/jobs.py`, `services/discovery.py` | P1: jobs skip persistent runners |
| `services/admission.py` | Key the limiter by any string so a second instance caps sessions per actor |
| `store/` | A new migration (numbered when written): `lpb_sessions` (job_id, state, limits, `control_secret_ref`, opened/closed/last_activity, ending); `SessionRepo` port; matching change if Q7 |
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

**Seams.** Clock, the SDK's `PAYMENT_INTERVAL_S` (monkeypatched, as `test_runner_call.py` does), harness
route statuses, signer call counter. No aiohttp mocks.

**Starter test:** a job for an app whose only runner is `persistent` fails `no_offering` and makes no
runner request (P1; red on `releases/m3`).

```text
Defects (red first)
[ ] P1 jobs.run on a persistent-only app: no_offering, no request sent, no signing
[ ] P2 a non-admin job read of a session attempt shows no raw manifest_id
Open
[ ] open on a free persistent runner: Session open, job running, proxy_path set, no app_url anywhere
[ ] open stores the manifest as unknown before signing; a failed intent write signs nothing (D54)
[ ] reserve refused after signing (503 capacity) fails over to another orchestrator; failed_after_payment
[ ] paid failover stops at LPB_MAX_PAID_ATTEMPTS (D55)
[ ] signer refusal: job failed, unpaid, no failover
[ ] reply missing session_id/app_url/control_url after payment: stop sent, failed_after_payment
[ ] session admission full: NoCapacity before any request; job slots unaffected
[ ] repeated operation_ref returns the existing Session (200, Idempotent-Replayed); a different body is 409
Traffic
[ ] HTTP call through proxy_path reaches app_url+path; body and allowed headers pass, Livepeer-* do not
[ ] SSE through proxy_path arrives chunk by chunk
[ ] WebSocket relay: binary and text frames both ways, unchanged; runner close code reaches the client
[ ] WebSocket relay: the first frame is the engine's start frame and the last is its end frame (Q2)
[ ] another actor's proxy_path is 404; a revoked key mid-session is refused on the next request
Funding and endings
[ ] renewals continue while open; no evidence write per renewal
[ ] renewal 404: ending released, job succeeded, client socket closed with the ending
[ ] renewal 403/409: ending funding_stopped; stop sent
[ ] renewal 5xx: retried each cycle; the session ends only on release
[ ] close: funding stopped, POST control_url/stop once, ending closed
[ ] idle_timeout and max_duration fire on the virtual clock
[ ] engine.stop closes open sessions after the drain window
[ ] restart with an open session: job uncertain, best-effort stop from the SecretStore ref
[ ] no log line or record contains app_url or control_url (structural scan)
Cost
[ ] JobCost pending while open; projected after close
[ ] several Batteries payment sessions for one manifest still match (built only if a live run shows the split, Q7)
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
| livepeer-python-gateway | No | Import `LivePaymentSession`/`LivePaymentChallenge`/`get_signer_info` as today. **Vendor** the reserve-response parsing and the stop (fingerprinted), as D53/#61 did. Renewals stay the SDK's `run_payments()`; its redirect fix comes with a pin bump (#87). Trickle, `Control`, channel and media modules are **not** needed in M4; if the engine ever bridges Trickle, import them in `adapters/trickle.py` and note they follow redirects |
| clearinghouse-batteries | Not for the call | Cost matching may need an engine-side rule change (Q7); a refresh keeps the signer state, so confirm one Batteries session per session in the live run |

## Consumer impact

None breaks. The console may add a sessions view; ChatLPT and inference-gateway do not open sessions.
Ziply.io and the media-runner teams are the first users, through the embedded `SessionManager`.

## Alternatives considered

- **Call the SDK's `reserve_session`** (today's `engine.md` text): breaks D54, D52/D55 and D43 (above).
- **`delivery: "direct"` in M4** (#73 P3): it hands out a capability equal to the reservation (P2). Later.
- **`wss /v1/realtime/{app}` opening on upgrade** (#73 P1): opens a paid session on a GET; alias later.

## Questions locked

Locked by Mike on 12 and 13 November 2026.

- **Q1, scope:** as recommended, with a change. ISS-27 is the session core, the proxy for HTTP, SSE and
  WebSocket, `realtime-transcription` and the embedded `SessionManager`. The other #73 items are M4
  stretch tickets that may move to M5: #118 (alias routes), #119 (per-message socket), #120 (tokens and
  CORS), #121 (Trickle bridge), #122 (`MediaStore`), #123 (the client wheel).
- **Q2, engine frames in the relay:** the engine sends the two frames #73 proposes, a first `{type:
  "start", job_id, session_id}` and a last `{type: "end", job_id, reason}`. Every other frame is the
  runner's, relayed unchanged. **This replaces the recommendation** (a header and close codes only), so
  step 3 of the decision and the relay tests follow #73. The HTTP and SSE proxy still writes nothing into
  the stream.
- **Q3, permission:** `jobs.run` opens a session and `jobs.read` reads one. No `sessions.open`.
- **Q4, the representative endpoint:** `realtime-transcription`, with the loopback fake in CI. John
  arranges the GPU runner, which is needed only for slice 5. Agreed fallback: if no GPU runner is
  available then, the live evidence uses a small CPU-only WebSocket runner, and the
  `realtime-transcription` run follows when a runner exists. Either run confirms the cost matching (Q7)
  and the node's release behaviour.
- **Q5, the job mode and a repeated open:** the job has mode `session`. A second `POST /v1/sessions` with
  the same `operation_ref` returns the existing `Session` in any state, with 200 and `Idempotent-Replayed:
  true`, and never opens a second reservation. A different body is 409 `OperationConflict`. **This
  replaces the recommendation** (409 `OperationExists`) and follows the async rule of ISS-26.
- **Q6, redaction:** a keyed hash replaces `manifest_id` on non-admin reads of session jobs, and the
  discovery address replaces the session address. The implementation names a key source for installs
  without `LPB_SECRET_KEY`. Checked on 13 November: defect P2 still holds on livepeer/node `2364436`.
- **Q7, matching:** the fallback rule is decided now and built only if a live run shows several Batteries
  payment sessions for one manifest. Several sessions for one key match when the engine has exactly one
  entry for that key. Slice 2 looks for the split on its first funded reserve.
- **Q8, funding failures:** accept what the SDK loop reports. A renewal 404 ends the session `released`;
  any other 4xx except 408 and 429 ends it `funding_stopped`; 5xx and network errors are retried by the
  SDK and are not visible to the engine. The idle limit and maximum duration bound a session whose
  orchestrator is unreachable. Checked on 13 November: the loop is unchanged on SDK `7dc2a07`.

**Other decisions that touch this guide.**

- **Defect P1** (a job sent to a persistent runner reserves a paid session) is fixed in slice 1 of this
  ticket, not ahead of it. Until then the defect is present.
- **Migration numbers.** The guide names migration `0009`. It is numbered when it is written.
  Secret-reference columns are unbounded text (#115).
- **The node pin moves** to a commit at or after `2364436` (#117). The node facts in this guide were read
  at `406b248` and need a second look at the new pin.
- **The builder console** runs the session from an audio file, with the socket on its server side
  (ISS-34).

### Where the runner standard plugs in later

Runner contract v1 (ISS-47) is not vetted yet. This ticket therefore routes by app ID, as today, and reads
no v1 metadata. ISS-48 adopts the standard as the last feature of M4, and it may move to M5.

- **Working assumption for ISS-48 (12 November):** app IDs stay the routing key; v1 metadata describes a
  runner and never routes; missing, old or unreadable metadata is ignored and logged; model and profile
  are filters; no schema check.
- **Criterion moved to ISS-48 on 13 November:** persistent endpoints come from the runner's v1 session
  declaration (start and stop paths, channels, idle and drain timeouts) and its registry entry.
- **The seam to keep:** `adapters/reservation.py` is the one place that knows how a session starts and
  stops, so a declared start path can replace the reserve route there.

## Delivery slices

1. P1 fix with its regression test (can ride M3 with Mike's agreement).
2. `adapters/reservation.py` with harness tests, reading the SDK loop's result.
3. `SessionService`, store, limits, endings, recovery, embedded `SessionManager`, `FakeEngine` parity.
4. REST routes, HTTP/SSE proxy, P2 redaction, docs.
5. WebSocket relay, `websockets` in `[server]`, live evidence on `realtime-transcription`.
