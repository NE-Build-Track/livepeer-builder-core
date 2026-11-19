# Security

## Secrets

| Secret | Where it lives | Never appears in |
| --- | --- | --- |
| Allocation keys (`lpg_`) | `SecretStore` (encrypted DB by default, which needs `LPB_SECRET_KEY`, or an enterprise vault); records hold a `SecretRef` | DB plaintext, responses, events, logs, `ActorContext` |
| Payment keys a clearinghouse issues (`lpp_`, #166) | Only their SHA-256 hash is stored (`lpb_payment_keys`); the token is shown once, in the answer to the allotment or the rotation that issued it | DB plaintext, the operation ledger, any read, logs |
| Engine API keys (`lpk_`), a tenant admin's included | Only their SHA-256 hash is stored; the token is shown once, and the answer to onboarding a tenant is the one place a tenant's first admin key appears | DB plaintext, list/inspect responses |
| `LPB_SECRET_KEY` (Fernet KEK) | The deployment's secret manager | The database, backups |
| `LPB_BATTERIES_TOKEN` | The deployment's secret manager | Clients, responses |
| `LPB_BATTERIES_GRANTS_TOKEN` (optional, the tenant desk) | The deployment's secret manager | Clients, responses |
| `LPB_CLEARINGHOUSE_TOKEN` (#168): the tenant admin key of an engine that uses a payment clearinghouse | The deployment's secret manager. It is sent as a bearer to the tenant's routes at `LPB_CLEARINGHOUSE_URL`, and to the discovery address only when that is the clearinghouse's runner list. No redirect is followed | Logs, error text, `repr`, the output of `lpb payments check`, the clearinghouse's two signer paths, any runner, any other address |
| The payment keys an enterprise's engine is handed by its clearinghouse (`lpp_`, #168) | The enterprise's `SecretStore`, where an allocation key is kept with self-hosted payments; the actor's record holds a `SecretRef`. It is sent as the bearer of that actor's signer calls, at the clearinghouse's two signer paths and nowhere else | The engine's own answers (`payment_key: null`), DB plaintext, the operation ledger, logs, the tenant's routes, any runner |

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
- **Payment keys (D87, #166):** a clearinghouse gives an enterprise's engine one payment key for each
  actor, never the Batteries key behind it. A payment key finds one actor, and only while that actor
  holds the allocation the key was issued for: rotating it stops the old one at once, and revoking the
  allocation stops it. It is no access key: as a bearer on any route but the signer's two paths it gets
  what no credential gets. `tests/unit/test_issued_payment_keys.py` checks both, and that the token is in
  no table and no log line ([payments.md](contracts/payments.md#payment-keys)).
- **Signing through a clearinghouse (D87, #166):** `POST /sign-orchestrator-info` and
  `POST /generate-live-payment` take a payment key and nothing else: no credential, an access key and a
  Batteries key are 401 there, on both paths, and nothing is forwarded for them. The clearinghouse reads
  the actor's Batteries key from its own secret store and calls its signer with it. That key is in no
  answer, error body or log line, an answer that held it would be withheld, and the payment key is never
  sent to the signer or to Batteries. Only the request body and its content type go to the signer, and
  only the signer's status, body and content type come back. The private signer's address is in the log
  only (`tests/unit/test_signing_routes.py`,
  [payments.md](contracts/payments.md#signing-through-a-clearinghouse)).
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
  tenant's key ([access.md](contracts/access.md#defaultpolicy)). No Batteries credential leaves the
  engine.
- **The private listener (D84, #167):** a clearinghouse admin key can onboard tenants and top up grants,
  so a leaked one could spend the wallet. A deployment that sets `LPB_HTTP_PRIVATE_PORT` serves what
  only a clearinghouse admin does (onboarding, the tenant list, a grant's top-up, a tenant's status and
  the wallet report) on a private listener, which binds loopback unless told otherwise. On the public
  listener those routes are 404, as any unknown path, and a clearinghouse admin credential is 403 on
  every route that takes an access credential, so a leaked clearinghouse admin key does nothing where
  the engine is published. The private listener takes a clearinghouse admin credential only. Reaching
  it is not enough: it still asks for the credential. The signer's two paths (#166) are on both
  listeners and take a payment key on both; an access key of any kind is 401 there. With
  one listener, the default, there is no such boundary: keep the engine off the public network, or put
  the admin routes behind your own proxy ([access.md](contracts/access.md#listeners)).
- **The wallet report (#167):** it states the wallet's deposit and reserve, the unspent grants and
  whether the signer answers. It holds no key material, no keystore path and no RPC address; the
  wallet's public address is in it because Batteries reports it. The engine reads no chain and holds no
  RPC address ([payments.md](contracts/payments.md#the-wallet-report)).
- **A tenant's usage (#166):** the usage route reads Batteries with its `grant_id` filter set to the
  tenant in the path, after the policy check, and a Batteries cursor is bound to that filter, so one
  tenant's admin reads no row of another's ([payments.md](contracts/payments.md#a-tenants-usage)). The
  clearinghouse's runner list names orchestrators and runners, which the network publishes anyway; it is
  still served to a tenant's admin or auditor only.
- **Management API:** the engine uses a least-privilege Batteries credential. It may `grants.fund` and has
  no `grants.*`. Batteries applies `grants.fund` to every grant on the instance, so only a clearinghouse
  admin may top up a grant (`POST /v1/clearinghouse/tenants/{tenant_id}/grant/top-up`); a tenant admin
  and an actor key cannot. Onboarding a tenant, the tenant list, a grant's read and a tenant's status use
  `LPB_BATTERIES_GRANTS_TOKEN` (`grants.read`, `grants.create`, `grants.fund`, `grants.status`,
  `allocations.read` and, for the wallet report, `escrow.read`); it is optional, and without it the
  tenant desk and its routes are absent.
  Batteries keeps replayed API-key secrets in its idempotency table, so protect Batteries backups like
  a vault.

## Reporting

Report vulnerabilities privately to the repository owner. Don't open a public issue.
