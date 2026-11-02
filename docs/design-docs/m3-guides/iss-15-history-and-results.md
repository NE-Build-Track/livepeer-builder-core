# ISS-15: Job history, inputs and results

**Status:** proposal for discussion, 29 October 2026.\
**Tracker:** ISS-15 ([Network-Engineering-SPE#28](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/28)).\
**Owner:** John (engine); Mike for the console change. **Reviewers:** Mike, John.\
**Verified against:** builder-core `releases/m3` at `d3df806`; the console (`builder-core-mgmt-ui` main).

## Done when (from the 21 October M3 plan)

The docs say how inputs and results are accessed (inline results only in M3) and what a builder sees
when a provider asset has expired or is unavailable. Job history and ownership checks exist; stored
results are M4 (ISS-26).

## Current state (verified)

**Built and tested.** `GET /v1/jobs`, `GET /v1/jobs/{id}` (the job with its attempts, failure, result
reference and usage) and `GET /v1/jobs/{id}/result` (501 after the permission and ownership checks). The
ownership rule (403 for a missing permission before any lookup, 404 for another actor's job or a missing
one) is in `services/ownership.py` and `access.md`, tested in `test_ownership.py` and
`test_http_routes.py`.

**Documented.** JSON `payload` and opaque `raw_body` inputs (`engine.md`), streaming, and inline results
with a `ResultRef` (`storage="inline"`, content type, size, sha256) (`engine.md#results`).

**Gaps.**

1. **No builder-facing statement of how inputs and results are accessed.** The facts are spread across
   the contract; there is no guide for REST builders (`docs/guides` has only `embedded-mode.md`).
2. **URL inputs are undocumented.** The engine passes a URL in a payload to the runner untouched and
   hosts no media, which matters for media apps that take URLs.
3. **Provider assets.** The representative image job (FLUX schnell) returns `output.images[0].url`, a
   temporary fal URL (the console's `m3-journey.md`). The engine keeps only the response's hash, so after
   the URL expires nothing can reproduce the image. The job stays `succeeded` and its cost billable; the
   console shows a broken image (`<img>` with no error handling). Nothing documents this.
4. **Failures are not explained for builders.** The failure table maps kinds to statuses but not to
   "what it means, what to do".
5. **REST history cannot page:** the service accepts `before`, the route only `limit`.

## Decision proposed

**Document M3's access model in the contract and a short builder guide; keep the engine out of media
hosting.** No new storage in M3 (that is ISS-26).

### Docs

- `engine.md`, new section **"Inputs and results in M3"**:
  - inputs are a JSON `payload` or a `raw_body` up to 8 MiB; a URL inside a payload is passed through to
    the runner, which fetches it; the engine hosts and validates no media;
  - results are returned inline, once, and not kept; the job keeps a `ResultRef` (size and sha256) so a
    builder can check a copy it saved; `GET /v1/jobs/{id}/result` answers 501 until M4;
  - **provider-hosted assets**: some runners return a URL to a file hosted by the model provider (fal for
    FLUX schnell). That URL belongs to the provider, expires on its schedule, and the engine cannot
    reissue it. When it has expired, the job still reads `succeeded` and its cost stands; fetching the URL
    returns the provider's own error. Save the asset when the job returns, or ask the runner for inline
    output where it offers one (for FLUX schnell, `sync_mode: true` returns a data URI);
  - job history: `GET /v1/jobs` (own jobs for an actor, the application's for an admin or auditor), and
    what each attempt records.
- `engine.md#failures`: a "what it means / what to do" column, written once with ISS-12's reasons.
- `docs/guides/rest-builder.md` (new): the builder's path through REST for history and results, linking
  the contract instead of repeating it, and linked from the console's journey guide.

### Code (small)

- `GET /v1/jobs` accepts `before` (an ISO-8601 time) for paging, as the service already does (Q2).
- No change to results or storage.

### Console (Mike)

- Image views handle a failed load with "the provider no longer serves this image" instead of a broken
  image.
- Decide whether the FLUX schnell preset sends `sync_mode: true` (Q1), which makes ISS-25's evidence
  durable.

## Test plan (test-driven)

```text
[ ] ResultRef.sha256 and size match the bytes returned inline (pin the claim the docs now make)
[ ] /result answers 404 for another actor's job before it answers 501 (exists: keep)
[ ] /result's 501 names M4 and ISS-26 in the body
[ ] GET /v1/jobs?before=… returns the next page, newest first, own jobs only     [?] Q2
[ ] a URL in a payload reaches the runner unchanged (loopback runner)
[ ] doc test: every failure kind in contracts/jobs.py appears in the engine.md table
```

**Definition of done.** The docs above written; the tests green; the checks in `AGENTS.md` pass; the
console change merged; ISS-15 closed with links.

## Upstream dependencies

None. Asset expiry is the model provider's behavior (fal), outside the Livepeer stack; no node, SDK or
Batteries change is involved.

## Open questions to lock

**Locked by Mike, 30 October 2026:** Q1 the console's FLUX schnell preset sends `sync_mode: true` (a
console change); the engine docs still document provider-hosted asset expiry and name `sync_mode: true`
as the inline option; Q2 expose `before` on `GET /v1/jobs` now (ISO-8601, newest first, own jobs only for
an actor); Q3 a separate `docs/guides/rest-builder.md`. The rules become [D59](../m3-decisions.md).

- **Q1.** Switch the console's FLUX schnell preset to `sync_mode: true` so the image is inline and
  survives, or keep the URL and document the expiry (both are documented either way)?
- **Q2.** Expose `before` on `GET /v1/jobs` now (recommended, small), or leave paging to M4?
- **Q3.** A separate `docs/guides/rest-builder.md`, or keep everything in `engine.md`?
