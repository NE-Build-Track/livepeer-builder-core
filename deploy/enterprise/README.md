# Enterprise project

An enterprise that uses a payment clearinghouse, on one host: its own engine, in its `work` role.
Optional, and not part of CI. The arrangement, its limits and the proof runbook are in
[../README.md](../README.md); the clearinghouse's side is [../clearinghouse/](../clearinghouse/README.md).

| Service | What it is |
| --- | --- |
| `engine` | This repository, served with `lpb serve`, with `LPB_ROLE=work`, on `127.0.0.1:8280`. It serves the work routes (jobs, the catalog, runners, costs, the allowance read and its own access keys) and no clearinghouse route. It runs the enterprise's jobs and sends them straight to orchestrators. It allots, reads allowances and reads usage at the clearinghouse, and signs each payment itself at the signer the clearinghouse publishes |

There is no Batteries and no signer here, and the engine holds no address or credential of Batteries.
It holds four things from the clearinghouse: its public listener's address
(`LPB_CLEARINGHOUSE_URL`), the address of its published signer (`LPB_SIGNER_URL`), a tenant admin key
and the tenant's id. It has its own database and its own `LPB_SECRET_KEY`, under which it stores the
key of each actor's allocation: the clearinghouse hands that key over once, when this engine allots or
rotates, and keeps none. Its discovery address is unset, so it reads the clearinghouse's runner list
(D87). To use a discovery of its own, set `LPB_DISCOVERY_URL` in `compose.yaml`: the tenant admin key
is then sent to the clearinghouse alone.

- **`LPB_SIGNER_URL` is needed to pay.** Without it the engine starts and runs its jobs unpaid, as any
  engine without a signer, and `lpb payments check` fails and says so. The clearinghouse's address
  never stands in for the signer's.
- **`LPB_SECRET_KEY` is needed to start.** The engine stores the keys it is handed.
- **A key crosses to the signer with every payment,** as the bearer of the two signing calls, and in the
  clear here: there is no TLS on one host ([Limits](../README.md#limits)).

## Files

| File | Committed | What it is |
| --- | --- | --- |
| `compose.yaml` | Yes | The project, and the engine's settings |
| `.env.example` | Yes | The variables Compose needs, with placeholders. `.env` is git-ignored |
| `allot.py` | Yes | Allots one actor through the engine's embedded API (below) |
| `secrets/` | No | Where the runbook keeps this side's keys |

## Start

The clearinghouse project is started first: this one joins its network `lpb-clearinghouse-edge`, where
the clearinghouse's public listener is `http://clearinghouse:8080` and its signer is
`http://signer:8937`.

```sh
cd deploy/enterprise
cp .env.example .env && chmod 600 .env   # LPB_CLEARINGHOUSE_TOKEN and LPB_CLEARINGHOUSE_TENANT_ID from
                                         # onboarding, and a new LPB_SECRET_KEY
docker compose up -d --build
docker compose exec engine lpb payments check   # its grant, its usage and the runner list at the
                                                # clearinghouse, and the published signer, with GET
                                                # requests only
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
  tenant's id at the clearinghouse (`LPB_CLEARINGHOUSE_TENANT_ID`). The routes this engine calls there
  name no tenant: the clearinghouse takes it from the tenant admin key. So an actor in another tenant is
  refused by this engine, before anything is sent
  ([payments.md](../../docs/contracts/payments.md#who-the-engine-is-at-the-clearinghouse)), and
  `lpb payments check` fails when the key is another tenant's than the setting names.
- **Allotting is `allot.py`.** An engine in its `work` role serves no route that allots: the allocation
  routes are the clearinghouse half's. `allot.py` calls `engine.payments.allot` in the engine's own
  container, which takes the amount from the tenant's grant at the clearinghouse and stores the key the
  clearinghouse hands over in this engine's secret store, where its jobs find it. Its output says
  `key_kept_by: secret_store`. An allocation made at the clearinghouse directly hands its key to that
  caller, and this engine has no record of the actor (tech debt T41). An enterprise app that embeds the
  engine calls `engine.payments.allot` itself.
- **A lost answer.** If an allotment's answer never arrives, the engine has no key: it says so, and the
  remedy is in [payments.md](../../docs/contracts/payments.md#the-key-an-enterprises-engine-is-handed).
- **Costs** are read here: `GET /v1/jobs/{id}/cost`, after `POST /v1/costs/sync` with this engine's
  admin key, which reads the tenant's usage at the clearinghouse.
- **Stop:** `docker compose down` keeps the engine's records; `docker compose down -v` removes them.
