# Enterprise project

An enterprise that uses a payment clearinghouse, on one host: its own engine, in its `work` role.
Optional, and not part of CI. The arrangement, its limits and the proof runbook are in
[../README.md](../README.md); the clearinghouse's side is [../clearinghouse/](../clearinghouse/README.md).

| Service | What it is |
| --- | --- |
| `engine` | This repository, served with `lpb serve`, with `LPB_ROLE=work`, on `127.0.0.1:8280`. It serves the work routes (jobs, the catalog, runners, costs, the allowance read and its own access keys) and no clearinghouse route or signer path. It runs the enterprise's jobs and sends them straight to orchestrators, and it pays through the clearinghouse |

There is no Batteries and no signer here, and the engine holds no address or credential of either. It
holds three things from the clearinghouse (its public listener's address, a tenant admin key and the
tenant's id), its own database and its own `LPB_SECRET_KEY`, under which it keeps the payment key the
clearinghouse issues for each actor. Its discovery address is unset, so it reads the clearinghouse's
runner list (D87). To use a discovery of its own, set `LPB_DISCOVERY_URL` in `compose.yaml`: the tenant
admin key is then sent to the clearinghouse alone.

## Files

| File | Committed | What it is |
| --- | --- | --- |
| `compose.yaml` | Yes | The project, and the engine's settings |
| `.env.example` | Yes | The variables Compose needs, with placeholders. `.env` is git-ignored |
| `allot.py` | Yes | Allots one actor through the engine's embedded API (below) |
| `secrets/` | No | Where the runbook keeps this side's keys |

## Start

The clearinghouse project is started first: this one joins its network `lpb-clearinghouse-edge`, where
the clearinghouse's public listener is `http://clearinghouse:8080`.

```sh
cd deploy/enterprise
cp .env.example .env && chmod 600 .env   # LPB_CLEARINGHOUSE_TOKEN and LPB_CLEARINGHOUSE_TENANT_ID from
                                         # onboarding, and a new LPB_SECRET_KEY
docker compose up -d --build
docker compose exec engine lpb payments check   # its grant, its usage, the signer's identity path and the
                                                # runner list at the clearinghouse, with GET requests only
```

`docker compose --env-file .env.example config` shows the project with the placeholders.

## Use

```sh
docker compose exec -T engine lpb bootstrap-admin          # this engine's own admin key, shown once
curl -s -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d '{"kind":"actor","actor_id":"alice","tenant_id":"'$TENANT'","operation_ref":"key-alice"}' \
  http://127.0.0.1:8280/v1/keys                             # an actor's access key
docker compose exec -T engine python /opt/enterprise/allot.py alice 1 allot-alice   # 1 USD from the grant
curl -s http://127.0.0.1:8280/v1/offerings                 # public: the clearinghouse's runner list
curl -s -i -H "Authorization: Bearer $ALICE" -H 'Content-Type: application/json' \
  -d '{"app":"<app from offerings>","path":"<path>","payload":{}}' http://127.0.0.1:8280/v1/jobs
```

- **Every actor is in the tenant.** The `tenant_id` of each access key this engine issues is the
  tenant's id at the clearinghouse (`LPB_CLEARINGHOUSE_TENANT_ID`): an actor in another tenant is
  refused there ([payments.md](../../docs/contracts/payments.md#who-the-engine-is-at-the-clearinghouse)).
- **Allotting is `allot.py`.** An engine in its `work` role serves no route that allots: the allocation
  routes are the clearinghouse half's. `allot.py` calls `engine.payments.allot` in the engine's own
  container, which takes the amount from the tenant's grant at the clearinghouse and keeps the actor's
  payment key in this engine's secret store, where its jobs find it. An allocation made at the
  clearinghouse directly gives this engine no payment key (tech debt T41). An enterprise app that
  embeds the engine calls `engine.payments.allot` itself.
- **Costs** are read here: `GET /v1/jobs/{id}/cost`, after `POST /v1/costs/sync` with this engine's
  admin key, which reads the tenant's usage at the clearinghouse.
- **Stop:** `docker compose down` keeps the engine's records; `docker compose down -v` removes them.
