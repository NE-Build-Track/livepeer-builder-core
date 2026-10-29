# ISS-34: Reference-app coverage of baseline execution and reporting

**Status:** proposal for discussion, 29 October 2026. Nothing here is decided until it is locked; the
console's own decisions live in its `docs/design-docs/decisions.md`.\
**Tracker:** ISS-34 ([Network-Engineering-SPE#48](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/48)),
source task R2.\
**Owner:** Mike (the console, built by an agent under his review). **Reviewers:** Mike, John.\
**Verified against:** builder-console `main` at `938db3e` (pins builder-core `d3df806`); builder-core
`releases/m3` at `d3df806`.

## Done when

- The minimal app (the builder console, M3 decision 1) runs the selected **immediate, asynchronous and
  persistent-endpoint** jobs, with durable status, results and history, understandable failures, and
  correlated usage and cost.
- Missing metadata, unavailable results and pending or uncertain costs are shown visibly. The console
  keeps using the shared engine; extra screens and product features stay stretch scope.

**Depends on:** ISS-22 (#35, the first journey, done in M3), ISS-28 (#42, failure classification and
recovery), ISS-29 (#43, REST coverage of every mode). In practice also ISS-26 (async) and ISS-27
(persistent endpoints), through ISS-28 and ISS-29. **External:** none.

## Current state (verified)

The console's Builder area (`web/src/app/builder/`) covers immediate jobs only, as M3 decided
(`docs/product-specs/m3-journey.md`, "What M3 covers, and what moves to M4").

| Need | Console today | Engine today |
| --- | --- | --- |
| Immediate job, buffered and streamed | Done: generic form, presets, output views (`components/console/run/`, `app/builder/run/route.ts`) | Done |
| Async job | **Not found.** `JobRequestBody` omits `mode` (`domain/builder.ts:67-68`); the job record already maps `queued` to "unknown" (`job-record.tsx`, `JOB_STATES`) | `mode="async"` answers 501 (`engine.md#async-jobs-reserved-m4--iss-26`) |
| Persistent endpoint | **Not found** | `POST /v1/sessions` answers 501 (`services/reserved.py:17-26`); `Session.proxy_path` reserved |
| Stored result read | **Not found.** `KeyedEngine` has no result call (`ports/keyed.ts:34-52`) | `GET /v1/jobs/{id}/result` exists; immediate results are not retained |
| Job record by URL | Job record polled after a run; `app/builder/api/jobs/[id]/route.ts`; **no page per job id** under `/builder/jobs` | `GET /v1/jobs/{id}` |
| Failure display | Kind, message, payment note (`failureNote` in `job-record.tsx`) | `Failure.kind`; ISS-12 adds `reason`; ISS-28 the full taxonomy |
| Cost classes | `pending` → final, `billable` beside `fees`, every class of a failover (console 28 Oct entry) | Done |
| Missing metadata | Stale or missing rates marked (`services/builder.ts`, `STALE_AFTER_S = 300`); gaps G13, G14 | Input descriptors are builder-core #18 |
| Browser checks | Manual headless Chromium runs, recorded in `console-m3.md`; **no committed browser test** (console tech debt T4) | |
| Unit tests | vitest on pure services and domain (`web/src/**/*.test.ts`), structure tests (`web/tests/structure/`) | |

The ISS-19 matrix says only "Asynchronous with stored results (ISS-26); persistent endpoints (ISS-27)"
for M4. **Which async job and which persistent endpoint is not selected yet.** builder-core #73 (a
proposal) starts persistent endpoints with realtime speech-to-text over a WebSocket, which a browser
cannot authenticate with an `Authorization` header (#73, "Browsers").

## Decision proposed

**Extend the Builder area, not add areas: one job form with a mode choice, one job page per job id that
covers all three modes, and a session panel for persistent endpoints. Every view is a pure function of
the engine's records, so the states are unit-tested without a browser; one committed browser smoke runs
the three journeys against a scripted engine.**

### Selected jobs (to confirm in ISS-19's M4 update)

| Mode | Proposed job | Why |
| --- | --- | --- |
| Immediate | Chat (Qwen, streamed) and image (FLUX schnell), unchanged | M3 evidence exists |
| Async | Image (FLUX schnell) with `mode="async"` | Same runner, same rate; the only change is the lifecycle, so the evidence isolates async |
| Persistent | The first ISS-27 session the engine supports. If that is realtime speech-to-text over a socket, the console's server proxies the socket (below) | ISS-27 decides the shape; the console follows |

### What the console adds

1. **Mode on the job form.** `RunInput` gains `mode: "immediate" | "async"`; `parseRun` validates it;
   `JobRequestBody` adds `mode`. Streaming stays immediate-only: the form disables the stream
   switch when async is chosen (ISS-26 decides whether the engine refuses the pair; nothing in
   `releases/m3` defines it).
2. **Async submit.** `app/builder/run/route.ts` returns the queued job id at once for async. The browser
   navigates to the job page; nothing waits on the open request, so a closed tab never affects the job
   (J2).
3. **A page per job:** `app/builder/jobs/[id]/page.tsx`, reachable from My jobs and after every run.
   It polls `GET /v1/jobs/{id}` until the state is final and the cost has settled (the existing
   `jobState` rule), survives a reload and an engine restart (it only holds the id), and shows:
   - state with its words for `queued`, `running`, `succeeded`, `failed`, `uncertain`;
   - attempts, payment per attempt, the result reference;
   - **the stored result** through a new `KeyedEngine.result(id)` (`GET /v1/jobs/{id}/result`), rendered
     with the existing output views (`output-view.tsx`);
   - **result unavailable** as its own state: an immediate result that was never retained, a stored
     result past retention, or an expired provider URL (ISS-15's wording), each with its reason;
   - cost from `pending` to its final class, `corrected` explained for sessions.
4. **Persistent endpoints.** A session panel in the job page: open (`POST /v1/sessions`, a write through
   the keyed engine), state (`opening`, `open`, `closed`, `failed`), time open against
   `max_duration_s`, close, and the interaction. For HTTP endpoints the console's server forwards the
   builder's request to `proxy_path` with the session's key. For a socket endpoint, the browser connects
   to a console route that holds the actor key server-side and relays frames to the engine
   ([?] Q2: Next.js route handlers and WebSocket upgrade must be checked in
   `web/node_modules/next/dist/docs/` first, the console's own rule).
5. **Failures by category.** A pure `failureView(job)` maps ISS-28's categories (validation, access,
   capacity, execution, payment, reporting) and ISS-12's `reason` to a title, a sentence, whether
   payment went out, and what the builder can do (retry, ask the operator, wait for cost). Unknown
   kinds fall back to the engine's message, never to a blank.
6. **Missing metadata.** Offerings without input descriptions start from `{}` with a visible note (as
   today); a missing or stale rate stays marked; a job page whose job has no `usage` (`Job.usage` is
   `MeasuredUsage | None`, `contracts/jobs.py:200`) says "no usage reported".
7. **Reporting.** My jobs lists all three modes with a mode column; Usage and cost is unchanged apart
   from `by_app` (ISS-18) when the engine provides it.

No new top-level page; Operator actions are untouched (ISS-31 may hide money actions in its second
configuration).

### Architecture in the console

Follow its layering (`web/tests/structure/layers.test.ts`): `domain/` types, `ports/keyed.ts`
(`KeyedEngine` gains `result`, `openSession`, `session`, `closeSession`; `RunInput` gains `mode`),
`adapters/engine/keyed.ts` implements them, `services/builder.ts` holds the pure views, `app/` and
`components/` only render. Regenerate `openapi/` and `domain/engine-schema.d.ts` at the pin that
implements ISS-26/27/29 (`make gen-api`); `pins.test.ts` checks the pin.

## Code areas

| File (console `web/src/`) | Change |
| --- | --- |
| `domain/builder.ts` | `RunInput.mode`, `JobRequestBody.mode`, an async image preset |
| `domain/output.ts` | result-unavailable view kinds |
| `ports/keyed.ts`, `adapters/engine/keyed.ts` | `result`, session calls |
| `services/builder.ts` | `parseRun` (mode), `jobState` (queued), `failureView`, `resultView`, `sessionView` |
| `app/builder/run/route.ts` | async returns the job id; sync unchanged |
| `app/builder/jobs/[id]/page.tsx` (new) | the job page |
| `components/console/run/job-record.tsx`, `job-runner.tsx`, `output-view.tsx` | modes, unavailable results, session panel |
| `web/tests/structure/writes.test.ts` | session open and close are allowed writes, from the Builder only |
| `web/e2e/` (new) | the browser smoke (console T4) |
| `docs/product-specs/console.md`, `m3-journey.md` (M4 section), `guides/m3-journey.md` | the three modes |

## Test plan (test-driven)

**Levels.** (1) vitest unit tests on pure functions in `services/` and `domain/`, fed by engine JSON
fixtures (`web/tests/support/fixtures.ts`): every state, failure category and cost status is a fixture,
which is where most of the behavior lives. (2) Structure tests for layering and writes. (3) **One
committed browser smoke** (Playwright, closing console T4) that drives the three journeys in headless
Chromium against a **scripted engine**: builder-core's `testing.make_engine` with a
`ScriptedStreamTransport` and fakes, served by `http.app.create_app` from a small fixture script. It
spends nothing and is deterministic. (4) The paid run on the real stack is ISS-36's.

**Seams.** The `KeyedEngine` port (a fake in unit tests), the clock passed to views (`now: Date`, as
`catalogEntry` does), and the scripted engine for the browser smoke. No mocking of `fetch` inside
services.

**Starter test:** `parseRun` accepts `mode: "async"` and refuses `mode: "async"` with `stream: true`.

```text
Form and submit
[ ] parseRun accepts immediate and async; refuses an unknown mode
[ ] async with stream is refused before anything is sent
[ ] the request body carries mode; immediate omits nothing it sends today
Job page
[ ] jobState: queued and running are not settled; succeeded with cost pending is not settled
[ ] a reload of /builder/jobs/{id} renders the same record (server render from the id)
[ ] a stored result renders with the output view chosen by content type
[ ] resultView: inline result not retained → "not kept" with the reason
[ ] resultView: stored result 404 or expired provider URL → "no longer available" with the reason
[ ] failureView maps each ISS-28 category to a title and an action             [?] Q1
[ ] failureView: an unknown kind shows the engine's message
[ ] uncertain shows "may have run and been charged" and the cost as pending
Sessions
[ ] sessionView: opening, open, closed, failed, with time left against max_duration_s
[ ] a session cost reads corrected after it grows, with the explanation
[ ] open and close go through the keyed engine only from the Builder (structure)
[ ] socket relay keeps the actor key server-side                                [?] Q2
Reporting
[ ] My jobs shows the mode of each job
[ ] a job with no usage reads "no usage reported"
Browser smoke (scripted engine, headless Chromium, light and dark, 1280 and 390 px)
[ ] immediate streamed chat, buffered image, async image, one session: no console errors, no overflow
[ ] keyboard: the form, the job page and the session panel work without a pointer
```

**Definition of done.** Every item `[x]`, `[=]` or `[-]` with a reason, every `[?]` answered; the console's
`make check` (typecheck, lint, `vitest run`, compose config) green; the browser smoke committed and green;
the console pinned to the builder-core merge that implements ISS-26, ISS-27 and ISS-29, with `openapi/`
regenerated; console docs updated; one unpaid dry run of each mode on the dev stack recorded in
`console-m3.md` (or its M4 successor).

## Upstream dependencies

| Upstream | Needed? | Why / workaround |
| --- | --- | --- |
| livepeer/node | No | The console never talks to the signer for jobs |
| livepeer-python-gateway | No | Through the engine only |
| clearinghouse-batteries | No | The console's direct reads are unchanged |

The real dependencies are builder-core's ISS-26, ISS-27, ISS-28 and ISS-29, and #73 if the persistent
endpoint is a socket.

## Consumer impact

The console is the consumer. ChatLPT, inference-gateway and the enterprise desk are unaffected.

## Alternatives considered

- **A separate Async page and Sessions page.** Rejected: the job page already shows any job's record;
  splitting by mode doubles the views and contradicts "extra screens remain stretch".
- **Browser-only smoke against the live stack.** Rejected as the committed test: it spends money and
  depends on runner availability. It stays the ISS-36 run.
- **The browser holding a realtime token (#73) instead of a server relay.** Possible once the engine
  ships the token route; the relay keeps the console's rule that the browser holds no engine key.

## Open questions to lock

- **Q1.** Failure categories and their wording come from ISS-28; lock the list there first, then here.
- **Q2.** Persistent endpoint for the matrix: an HTTP endpoint (no socket, simplest), or realtime
  speech-to-text over a socket (needs a relay route or #73's token route)? Recommended: whatever ISS-27
  ships first, relayed server-side.
- **Q3.** Async job: FLUX schnell with `mode="async"` (recommended), or a job that is naturally long?
- **Q4.** Scripted engine for the browser smoke: a fixture script in the console using builder-core's
  `testing` package (recommended), or a fixture server shipped by builder-core?

## Delivery slices

1. Job page by id, `result` read, result-unavailable and failure views (works for immediate jobs today).
2. Async mode on the form and submit, once ISS-26 merges.
3. Session panel and relay, once ISS-27 merges.
4. Browser smoke with the scripted engine; docs; re-pin.
