# ISS-01: Verify the integration baseline and reusable source material

## Status

**Accepted 12 October 2026** (Mike and John). GitHub: Cloud-SPE/Network-Engineering-SPE#7.

## Goal

Pin the upstream revisions the engine builds on, recheck the behavior it depends on, and record
reuse permissions and gaps.

## Acceptance

- [x] Selected revisions recorded: [references/upstream-baseline.md](../../references/upstream-baseline.md)
- [x] Source behavior rechecked by John; flows accepted
- [x] Source evidence distinguished from runtime proof: [7 October run](../../references/evidence/2026-10-07-arbitrum-run.md)
- [x] Reuse and permissions: the SDK and node are depended on (MIT); Batteries is used as a service;
      Console is out of scope
- [x] Inc source (simple-infra) reviewed; findings are in the 1 October proposal
- [x] The engine builds without private source

## Progress

- [x] 2026-10-12: decisions recorded; summary posted on #7 for Mike to close.
- [ ] Re-run the attribution evidence on the new baseline: [active/m2-attribution-rerun.md](../active/m2-attribution-rerun.md)

## Decision log

- 2026-10-12: Baseline = eliteprox forks at `main`, with M2 work on `releases/m2` (D1).
- 2026-10-12: livepeer/node replaces go-livepeer (D2). Console is out of scope (D3).
- 2026-10-12: SDK #71 is required; #70 and multipart are deferred (D24).
