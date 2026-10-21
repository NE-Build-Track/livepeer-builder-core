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
- **Discovery:** no credential goes to the orchestrator list (`LPB_ORCHESTRATOR_LIST`) or to the
  orchestrators. Discovery and paid calls verify TLS with the process trust store; verification is
  never disabled and no CA is installed. An orchestrator with an untrusted certificate is left out of
  discovery and never called (D42). The list is operator configuration: it decides which runners
  may be selected, so serve a URL list only from a host you control.
- **Management API:** the engine uses a least-privilege Batteries credential. It may `grants.fund` and has no `grants.*`. Batteries applies `grants.fund` to every grant on the instance. An engine admin may fund any grant it names (`POST /v1/admin/grant/fund`); an actor key cannot. Listing and creating grants uses `LPB_BATTERIES_OPERATOR_TOKEN` (`grants.read` and `grants.create`).
  Batteries keeps replayed API-key secrets in its idempotency table, so protect Batteries backups like
  a vault.

## Reporting

Report vulnerabilities privately to the repository owner. Don't open a public issue.
