# M2 decisions

**Source:** the M2 ticket walkthrough held by Mike Zupper and John on 12 October 2026, and the
accepted Build Track baseline (Cloud-SPE/Network-Engineering-SPE, approved 30 September 2026).

**Status:** accepted. Changing any of these decisions needs Mike's approval. Record the change here.

## Baseline and scope

| # | Decision | Consequence in this repository |
| --- | --- | --- |
| D1 | The baseline is the `eliteprox` forks at `main` (12 Oct). Each repository has a `releases/m2` branch for M2 work | [references/upstream-baseline.md](../references/upstream-baseline.md) |
| D2 | **livepeer/node** is the signer and orchestrator. go-livepeer is no longer used | node's durable outbox removes the dropped-event gap (go-livepeer #4100) |
| D3 | Console is out of scope (at most a later reference-app update) | No Console code or parity work |
| D4 | This repository (`NE-Build-Track/livepeer-builder-core`, formerly `mikezupper/livepeer-builder-core`) replaces the `eliteprox/livepeer-core` prototype | Prototype code is a starting point and was refactored into these layers |
| D5 | Keep the accepted M2 plan: the core skeleton, SQLite through a vendor-neutral ORM (Postgres too), and minimal CI | `store/` uses SQLAlchemy Core and Alembic; CI runs SQLite and Postgres |
| D6 | No deadline changes. Nothing closes without Mike's review. Josh reviews engine code | Process; see [PLANS.md](../PLANS.md) |

## Engine contracts (ISS-02)

