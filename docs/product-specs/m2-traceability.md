# M2 traceability: tickets → code → tests → docs

Status as of 12 October 2026 (an uncommitted scaffold on `releases/m2`). Update this file in the same
change that moves a ticket.

## ISS-01: Integration baseline (accepted 12 October)

| Need | Where |
| --- | --- |
| Pinned revisions | [references/upstream-baseline.md](../references/upstream-baseline.md); `pyproject.toml` (SDK pin) |
| Behavior rechecked; runtime evidence | [evidence/2026-10-07-arbitrum-run.md](../references/evidence/2026-10-07-arbitrum-run.md), [evidence/2026-10-12-dev-stack-paid-run.md](../references/evidence/2026-10-12-dev-stack-paid-run.md) |
| Upstream wire formats relied on | [batteries-api.md](../references/batteries-api.md), [node-signer.md](../references/node-signer.md), [sdk-gateway.md](../references/sdk-gateway.md) |
| **Open follow-ups** | Re-run attribution without workarounds ([m2-attribution-rerun](../exec-plans/active/m2-attribution-rerun.md)); blocked by T9 and T10 |

## ISS-02: Engine contracts

| Contract area | Code | Tests | Doc |
| --- | --- | --- | --- |
| Access context | `contracts/access.py` | `tests/unit/test_access.py` | [access.md](../contracts/access.md) |
| Capabilities and rates | `contracts/discovery.py`, `services/discovery.py`, `adapters/sdk.py` | `test_adapters.py::test_discovery_entries_become_runners` | [engine.md#discovery](../contracts/engine.md#discovery) |
| Jobs and attempts; failover; idempotency; write ordering; recovery; admission | `contracts/jobs.py`, `services/jobs.py`, `services/admission.py` | `tests/unit/test_jobs.py` (12 tests) | [engine.md](../contracts/engine.md#behavior-the-contract-fixes) |
| Results | `ResultRef` (inline sha256 and size) | `test_success_records_attempt_payment_and_result_reference` | [engine.md#results](../contracts/engine.md#results) |
| Measured usage | `JobService.report_usage`, `MeasuredUsage` | `test_usage_is_reported_once_by_the_owner` | [engine.md#measured-usage](../contracts/engine.md#measured-usage) |
| Cost states and classification | `contracts/costs.py`, `services/costs.py` | `tests/unit/test_costs.py` | [payments.md#cost-classification](../contracts/payments.md#cost-classification) |
| Extension boundaries | `protocols/` | `tests/structure/test_layers.py` | [ARCHITECTURE.md#providers](../../ARCHITECTURE.md#providers) |
| Async, persistent, events (**defined**) | `contracts/reserved.py`, `services/reserved.py` → 501 | `test_reserved_execution_modes_fail_loudly`, `test_failures_and_reserved_routes` | [engine.md](../contracts/engine.md#persistent-sessions), [events.md](../contracts/events.md) |
| Deployment, performance and recovery assumptions | — | — | [engine.md](../contracts/engine.md#deployment-performance-and-recovery-assumptions) |
| Failover when the signer refuses one orchestrator (D26) | `adapters/sdk.py::call_error`, `services/jobs.py` | `test_signer_refusal_for_one_orchestrator_fails_over`, `test_caller_side_payment_refusal_does_not_fail_over`, `test_sdk_errors_keep_payment_evidence` | [engine.md](../contracts/engine.md#behavior-the-contract-fixes) |
| **Open** | Final idempotency semantics (John's PR, D11 / T2) | | |

## ISS-03: Access (design accepted)

| Need | Code | Tests | Doc |
| --- | --- | --- | --- |
| `Authenticator` / `AccessPolicy` protocols; chain semantics | `protocols/`, `adapters/auth.py` | `test_unrecognized_credentials_fall_through_but_invalid_ones_stop` | [access.md#protocols](../contracts/access.md#protocols) |
| Standalone API keys (hashed, scoped, revocable; inspection without disclosure) | `services/keys.py`, `adapters/auth.py` | `test_issued_key_authenticates_and_revocation_stops_it` | [access.md#modes](../contracts/access.md#modes) |
| Ownership and admin boundary | `services/policy.py` | `test_policy_needs_permission_and_ownership`, `test_admin_operations_need_an_admin_credential`, `test_other_actors_jobs_are_invisible` | [access.md#defaultpolicy](../contracts/access.md#defaultpolicy) |
| One-time admin bootstrap | `KeyService.bootstrap_admin`, `lpb bootstrap-admin` | `test_bootstrap_admin_is_one_time` | [access.md#boundaries](../contracts/access.md#boundaries) |
| Unchecked fields can't establish identity | `adapters/auth.py` | `test_request_fields_cannot_establish_identity` | [access.md#boundaries](../contracts/access.md#boundaries) |
| Payment-key custody | `adapters/secrets.py` | `test_secret_stores`, `test_payment_key_is_stored_encrypted_and_only_referenced` | [SECURITY.md](../SECURITY.md) |
| JWT bearer, trusted backend, MCP metadata (**defined**, M4) | — | — | [access.md#service-mode](../contracts/access.md#service-mode) |

## ISS-04: Payments

| Need | Code | Tests | Doc |
| --- | --- | --- | --- |
| Allocation modes; no fallback to a shared key | `services/payments.py` | `test_unprovisioned_actor_fails_before_any_payment`, `test_per_application_mode_uses_the_configured_key` | [payments.md#allocation-modes](../contracts/payments.md#allocation-modes) |
| Explicit provisioning; idempotent money moves; rotation and revocation | `services/payments.py`, `adapters/batteries.py` | `tests/unit/test_payments.py` (9 tests), `test_create_key_for_grant_is_one_idempotent_call` | [payments.md#provisioning](../contracts/payments.md#provisioning) |
| Least-privilege management credential | `deploy/dev-stack/config/init-secrets.py` | dev-stack run | [batteries-api.md](../references/batteries-api.md) |
| Reporting access and units | `BatteriesUsageSource`, `allowance_from_body`, `Money` | `test_usage_pages_and_cursor_mismatch`, `test_allowance_follows_the_allocation_currency` | [payments.md#units](../contracts/payments.md#units) |
| Stable references (`auth_id` join) | `services/costs.py::classify`, `lpb_attempt_auth_ids` | `test_failed_after_payment_and_forged_manifest_reconcile` | [payments.md#correlation](../contracts/payments.md#correlation) |
| Implementation evidence | — | — | [evidence/2026-10-12-dev-stack-paid-run.md](../references/evidence/2026-10-12-dev-stack-paid-run.md): balanced reconciliation |
| **Open** | SDK #71 upstream position; T9 (SDK `"state"` key); T10 (signer rejects address ≠ recipient); Mode B (M4) | | |

## ISS-05: Core packages

| Need | Where |
| --- | --- |
| One distribution with `[server]` and `[postgres]` extras; dev versions from git | `pyproject.toml` (hatchling + hatch-vcs) |
| Imports and starts without the reference app, PymtHouse or private source | `lpb smoke`; verified by a clean install of the built wheel (12 October) |
| Public interfaces documented | [ARCHITECTURE.md](../../ARCHITECTURE.md), [contracts/](../contracts/index.md), package-root exports in `livepeer_builder/__init__.py` |
| Layering and third-party confinement enforced | `tests/structure/test_layers.py` |
| Durable default store (no silent in-memory store) | `config.py` `DEFAULT_DATABASE_URL`; `test_settings_from_environment` |
| SDK dependency survives installation | Direct-reference pin, verified to include `auth_ids` (T1) |

## ISS-06: SQLite records

| Need | Where |
| --- | --- |
| Store behind `EngineStore`, vendor-neutral | `store/sql.py`, `store/schema.py`; `tests/store/test_contract.py` on SQLite and PostgreSQL 16 |
| Migrations as package data; drift check | `store/migrations/`, `store/migrate.py`; `test_migrations_match_the_schema`, `test_built_wheel_contains_migrations` |
| Restart; migration over retained records | `test_records_survive_restart_and_interrupted_jobs_resolve`, `test_upgrade_preserves_records_written_at_each_revision` |
| Backup and restore | `store/backup.py`, `lpb backup/verify/restore`; `test_sqlite_backup_and_restore` |
| Configurable retention | `store/retention.py`, `LPB_RETENTION_*`; `test_retention_keeps_uncertain_and_recent_jobs` |
| Provider ledger stays authoritative | [persistence.md](../contracts/persistence.md); assumption A1 |
| Assumptions | A1–A2 accepted by Mike (12 October) |

## ISS-07: Builds and CI

| Need | Where |
| --- | --- |
| Lint, types, docs, generated-docs drift, tests | `.github/workflows/ci.yml` job `checks` |
| PostgreSQL store suite | job `postgres` |
| Versioned dev build and clean-install smoke | job `build-install` (artifact `livepeer-builder-dist`) |
| **Open** | First green run after the branch is pushed |
