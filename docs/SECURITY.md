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
- **Discovery:** no credential goes to the discovery URL (`LPB_DISCOVERY_URL`) or to the orchestrators
  in `LPB_ORCHESTRATOR_LIST`.
  - The discovery URL, the startup probe of each listed orchestrator, and every paid call verify TLS
    with the process trust store. Verification is never disabled and no CA is installed (D42).
  - A listed orchestrator whose certificate the store rejects is left out of discovery and never
    called.
  - Both settings are operator configuration: they decide which runners may be selected. Serve a
    discovery URL only from a host you control.
- **Management API:** the engine uses a least-privilege Batteries credential. It may `grants.fund` and has no `grants.*`. Batteries applies `grants.fund` to every grant on the instance. An engine admin may fund any grant it names (`POST /v1/admin/grant/fund`); an actor key cannot. Listing and creating grants uses `LPB_BATTERIES_OPERATOR_TOKEN` (`grants.read` and `grants.create`).
  Batteries keeps replayed API-key secrets in its idempotency table, so protect Batteries backups like
  a vault.

## Reporting

Report vulnerabilities privately to the repository owner. Don't open a public issue.
