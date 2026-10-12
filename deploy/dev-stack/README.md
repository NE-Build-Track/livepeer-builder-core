# Dev stack

An optional local stack for driving the engine against real Livepeer payment components. It is not
part of CI. **Orchestrators and runners are assumed to exist on the network**, so the stack runs only
the payment side and the engine:

| Service | What it is |
| --- | --- |
| `clearinghouse` | Clearinghouse Batteries `ca09e47`: authorization webhook, management API, embedded Kafka, accounting |
| `signer` | livepeer/node `1432123` remote signer. It discovers and pays the orchestrators listed in `config/signer.toml` |
| `engine` | This repository, built from the checkout and served with `lpb serve` on `127.0.0.1:8080` |

Upstream sources are built from the M2 baseline commits
([upstream-baseline](../../docs/references/upstream-baseline.md)), so the first build takes a while.

## Prerequisites

- A **payer keystore** whose account has a funded TicketBroker deposit and reserve on Arbitrum One.
- An **Arbitrum One RPC URL**. It may contain an API key; it lives only in `secrets/rpc-url`.
- The orchestrators to use, listed in `config/signer.toml` (`Orchestrators`).

## Start

```sh
cd deploy/dev-stack
python3 config/init-secrets.py            # Batteries creds, engine token, webhook, Kafka users
# add (mode 0600; no trailing newline): secrets/rpc-url, secrets/keystore.json, secrets/keystore-password
python3 config/pin-orchestrator-certs.py  # pin the listed orchestrators' (often self-signed) TLS certs
docker compose up -d --build clearinghouse signer
docker compose exec clearinghouse clearinghouse --db-path /data/clearinghouse.db \
  grant create --name Dev --sponsor Dev --amount-usd 5 --status active      # note the grant id
cp .env.example .env && chmod 600 .env     # fill LPB_BATTERIES_TOKEN (secrets/engine-token), LPB_GRANT_ID, LPB_SECRET_KEY
docker compose up -d --build engine
```

Check the signer: `docker compose exec clearinghouse curl -s http://127.0.0.1:8938/readyz` (it checks
chain funds and price policy) and `…:8937/discover-orchestrators` (the runners it can pay).

## Use

```sh
docker compose exec engine lpb bootstrap-admin            # prints the admin token once
ADMIN=lpk_...
curl -s -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d '{"kind":"actor","actor_id":"alice"}' http://127.0.0.1:8080/v1/admin/keys          # an actor key
curl -s -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d '{"amount":{"amount":"1","currency":"USD"}}' \
  http://127.0.0.1:8080/v1/admin/applications/default/actors/alice/allocation          # explicit funding
ALICE=lpk_...
curl -s -H "Authorization: Bearer $ALICE" http://127.0.0.1:8080/v1/offerings
curl -s -i -H "Authorization: Bearer $ALICE" -H 'Content-Type: application/json' \
  -d '{"app":"<app from offerings>","path":"/v1/embeddings","payload":{"model":"...","input":"hi"}}' \
  http://127.0.0.1:8080/v1/jobs
```

Check attribution with `GET /v1/jobs/{id}/cost` and `GET /v1/admin/reconciliation?actor_id=alice`.

## Known blockers (12 October)

- **T10:** the node signer requires an orchestrator's address to equal its ticket recipient. The
  public orchestrators tested differ, so signing fails with 400.
- ~~**T9**~~ **Resolved** in SDK PR 5 `84ec542`, which reads node's lower-case `"state"` key. Before the fix, `auth_id`s came back empty and
  costs stay `pending`.

See [the evidence](../../docs/references/evidence/2026-10-12-dev-stack-paid-run.md) and the
[tech-debt tracker](../../docs/exec-plans/tech-debt-tracker.md).

## Notes

- **`secrets/`, `.env` and `config/pinned-certs/` are git-ignored.** Keep the RPC URL and keys only
  there.
- **Containers run as root** (`user: "0:0"`) so bind-mounted secrets stay readable. This is dev only.
- **The engine's management credential** (`builder-engine` in `secrets/clearinghouse.toml`) has no
  `grants.*`. The grant is created with the operator CLI.
- **Pinned certificates** are trust-on-first-use. Re-run `config/pin-orchestrator-certs.py` when an
  orchestrator rotates its certificate, and only list orchestrators you intend to pay.
- **Docker network error:** if Docker reports "all predefined address pools have been fully
  subnetted", run `docker network prune`.
