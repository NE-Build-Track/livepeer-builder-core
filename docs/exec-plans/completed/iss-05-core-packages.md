# ISS-05: Establish installable core packages and public extension interfaces

## Status

Owners: John, Mike. GitHub: #11. **Closed by Mike on 19 October.** The acceptance is met on
`releases/final`. Josh's code review and the SDK re-pin (T1) carry forward to M3 (ISS-12).

## Goal

The shared core imports and starts without the reference app, proprietary identity, PymtHouse or
private Inc source. The supported interfaces are documented, and an import/start smoke check runs
against a versioned development build.

## Acceptance

- [x] One distribution (`livepeer-builder`) with `[server]` and `[postgres]` extras; layers enforced
      by `tests/structure/`
- [x] Public interfaces documented: [ARCHITECTURE.md](../../../ARCHITECTURE.md#providers), [contracts/](../../contracts/index.md)
- [x] No silent in-memory store; SQLite file by default
- [x] SDK dependency pinned so an installed build gets #77 (`on_payment_session`, `manifest_id`); see tech debt T1
- [x] `lpb smoke` import/start/health/stop check
- [x] The CI clean-install job is green on a versioned dev build (ISS-07)
- [x] Mike's review (closed 19 October)
- [ ] Josh's code review: carried forward to M3

## Progress

- [x] 2026-10-12: Scaffolded from the `livepeer-core` prototype into enforced layers.

## Decision log

- 2026-10-12: One distribution with extras (Mike). Core paths working, not stubs (Mike).
