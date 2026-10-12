# The 1 October builder-layer proposal, and what became of it

**Source:** Cloud-SPE/Network-Engineering-SPE,
`docs/references/analysis/2026-10-01-Builder-Layer-Abstraction-and-Two-Application-Review.md` (Mike
Zupper), and its assessment `2026-10-01-Builder-Layer-Proposal-Assessment.md`. The proposal extracted a
shared layer from an Enterprise App and tested it against a second application (simple-infra). This
digest records each idea and its **disposition in this repository**.

## The seam (adopted)

> What the network did and what it cost moves to the shared layer; who the customer is and what they
> are charged stays in the application.

Three rules govern every interface. All three are adopted.

1. **Composition, not callbacks.** The engine never calls application code in the middle of a job. The
   application does its own work (placing a hold, say), calls `jobs.run`, then settles on the result.
   The same order works over HTTP. Pluggable protocols (policy, selection, authenticators) are
   extension points configured at startup, not mid-job commercial callbacks; the assessment asked for
   this clarification.
2. **The engine speaks no application protocol.** A job is an HTTP request to a runner app at a path.
   Knowledge of request and response bodies stays with each application.
3. **One key crosses the seam in each direction:** the application's `operation_ref` in, the engine's
   `job.id` out. Payment correlation (`auth_id`; the proposal said manifest) stays inside the engine.

**Vocabulary:** capability, offering, job, usage, network cost, allowance. The layer hides the 402
challenge, tickets, signer headers, manifest IDs, orchestrator gRPC, the discovery wire format and the
Batteries feed.

## Package layout

| Proposal | Disposition |
| --- | --- |
| Three distributions: core, service, runner | **Changed:** one distribution `livepeer-builder` with `[server]` and `[postgres]` extras (Mike, 12 October). The runner shell is not in M2 scope |
| One-way module imports enforced by an import linter | **Adopted** as `tests/structure/test_layers.py`, with remediation messages |
| Ship test fakes | **Adopted:** `livepeer_builder.testing` |
| SQLite **and** PostgreSQL stores passing one contract suite | **Adopted** (D5): `tests/store/` runs on both |
| `lpb` prefix; working names | **Adopted** for tables (`lpb_`) and the CLI (`lpb`). The repository name is `livepeer-builder-core` |

## Services and protocols: proposal → repository

| Proposal | Here | Note |
| --- | --- | --- |
| `BuilderEngine.from_settings`, `start`, `stop`, `health` | `runtime/engine.py` | `from_settings` takes overrides for every adapter |
| `DiscoveryService` | `services/discovery.py` | `resolve_app` deferred (T3) |
| `JobService.run` | `services/jobs.py` | Implemented |
| `JobService.stream` | — | Streaming is **stretch scope** (baseline). Not adopted for M2, despite the proposal's "include it" |
| `JobService.submit` / `result`; `sessions` | Reserved → 501 | D8: defined in M2, implemented in M4 |
| `JobService.report_usage` | Implemented | The app-reported default for service mode |
| `CostService` (`sync_once`, `for_job`, `summary`) | `services/costs.py` | Plus classification and `reconcile` |
| `AccessService` (issue, revoke, authenticate) | `services/keys.py` + `adapters/auth.py` | Split, so authentication is an adapter |
| `EventFeed` | Reserved → 501 | D10 |
| `DiscoverySource`, `RunnerTransport`, `SelectionPolicy` | `protocols/` | Adopted |
| `UsageMeter` (capability packs) | Not in M2 | Engine meters are a later extension; packs stay application-owned |
| `PaymentProvider` (one protocol) | Split into `AllocationManager`, `UsageSource`, `CredentialResolver`, `AllowanceReader` | Smaller seams; the business rules live in `PaymentsService` |
| `Authenticator(credentials)` | `Authenticator.authenticate(AuthRequest)` plus `AccessPolicy` | D12: sees the whole request |
| `EngineStore` | `EngineStore` (+ repos) | Adopted |
| — | `SecretStore` | Added (D15) |

## Behavior the contract fixes

| Proposal rule | Status |
| --- | --- |
| 1. Fail over only before payment, on `refused`/`unreachable` | Implemented |
| 2. One `(application, actor, operation_ref)` dispatches at most once | Implemented (interim 409 semantics; John owns the final design, D11) |
| 3. Write the attempt before the call; paid-but-unknown is `uncertain` | Implemented, plus restart recovery |
| 4. A stream is returned only after its first byte; `first_byte_timeout_s` | Deferred with streaming |
| 5. Missing cost is `pending`; `none` only when nothing was paid | Implemented |

## Data contract changes from the proposal

- **Cost correlation:** `Attempt.manifest_id` was the proposal's join key. It is now
  **`Attempt.auth_ids`** (`payment_session_id`), because the 7 October run showed a client can forge
  `manifest_id` (D20).
- **Cost source:** the proposal's Batteries `/v1/cost/events` feed became upstream `GET /v1/usage`
  with a cursor.
