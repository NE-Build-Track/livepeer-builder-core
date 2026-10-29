# ISS-38: Release artifacts and controlled release procedures

**Status:** proposal for discussion, 29 October 2026. Nothing here is decided until it is locked in
review with Mike and John.\
**Tracker:** ISS-38, a draft item on [Project 13](https://github.com/orgs/Cloud-SPE/projects/13) (no issue
yet); [planning reference](https://github.com/Cloud-SPE/Network-Engineering-SPE/blob/eea61ba3ddec7326934d0218cb5503f16ffc5286/docs/design-docs/build-track-december-2026-issue-proposal-draft.md#iss-38-prepare-final-release-artifacts-and-controlled-release-procedures).\
**Owner:** unassigned (draft item; M5 owner Mike). **Reviewers:** Mike, John.\
**Verified against:** builder-core `releases/m3` at `d3df806`; livepeer/node `a4b68ed` (comparison only);
the consumer pins listed below.

## Done when

From the tracker, condensed:

- Versioned packages and service and application containers are built, and the delivered components pass
  their tests, lint and type checks, and install and start checks. Companion APP artifacts are linked.
- Release notes, configuration examples, documented reuse and license decisions, and controlled release
  procedures exist. **Public publication, organization transfer and ongoing operation are separate
  authorized actions.**

The source task (L2) adds that artifacts "build reproducibly".

**Depends on:** ISS-07, ISS-29, ISS-30, ISS-34, ISS-35, ISS-37. **External:** H5 (source access and
permitted reuse).

## Current state (verified)

### Versioning

- `pyproject.toml` builds with **hatchling and hatch-vcs** (not setuptools-scm directly): `source = "vcs"`
  and `fallback-version = "0.0.0"`. Only `v[0-9]*` tags count (`describe_command … --match v[0-9]*`), so
  label tags can share a commit.
- **Tags:** one version tag, `v0.1.0` (annotated, "Build Track M2: Foundation and integration
  contracts"), and the label tag `m2-foundation-and-integration-contracts`, both on `153f0de`.
  `d3df806` describes as `v0.1.0-147-gd3df806`, a dev version `0.1.1.dev147+gd3df806`. M3 has no tag.
- **The console's `0.0.1.dev1+g…` is reproduced.** A depth-1 clone of `releases/m3` without tags gives
  `0.0.1.dev1+gd3df80657` from `hatch version` (run 29 October in the scratchpad). The console builds the
  engine from a remote git context
  (`context: https://github.com/NE-Build-Track/livepeer-builder-core.git#<sha>` in its
  `deploy/compose.yaml`), and its run log records `0.0.1.dev1+g165e60192` and `0.0.1.dev1+g93756ba27`.
  A tagged release built that way would also report `0.0.1.dev1`, so the version in `/readyz` would lie.
- The local `dist/` holds dirty builds (`…+g4a9747f.d20261027`). `tests/store/test_packaging.py` picks
  `sorted(dist/*.whl)[-1]`, which sorts `dev99` after `dev108`. That is harmless in CI, which has one
  wheel, but it is not "the wheel we built".

### Packages, images and CI

| Artifact | Today |
| --- | --- |
| Wheel and sdist | `uv build` in CI job `build-install`; uploaded as artifact `livepeer-builder-dist` (14 days). **Nothing is published** (ISS-07). No GitHub releases exist. `livepeer-builder` is not on PyPI (404) |
| Dependencies | `uv.lock` (frozen in CI). The SDK is a **direct reference**, `livepeer-gateway @ git+…@2017a80` (`allow-direct-references = true`, T1). PyPI refuses uploads whose metadata has direct references, so PyPI is blocked until an SDK release. Installing the wheel needs `git` |
| Engine image | `deploy/dev-stack/engine/Dockerfile` and `deploy/railway/engine/Dockerfile`; discovery image `deploy/railway/discovery/Dockerfile`. All run `pip install ".[server…]"` from the source tree: **not from the wheel and not from `uv.lock`**, so dependency versions float at build time. Base `python:3.12.7-slim` by tag, not digest |
| Image checks | CI job `container`: non-root, `lpb smoke`, `/readyz`, unauthenticated `POST /v1/jobs` → 401 |
| Install check | CI job `build-install`: wheel into a clean venv, `lpb smoke`, `lpb revision`; `test_packaging.py` checks that the migrations ship |
| Other CI | `checks` (ruff, format, mypy strict, `check_docs`, `gen-docs --check`, tests with coverage gates), `postgres`, `railway-compose` |
| Actions | Pinned by major tag (`actions/checkout@v4`, `astral-sh/setup-uv@v5`, `actions/upload-artifact@v4`), not by SHA. `deploy.yml` runs `npm install -g @railway/cli` unpinned |
| Deploys | `deploy.yml` deploys **production** on every push to `main` or `releases/**`. `preview.yml` deploys a preview per PR on a Neon branch |
| Registry | `IMAGE_REGISTRY=registry.livepeer.tools` in `deploy/dev-stack/.env.example` only prefixes local tags; builds never push. No GHCR packages |
| Release notes | No `CHANGELOG` and no release workflow. Decisions and `m2-traceability.md` partly serve |

### Branches and PR #32

`main` is `eb3c3ba`, the M2 merge (`releases/final`, #24). [PR #32](https://github.com/NE-Build-Track/livepeer-builder-core/pull/32),
"Draft: Prepare for M3 release", is a draft with an empty body opened 21 October. Its head is
`releases/m3` and its base is **`releases/final`, not `main`**. M2 travelled `releases/m2` → `main` (#21),
then `releases/final` → `main` (#24).

### Consumers pin three different commits

| Consumer | Pin | How |
| --- | --- | --- |
| Console | `d3df806` | `web/src/domain/pins.ts` and both compose files; `tests/structure/pins.test.ts` checks that they agree and refuses NE-Build-Track forks of livepeer repositories |
| ChatLPT | `2134e29` | `pyproject.toml` direct reference |
| inference-gateway | `0d0b532` | `pyproject.toml` direct reference |

All three commits are on `releases/m3`.

### Licenses and reuse (H5)

builder-core is MIT (`LICENSE`, "Copyright (c) 2026 Mike Zupper"; `license = "MIT"`). The vendored SDK
code in `adapters/runner_call.py` carries the SDK's MIT notice (lines 1–25). The SDK and livepeer/node are
MIT. **clearinghouse-batteries publishes no license** (no `LICENSE` at `ca09e47` or at `origin/main`
`4b32940`; GitHub reports none). Nor does the prototype `eliteprox/livepeer-core`, the starting point for
this repository (D4). The enterprise desk (`enterprise-ui`) has no license either. `upstream-baseline.md#licenses`
records the first two facts. H5 was marked done in M2 with "nothing private reused".

### Comparison: livepeer/node `release.yml` (`a4b68ed`)

The release workflow is manual (`workflow_dispatch`). Its stages:

1. `release.sh --authorize` requires `main` and a current repository admin, and runs again in every
   writing job.
2. `--check-tag` runs before anything is built.
3. The workflow reuses `ci.yml` and `package.yml`. Bundles are deterministic (`SOURCE_DATE_EPOCH`,
   sorted `tar`) and ship `SHA256SUMS`.
4. A `publish` job in a `release` environment uploads the verified assets.
5. Images go to GHCR and Docker Hub, multi-architecture, with digests checked.

Actions are pinned by SHA. This is a comparison only; builder-core copies nothing from it.

## Decision proposed

### 1. Versions

- PEP 440 versions from annotated `v*` tags, through hatch-vcs as today. Proposed tags (Q1): `v0.2.0` at
  M3 close, `v0.3.0` at M4 close, `v1.0.0rc1`, `rc2` and so on in M5, then **`v1.0.0`**.
- **A release version has no local segment.** A version containing `+g` or `.d` is refused by the release
  check. This rules out an untagged commit and a dirty tree.
- **Images get the version from the wheel, not from `.git`.** The release image installs the built wheel,
  so the image and the wheel cannot disagree. Builds from a git context (the console) pass
  `SETUPTOOLS_SCM_PRETEND_VERSION` as a build argument. Until then, the console's footer check compares
  commits, not versions.

### 2. Artifacts per release

| Artifact | How |
| --- | --- |
| Wheel and sdist | `uv build`; `SHA256SUMS`; the version must equal the tag |
| Engine image (`lpb serve`, `lpb discovery`) | One `deploy/image/Dockerfile`: installs **the wheel** with dependencies constrained by `uv export --frozen --all-extras --no-dev --no-emit-project`; base pinned by digest; non-root. The Railway and dev-stack Dockerfiles build `FROM` it, or are replaced by it |
| SBOM | CycloneDX, generated from the locked environment and from the image (Q4) |
| Provenance | GitHub artifact attestations (Sigstore, keyless) for the wheel, sdist and image digest; a signed annotated tag by Mike (Q5) |
| Release notes | `CHANGELOG.md` (one section per version). The GitHub release body adds: pins (SDK, node, Batteries), migration revisions, upgrade steps, `limitations.md` from ISS-37, and links to the console's matching artifacts |
| Configuration examples | `deploy/examples/`: service `per_actor`, service `per_application`, JWT bearer, embedded (`.env` files with placeholders only) |
| Reuse and license record | A new `docs/references/reuse-and-licenses.md`, extending `upstream-baseline.md#licenses`. It lists what each artifact redistributes (our code; the vendored MIT SDK portion; PyPI dependencies, with licenses from the SBOM) and what it does not. **Batteries and node images are not published by us**: operators build them from source at the pins, because Batteries publishes no license. It also records the D4 prototype disposition (H5) |
| Companion APP | The console's image, built by its CI at the matching tag, linked from the release notes |

Where artifacts go (Q2): a **draft** GitHub release on `NE-Build-Track/livepeer-builder-core`, and the
image in GHCR with **private** package visibility. Making either public is the separately authorized
step the tracker requires. The repository itself is already public.

### 3. The release procedure: `release.yml` plus `docs/RELEASING.md`

The workflow is manual (`workflow_dispatch`, input: tag). Each gate must pass before the next starts.

| Gate | Check |
| --- | --- |
| 0. Ready | ISS-37 docs and `limitations.md` at the candidate; the dependent trackers accepted; `CHANGELOG.md` section present |
| 1. Authorize | The actor is a repository admin. The tag exists, is annotated, points at the head of the release branch, and matches `v\d+\.\d+\.\d+(rc\d+)?` |
| 2. Checks | `ci.yml` reused through `workflow_call`: everything in `checks`, `postgres` and `container` |
| 3. Package | `uv build`; `scripts/release_check.py` (new) checks: the version equals the tag with no local segment; the exact built wheel (by name, not glob order) ships every migration; `SHA256SUMS` verifies. Then the image is built from the wheel |
| 4. Install and upgrade | Clean venv and clean container: `lpb smoke`, `lpb --version` equals the tag, `/readyz` 200, 401 unauthenticated. **Upgrade:** install the previous release, write records, install the candidate, `lpb migrate`, `lpb revision` exits 0, records intact |
| 5. Supply chain | SBOM; vulnerability scan of the lock and image (`pip-audit` or `osv-scanner`) with a reviewed waiver list; the license allowlist checked against the SBOM; actions pinned by SHA and the base image by digest (enforced by a structure test) |
| 6. Draft | Draft GitHub release with assets, `SHA256SUMS`, SBOMs, attestations and notes; image pushed to GHCR by digest; the digest recorded in the notes |
| 7. Human | Mike reviews the draft, then publishes it or leaves it as a draft. Visibility, transfer and operation are separate decisions |

The branch flow finishes M3 through PR #32 as drafted (`releases/m3` → `releases/final` → `main`, then tag
`v0.2.0`). M5 cuts `releases/v1.0` from `main`. Because a push to `main` deploys production (`deploy.yml`),
the procedure states that merging the release PR deploys Railway, unless Q3 changes that.

### 4. Consumers converge on the release

The console's `pins.ts`, ChatLPT and inference-gateway re-pin to the release commit, and later to the
wheel by version. The console's `pins.test.ts` may assert that the engine pin equals a tagged commit.

## Code areas

| File | Change |
| --- | --- |
| `.github/workflows/release.yml` (new) | Gates 1–6 |
| `.github/workflows/ci.yml` | `on: workflow_call`; actions pinned by SHA |
| `.github/workflows/deploy.yml` | Pin the Railway CLI version; maybe deploy from tags instead of pushes (Q3) |
| `deploy/image/Dockerfile` (new) | Wheel-based, lock-constrained, digest-pinned base |
| `deploy/*/engine/Dockerfile`, `deploy/railway/discovery/Dockerfile` | Build `FROM` the release image, or are deleted |
| `scripts/release_check.py` (new) | Version, tag, wheel contents, checksums |
| `tests/store/test_packaging.py` | Inspect the wheel named by the build, not `sorted(...)[-1]` |
| `tests/structure/test_release.py` (new) | Workflow and Dockerfile invariants, like `test_deploy.py` |
| `CHANGELOG.md`, `docs/RELEASING.md`, `docs/references/reuse-and-licenses.md`, `deploy/examples/` (new) | Notes, procedure, H5 record, configuration examples |
| `docs/index.md`, `AGENTS.md` | Link the new docs |

## Test plan (test-driven)

**Levels.** Unit tests for `scripts/release_check.py` (pure functions over strings and zip listings).
Structure tests over workflow YAML and Dockerfiles. CI runs as the integration level. One **rehearsal
release** (`v0.2.0` or an `rc` tag) kept as a draft, which is the end-to-end test of the procedure.

**Seams.** The version string and the wheel's file list are values; there are no doubles. The workflow
is tested by reading its YAML (as `test_deploy.py` reads deploy config) and, finally, by the rehearsal.

**Starter test:** `release_version("0.1.1.dev147+gd3df806")` is refused, and `release_version("1.0.0rc1")`
is accepted.

```text
release_check (unit)
[ ] a version with a local segment (+g…, .d…) is not a release version
[ ] a dev version is not a release version; 1.0.0 and 1.0.0rc1 are
[ ] the built version must equal the tag without its "v"
[ ] the named wheel must ship every migration revision up to head
[ ] SHA256SUMS mismatch fails
Structure
[ ] every "uses:" in .github/workflows is pinned to a 40-character SHA
[ ] the release Dockerfile's FROM lines carry a digest
[ ] the release image installs a wheel, not the source tree
[ ] release.yml runs authorize before any build job, and every publish job needs authorize
[ ] test_packaging reads the wheel the build produced, not glob order
Integration (CI and rehearsal)
[ ] image built from the wheel reports the tag in lpb --version and /readyz
[ ] the console-style git-context build with the pretend-version argument reports the tag
[ ] the upgrade from the previous release keeps records and ends at head revision
[ ] the SBOM lists livepeer-gateway at 2017a80 and every runtime dependency
[ ] the license allowlist passes, or each exception is recorded in reuse-and-licenses.md   [?] Q4
[ ] the rehearsal produces a draft release with all assets and attestations; nothing is public
```

**Definition of done.** Every item is `[x]`, or `[=]` or `[-]` with a reason. The `AGENTS.md` checks are
green. A rehearsal draft release exists for the candidate, and `RELEASING.md` was followed as written
by someone other than its author (it feeds ISS-37's fresh-reviewer check).

## Upstream dependencies

| Upstream | Needed? | Why |
| --- | --- | --- |
| livepeer-python-gateway | **Only for PyPI** | The direct reference blocks a PyPI upload (T1). A GitHub release wheel installs fine with `git` present. Vendoring the whole SDK to remove the reference is possible but large; D53 vendored one function. Recommendation: no PyPI in the Build Track unless Livepeer releases the SDK |
| livepeer/node | No | Not redistributed. Operators build `406b248`, or use node's published images if a tag matches the pin (T8) |
| clearinghouse-batteries | No change; a reuse constraint | It has no license, so we do not publish its image. Operators build it from source at `ca09e47`; the reuse record says so |

## Consumer impact

| Consumer | Change |
| --- | --- |
| Console | Re-pin to the tag commit. The pretend-version build argument makes its footer show the real version. Its own image is the companion artifact |
| ChatLPT, inference-gateway | Re-pin from three different commits to the release |
| Enterprise desk | None; it consumes REST. It needs a license before it is referenced as a delivered artifact |

## Alternatives considered

- **Publish to PyPI now.** Blocked by the direct reference, and publication needs separate authorization.
- **Keep building images from source.** Rejected: unpinned dependencies, and a version that depends on
  how `.git` arrived.
- **Copy node's release scripts.** Rejected: Go-specific. Their design (authorize, tag check, gated publish) is reused.

## Open questions to lock

- **Q1.** Final version `v1.0.0` (recommended: M5 is the delivery), or stay at `v0.x`?
- **Q2.** Draft GitHub release plus a private GHCR image (recommended), or CI artifacts only?
- **Q3.** Keep production deploys on push to `main` and `releases/**`, or deploy from tags
  (recommended, so a release merge does not deploy by surprise)?
- **Q4.** SBOM and scan tools, and the license allowlist (proposed: MIT, BSD, Apache-2.0, PSF, ISC,
  MPL-2.0, each exception recorded)?
- **Q5.** Signing: Mike's signed tag plus GitHub attestations (recommended), or a cosign key?
- **Q6.** Finish M3 through PR #32 into `releases/final` (recommended, as drafted), or retarget it to
  `main`?

## Delivery slices

1. **Version hygiene:** `release_check.py` and its tests, the `test_packaging` fix, the M3 tag, and the
   pretend-version argument for git-context builds.
2. **Reproducible image:** a wheel-based, lock-constrained, digest-pinned image; SHA-pinned actions.
3. **`release.yml`** with gates 1–6, rehearsed on an `rc` tag as a draft.
4. **Notes and records:** `CHANGELOG.md`, `RELEASING.md`, the reuse and license record (H5), the
   configuration examples.
5. **Consumers** re-pin to the release candidate; then the final tag.
