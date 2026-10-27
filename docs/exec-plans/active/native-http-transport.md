# Native HTTP transport

## Status

Owner: Mike. Implementation verified offline; awaiting human review. PR targets releases/m3. No live or paid acceptance.

## Goal

Let embedded applications carry bounded opaque bodies through paid jobs without JSON
reconstruction, and observe response headers through the stream facade. Preserve operation
identity and existing payment/cleanup behavior. Keep inference protocols outside Builder Core.

## Acceptance

- [x] JSON requests retain their existing behavior and fingerprints.
- [x] Raw bodies, including empty/non-UTF-8 bytes, reach the SDK unchanged with explicit media type.
- [x] Ambiguous bodies, unsafe headers and oversized input fail validation.
- [x] Raw bytes/media type/headers affect operation identity without persisting request content.
- [x] Stream response headers reach embedded callers, including repeated fields.
- [x] Validated custom runner headers reach the SDK; native HTTP errors remain failed jobs.
- [x] Tests, types, documentation and packaging checks pass; SDK gaps are explicit.

## Progress

- 2026-10-27: Inspected releases/m3 at 5a983a6 and confirmed pinned SDK already includes raw payloads.

- 2026-10-27: 955 tests passed, 21 PostgreSQL cases skipped locally. Overall branch/line
  coverage 97.11%; changed executable lines 100%. Layer coverage floors, lint, formatting,
  typing, documentation generation, wheel/sdist build and `lpb smoke` pass. CI owns the
  PostgreSQL service and container checks. No paid/live inference was run.

## Decision log

- 2026-10-27: User requested this PR based on releases/m3. Preserve default JSON and failure semantics.
- 2026-10-27: Use bounded bytes first, with base64 in the stock JSON REST envelope; streaming uploads
  remain out of scope. The public embedded API accepts bytes directly.
- 2026-10-27: The current SDK cannot send custom runner headers or return full HTTP error responses.
  Expose validated request headers for custom transports, fail explicitly in the default SDK adapter,
  and defer transparent error delivery rather than changing payment/retry behavior speculatively.

- 2026-10-27: Follow-up consumes NE-Build-Track SDK PR #6 at f500ecd. Custom headers and
  opt-in HTTP error delivery are implemented. Default exception/payment behavior stays
  unchanged; complete native error responses record failure without persisting error bodies.
  User requires all publishing to stay within NE-Build-Track forks.

- 2026-10-27: Follow-up validation: 969 tests passed, 21 PostgreSQL cases skipped locally;
  total coverage 97.12%, changed-line coverage 98%. Full layer coverage gates pass.