| # | Decision | Where |
| --- | --- | --- |
| D7 | Contract docs live here, under `docs/contracts/`, next to the code | [contracts/](../contracts/index.md) |
| D8 | Async jobs **and** persistent sessions are **defined in M2 and implemented in M4** (ISS-26, ISS-27). Async uses engine-level async by default, with runner-level recovery handles as an optional extension | `JobRequest.mode`, `ResultRef.storage="stored"`, `services/reserved.py` → 501 |
| D9 | **The engine proxies the persistent `app_url`.** Clients never receive it | [engine.md#persistent-sessions](../contracts/engine.md#persistent-sessions) |
| D10 | The events feed is a **contract definition only** in M2 | [events.md](../contracts/events.md) |
| D11 | Idempotency semantics are owned by John's PR. Interim behavior: a repeated `operation_ref` returns 409 `OperationExists`; a changed body returns 409 `OperationConflict` | [engine.md#idempotency](../contracts/engine.md#idempotency) |

## Access (ISS-03): accepted design

| # | Decision | Where |
| --- | --- | --- |
| D12 | Two small protocols: `Authenticator` (sees the whole request; returns an actor, "not mine", or rejects) and `AccessPolicy`. Imported mode needs no authenticator | `protocols/`, [access.md](../contracts/access.md) |
| D13 | **Define all modes, implement two.** API key is implemented (standalone baseline). JWT bearer comes in M4, plus MCP protected-resource metadata. Authorization-server flows (PKCE, CIMD, device, token exchange) belong to the enterprise identity provider | [access.md#modes](../contracts/access.md#modes) |
| D14 | Enterprise service mode: **both** JWT bearer **and** a trusted backend using a service credential with `actors.assert`, bound to one application | [access.md#service-mode](../contracts/access.md#service-mode) |
| D15 | Payment keys live behind a `SecretStore`; the database holds only a `SecretRef`. Built-ins: a settings store and an encrypted-database store | `adapters/secrets.py` |
| D16 | Harvest John's auth drafts into `access.md`, with a non-normative appendix for authorization-server flows | [access.md](../contracts/access.md) |

## Payments and costs (ISS-04)

| # | Decision | Where |
| --- | --- | --- |
| D17 | Two allocation modes; **`per_actor` is the default** for enterprise deployments. There is no silent fallback to a shared key | `services/payments.py` |
| D18 | **No auto-provisioning.** Provisioning is an explicit admin command. A zero amount sets up identity without money; funding is a separate explicit command with an `operation_ref` | [payments.md#provisioning](../contracts/payments.md#provisioning) |
| D19 | Units: `Money(amount, currency)` throughout. Cost carries `fee_eth` and `fee_usd`. **USD grants are the default** | `contracts/common.py` |
| D20 | Correlation key stays **`(allocation_id, payment_session_id)`**. **Resolved 14 October** on [livepeer-python-gateway#74](https://github.com/livepeer/livepeer-python-gateway/issues/74): the SDK reports `manifest_id` from the 402 challenge ([#77](https://github.com/livepeer/livepeer-python-gateway/pull/77)) and does not decode signer state. The engine keeps its own session log and resolves each entry to a Batteries `payment_session_id` when exactly one usage session and one log entry share `(allocation_id, manifest_id)`. Any other shape is `ambiguous`: the attempt is `uncertain` and those rows stay `unattributed`. Whether a manifest is unique per challenge is still open (T13) | [payments.md#correlation](../contracts/payments.md#correlation) |
| D21 | Rejected-ticket charges are accepted and documented: network cost is expected value at signing and is not adjusted | [payments.md#cost-classification](../contracts/payments.md#cost-classification) |
| D22 | Cost classification: `delivered` / `failed_after_payment` / `uncertain` / `pending`, plus `unattributed` and reconciliation | `services/costs.py` |
| D23 | Mode B (hosted payment operator) is deferred to M4 (P4). It is recorded as a gap | [payments.md#mode-b](../contracts/payments.md#mode-b) |
| D24 | SDK #71 is **required**. #70 (pool size) and multipart are deferred beyond M2 | `pyproject.toml` pin |

## Runtime findings

| # | Decision | Where |
| --- | --- | --- |
| D25 | ~~**T10: no upstream change to the node signer for now** (Mike, 12 October).~~ **Superseded 13 October:** Mike filed [livepeer/node#1](https://github.com/livepeer/node/issues/1), and Josh fixed it in `f970be6` (the signer uses `TicketParams.Recipient` and ignores `OrchestratorInfo.Address`). The dev stack now pins `f970be6` | [tech-debt T10](../exec-plans/tech-debt-tracker.md) |
| D26 | **Fail over when the signer refuses a specific orchestrator before payment** (Mike, 12 October). The outcome is `payment_refused` (signer 400/481); caller-side refusals (402 and others) don't fail over | [engine.md](../contracts/engine.md#behavior-the-contract-fixes), `services/jobs.py` |
| D27 | **Orchestrators serve CA-signed TLS certificates; the signer won't skip verification** (Josh, 13 October; Mike agreed). [livepeer/node#2](https://github.com/livepeer/node/issues/2) was closed as not planned. Mike will move the orchestrators the build track uses to publicly trusted certificates. Until then the dev stack pins their certificates (`config/pin-orchestrator-certs.py`) | [dev-stack README](../../deploy/dev-stack/README.md) |
| D28 | **No configured grant** (John, 16 October, [#15](https://github.com/NE-Build-Track/livepeer-builder-core/issues/15)). Provisioning and `POST /v1/admin/grant/fund` use the grant on the request, or the grant of the caller's own allocation when the request omits one. An admin may name any grant. The usage feed is not filtered by grant. `LPB_GRANT_ID` is not read | [payments.md#provisioning](../contracts/payments.md#provisioning) |

## Assumptions accepted by Mike (12 October)

- **A1 (accepted).** `lpb_usage_rows` keeps a cache of Batteries usage rows for attribution and reconciliation.
  Batteries remains the authority. Retention is configurable.
- **A2 (accepted).** Retention never deletes `uncertain` jobs automatically.
- **A3 (accepted; updated 21 October).** The SDK is pinned to `3238731`, fetched from NE-Build-Track/livepeer-python-gateway `dev/bytestreaming` ([NE livepeer-python-gateway#5](https://github.com/NE-Build-Track/livepeer-python-gateway/pull/5); was `e6d243b`, the head of [livepeer-python-gateway#77](https://github.com/livepeer/livepeer-python-gateway/pull/77)): `on_payment_session` reports `LivePaymentSession.manifest_id` and does not decode signer state. Re-pin when NE #5 merges, then to a release (T1).
