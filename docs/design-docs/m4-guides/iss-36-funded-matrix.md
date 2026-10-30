# ISS-36: Funded journeys across the acceptance matrix

**Status:** proposal for discussion, 29 October 2026. Nothing here is decided until it is locked.\
**Tracker:** ISS-36 ([Network-Engineering-SPE#50](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/50)),
source task V2.\
**Owner:** Mike. **Reviewers:** Mike, John.\
**Verified against:** builder-core `releases/m3` at `d3df806`; console `main` at `938db3e`; ChatLPT
(pins builder-core `2134e29`); the M3 scope decision of 21 October.

## Done when

- The matrix selected in ISS-19 is complete: **all seven outcomes** across the required execution modes,
  interfaces, integration modes and **both payment configurations**, with pinned versions and real
  payment observations.
- Reproducible commands, results and correlation evidence are retained, with explicit limitations. No
  full-catalog or Cartesian-product obligation. ISS-39 (M5) rechecks the final artifacts.

**Depends on:** ISS-19 (#32), ISS-25 (#38), ISS-31 (#45), ISS-32 (#46), ISS-33 (#47), ISS-34 (#48),
ISS-35 (#49). **External:** H4 (funded environment).

## Current state (verified)

| Input | State | Where |
| --- | --- | --- |
| ISS-19 matrix | Dimensions named, rows not chosen: async (ISS-26), persistent (ISS-27), MCP (ISS-30), embedded vs service (ISS-32), enterprise auth (ISS-33), "the second payment configuration (ISS-31)" | console `docs/product-specs/m3-journey.md`, "What M3 covers, and what moves to M4" |
| ISS-25 method | Agent runs, Mike watches, after a dry run, on merged code; key issued through the console; operator funding shown; costs followed to their final class; **$1 cap** across dry and final runs; expected cost stated before each paid run | M3 decision 8 (`2026-10-21-m3-reference-app-and-scope.md`) |
| ISS-25 run | Dry run due 13 November, final by 20 November; not yet run | console `docs/exec-plans/active/console-m3.md` |
| Observed prices (21 Oct dev runs) | Chat (Qwen, streamed) $0.000111, one ticket; image (FLUX schnell) $0.003007, one ticket | `m3-journey.md` |
| Evidence precedent | Markdown per run: component pins table, ids per step, Batteries rows behind each cost, reconciliation, "Not covered" | `docs/references/evidence/2026-10-19-dev-stack-session-log.md` |
| Evidence collector | **Not found.** Evidence has been assembled by hand from REST and Batteries reads | |
| Live probe | Proposed by ISS-23 (`scripts/live_probe.py`), not built | ISS-23 guide |
| Payment configurations | Configuration 1 = the console stack (ISS-13). Configuration 2 = separately operated, `per_application` (ISS-31 guide) | |

Note: the console calls the second configuration "`per_application`". ISS-31 defines it as a separately
operated payment service that runs `per_application`; the matrix uses ISS-31's definition.

## Decision proposed

**Seven rows that cover every value of every dimension at least once (a covering set, not a product),
each run end to end with all seven outcomes; one evidence record per row produced by a collector script
from the job ids; a $3 cap across dry and final runs; the ISS-25 procedure (agent runs, Mike watches).**

### The matrix

| Row | Job | Mode | Interface | Integration | Access (outcome 1) | Payment configuration |
| --- | --- | --- | --- | --- | --- | --- |
| **A** | Chat, Qwen, streamed | immediate | REST (console) | service | engine actor key | 1, `per_actor` |
| **B** | Image, FLUX schnell | immediate | REST (console) | service | enterprise sign-in (ISS-33, the console's OIDC flow) | 1, `per_actor` |
| **C** | Image, FLUX schnell | **async** | REST (console) | service | engine actor key | 1, `per_actor` |
| **D** | First ISS-27 endpoint | **persistent** | REST (console) | service | engine actor key | 1, `per_actor` |
| **E** | Chat, Qwen | immediate | **MCP** (pinned client, ISS-30) | service | JWT from the enterprise provider | 1, `per_actor` |
| **F** | Image, FLUX schnell | async | **MCP** (status and result tools) | service | JWT | **2**, `per_application` |
| **G** | Chat, Qwen, streamed | immediate | facade | **embedded** (ChatLPT) | ChatLPT's own sign-in | **2**, `per_application` |

Coverage check: modes (A, C, D); REST and MCP (A, E); service and embedded (A, G); standalone and
enterprise access (A, B); configuration 1 and 2 (A, F, G). Two rows on configuration 2 because one
alone could not show both interfaces. ChatLPT already runs `per_application` (its decision D3), so row
G changes only where its payment service runs.

**Failure and recovery observations** ride on the rows, not extra rows: on row A, a second job with
the allocation paused (an understandable failure before payment, $0); on row C, the engine restarted
while the async job is queued (ISS-35 observed check); on row D, the session closed by
`max_duration_s`.

### The seven outcomes, per row

| # | Outcome | Evidence field |
| --- | --- | --- |
| 1 | One credential | credential kind and id (key id, or JWT issuer and subject); never the token |
| 2 | Discover | offering, orchestrator, `observed_at` from the engine |
| 3 | Rate | amount, currency, unit, source and age, before submission |
| 4 | Invoke | job id, `operation_ref`, mode, the exact command or tool call |
| 5 | Result or failure | state, result reference and content type, SHA-256 of a stored result; failure kind and reason |
| 6 | Pay without crypto | allocation id; per attempt `manifest_id`, `payment_state`, `payment_session_id`; operator funding step and its id |
| 7 | Usage and charge | Batteries usage row ids and fees; `JobCost` status, class per attempt, `fees`, `billable`; reconciliation `balanced` |

### Evidence format

- **One JSON record per row**, `docs/references/evidence/m4-matrix/<row>.json`, and **one Markdown page**
  for the run, `docs/references/evidence/<date>-m4-matrix.md`, in the 19 October style: pins table,
  one section per row with the outcome table, limitations, "Not covered".
- **Pins** in every record: builder-core commit, console, ChatLPT, livepeer/node, clearinghouse-batteries,
  SDK, MCP client and version, discovery list, Compose project names.
- **Collector:** new `scripts/collect_evidence.py --row C --job <id> [--job <id>]`. It reads the engine
  with an auditor credential (`GET /v1/jobs/{id}`, `/cost`, `/v1/offerings`, `/v1/admin/reconciliation`)
  and Batteries with a read-only token (`GET /v1/usage?allocation_id=…&manifest_id=…`), and writes the
  JSON. It never writes, never prints a token, and fails when a job's cost is still `pending`, when
  reconciliation is still not balanced after a sync (it reads `False` while Batteries' `spent` is
  ahead of the synced rows, `services/costs.py:232-240`), or when a usage row has no matching session.
  It is the same
  correlation for every row, however the job was submitted.
- **Commands** are recorded as run: `curl` from the console's request panel ("Copy as curl"), the MCP
  client's command line, ChatLPT's request, each with credentials replaced by variable names.

### Budget

| Item | Expected | Basis |
| --- | --- | --- |
| Chat jobs (A, E, G) | about $0.0001 each | observed 21 Oct |
| Image jobs (B, C, F, recovery on C) | about $0.003 each | observed 21 Oct |
| Persistent session (D) | advertised rate × at most 60 s | stated before the run from the catalog |
| Dry runs | the same rows once more | |

**Cap $3 across dry and final runs** (M3 capped $1 for two jobs; seven rows, a session and a second
configuration need more headroom). The expected cost is stated before each paid run; spend is read
from Batteries after each row; the run stops when the cap would be passed.

### Runbook

1. **Preconditions.** ISS-31 to ISS-35 merged; console, ChatLPT and the MCP client pinned to the same
   builder-core merge; `lpb payments check` green on both configurations; ISS-35's `lpb costs verify`
   clean; payer deposit and reserve funded; one signer per payer (ChatLPT, the console stack and the
   configuration 2 operator share it today), so rows run one configuration at a time.
2. **Dry run** of every row on the same stacks, with the expected cost stated first; fix and re-pin
   before the final run.
3. **Per row, final run** (agent driving, Mike watching):
   1. state the row, the pins and the expected cost;
   2. operator step, shown: issue the credential, fund the allocation (configuration 1) or show the
      operator's funded allocation (configuration 2);
   3. outcomes 2 to 5 through the row's interface;
   4. wait for the cost to leave `pending` (cost sync every 30 s), or `POST /v1/admin/costs/sync`;
   5. `scripts/collect_evidence.py`; check the record; note anything unexpected.
4. **Stop rules.** Cap reached; a cost class other than expected; reconciliation unbalanced; a
   job `uncertain` outside the recovery observation. Stop, record, investigate; do not repeat a paid row
   until the cause is known.
5. **Write-up** the same day; Mike reviews; the matrix row in `m3-journey.md` (or its M4 successor)
   links each record.

## Code areas

| File | Change |
| --- | --- |
| `scripts/collect_evidence.py` (new) | the collector |
| `tests/unit/test_collect_evidence.py` (new) | against `httpx.MockTransport` |
| `docs/references/evidence/m4-matrix/` (new) | records |
| console `docs/product-specs/m3-journey.md` | the matrix rows (ISS-19's M4 update) |
| `docs/product-specs/milestones.md` | ISS-36 row |

## Test plan (test-driven)

The run itself is evidence, not a test. The collector is code and is test-driven with recorded engine
and Batteries responses; nothing in the suite spends money.

**Starter test:** a job with an `observed` cost and one matched session yields a record whose outcome 7
lists exactly that session's usage rows.

```text
[ ] observed job → record with fees, class per attempt and the usage row ids
[ ] pending cost → the collector fails and says which session waits
[ ] unbalanced reconciliation → fails
[ ] a usage row with no session in the record → fails
[ ] failover job → both attempts, billable apart from fees
[ ] no token or secret appears in the output (scan for lpk_, lpg_, Bearer)
[ ] the collector sends only GET requests
[ ] pins are read from the running services' reported versions, not typed by hand   [?] Q3
```

**Definition of done.** Every row's record present with all seven outcomes and its limitations; the
Markdown page reviewed by Mike; spend under the cap; the collector's tests and the checks in `AGENTS.md`
green; the ISS-19 matrix updated with links; open limitations listed for ISS-39.

## Upstream dependencies

| Upstream | Needed? | Why / workaround |
| --- | --- | --- |
| livepeer/node | No | Pinned `406b248`; payments observed as in M3 |
| livepeer-python-gateway | No | The engine owns the paid call (D53) |
| clearinghouse-batteries | No | Usage reads with a read token; configuration 2 limits per ISS-31 |

H4 is the real dependency: representative runners that are up on the day (chat, image, the ISS-27
endpoint), and the funded payer.

## Consumer impact

Console, ChatLPT and the MCP client are run, not changed, apart from pins. ChatLPT's configuration 2
setup (a separately operated signer and Batteries) is a deploy change in its repository.

## Alternatives considered

- **Every combination.** Rejected by the tracker ("no Cartesian product").
- **Evidence by hand, as in M2.** Rejected: seven rows make manual correlation error-prone; the
  collector checks the chain the same way each time.
- **A separate failure row with a paid failure.** Rejected: a pre-payment failure and the ISS-35
  restart show outcome 5 without paying for a failure on purpose.

## Open questions to lock

- **Q1.** The seven rows above, or a smaller set (for example, drop F and put MCP on configuration 2 in E)?
- **Q2.** Cap: $3 across dry and final runs (recommended), or keep $1?
- **Q3.** Where the evidence lives: builder-core `docs/references/evidence/` (recommended, home ENGINE),
  or the console repository as for ISS-25?
- **Q4.** Does M3 decision 8 (agent runs, Mike watches) carry to M4 unchanged?

## Delivery slices

1. Collector and its tests; the ISS-19 matrix update (rows, inputs, expected observations).
2. Dry run of all rows; fixes.
3. Final run, records, write-up, review.
