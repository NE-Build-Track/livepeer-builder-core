# Dev stack

An optional local stack for driving the engine against real Livepeer payment components. It is not
part of CI. **Orchestrators and runners are assumed to exist on the network**, so the stack runs only
the payment side and the engine:

| Service | What it is |
| --- | --- |
| `clearinghouse` | Clearinghouse Batteries `ca09e47`: authorization webhook, management API, embedded Kafka, accounting |
| `signer` | livepeer/node `f970be6` remote signer. It discovers and pays the orchestrators listed in `config/signer.toml` |
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
cp .env.example .env && chmod 600 .env     # fill LPB_BATTERIES_TOKEN (secrets/engine-token),
                                           # LPB_BATTERIES_OPERATOR_TOKEN (secrets/grant-desk-token),
                                           # LPB_SECRET_KEY
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

## Status (14 October)

- **T10 resolved:** the signer at `f970be6` pays orchestrators whose ticket recipient differs from
  their node address. Paid jobs on public orchestrators succeed and reconcile.
- **T9 resolved:** SDK PR 5 reads node's lower-case `"state"` key, so attempts record their `auth_id`
  with no shim.

See [the 14 October evidence](../../docs/references/evidence/2026-10-14-dev-stack-public-orchestrators.md)
and the [tech-debt tracker](../../docs/exec-plans/tech-debt-tracker.md).

## Notes

- **`secrets/`, `.env` and `config/pinned-certs/` are git-ignored.** Keep the RPC URL and keys only
  there.
- **Image registry:** `IMAGE_REGISTRY` in `.env` prefixes the images this stack builds
  (`<registry>/livepeer-builder-dev/{clearinghouse,node,engine}`). This environment uses
  `registry.livepeer.tools`; leave it empty for local-only names. Builds never push; publishing an
  image is an explicit `docker compose push <service>`. Base images (`python`, Go) still come from
  Docker Hub.
- **The clearinghouse and signer containers run as root** (`user: "0:0"`) so bind-mounted secrets
  stay readable. This is dev only.
- **The engine image runs as `lpb` (uid 10001)**, and `/.dockerignore` keeps `secrets/`, `.env`,
  `.venv` and caches out of the build context. An `engine-data` volume created by an older, root-run
  image needs its ownership fixed once:
  `docker compose run --rm --user 0 engine chown -R 10001:10001 /data`.
- **The engine's management credential** (`builder-engine` in `secrets/clearinghouse.toml`) may
  `grants.fund` and has no `grants.*`. Batteries does not scope that permission to one grant, and an
  engine admin may fund any grant it names. `grant-desk` has `grants.read` and `grants.create` for
  `LPB_BATTERIES_OPERATOR_TOKEN`. A stack created before that credential existed needs it added to
  `secrets/clearinghouse.toml`, plus `secrets/grant-desk-token`, before the engine will start.
- **Pinned certificates** are trust-on-first-use and **temporary** (T12). Orchestrators are to serve
  CA-signed certificates (D27); once the ones listed do, drop the script and the `pinned-certs` mount.
  Until then, re-run `config/pin-orchestrator-certs.py` when an orchestrator rotates its certificate,
  and only list orchestrators you intend to pay.
- **Docker network error:** if Docker reports "all predefined address pools have been fully
  subnetted", run `docker network prune`.