- **`JobCost`:** keeps `none/pending/observed/corrected`, gains a per-attempt **classification** (D22),
  and drops the second application's proposed `unavailable` status.
- **Money:** `Money(amount, currency)` throughout (D19); `fee_wei` → `fee_eth` (a decimal) plus `fee_usd`.
- **Events:** "replay from zero" is replaced by a retention window and 410 on an expired cursor
  ([events.md](../contracts/events.md)).
- **Discovery:** the runner descriptor (`lpb: 1`, at most 1,024 bytes) is deferred; `capability` and
  `model` are labels in M2.
- **Failure kinds:** adds `no_capacity` (all runners busy) and `not_provisioned`. Keeps the proposal's
  mapping table ([engine.md#failures](../contracts/engine.md#failures)).

## Second-application gaps

| # | Gap | Disposition |
| --- | --- | --- |
| 1 | Who pays: a credential per request, or per application | **Resolved:** `per_actor` (default) / `per_application` modes; a `CredentialResolver` per actor (D17) |
| 2 | Cost reporting assumed Batteries; `unavailable` status | Batteries is the payment path; no `unavailable` status (missing = `pending`) |
| 3 | Capability → app mapping in config; `allowed_orchestrators` | Deferred (T3). In the dev stack, the signer's `Orchestrators` list bounds who is paid |
| 4 | Idempotency: join-and-return; release on pre-dispatch decline | Owned by John's PR (D11). Interim: 409 |
| 5 | BYOC as a second transport | **Excluded:** Live Runner only. `RunnerTransport` stays transport-agnostic |
| 6 | Admission limits | **Implemented:** `AdmissionLimiter`, 503 + `Retry-After` before payment |

Smaller gaps: runner registration helper (not in scope), URL-list discovery source (a
`DiscoverySource` can do it), latency ranking (`SelectionPolicy`), long-job progress (with async, M4),
unpaid dispatch (`per_application` with no key gives unpaid calls).

## The proposal's open decisions

| Decision | Proposal recommendation | Disposition |
| --- | --- | --- |
| Names and the `lpb` prefix | Decide before step 1 | `livepeer-builder` / `livepeer_builder` / `lpb_`; the repository is `livepeer-builder-core` |
| Usage measurement in service mode | The app measures and reports back | **Adopted** (`report_usage`) |
| Capability → app resolution | Both, behind one resolver | Deferred (T3) |
| Joining retail records to network facts | Event-fed copy | **Adopted** as the events contract (M3) |
| Allocation granularity | Per application | **Changed:** `per_actor` default; both supported (D17) |
| Streaming in core | Include it | **Not adopted:** stretch scope per baseline |
| Async jobs, stored results, sessions | Reserve now, freeze later | **Adopted** (D8) |
| Hosted payment operator (Mode B) | Unknown | Deferred to M4; recorded as a gap (D23) |
| Upstream merges | Send now; pin forks | **Adopted:** forks with `releases/m2`; SDK pin (T1) |
| Who pays | Both | **Adopted** (D17) |
| BYOC | Live Runner only | **Adopted** |

## Extraction path

| Proposal step | Milestone | Status here |
| --- | --- | --- |
| 1. Contracts, protocols, fakes, store suite | M2 | Done |
| 2. Discovery | M2 | Done (no descriptor parser) |
| 3. Execution | M3 | Immediate execution done; async and sessions M4 |
| 4. Cost and events | M3 | Cost done; events defined |
| 5. Runner package | M3 | Not in scope (decided) |
| 6. Service adapters (REST, MCP, keys, migrations) | M3–M4 | REST (minimal), keys and migrations done; MCP M4 |
| 7. Enterprise App adoption | M4 | Out of this repository |
| 8. Provisioning and release | M4–M5 | Provisioning done; release with ISS-38 |

## The assessment's contract questions

| Question | Resolution |
| --- | --- |
| Trusted access for cost/event/key/provider methods | Every service method takes an `ActorContext` and calls the `AccessPolicy` |
| Payment secrets copied into actor attributes | Never: `SecretStore` + `SecretRef` (D15) |
| `OperationExists` vs join-and-return | Interim 409; John's PR (D11) |
| Atomic persistence and recovery after dispatch | Attempt written before the call; restart recovery → `uncertain` |
| `payment_sent` evidence at the SDK revision | SDK #71 (`3250c08`); verified in the 12 October run |
| Manifest uniqueness and multi-attempt cost | Keyed by `auth_id` per attempt; a job sums its attempts |
| Units: expected value vs settlement vs retail | Three figures kept distinct ([payments.md](../contracts/payments.md#cost-classification)) |
| Provenance of app-reported usage | `MeasuredUsage.source = app_reported` |
| Replay from zero vs retention | Retention window and 410 ([events.md](../contracts/events.md)) |
| Sessions vs persistent endpoints vs streaming | Distinguished ([engine.md#persistent-sessions](../contracts/engine.md#persistent-sessions)) |
