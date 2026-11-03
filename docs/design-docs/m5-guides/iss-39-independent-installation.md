# ISS-39: Independent installation proof and final acceptance

**Status:** proposal for discussion, 29 October 2026. Nothing here is decided until it is locked in
review with Mike and John.\
**Tracker:** ISS-39, a draft item on [Project 13](https://github.com/orgs/Cloud-SPE/projects/13) (no issue
yet); [planning reference](https://github.com/Cloud-SPE/Network-Engineering-SPE/blob/eea61ba3ddec7326934d0218cb5503f16ffc5286/docs/design-docs/build-track-december-2026-issue-proposal-draft.md#iss-39-prove-independent-installation-and-record-final-acceptance).
Home: **PLAN** (Network-Engineering-SPE). The evidence links to ENGINE and APP.\
**Owner:** unassigned (draft item; PLAN owner Mike). **Reviewers:** Mike, John.\
**Verified against:** Network-Engineering-SPE `3c365a4`; builder-core `releases/m3` at `d3df806`; the
console at `938db3e`; the M2 trackers on Cloud-SPE/Network-Engineering-SPE (#7–#13, epic #19).

## Done when

From the tracker, condensed:

- A reviewer reproduces the agreed seven-outcome journey **from a clean environment**, using the
  delivered artifact versions and instructions. This includes the required configuration and reuse
  evidence and the explicit payment prerequisites.
- The acceptance record links the exact versions, commands, results, known limits and remaining external
  dependencies. The applicable scope and evidence sign-off is obtained; the tracker itself approves
  nothing.

The source task (L3) adds: "The designated acceptance authority reviews that evidence against the
agreed scope; this plan does not appoint the reviewer." ISS-36 says its funded matrix "final artifacts
are rechecked in ISS-39".

**Depends on:** ISS-36 (the funded matrix), ISS-37 (docs), ISS-38 (artifacts). **External:** H6 (scope and
final acceptance). The M5 window is 21–31 December 2026, and M4 ends on 18 December.

## Current state (verified)

### How earlier milestones were accepted

| Milestone | Form of the record | Where |
| --- | --- | --- |
| M1 | A **decision record**: status, effective date, recorder; context and evidence (a no-objection review window from 27 to 30 September, and committee responses); the decision, naming the accepted document set and a snapshot commit (`a3ce431`); alternatives and consequences; change history. It states what it relies on ("Mike's account… Discord messages were not independently retrieved") | NESPE `docs/decisions/2026-09-30-build-track-architecture-and-milestones.md` |
| M2, per task | An **evidence comment** on each tracker: "## ISS-NN summary: accepted <date>", "Mike reviewed and accepted the evidence on `<branch>` (`<sha>`)", an *Acceptance → Evidence* table, and the decisions. A later "PR attribution" comment adds the PRs that carried it to `main` (for example ISS-07, #13) | NESPE issues #7–#13 |
| M2, milestone | An **epic closing record** listing each task's evidence comment and the PRs (#1–#20, #21, #24, #23) (epic #19, 27 October). A **status update** for readers outside the team | NESPE `docs/updates/2026-10-m2-complete-status-update.md` |
| M2, in ENGINE | `docs/product-specs/milestones.md` (each criterion verbatim, then where it is met); `m2-traceability.md` (ticket → code → tests → docs; carried-forward items); **runtime evidence files** with a *Component / Revision* table, numbered results, and a "Not covered" section (for example `2026-10-19-dev-stack-session-log.md`); tag `v0.1.0` | builder-core |

There is **no decision record for M2's acceptance**. NESPE `docs/decisions/` holds only the M1 record and
the M3 scope record. builder-core's `milestones.md` states H6 as "Mike's review per ticket; procedure open
for M5". **This guide proposes that procedure.**

### What independent reproduction faces today

- **Every run so far was by the team.** M2's runs were by Mike or John, and M3's funded run is "run by the
  agent with Mike watching" (M3 decision 8). Nobody outside the team has installed the engine.
- **The instructions assume insider material.** The console's `deploy/README.md` says to "reuse
  ChatLPT's values", and its journey guide says to copy ChatLPT's `deploy/secrets/`. builder-core's
  README installs from PyPI, where the package does not exist (see the ISS-37 guide).
- **The payment prerequisites are real.** They are a payer keystore with a funded TicketBroker deposit
  and reserve on Arbitrum One, and an RPC URL (`deploy/dev-stack/README.md#prerequisites`). M3 caps spend
  at $1 across all runs (decision 8). The payer keystore is a secret the team cannot hand to an outside
  reviewer.
- **No artifacts exist** to install from: no release, no published image (ISS-38).

## Decision proposed

### 1. What counts as an independent installation proof

The proof holds when all four conditions are met:

1. **Independent reviewer.** The reviewer did not write the engine, the console or the docs under test,
   and has no access to the team's chat history or private material (Q1).
2. **Delivered artifacts only.** The reviewer uses the ISS-38 release artifacts, identified by version
   and `sha256` or image digest, and the ISS-37 docs **at the release tag**. Nothing is built from an
   unreleased branch.
3. **Clean environment.** A fresh VM or host. The reviewer installs only the documented prerequisites
   and records the OS, Python and Docker versions.
4. **No author help** beyond the documented prerequisite handoff. Every question the reviewer has to ask
   is recorded as a doc defect.

Under those conditions, the reviewer completes the seven outcomes for the selected jobs, plus the matrix
rows ISS-36 marks for recheck on final artifacts.

### 2. Payment arrangement: separate the builder from the operator

**Recommended (Q2):** use the **separately operated** configuration from ISS-31. Mike's operator stack
(signer and Batteries at the pins, funded payer) stays up for the window, and the reviewer receives the
operator endpoints and a scoped engine-side Batteries credential through the documented handoff. The
reviewer installs and runs **the engine and the console** from artifacts, and holds no crypto, which is
outcome 6 by construction.

The **self-operated** configuration is rechecked by the reviewer up to "signer ready, Batteries healthy",
built from source at the pins as documented. Its funded evidence stays ISS-36's. A spend cap is stated
before each paid run (Q4).

### 3. The evidence bundle (ENGINE)

A runtime evidence file in builder-core, in the existing format:
`docs/references/evidence/2026-12-<dd>-independent-installation.md`. Required sections:

| Section | Content |
| --- | --- |
| Reviewer and window | Name or role, independence statement, dates |
| Components | Table: wheel version and `sha256`, engine image digest, console artifact, discovery source, signer commit, Batteries commit, SDK commit, Python, OS. Each must equal the ISS-38 release notes |
| Prerequisites as provided | What was handed over, by whom, through which channel. **No secret values** |
| Steps | One row per step: doc section followed → command → expected → observed → pass or fail. Outcomes 1–7 for each selected job; then the matrix rows (embedded and service, payment configuration, enterprise auth, async and persistent jobs, a restart or recovery case, backup and restore, upgrade) |
| Cost evidence | Job ids, `manifest_id`, `payment_session_id`, `observed` cost, reconciliation `balanced`, as in the 19 October evidence |
| Doc defects | Each question or stall, its severity, its fix commit, and whether the affected steps were re-run |
| Not covered and known limits | From `limitations.md` and from what was observed |
| Remaining external dependencies | For example T1 (SDK release), Batteries licensing, Mode B upstream scoping, published node and Batteries images (T8) |

### 4. The acceptance record (PLAN)

Four pieces, following the M1 and M2 precedents:

1. **Per-task evidence comments** on the ISS-37, ISS-38 and ISS-39 trackers, in M2's form.
2. **A decision record**, `docs/decisions/2026-12-<dd>-build-track-final-acceptance.md`, in M1's form. It
   cites the accepted scope (the seven outcomes; the M4 and M5 gates), the exact release (tag, digests),
   the evidence links (ISS-36 matrix, ISS-37 fresh-reviewer record, ISS-38 release, this bundle), the
   exceptions and known limits, and the authority and process used (Q3). Under consequences it states
   that publication, organization transfer and ongoing operation are separate authorized actions.
3. **The M5 epic closing record** and a `docs/decisions/index.md` row.
4. **A status update**, `docs/updates/2026-12-build-track-final-status-update.md`, in the M2 update's form.

### 5. Order of work

```text
ISS-36 funded matrix on M4 code ─┐
ISS-37 docs at candidate ────────┼─> ISS-38 rc tag ─> fresh-reviewer rehearsal (ISS-37)
                                 │                     └─> defects fixed ─> next rc
                                 └─> reviewer chosen and payment handoff agreed (in M4)
next rc ─> independent run (ISS-39) ─> code fix? new rc, re-run affected steps
                                    └─> doc-only fix? errata in the bundle (Q5)
        ─> final tag v1.0.0 ─> decision record ─> sign-off (H6) ─> status update
```

The M5 window holds about seven working days around the holidays, so the reviewer, the payment handoff
and the sign-off process must be agreed **during M4**.

## Code areas

| Repository and file | Change |
| --- | --- |
| builder-core `docs/references/evidence/2026-12-<dd>-independent-installation.md` (new) | The bundle |
| builder-core `docs/references/evidence/TEMPLATE-acceptance.md` (new, or inside `docs/RELEASING.md`) | The section skeleton above |
| builder-core `tests/structure/test_evidence.py` (new) | Lints acceptance bundles (below) |
| builder-core `docs/product-specs/milestones.md` | The H6 row and the M5 rows point to the record |
| NESPE `docs/decisions/2026-12-<dd>-build-track-final-acceptance.md`, `docs/decisions/index.md`, `docs/updates/…` | The record and the update |
| Console `docs/` | An evidence link for the APP side, if the console runs are recorded there |

## Test plan (test-driven)

**Levels.** The acceptance itself is a manual end-to-end test run by the reviewer. What can be automated
is the **shape and consistency of the evidence**: a structure test over any evidence file marked
`acceptance: m5`. It checks the required sections, that every step row has an observed result, and that
the component versions and digests equal the `SHA256SUMS` and image digest in the release notes for the
named tag.

**Seams.** The evidence file and the release notes are text. The test parses them; it needs no network
and no doubles.

**Starter test:** a bundle without a *Components* table is rejected with a message naming the missing
section.

```text
Evidence lint (builder-core tests/structure)
[ ] a bundle missing a required section is rejected, naming the section
[ ] a step row with no observed result is rejected
[ ] a component digest that differs from the release notes for the named tag is rejected
[ ] a bundle containing a secret-shaped value (lpk_, lpg_, a 0x private key, a URL with credentials) is rejected
Acceptance run (manual; each line is a row in the bundle)
[ ] reviewer independence and clean-environment facts recorded
[ ] install the service from the image and embedded from the wheel, by digest, from the docs at the tag
[ ] outcomes 1–7 for chat (streamed) and image, plus the jobs ISS-19 adds in M4 for async and persistent
[ ] the enterprise-auth row (ISS-33 configuration) and isolation between two actors
[ ] the separately operated payment configuration, funded; the self-operated one up to readiness   [?] Q2
[ ] restart during a job ends uncertain or failed, never re-run; backup, restore and upgrade from the previous release
[ ] cost evidence: observed, matched, reconciliation balanced, spend within cap                    [?] Q4
[ ] doc defects listed with dispositions; none blocking open
Record
[ ] evidence comments on the ISS-37, ISS-38 and ISS-39 trackers
[ ] decision record merged in NESPE with the release tag and every evidence link
[ ] sign-off obtained by the agreed process                                                       [?] Q3
[ ] status update published; M5 epic closed
```

**Definition of done.** Every item is `[x]`, or `[-]` with a reason, and every `[?]` is answered. The
evidence lint is green under `uv run pytest`, alongside the rest of the `AGENTS.md` checks. The record
names one release tag, and every link resolves.

## Upstream dependencies

| Upstream | Needed? | Why |
| --- | --- | --- |
| livepeer/node | No | The reviewer runs or reaches the signer at the pinned `406b248` |
| livepeer-python-gateway | No | The SDK still installs as a git direct reference (T1). The bundle lists it as a remaining external dependency |
| clearinghouse-batteries | No | It is built from source at `ca09e47`. Its missing license is a reuse fact recorded by ISS-38 and listed among the external dependencies |

External (not upstream code): **H4**, a funded environment that stays up for the window; **H6**, the
authority and process for sign-off.

## Consumer impact

The console is the reference app the reviewer installs: its artifacts and its journey guide must be at
the matching tag (ISS-34, ISS-38). ChatLPT can serve as the embedded example only if ISS-36's matrix
names it. The enterprise desk is not involved.

## Alternatives considered

- **Mike or John runs it on a clean VM.** Not independent: the authors fill gaps from memory. This is
  useful as the ISS-37 rehearsal, not as the proof.
- **An AI agent session as the reviewer.** It has no history and needs only the docs, so it is a good
  rehearsal. Whether it satisfies "a reviewer" for H6 is Q1.
- **Hand the reviewer the team's payer keystore.** Rejected: a secret would leave its custody, and the
  builder-holds-no-crypto outcome would be muddied.

## Open questions to lock

- **Q1.** Who is the reviewer? Recommended: a person outside the delivery team, named by the H6
  authority, for example from the NE-SPE committee or Livepeer. A fresh agent session does the
  rehearsal. Does an agent run alone ever count?
- **Q2.** Payment arrangement: separately operated through Mike's stack for the funded journey, with
  self-operated checked up to readiness (recommended), or a funded self-operated run by the reviewer?
- **Q3.** Sign-off: affirmative written approval from the NE-SPE committee, or a no-objection window
  recorded as for M1? Recommended: affirmative where possible, with any no-objection fallback stated
  honestly in the record.
- **Q4.** Spend cap for the reviewer's runs (M3 used $1 in total). Proposed: $2, stated before each paid
  run.
- **Q5.** A defect found mid-run: allow doc-only fixes as recorded errata (recommended), while any code
  fix needs a new release candidate and a re-run of the affected steps?
- **Q6.** Evidence in builder-core `docs/references/evidence/` and the record in NESPE
  `docs/decisions/` (recommended, following the precedents), or everything in NESPE?

## Delivery slices

1. **Procedure and templates** (in M4): the bundle skeleton, the evidence lint, and this procedure locked.
2. **Arrangements** (in M4): the reviewer named, the payment handoff and spend cap agreed (H4), and the
   sign-off process agreed (H6).
3. **Rehearsal** on a release candidate (with ISS-37); fix the defects it finds.
4. **The independent run** on the final candidate; the bundle merged.
5. **The record:** tracker comments, the decision record, the sign-off, the status update, and the M5
   epic closed.
