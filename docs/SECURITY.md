# Security

## Secrets

| Secret | Where it lives | Never appears in |
| --- | --- | --- |
| Allocation keys (`lpg_`) | `SecretStore` (encrypted DB by default, which needs `LPB_SECRET_KEY`, or an enterprise vault); records hold a `SecretRef` | DB plaintext, responses, events, logs, `ActorContext` |
| Engine API keys (`lpk_`) | Only their SHA-256 hash is stored; the token is shown once | DB plaintext, list/inspect responses |
| `LPB_SECRET_KEY` (Fernet KEK) | The deployment's secret manager | The database, backups |
| `LPB_BATTERIES_TOKEN` | The deployment's secret manager | Clients, responses |
| `LPB_BATTERIES_OPERATOR_TOKEN` (optional, the grant desk) | The deployment's secret manager | Clients, responses |
| `LPB_SHARED_PAYMENT_KEY` (`per_application` mode) | The deployment's secret manager | Clients, responses, logs |

Settings use `SecretStr`, so secrets don't print in reprs. `tests/unit/test_payments.py` checks that
no plaintext payment key is stored, and `tests/unit/test_http.py` checks that none is returned.

**The payment path stays out of builder responses (D58).** A payment failure's message is the
engine's own; the signer URL and the signer's or Batteries' text go to the log only, and
`GET /v1/allowance` answers a provider error with a fixed message. `LPB_SIGNER_URL` may carry no
userinfo, query or fragment (refused at startup), so the URL in the log holds no secret, and a refused
setting is never echoed. `tests/unit/test_payment_failures_http.py` scans every builder-facing body
for a payment key, the signer URL and the signer's words ([payments.md](contracts/payments.md#who-answers-what)).

## Trust boundaries

See [contracts/access.md](contracts/access.md#boundaries).

- **Identity:** requests authenticate through the authenticator chain. No request field establishes
  identity.
- **Admin:** admin is a distinct credential kind, bootstrapped once from the CLI. There is no network
  bootstrap and no static admin token.
- **Payment calls:** the caller's credential stops at the engine. Only the actor's allocation key
  goes to the signer.
- **Management API:** the engine uses a least-privilege Batteries credential. It may `grants.fund` and has no `grants.*`. Batteries applies `grants.fund` to every grant on the instance. An engine admin may fund any grant it names (`POST /v1/admin/grant/fund`); an actor key cannot. Listing and creating grants uses `LPB_BATTERIES_OPERATOR_TOKEN` (`grants.read` and `grants.create`); it is optional, and without it the grant desk and its routes are absent.
  Batteries keeps replayed API-key secrets in its idempotency table, so protect Batteries backups like
  a vault.

## Reporting

Report vulnerabilities privately to the repository owner. Don't open a public issue.
