# M3 traceability: the first journey's checks

What checks access, repeated commands and accounting on the first builder journey (ISS-23,
[Network-Engineering-SPE#36](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/36); design in
the [verification guide](../design-docs/m3-guides/iss-23-verification.md), rules in
[D61](../design-docs/m3-decisions.md)). Each row is one behavior, in the builder's or operator's words,
and the check that holds it. Update this file in the same change that adds, renames or removes a check.

Checks are cited as `tests/<path>.py::<test name>`, never by line. `scripts/check_docs.py` (check 7)
fails when a cited test does not exist, so a rename fails CI until this file follows.

| Level | Meaning |
| --- | --- |
| U | Unit: the real engine on fakes (`FakeAllocationManager`, `FixedClock`) and in-memory SQLite |
| S | Store contract: the same test on SQLite and PostgreSQL (PostgreSQL in CI) |
| H | HTTP route: the REST app in-process, over `httpx.ASGITransport` |
| N | Loopback network: `tests/unit/livepeer_net.py`, a local orchestrator and signer driving the real runner call |
| P | Property test (Hypothesis) |
| L | Live, against a running stack: `scripts/live_probe.py` ([below](#live-probe)), the console's live checks, or the recorded [M3 live suite](../references/evidence/2026-10-30-m3-live-suite.md) |

**Evidence** is `fixture` for every level but L, and `live` for L. **Ticket** is ISS-23 for its own
checks, or the guide that owns the row: ISS-11/14 (repeat-safe commands, D57), ISS-12 (payment
failures, D58). Funded paid-job proof is ISS-25; broader recovery is ISS-35.

## Cross-actor access

| Behavior | Check | Level | Evidence | Ticket |
| --- | --- | --- | --- | --- |
| Another actor's job is invisible: not listed, and read as missing | `tests/unit/test_jobs.py::test_other_actors_jobs_are_invisible` | U | fixture | ISS-23 |
| Another actor's job and a missing one get the same 404, on every job and cost route | `tests/unit/test_ownership.py::test_another_actors_job_and_a_missing_one_are_404` | U | fixture | ISS-23 |
| The same over REST | `tests/unit/test_http_routes.py::test_a_missing_permission_is_403_and_another_actors_job_404_over_rest` | H | fixture | ISS-23 |
| `FakeEngine` answers the same | `tests/unit/test_fake_engine_contract.py::test_another_actors_job_is_not_found` | U | fixture | ISS-23 |
| The policy refuses another actor's or application's resource | `tests/unit/test_access.py::test_policy_needs_permission_and_ownership` | U | fixture | ISS-23 |
| An admin sees every actor's jobs in its application; an auditor reads them and changes nothing | `tests/unit/test_ownership.py::test_an_admin_lists_every_actors_jobs_in_its_application`, `tests/unit/test_ownership.py::test_an_auditor_reads_every_actor_in_its_application_and_changes_nothing` | U | fixture | ISS-23 |
| An auditor sees only its own application's keys, actors and grants | `tests/unit/test_access.py::test_an_auditor_reads_only_its_own_applications_keys`, `tests/unit/test_payments.py::test_an_auditor_lists_only_its_applications_actors`, `tests/unit/test_payments.py::test_an_auditor_never_sees_the_operators_grants` | U | fixture | ISS-23 |
| An `operation_ref` is scoped to its actor | `tests/store/test_contract.py::test_operation_ref_is_unique_per_actor` | S | fixture | ISS-23 |
| Bob reusing Alice's `operation_ref` runs his own job; Alice's job never appears | `tests/unit/test_verification_access.py::test_another_actors_operation_ref_runs_their_own_job_and_never_shows_the_first` | U | fixture | ISS-23 |
| The same over REST | `tests/unit/test_verification_access.py::test_another_actors_operation_ref_over_rest_never_shows_the_first_job` | H | fixture | ISS-23 |
| Cost summary, recent costs and allowance leave out another actor's fees; naming the other actor is refused | `tests/unit/test_verification_access.py::test_cost_summary_recent_costs_and_allowance_leave_out_another_actors_fees` | U | fixture | ISS-23 |
| On a live stack: another actor's job and its cost are 404, and its list leaves the job out | `scripts/live_probe.py` (cross-actor checks) | L | live | ISS-23 |
| On a live stack: another actor's `operation_ref` makes a new job | `scripts/live_probe.py` | L | live | ISS-23 |
| On a live stack: another actor's result, cost and usage report are 404 | [M3 live suite](../references/evidence/2026-10-30-m3-live-suite.md), run B | L | live | ISS-23 |

## Revoked credentials

| Behavior | Check | Level | Evidence | Ticket |
| --- | --- | --- | --- | --- |
| A revoked engine key stops authenticating at once | `tests/unit/test_access.py::test_issued_key_authenticates_and_revocation_stops_it` | U | fixture | ISS-23 |
| A rotated key's old token stops at once; only an active key rotates | `tests/unit/test_access.py::test_rotation_replaces_a_key_at_once_with_the_same_identity`, `tests/unit/test_access.py::test_only_an_admin_rotates_and_only_an_active_key` | U | fixture | ISS-23 |
| The old token answers 401 over REST right after a rotation | `tests/unit/test_http_routes.py::test_keys_are_inspected_and_rotated_over_rest` | H | fixture | ISS-23 |
| A key's revoke and its replacement commit together | `tests/store/test_contract.py::test_rotating_a_key_revokes_it_and_inserts_its_replacement_together` | S | fixture | ISS-23 |
| A revoked admin key and a revoked auditor key get 401 on admin routes | `tests/unit/test_verification_access.py::test_a_revoked_admin_or_auditor_key_is_401_on_admin_routes` | H | fixture | ISS-23 |
| A JWT signed by a key the provider removed stops within minutes | `tests/unit/test_jwt.py::test_a_due_refresh_runs_beside_requests_and_a_removed_key_stops_within_minutes` | U | fixture | ISS-23 |
| Revoking an allocation forgets its payment key; rotation revokes the old key | `tests/unit/test_payments.py::test_revoke_forgets_the_key`, `tests/unit/test_payments.py::test_rotation_replaces_the_key_and_revokes_the_old_one` | U | fixture | ISS-23 |
| A job after revoke-allocation fails `not_provisioned` with no runner call | `tests/unit/test_verification_access.py::test_a_job_after_the_allocation_is_revoked_fails_not_provisioned_without_a_runner_call` | U | fixture | ISS-23 |
| `not_provisioned` stays 402 with no reason | `tests/unit/test_payment_failures_http.py::test_not_provisioned_stays_402_and_has_no_reason` | H | fixture | ISS-12 |
| Re-provisioning after a revoke is a new operation | `tests/unit/test_service_edges.py::test_reprovisioning_after_revoke_is_a_new_operation` | U | fixture | ISS-11/14 |
| On a live stack: a revoked actor key answers 401 | `scripts/live_probe.py` (revocation checks) | L | live | ISS-23 |
| On a live stack: a revoked auditor key is 401 on admin reads | [M3 live suite](../references/evidence/2026-10-30-m3-live-suite.md), run B | L | live | ISS-23 |
| On a live stack: a job after revoke-allocation fails `not_provisioned` (402) and pays nothing | [M3 live suite](../references/evidence/2026-10-30-m3-live-suite.md), run A | L | live | ISS-23 |

## Duplicate job submissions

| Behavior | Check | Level | Evidence | Ticket |
| --- | --- | --- | --- | --- |
| One `operation_ref` dispatches at most once | `tests/unit/test_jobs.py::test_operation_ref_dispatches_at_most_once` | U | fixture | ISS-11/14 |
| A repeat while the job runs is 409 in progress and dispatches nothing | `tests/unit/test_jobs.py::test_a_repeat_while_the_job_runs_is_in_progress_and_dispatches_nothing` | U | fixture | ISS-11/14 |
| A repeat after a job that reached a runner is 409 `OperationExists` | `tests/unit/test_jobs.py::test_a_repeat_after_a_job_that_reached_a_runner_is_operation_exists` | U | fixture | ISS-11/14 |
| A repeat after a failure that never reached a runner runs again | `tests/unit/test_jobs.py::test_a_repeat_after_a_failure_that_never_reached_a_runner_dispatches_again` | U | fixture | ISS-11/14 |
| A repeat with another body is a conflict, also when two race (B2) | `tests/unit/test_jobs.py::test_a_different_body_after_a_retryable_failure_is_a_conflict`, `tests/store/test_contract.py::test_a_duplicate_with_a_different_body_is_a_conflict` | U, S | fixture | ISS-11/14 |
| Two concurrent retries of one failed job make one new job | `tests/store/test_contract.py::test_two_concurrent_retries_of_one_failed_job_make_one_new_job` | S | fixture | ISS-11/14 |
| A stream keeps the same repeat rules | `tests/unit/test_streaming.py::test_stream_keeps_idempotency_and_needs_a_streaming_transport` | U | fixture | ISS-11/14 |
| The fingerprint follows the request, not its limits | `tests/unit/test_properties.py::test_fingerprint_tracks_the_request_not_its_limits` | P | fixture | ISS-11/14 |
| A repeat answers 409 with a stable code over REST | `tests/unit/test_http_routes.py::test_a_repeated_operation_ref_answers_409_with_a_stable_code` | H | fixture | ISS-11/14 |
| `FakeEngine` raises the same | `tests/unit/test_fake_engine_contract.py::test_a_repeated_operation_ref_raises_operation_exists_or_conflict` | U | fixture | ISS-11/14 |
| A repeat after a payment refusal runs again; after a signer that may have charged it is 409 | `tests/unit/test_payment_failures.py::test_a_repeat_after_a_payment_refusal_runs_again_under_its_reference`, `tests/unit/test_payment_failures.py::test_a_repeat_after_a_signer_that_may_have_charged_is_operation_exists` | U | fixture | ISS-12 |
| On a live stack: after an unpaid `allowance_exhausted` refusal and a fund, the same `operation_ref` runs a new job (the closed list) | [M3 live suite](../references/evidence/2026-10-30-m3-live-suite.md), run A | L | live | ISS-11/14 |
| On a live stack: job repeats: in progress, exists, conflict, re-run after an unpaid failure | builder-core-console `scripts/live-check-d57.py` (46 checks, 30 October) | L | live | ISS-11/14 |

## Duplicate admin commands

| Behavior | Check | Level | Evidence | Ticket |
| --- | --- | --- | --- | --- |
| A repeated fund is answered from the ledger without calling the provider again | `tests/unit/test_admin_repeats.py::test_funding_repeated_with_one_ref_is_answered_without_calling_the_provider_again` | U | fixture | ISS-11/14 |
| The same reference with another amount, or on another command, is a conflict | `tests/unit/test_admin_repeats.py::test_funding_with_one_ref_and_another_amount_is_a_conflict_before_the_provider_call`, `tests/unit/test_admin_repeats.py::test_one_ref_used_for_another_command_is_a_conflict` | U | fixture | ISS-11/14 |
| A repeat while the first may still run is in progress; after it went stale, a resume applies once | `tests/unit/test_admin_repeats.py::test_a_repeat_while_the_first_run_may_still_be_going_is_in_progress`, `tests/unit/test_admin_repeats.py::test_a_repeat_after_the_first_run_went_stale_resumes_it_and_applies_once` | U | fixture | ISS-11/14 |
| Two concurrent provisions with one reference create one allocation | `tests/unit/test_admin_repeats.py::test_two_concurrent_provisions_with_one_ref_create_one_allocation` | U | fixture | ISS-11/14 |
| A repeated payment-key rotation keeps the new key live (B1) | `tests/unit/test_payments.py::test_repeating_a_rotation_with_the_same_ref_keeps_the_new_key_live` | U | fixture | ISS-11/14 |
| Issuing or rotating an access key twice with one reference does it once; the replay has no token (B4) | `tests/unit/test_admin_repeats.py::test_issuing_a_key_twice_with_one_ref_issues_one_key_and_the_replay_has_no_token`, `tests/unit/test_admin_repeats.py::test_rotating_a_key_repeated_after_success_rotates_once_and_the_replay_has_no_token` | U | fixture | ISS-11/14 |
| One key per reference, even when two issues race | `tests/store/test_operation_ledger.py::test_two_concurrent_issues_under_one_reference_store_one_key` | S | fixture | ISS-11/14 |
| Funding Batteries' grant key is one idempotent call | `tests/unit/test_adapters.py::test_create_key_for_grant_is_one_idempotent_call` | U | fixture | ISS-11/14 |
| Every command that moves money or makes a credential needs a reference; a replay says so | `tests/unit/test_http_routes.py::test_every_command_that_moves_money_or_makes_a_credential_needs_a_ref`, `tests/unit/test_http_routes.py::test_an_admin_replay_has_the_original_status_and_says_it_is_a_replay` | H | fixture | ISS-11/14 |
| Repeated set-status, revoke-allocation and revoke-key change nothing more; the provider is not called again to revoke | `tests/unit/test_verification_access.py::test_repeating_set_status_revoke_allocation_or_revoke_key_changes_nothing_more` | U | fixture | ISS-23 |
| The same over REST: set-status 200 twice; revoke-allocation 200 twice and revoke-key 204 twice (a retry gets the first answer, D61); an unknown actor 404, an unknown key 400 | `tests/unit/test_verification_access.py::test_repeating_set_status_revoke_allocation_or_revoke_key_over_rest` | H | fixture | ISS-23 |
| On a live stack: a fund sent twice under one `operation_ref` is replayed (`Idempotent-Replayed: true`) and applied once | `scripts/live_probe.py` (with `--yes`) | L | live | ISS-23 |
| On a live stack: admin commands repeated under one reference replay (`Idempotent-Replayed: true`) and apply once; a reference reused for another request is 409 | builder-core-console `scripts/live-check-d57.py` (46 checks, 30 October) | L | live | ISS-11/14 |

## Authorization denial

| Behavior | Check | Level | Evidence | Ticket |
| --- | --- | --- | --- | --- |
| Every `/v1/admin/*` route answers 401 with no credential and 403 to an actor key; every admin write answers 403 to an auditor key; nothing changes | `tests/unit/test_admin_route_sweep.py::test_every_admin_route_is_denied_to_actor_and_auditor_keys_and_needs_a_credential` | H | fixture | ISS-23 |
| Admin operations need an admin credential; only admins issue keys | `tests/unit/test_access.py::test_admin_operations_need_an_admin_credential`, `tests/unit/test_access.py::test_only_admins_issue_keys` | U | fixture | ISS-23 |
| An auditor reads, in its application only | `tests/unit/test_access.py::test_an_auditor_reads_every_actor_in_its_application` | U | fixture | ISS-23 |
| A missing permission is 403 before any lookup, on every job and cost route | `tests/unit/test_ownership.py::test_a_missing_permission_is_403_before_any_lookup`, `tests/unit/test_ownership.py::test_a_missing_permission_is_403_on_the_list_routes` | U | fixture | ISS-23 |
| A usage report needs `jobs.run`, not `jobs.read` | `tests/unit/test_ownership.py::test_a_usage_report_needs_jobs_run_and_not_jobs_read` | U | fixture | ISS-23 |
| Unknown credentials fall through; invalid ones stop; request fields never establish identity | `tests/unit/test_access.py::test_unrecognized_credentials_fall_through_but_invalid_ones_stop`, `tests/unit/test_access.py::test_request_fields_cannot_establish_identity` | U | fixture | ISS-23 |
| JWT: only the admin scope makes an admin; `actors.assert` and unknown scopes are never granted | `tests/unit/test_jwt.py::test_only_the_admin_scope_makes_an_admin`, `tests/unit/test_jwt.py::test_actors_assert_and_unknown_scopes_are_never_granted` | U | fixture | ISS-23 |
| An unauthenticated request gets a challenge | `tests/unit/test_http.py::test_unauthenticated_requests_get_a_challenge` | H | fixture | ISS-23 |
| `FakeEngine` denies the same | `tests/unit/test_fake_engine_contract.py::test_a_missing_permission_is_access_denied_before_any_lookup` | U | fixture | ISS-23 |
| The signer's refusal is told apart: not authorized 403, allowance exhausted 402, signer unavailable 503 | `tests/unit/test_payment_failures.py::test_a_signer_refusal_says_why_is_unpaid_and_does_not_fail_over`, `tests/unit/test_payment_failures_http.py::test_each_payment_reason_has_its_own_status` | U, H | fixture | ISS-12 |
| No builder-facing body carries a payment key, the signer URL or its words | `tests/unit/test_payment_failures_http.py::test_no_builder_facing_body_carries_the_payment_path` | H | fixture | ISS-12 |
| On a live stack: no credential is 401; an actor key is 403 on admin reads and a write; an auditor key reads but is 403 on writes | `scripts/live_probe.py` (authorization checks) | L | live | ISS-23 |
| On a live stack: a key limited to `discover` reads runners and is 403 on jobs, history, costs and allowance | [M3 live suite](../references/evidence/2026-10-30-m3-live-suite.md), run B | L | live | ISS-23 |

## Late or duplicate cost observations

| Behavior | Check | Level | Evidence | Ticket |
| --- | --- | --- | --- | --- |
| Every cost class, and only applied rows count | `tests/unit/test_costs.py::test_classification_covers_every_class` | U | fixture | ISS-23 |
| A row seen again is never applied twice; a new row on the job marks it `corrected` | `tests/unit/test_costs.py::test_sync_attributes_by_payment_session_and_marks_corrections` | U | fixture | ISS-23 |
| Rows that arrive before the attempt match on the next sync | `tests/unit/test_costs.py::test_rows_that_arrive_before_the_attempt_match_on_the_next_sync` | U | fixture | ISS-23 |
| A manifest shared by two sessions is uncertain and its rows unattributed | `tests/unit/test_costs.py::test_a_manifest_shared_by_two_sessions_is_uncertain_and_unattributed` | U | fixture | ISS-23 |
| A rejected cursor restarts from the beginning without double counting; a second rejection is raised | `tests/unit/test_costs.py::test_cursor_mismatch_restarts_from_the_beginning`, `tests/unit/test_service_edges.py::test_a_second_cursor_rejection_is_raised` | U | fixture | ISS-23 |
| A row committed while a sync classifies the job is never lost | `tests/unit/test_costs.py::test_a_row_committed_while_a_sync_classifies_the_job_is_never_lost` | U | fixture | ISS-23 |
| Usage rows, the cursor and attribution round-trip on both stores | `tests/store/test_contract.py::test_usage_rows_cursor_projection_and_attribution` | S | fixture | ISS-23 |
| Late evidence never reopens a finished attempt, and puts an ended job's projection out of date | `tests/store/test_contract.py::test_a_late_evidence_report_never_reopens_a_finished_attempt`, `tests/store/test_contract.py::test_a_late_evidence_report_on_an_ended_job_puts_its_projection_out_of_date` | S | fixture | ISS-23 |
| A refused manifest is never matched or billed | `tests/store/test_contract.py::test_a_refused_intent_is_never_matched`, `tests/store/test_contract.py::test_a_refusal_that_lands_during_matching_is_never_billed` | S | fixture | ISS-23 |
| An expired manifest is stored unpaid and never billed | `tests/unit/test_signing_intent.py::test_an_expired_manifest_is_stored_unpaid_and_never_billed` | N | fixture | ISS-23 |
| Fees add up however rows are split; every attributed charge counts exactly once | `tests/unit/test_properties.py::test_fees_add_up_however_the_rows_are_split`, `tests/unit/test_properties.py::test_every_attributed_charge_is_counted_exactly_once` | P | fixture | ISS-23 |
| Measured usage is reported once, by the owner | `tests/unit/test_jobs.py::test_usage_is_reported_once_by_the_owner` | U | fixture | ISS-23 |
| Rows Batteries marks `duplicate` or `ignored` are never counted, on the job or as unattributed | `tests/unit/test_cost_observations.py::test_rows_batteries_marks_duplicate_or_ignored_are_never_counted` | U | fixture | ISS-23 |
| A row id delivered again with another fee is ignored and logged (`costs.usage_redelivered`), never a correction (Q2) | `tests/unit/test_cost_observations.py::test_a_redelivered_row_with_another_fee_is_ignored_and_logged` | U | fixture | ISS-23 |
| The same, when a new row id comes twice on one page: the first copy is stored, the second logged | `tests/unit/test_cost_observations.py::test_a_row_id_twice_on_one_page_with_another_fee_keeps_the_first_and_is_logged` | U | fixture | ISS-23 |
| A late row for a job retention purged stays unattributed, and reconciliation still balances | `tests/unit/test_cost_observations.py::test_a_late_row_for_a_job_retention_purged_stays_unattributed` | U | fixture | ISS-23 |
| On a live stack: three syncs and an engine restart change no total, and reconciliation stays balanced | [M3 live suite](../references/evidence/2026-10-30-m3-live-suite.md), run G | L | live | ISS-23 |
| On a live stack: with Batteries down a paid job stays succeeded, its cost `pending`, and `/readyz` names the sync error; it settles once Batteries is back | [M3 live suite](../references/evidence/2026-10-30-m3-live-suite.md), run F | L | live | ISS-23 |
| On a live stack: a long stream's cost is `pending` mid-stream and settles with several rows; after an engine restart mid-stream the job is `interrupted`, `uncertain`, and its cost still settles | [M3 live suite](../references/evidence/2026-10-30-m3-live-suite.md), run E | L | live | ISS-23 |

## Payment failures, live (ISS-12, D58)

The three failures ISS-12 must tell apart, and `not_provisioned`, on the real signer and Batteries.

| Behavior | Check | Level | Evidence | Ticket |
| --- | --- | --- | --- | --- |
| On a live stack: an exhausted allocation is 402 `allowance_exhausted`, no `Retry-After`, the job failed and unpaid | [M3 live suite](../references/evidence/2026-10-30-m3-live-suite.md), run A | L | live | ISS-12 |
| On a live stack: a paused allocation is 403 `not_authorized`, no `Retry-After` | [M3 live suite](../references/evidence/2026-10-30-m3-live-suite.md), run A | L | live | ISS-12 |
| On a live stack: a stopped signer is 503 `signer_unavailable` with `Retry-After: 10`, buffered and streamed, with no failover | [M3 live suite](../references/evidence/2026-10-30-m3-live-suite.md), run A | L | live | ISS-12 |
| On a live stack: no response carries a payment key or the signer's URL, and no key appears in the engine's logs | [M3 live suite](../references/evidence/2026-10-30-m3-live-suite.md), runs A and C | L | live | ISS-12 |

## Live probe

`scripts/live_probe.py` is a client of the engine's REST API only; it is not in CI or the pytest suite
(D61, Q1). One run against a stack marks the L rows `live` (Q3); paid jobs are ISS-25's funded proof.

```sh
LPB_PROBE_URL=https://engine.example \
LPB_PROBE_ADMIN_KEY=lpk_... \
LPB_PROBE_FUND_ACTOR=alice \
uv run --frozen python scripts/live_probe.py --yes
```

| Variable | Meaning |
| --- | --- |
| `LPB_PROBE_URL` | The engine's base URL. Unset: the probe says so and exits 0 |
| `LPB_PROBE_ADMIN_KEY` | An admin `lpk_` key (`lpb bootstrap-admin`). The engine must accept API keys. Unset: as above |
| `LPB_PROBE_FUND_ACTOR` | An actor already provisioned in application `default`. Unset: the fund check is skipped |
| `LPB_PROBE_FUND_AMOUNT` | Optional. Default 0.01 USD or 0.000005 ETH, in the allocation's currency; above 0.05 USD or 0.00002 ETH it refuses to spend |

Without `--yes` it spends nothing. It issues two actor keys and an auditor key for fresh
`lpb-probe-<run>` actors and revokes them at the end, records two failed `no_offering` jobs for them (no
payment), never provisions, and runs no paid job. Its jobs name the run's own app, `lpb-probe/<run>`;
it sends none if the catalog lists that app, and fails unless both end `no_offering` with nothing paid:
in `per_application` mode the shared key would pay for an advertised app on the POST itself. It prints PASS, FAIL or SKIP per check and a summary,
and exits 1 when a check failed. Its logic is checked without a network against the engine in-process:
`tests/unit/test_live_probe.py::test_every_check_passes_against_the_engine_and_the_fund_applies_once`.

**Evidence:** [first run, 30 October 2026](../references/evidence/2026-10-30-iss-23-live-probe.md),
against the console's stack with the engine at `ade673f`: 25 passed, 0 failed, 0 skipped.
