# Dev stack

An optional local stack for driving the engine against real Livepeer payment components. It is not
part of CI. **Orchestrators and runners are assumed to exist on the network**, so the stack runs only
the payment side and the engine:

| Service | What it is |
| --- | --- |
| `clearinghouse` | Clearinghouse Batteries `ca09e47`: authorization webhook, management API, embedded Kafka, accounting |
| `signer` | NE-Build-Track/node `406b248` (v1.0.2) remote signer, the same commit Railway deploys. It pays; the engine does not use it for discovery (D42) |
| `engine` | This repository, built from the checkout and served with `lpb serve` on `127.0.0.1:8080`. It discovers the orchestrators listed in `config/orchestrators.json` |

Upstream sources are built from the M2 baseline commits
([upstream-baseline](../../docs/references/upstream-baseline.md)), so the first build takes a while.

## Prerequisites

- A **payer keystore** whose account has a funded TicketBroker deposit and reserve on Arbitrum One.
- An **Arbitrum One RPC URL**. It may contain an API key; it lives only in `secrets/rpc-url`.
- The orchestrators to use, listed in `config/orchestrators.json` (`[{"address": "https://…"}]`). The
  engine discovers only those whose TLS certificate the system trust store accepts (D42).

## Start

```sh
cd deploy/dev-stack
python3 config/init-secrets.py            # Batteries creds, engine token, webhook, Kafka users
# add (mode 0600; no trailing newline): secrets/rpc-url, secrets/keystore.json, secrets/keystore-password
docker compose up -d --build clearinghouse signer
docker compose exec clearinghouse clearinghouse --db-path /data/clearinghouse.db \
  grant create --name Dev --sponsor Dev --amount-usd 5 --status active      # note the grant id
cp .env.example .env && chmod 600 .env     # fill LPB_BATTERIES_TOKEN (secrets/engine-token),
                                           # LPB_BATTERIES_OPERATOR_TOKEN (secrets/grant-desk-token),
                                           # LPB_SECRET_KEY
docker compose up -d --build engine
```

Check the signer: `docker compose exec clearinghouse curl -s http://127.0.0.1:8938/readyz` (it checks
chain funds and price policy). Check discovery: `curl -s http://127.0.0.1:8080/readyz` shows
`discovery_fresh` and any `discovery_error`; the engine log names each orchestrator it leaves out
(`discovery.orchestrator_excluded`).

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

## Status (19 October)

- **Session-log attribution works live** on node v1.0.2 and the NE-Build-Track pins: entries
  `matched`, reconciliation `balanced` ([evidence](../../docs/references/evidence/2026-10-19-dev-stack-session-log.md)).
- **Stacks created before 16 October** lack the `grant-desk` credential, and their `builder-engine`
  credential lacks `grants.fund`. Add both to `secrets/clearinghouse.toml` (as `config/init-secrets.py`
  writes them), save the grant-desk secret as `secrets/grant-desk-token`, set
  `LPB_BATTERIES_OPERATOR_TOKEN` in `.env`, and recreate `clearinghouse` and `engine`. The engine
  refuses to start without the operator token.
- **T10 resolved:** the signer at `f970be6` pays orchestrators whose ticket recipient differs from
  their node address. Paid jobs on public orchestrators succeed and reconcile.
- **T9 resolved:** SDK PR 5 reads node's lower-case `"state"` key, so attempts record their `auth_id`
  with no shim.

See [the 14 October evidence](../../docs/references/evidence/2026-10-14-dev-stack-public-orchestrators.md)
and the [tech-debt tracker](../../docs/exec-plans/tech-debt-tracker.md).

## Notes

- **`secrets/` and `.env` are git-ignored.** Keep the RPC URL and keys only there.
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
  The Railway Batteries image writes the same `builder-engine` list. `tests/structure/test_deploy.py`
  fails when the two lists drift.
- **No pinned certificates (D42).** The engine and the signer verify TLS with the system trust store,
  and nothing in this stack installs extra certificates. An orchestrator with a self-signed
  certificate is left out of discovery and never called. `config/orchestrators.json` lists the
  orchestrators that served publicly trusted certificates on 21 October 2026. `config/signer.toml`
  still has `Orchestrators`, but only the signer's own `/discover-orchestrators` reads it.
- **Docker network error:** if Docker reports "all predefined address pools have been fully
  subnetted", run `docker network prune`.
