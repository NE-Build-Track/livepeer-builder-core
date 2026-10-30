# ISS-15: Owned job history and basic results

## Status

- **Owner:** John (engine), per the guide; Mike for the console change. **Reviewers:** Mike, John.
  Test-first, one pull request.
- **Ticket:** ISS-15 ([Network-Engineering-SPE#28](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/28)) (M3).
- **Branch:** `tasks/mz/iss-15-history-results`, on `56ccb27` (`releases/m3` plus PR #78, ISS-12, D58,
  not merged yet).
- **Design:** the history-and-results guide, `docs/design-docs/m3-guides/iss-15-history-and-results.md`.
  Q1–Q3 locked by Mike on 30 October.
- **Progress:** engine and docs built and checked; awaiting review. The console change (image views
  handle a failed load; the FLUX schnell preset sends `sync_mode: true`) is to do, in the console repo.
- **Decision:** D59 (PR #77 also numbers a decision D58, so the number may shift at merge).

## Goal

A builder can read, from the contract and one short guide, how inputs and results are accessed in M3
(inline results only, kept nowhere), what it sees when a provider-hosted asset has expired, what each
failure means and what to do about it, and can page its own job history over REST.

## Acceptance

From the ticket and the 21 October M3 plan:

- [x] The docs say how inputs and results are accessed, inline results only in M3
  ([engine.md](../../contracts/engine.md#inputs-and-results-in-m3)).
- [x] The docs say what a builder sees when a provider asset has expired or is unavailable
  ([provider-hosted assets](../../contracts/engine.md#provider-hosted-assets)).
- [x] Job history and ownership checks exist; `GET /v1/jobs` pages with `before`
  (`tests/unit/test_history_results.py`).
- [x] Every failure kind and reason has a "what it means · what to do" cell
  ([engine.md#failures](../../contracts/engine.md#failures), `tests/structure/test_docs.py`).
- [x] A REST builder guide links the contract ([rest-builder.md](../../guides/rest-builder.md)).
- [x] Every test in the guide's list is `[x]` or `[=]` with a reason; the `AGENTS.md` checks are green.
- [ ] The console change is merged, and its journey guide links `rest-builder.md` (console repo).

### Test list

- [=] `ResultRef.sha256` and `size_bytes` match the bytes returned inline, buffered and streamed, over
  REST through the real runner call (pins the docs' claim; passed at once, fails when the hash is
  mutated)
- [=] `/result` answers 404 for another actor's job before it answers 501 (already in
  `test_ownership.py` and `test_http_routes.py`; repeated over REST beside the next)
- [=] `/result`'s 501 names M4 and ISS-26 in the body (`milestone`, `ticket`, the message; the handler
  already sent them)
- [x] `GET /v1/jobs?before=…` returns the next page, newest first, own jobs only; an admin's page is the
  application's (the starter: red because the route ignored `before`)
- [+] `before` without an offset, or not a time, is 422 (proved by mutation to a plain `datetime`)
- [=] a URL in a payload reaches the runner unchanged, on the unpaid request and the paid retry
  (loopback runner)
- [=] doc test: every failure kind in `contracts/jobs.py` appears in the `engine.md` table (exists since
  D58)
- [x] doc test: every row of the failure table says what it means and what to do (red on `abandoned`)
- [+] a `before` written in another zone (`+02:00`) pages from the same instant; paging is the same under
  `TZ=Pacific/Chatham` (Mike's review: nothing reads the server's local zone)
- [x] a `/v1/` body over `LPB_HTTP_MAX_REQUEST_BYTES` is 413 `request_too_large`, declared or streamed, before
  the credential; a body at the limit is read; routes outside `/v1/` are not limited; the default fits the
  largest `raw_body`; the service takes the limit from its settings (Mike's review: no request was bounded)

## Progress

- 2026-10-30: Baseline at `56ccb27`: 1578 passed, 49 skipped. Built test-first in one session.
  `FakeEngine.jobs.list_jobs` and the service already took `before`, so the embedded surface is
  unchanged; only the REST route gained it, as an `AwareDatetime`.
- 2026-10-30: Checks: 1586 passed, 49 skipped (PostgreSQL store tests skipped locally; the store is
  unchanged). Layer coverage floors pass (http 98.42%); diff coverage against `56ccb27` 100%. Ruff,
  format, mypy (strict), `check_docs.py`, `lpb gen-docs --check` and `lpb smoke` pass. The OpenAPI
  schema gains the `before` parameter.

## Decision log

- 2026-10-30: Q1 the console's FLUX schnell preset sends `sync_mode: true`, and the docs keep the
  expiry; Q2 `before` on `GET /v1/jobs` now; Q3 a separate REST builder guide (Mike). Recorded as D59.
- 2026-10-30: `before` must carry an offset: a naive time could mean any zone and compare wrongly with
  stored UTC times, so it is refused (422) rather than guessed.
- 2026-10-30: Paging stays on `created_at` alone, as the store already orders. Jobs created in the same
  microsecond can be skipped at a page boundary; documented, and left to ISS-29's `(created_at, id)`
  key (M4).
- 2026-10-30: Mike's review of the three points above. (1) Keep the 422 for a `before` without an
  offset; the engine is independent of the server's zone (UTC clock, `UTCDateTime` on every column),
  pinned by a `+02:00` test and a `TZ=Pacific/Chatham` test. The cost summary still reads a naive
  `start`/`end` as UTC: tech debt T25, strict in M4. (2) The equal-time skip stays documented for M4;
  the ISS-29 guide is corrected so its cursor carries the id, not only the order. (3) Only `raw_body` had
  a limit, and nothing bounded a request before FastAPI read it into memory: a `/v1/` request-size limit
  is added now (`LPB_HTTP_MAX_REQUEST_BYTES`, default 16 MiB, 413 `request_too_large`; D59 (4b)).
- 2026-10-30: Cursor's review of #81. (1) The failure table keyed "the same `operation_ref` runs again"
  off the last kind; after failover an earlier `unreachable` or unpaid `timeout` makes the repeat 409. The
  rows past dispatch now point at the closed list, a note says `failure` is the last attempt's, and a doc
  test keeps it. (2) `runner_error` said a paid attempt is `failed_after_payment`: only a paid 500 is; a
  paid 502 or 504 stays `uncertain` (D52). Pinned by a doc test.
