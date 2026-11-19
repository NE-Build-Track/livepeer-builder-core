# Security

## Secrets

| Secret | Where it lives | Never appears in |
| --- | --- | --- |
| Allocation keys (`lpg_`) | `SecretStore` (encrypted DB by default, which needs `LPB_SECRET_KEY`, or an enterprise vault); records hold a `SecretRef` | DB plaintext, ordinary reads, events, logs, `ActorContext` |
| Engine API keys (`lpk_`), a tenant admin's included | Only their SHA-256 hash is stored; the token is shown once, and the answer to onboarding a tenant is the one place a tenant's first admin key appears | DB plaintext, list/inspect responses |
| `LPB_SECRET_KEY` (Fernet KEK) | The deployment's secret manager | The database, backups |
| `LPB_BATTERIES_TOKEN` | The deployment's secret manager | Clients, responses |
| `LPB_BATTERIES_GRANTS_TOKEN` (optional, the tenant desk) | The deployment's secret manager | Clients, responses |

Settings use `SecretStr`, so secrets don't print in reprs. `tests/unit/test_payments.py` checks that
no plaintext payment key is stored, and `tests/unit/test_http.py` checks that none is returned.

**The payment path stays out of builder responses (D58).** A payment failure's message is the
engine's own; the signer URL and the signer's or Batteries' text go to the log only, and
`GET /v1/allowance` answers a provider error with a fixed message. `LPB_SIGNER_URL` may carry no
userinfo, query or fragment (refused at startup), so the URL in the log holds no secret. A refused
setting is never echoed: the refusal names the setting, and what was given is in none of its forms, its
text, `errors()`, `json()` or traceback (`tests/unit/test_settings_refusals.py`).
`tests/unit/test_payment_failures_http.py` scans every builder-facing body
for a payment key, the signer URL and the signer's words ([payments.md](contracts/payments.md#who-answers-what)).

## Trust boundaries

See [contracts/access.md](contracts/access.md#boundaries).

- **Identity:** requests authenticate through the authenticator chain. No request field establishes
  identity. A bearer longer than 16 KiB is a 401 before any authenticator sees it, and a token that
  cannot be read, however malformed, is a 401, never a 500 (#113, C22,
  [access.md](contracts/access.md#protocols)).
- **Admin:** an admin is a distinct credential kind: `clearinghouse_admin`, bootstrapped once from the
  CLI, or `tenant_admin`, which onboarding a tenant issues. There is no network bootstrap and no static
  admin token.
- **Payment calls:** the caller's credential stops at the engine. Only the actor's allocation key
  goes to the signer.

- **Runner addresses:** a runner URL names one app on an orchestrator, and a job's `path` is appended
  to it. The contract refuses a path that is not already normal (a dot or empty segment, an encoded
  separator, a backslash, a control character, a lone surrogate or a fragment) with a 422 before anything
  is written, and the transport sends the
  address as built and refuses one that no longer begins with the runner's (#113, C3,
  [engine.md](contracts/engine.md#jobs-and-attempts)).
- **Tenants (D83, #165):** Batteries cannot keep one tenant from another: each of its permissions covers
  every grant, allocation and key on the instance. The engine draws that line. A tenant's id is its
  grant's id, every command passes the tenant it acts on to the access policy, and a tenant admin is
  refused there for any other tenant (403) before any lookup or provider call. A tenant admin divides its
  tenant's grant and cannot enlarge it, issue an admin key, or act through `/v1/keys` on another
  tenant's key ([access.md](contracts/access.md#defaultpolicy)). Only allocation credentials are handed to authorized work engines; management credentials stay private.
- **A tenant's usage (#166):** the usage route reads Batteries with its `grant_id` filter set to the
  tenant in the path, after the policy check, and a Batteries cursor is bound to that filter, so one
  tenant's admin reads no row of another's ([payments.md](contracts/payments.md#a-tenants-usage)). The
  clearinghouse's runner list names orchestrators and runners, which the network publishes anyway; it is
  still served to a tenant's admin or auditor only.
- **Management API:** the engine uses a least-privilege Batteries credential. It may `grants.fund` and has
  no `grants.*`. Batteries applies `grants.fund` to every grant on the instance, so only a clearinghouse
  admin may top up a grant (`POST /v1/clearinghouse/tenants/{tenant_id}/grant/top-up`); a tenant admin
  and an actor key cannot. Onboarding a tenant, the tenant list, a grant's read and a tenant's status use
  `LPB_BATTERIES_GRANTS_TOKEN` (`grants.read`, `grants.create`, `grants.fund`, `grants.status` and
  `allocations.read`); it is optional, and without it the tenant desk and its routes are absent.
  Batteries keeps replayed API-key secrets in its idempotency table, so protect Batteries backups like
  a vault.

## Reporting

Report vulnerabilities privately to the repository owner. Don't open a public issue.

## Allocation credential handoff

Standalone clearinghouse allotment and rotation return the allocation API key once to the authorized
operator/work engine. Only the work engine persists it. Responses use `Cache-Control: no-store`.
Completed retries return no secret; explicit rotation recovers a lost delivery. The signer is reached
directly and must have its Batteries authorization webhook configured. There are no signer proxy routes.
