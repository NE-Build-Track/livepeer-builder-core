# Security

## Secrets

| Secret | Where it lives | Never appears in |
| --- | --- | --- |
| Allocation keys (`lpg_`) | `SecretStore` (encrypted DB by default, which needs `LPB_SECRET_KEY`, or an enterprise vault); records hold a `SecretRef`. A standalone clearinghouse stores none: allotment and rotation hand each key once to the enterprise's engine, which keeps it in its own `SecretStore` (#166) | DB plaintext, the operation ledger, events, logs, `ActorContext`, and every response but that one handover |
| Engine API keys (`lpk_`), a tenant admin's included | Only their SHA-256 hash is stored; the token is shown once, and the answer to onboarding a tenant is the one place a tenant's first admin key appears | DB plaintext, list/inspect responses |
| `LPB_SECRET_KEY` (Fernet KEK) | The deployment's secret manager | The database, backups |
| `LPB_BATTERIES_TOKEN` | The deployment's secret manager | Clients, responses |
| `LPB_BATTERIES_GRANTS_TOKEN` (optional, the tenant desk) | The deployment's secret manager | Clients, responses |

Settings use `SecretStr`, so secrets don't print in reprs. `tests/unit/test_payments.py` checks that
no plaintext payment key is stored, and `tests/unit/test_http.py` checks that none is returned.
`tests/unit/test_allocation_delivery.py` checks that a key handed over is in no table and no store, and
is masked wherever the answer is dumped or logged.

**The payment path stays out of builder responses (D58).** A payment failure's message is the
engine's own; the signer URL and the signer's or Batteries' text go to the log only, and
`GET /v1/allowance` answers a provider error with a fixed message. `LPB_SIGNER_URL` may carry no
userinfo, query or fragment (refused at startup), so the URL in the log holds no secret. A refused
setting is never echoed: the refusal names the setting, and what was given is in none of its forms, its
text, `errors()`, `json()` or traceback. A rule's own message says what the setting needs and quotes no
value, a number included (`tests/unit/test_settings_refusals.py` gives each rule a value of its own to
refuse, and fails when a rule is added without one).
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
- **The payment key handover (D85, D87, #166):** a standalone clearinghouse hands each allocation's own
  Batteries key (`lpg_`) to the engine of the tenant it belongs to, once, in the answer to the allotment
  or the rotation that minted it. That answer goes to the tenant's admin or a clearinghouse admin and to
  nobody else: another tenant's admin is 403 before any provider call. The clearinghouse keeps no copy.
  The key is in a buffer of that one call, never in a store, a table, the operation ledger or a log
  line, and the answer carries `Cache-Control: no-store`. A repeat of a finished command hands over
  nothing, a resumed one hands over the key Batteries returns again, and a command whose actor was
  revoked, rotated or allotted again meanwhile hands over none
  (`tests/unit/test_allocation_delivery.py`, [payments.md](contracts/payments.md#payment-keys)). A key
  spends its own allocation and no other: a leaked one is bounded by that allocation, and a rotation or
  a revoke ends it. On a self-hosted engine no key leaves: the engine stores each one and returns none.
- **The signer is published; Batteries is not (D85 point 1):** at a clearinghouse every enterprise's
  engine signs at the signer itself, with the keys it was handed. The clearinghouse's engine is not in
  that path, so the signer's Batteries authorization webhook is the only check at signing: the signer
  is reachable by enterprise engines and keeps that webhook configured. Batteries' management API stays
  private, reached by the clearinghouse's own engine alone.
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
  tenant's key ([access.md](contracts/access.md#defaultpolicy)). No Batteries management credential
  leaves the engine. The one Batteries credential that leaves a standalone clearinghouse is an
  allocation's own key, handed once to the engine of the tenant it belongs to (above).
- **The private listener (D84, #167):** a clearinghouse admin key can onboard tenants and top up grants,
  so a leaked one could spend the wallet. A deployment that sets `LPB_HTTP_PRIVATE_PORT` serves what
  only a clearinghouse admin does (onboarding, the tenant list, a grant's top-up, a tenant's status and
  the wallet report) on a private listener, which binds loopback unless told otherwise. On the public
  listener those routes are 404, as any unknown path, and a clearinghouse admin credential is 403 on
  every route that takes a credential, so a leaked clearinghouse admin key does nothing where the
  engine is published. The private listener takes a clearinghouse admin credential only. Reaching it is
  not enough: it still asks for the credential. Neither listener hands an allocation's key to a caller:
  an engine with two listeners stores each key for its own jobs, as an engine with one does, and the
  handover above is the `clearinghouse` role's (#169). With one listener, the default, there is no such
  boundary: keep the engine off the public network, or put the admin routes behind your own proxy
  ([access.md](contracts/access.md#listeners)).
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
