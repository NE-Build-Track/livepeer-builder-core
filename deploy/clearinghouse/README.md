# Clearinghouse project

A payment clearinghouse on one host: the engine in its `clearinghouse` role, in front of Batteries, the
signer and discovery. Optional, and not part of CI. The arrangement, its limits and the proof runbook
are in [../README.md](../README.md); the enterprise's side is [../enterprise/](../enterprise/README.md).

"Payment clearinghouse" means this whole project. "Batteries" is the upstream program
livepeer/clearinghouse-batteries, whose own binary and config keys say `clearinghouse`: here it is the service
`batteries`.

| Service | What it is | Reached by |
| --- | --- | --- |
| `engine` | This repository, served with `lpb serve`, with `LPB_ROLE=clearinghouse`. Its **public listener** (`:8080`) serves a tenant's own routes, the runner list and the signer's two paths. Its **private listener** (`127.0.0.1:8081` in its own container) also serves what only a clearinghouse admin does: onboarding, the tenant list, a grant's top-up, a tenant's status and the wallet report. It serves no job route and runs no job | The host, at `127.0.0.1:8180` (the public listener). An enterprise project, at `http://clearinghouse:8080` on the network `lpb-clearinghouse-edge` |
| `batteries` | Clearinghouse Batteries v0.0.3 (`03bc9fb`): the authorization webhook, the management API, embedded Kafka, accounting, and its on-chain listener (`--enable-onchain-listener`), which reports the wallet's deposit and reserve | The engine only, on the `inside` network. Published nowhere |
| `signer` | livepeer/node `eee09c4` remote signer (v1.0.7), in the `batteries` network namespace. It signs with the wallet's key | The engine only, at `http://batteries:8937`. Published nowhere |
| `discovery` | livepeer/node's `livepeer-discovery` from that commit, static mode: the orchestrators in `config/discovery.toml`. The engine publishes what it reads as the clearinghouse's runner list | The engine only. Published nowhere |
| `private` | A curl in the engine's network namespace, for the private listener. Not started by `up` | `docker compose run --rm private ...` |

Upstream sources are built from the pinned commits
([upstream-baseline](../../docs/references/upstream-baseline.md)), so the first build takes a while.

## Files

| File | Committed | What it is |
| --- | --- | --- |
| `compose.yaml` | Yes | The project, and the engine's settings |
| `.env.example` | Yes | The variables Compose needs, with placeholders. `.env` is git-ignored |
| `config/batteries.toml`, `config/signer.toml` | Yes | Batteries' and the signer's configuration. No address or key |
| `config/discovery.example.toml` | Yes | A discovery configuration that names `orchestrator.example`. `config/discovery.toml`, with real orchestrators, is git-ignored |
| `config/init-secrets.py` | Yes | Generates `secrets/` and `.env`, once |
| `secrets/` | No | Batteries' credential registry (`batteries.toml`), the engine's two tokens, the signer's webhook and Kafka files, and `rpc-url` |

## Prerequisites

- A **wallet key folder**: an encrypted key file and its password file, for an account with a funded
  TicketBroker deposit and reserve on Arbitrum One. The folder is mounted read-only into the signer from
  wherever it is kept (`WALLET_KEY_DIR`); nothing is copied here. Only one signer may use a wallet at a
  time.
- The wallet's **public address** (`WALLET_ADDRESS`), for Batteries' on-chain listener.
- An **Arbitrum One RPC address**. It may contain an API key; it lives only in `secrets/rpc-url`.
- The **orchestrators** to list, in `config/discovery.toml`.

## Start

```sh
cd deploy/clearinghouse
python3 config/init-secrets.py                 # secrets/ and .env: Batteries' credentials, the engine's
                                               # two tokens and a new LPB_SECRET_KEY
# add secrets/rpc-url (mode 0600; no trailing newline)
cp config/discovery.example.toml config/discovery.toml      # then name the orchestrators there
# in .env: WALLET_KEY_DIR, WALLET_KEYSTORE_FILE, WALLET_PASSWORD_FILE, WALLET_ADDRESS
docker compose up -d --build
docker compose exec engine lpb payments check  # the signer and Batteries, with GET requests only
```

`docker compose --env-file .env.example config` shows the project with the placeholders.

## Use

```sh
docker compose exec -T engine lpb bootstrap-admin          # the clearinghouse admin's key, shown once
private() { docker compose --progress quiet run --rm -T private "$@"; }   # curl on the private listener
private -H "Authorization: Bearer $ADMIN" http://127.0.0.1:8081/v1/clearinghouse/wallet
private -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d '{"name":"Acme","amount":{"amount":"5","currency":"USD"},"operation_ref":"onboard-acme"}' \
  http://127.0.0.1:8081/v1/clearinghouse/tenants            # onboards a tenant: its id and its admin key
curl -s http://127.0.0.1:8180/readyz                        # the public listener; "role": "clearinghouse"
```

- **A clearinghouse admin key does nothing on the public listener** (403), and the routes above are 404
  there ([access.md](../../docs/contracts/access.md#listeners)). A tenant's admin and its engine use the
  public listener, and are refused on the private one.
- **Hand a tenant three things:** the public listener's address, the tenant's id and its tenant admin
  key. Its engine holds nothing else of this project
  ([payments.md](../../docs/contracts/payments.md#the-remote-adapters)).
- **The wallet report** shows the deposit and reserve once Batteries' on-chain listener has read to the
  chain's head, and whether the signer answers
  ([payments.md](../../docs/contracts/payments.md#the-wallet-report)). The signer's own readiness, which
  checks chain funds and price policy: `docker compose exec batteries curl -s http://127.0.0.1:8938/readyz`.

## Batteries' permissions

`config/init-secrets.py` writes the two credentials the engine uses, as the dev stack does:

- `builder-engine` (`LPB_BATTERIES_TOKEN`): `grants.fund`, `allocations.create`, `allocations.fund`,
  `allocations.read`, `allocations.status`, `allocations.revoke`, `api_keys.create`, `api_keys.read`,
  `api_keys.revoke` and `usage.read`;
- `grant-desk` (`LPB_BATTERIES_GRANTS_TOKEN`): `grants.read`, `grants.create`, `grants.fund`,
  `grants.status`, `allocations.read` and, for the wallet report, `escrow.read`.

A clearinghouse needs both: without the grants credential the engine does not start in this role.

## Notes

- **Restarting `batteries` needs the signer recreated.** The signer runs in its network namespace, so
  after a restart it stays on the old one and every payment fails `signer_unavailable`:
  `docker compose up -d --force-recreate --no-build signer`.
- **The signer container runs as root** (`user: "0:0"`) so the read-only key folder and `secrets/` stay
  readable whatever the host uid is. `config/discovery.toml` is read by the image's own user, so it is
  an ordinary file, not one in `secrets/`.
- **The engine image is the dev stack's** (`deploy/dev-stack/engine/Dockerfile`), under this project's
  own tag, and runs as `lpb` (uid 10001).
- **Stop:** `docker compose down` keeps the ledger and the engine's records; `docker compose down -v`
  removes them.
