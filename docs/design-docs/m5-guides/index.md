# M5 implementation guides

One guide per M5 tracker (M5: independent installation and release). The trackers are still draft
items on the project board. Each guide is a proposal until it is locked in review with Mike and John.
Written 29 October 2026 and verified against builder-core `releases/m3` at `d3df806`.

**No M5 guide needs an upstream change.** Publishing to PyPI is blocked while the SDK is a git
dependency (T1); a GitHub release wheel works without it, and the remaining SDK imports could be vendored
if PyPI matters.

## Review agenda

| # | Guide | Owner | Depends on | What it decides | Questions |
| --- | --- | --- | --- | --- | --- |
| 1 | [iss-38-release.md](iss-38-release.md) | John | ISS-07, ISS-29, ISS-30, ISS-34, ISS-35, ISS-37 | Images built from the wheel and lock file; a manual release workflow with gates; SBOM and attestations; changelog; a reuse and licence record | Q1–Q6 |
| 2 | [iss-37-documentation.md](iss-37-documentation.md) | John | ISS-24, ISS-31–35 | Task guides in `docs/guides/`; tests that keep docs matched to code; a fresh-reviewer rehearsal per release candidate | Q1–Q5 |
| 3 | [iss-39-independent-installation.md](iss-39-independent-installation.md) | Mike | ISS-36, ISS-37, ISS-38 | An outside reviewer installs from release artifacts and docs alone; evidence and acceptance record formats | Q1–Q6 |

The release guide comes first in review because its decisions (artifacts, versioning, the deploy
trigger) shape what ISS-37 documents and what ISS-39's reviewer installs. M5 is short (about seven working
days), so the reviewer, the payment handoff and the sign-off procedure should be agreed during M4.

## Findings to raise in review

- **Merging to a release branch deploys production:** `deploy.yml` runs on every push to `main` and
  `releases/**`. ISS-38.
- **Images ignore the lock file:** engine and discovery images run `pip install .`, so dependencies
  float at build time; the base image is pinned by tag, not digest. ISS-38.
- **A build from a shallow clone reports `0.0.1.dev1+g…`**, which is what the console's engine shows.
  ISS-38.
- **PR #32 merges `releases/m3` into `releases/final`**, still a draft with an empty body. ISS-38.
- **Clearinghouse Batteries has no licence** (at `ca09e47` and its main), nor does the prototype the engine
  started from (D4): do not publish a Batteries image; record the reuse answer (H5). ISS-38.
- **The README's install steps fail** (`pip install livepeer-builder` is not on PyPI; "Run the service"
  never sets `LPB_DISCOVERY_URL`). ISS-37.
- **Three consumers pin three engine commits** (console `d3df806`, ChatLPT `2134e29`, inference-gateway
  `0d0b532`). ISS-37, ISS-38.
- **No final-acceptance procedure exists:** M2 has no acceptance decision record, and sign-off is listed as
  open for M5. ISS-39.
