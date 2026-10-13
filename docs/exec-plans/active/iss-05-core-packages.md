# ISS-05: Establish installable core packages and public extension interfaces

## Status

Owners: John, Mike. GitHub: #11. **Meets the acceptance on `releases/m2`, with CI green; waiting on
Josh's code review and the SDK re-pin (T1).**

## Goal

The shared core imports and starts without the reference app, proprietary identity, PymtHouse or
private Inc source. The supported interfaces are documented, and an import/start smoke check runs
against a versioned development build.

## Acceptance

- [x] One distribution (`livepeer-builder`) with `[server]` and `[postgres]` extras; layers enforced
      by `tests/structure/`
- [x] Public interfaces documented: [ARCHITECTURE.md](../../../ARCHITECTURE.md#providers), [contracts/](../../contracts/index.md)
- [x] No silent in-memory store; SQLite file by default
- [x] SDK dependency pinned so an installed build gets SDK #71 (`auth_ids`); see tech debt
- [x] `lpb smoke` import/start/health/stop check
- [x] The CI clean-install job is green on a versioned dev build (ISS-07)
- [ ] Josh's code review; Mike's review

## Progress

- [x] 2026-10-12: Scaffolded from the `livepeer-core` prototype into enforced layers.

## Decision log

- 2026-10-12: One distribution with extras (Mike). Core paths working, not stubs (Mike).
