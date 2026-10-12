# Security

## Secrets

| Secret | Where it lives | Never appears in |
| --- | --- | --- |
| Allocation keys (`lpg_`) | `SecretStore` (encrypted DB by default, or an enterprise vault); records hold a `SecretRef` | DB plaintext, responses, events, logs, `ActorContext` |
| Engine API keys (`lpk_`) | Only their SHA-256 hash is stored; the token is shown once | DB plaintext, list/inspect responses |
| `LPB_SECRET_KEY` (Fernet KEK) | The deployment's secret manager | The database, backups |
| `LPB_BATTERIES_TOKEN` | The deployment's secret manager | Clients, responses |

Settings use `SecretStr`, so secrets don't print in reprs. `tests/unit/test_payments.py` checks that
no plaintext payment key is stored, and `tests/unit/test_http.py` checks that none is returned.

## Trust boundaries

See [contracts/access.md](contracts/access.md#boundaries).

- **Identity:** requests authenticate through the authenticator chain. No request field establishes
  identity.
- **Admin:** admin is a distinct credential kind, bootstrapped once from the CLI. There is no network
  bootstrap and no static admin token.
- **Payment calls:** the caller's credential stops at the engine. Only the actor's allocation key
  goes to the signer.
- **Management API:** the engine uses a least-privilege Batteries credential with no `grants.*`.
  Batteries keeps replayed API-key secrets in its idempotency table, so protect Batteries backups like
  a vault.

## Reporting

Report vulnerabilities privately to the repository owner. Don't open a public issue.
