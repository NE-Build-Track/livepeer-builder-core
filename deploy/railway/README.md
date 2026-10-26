# Railway

This directory deploys Batteries, the signer, the discovery service and the engine. The local payment
stack remains [deploy/dev-stack](../dev-stack/README.md). [compose.yaml](compose.yaml) is the same four
services for a local Docker run. CI checks that file with `docker compose config` and does not start it.

The engine image is this checkout (`pip install ".[server,postgres]"`), not a pinned clone of the
repository. Batteries is [NE-Build-Track/clearinghouse-batteries](https://github.com/NE-Build-Track/clearinghouse-batteries)
`ca09e47`. The signer is [NE-Build-Track/node](https://github.com/NE-Build-Track/node) `406b248` (v1.0.2), the same
commit the dev stack builds.

## What runs

| Railway service | This directory | Upload |
| --- | --- | --- |
| batteries | `batteries/` | `railway up deploy/railway/batteries --path-as-root --service batteries` |
| signer | `signer/` | `railway up deploy/railway/signer --path-as-root --service signer` |
| discovery | `discovery/` | `railway up --service discovery` from the repository root |
| engine | `engine/` | `railway up --service engine` from the repository root |
| Postgres | Railway database; not a deploy target | |

`batteries/entrypoint.py` writes the credential file when the container starts. The `builder-engine`
credential (`ENGINE_TOKEN`, wired to `LPB_BATTERIES_TOKEN`) includes `grants.fund` and has no
`grants.*`. That is the same list as `deploy/dev-stack/config/init-secrets.py`.
`tests/structure/test_deploy.py` fails when they drift. The operator credential (`OPERATOR_TOKEN`,
wired to `LPB_BATTERIES_OPERATOR_TOKEN`) keeps `grants.*`, so the desk can list and create grants.

A Batteries deploy rewrites that file and restarts the process. An engine deploy does not. Fund
grant stays `403 Forbidden` until this Batteries image is what production is running.

Each Railway environment already has these services and their variables. The five Batteries tokens
stay `OPERATOR_TOKEN`, `ENGINE_TOKEN`, `WEBHOOK_TOKEN`, `KAFKA_READER_TOKEN`, and
`KAFKA_PRODUCER_TOKEN`.

## Environments

The workflow reads `RAILWAY_TOKEN` from a GitHub environment. The token is a Railway project token,
so the workflow does not pass `--project` or `--environment`. Put the secret on the GitHub
environment, not on the repository: a repository secret of the same name is used when the
environment secret is missing.

| Git ref | GitHub environment | Railway token |
| --- | --- | --- |
| `main`, `releases/*` | `production` | production project token |
| any other branch | `preview` | preview project token |

A push to `main` or `releases/*` deploys Batteries, the signer, the discovery service and the engine.
A push to any other branch deploys the engine only, so a preview branch does not restart the shared
Batteries, signer and discovery. **Deploy** can also be run by hand for one service or all four, on the
selected branch.
Limit the `production` environment's deployment branches to `main` and `releases/*` so a branch job
cannot select it. Required reviewers on `production` gate that deploy.

## Upload

The engine and discovery jobs run `railway up --service <name> --ci` with the repository root as the
Docker context. Uploading only their directories would leave the Python package out of the image.

Batteries and the signer upload their own directories with `--path-as-root`, so their Railway
service settings use Dockerfile path `Dockerfile`. Batteries keeps its healthcheck on `/readyz`
at port `8081`, and both Batteries and the signer keep the `/data` volume. Those settings are
already on the services. This workflow does not change them.

## Engine service settings

Railway does not read a `railway.toml` from the upload. The engine's build and deploy settings live
on the engine service in each Railway environment, and every environment needs all of them:

| Setting | Value |
| --- | --- |
| Dockerfile path | `deploy/railway/engine/Dockerfile` |
| Pre-deploy command | `python3 -u /usr/local/bin/entrypoint.py migrate` |
| Healthcheck path | `/livez` |
| Healthcheck timeout | `300` seconds |
| Restart policy | always, 10 retries |

Without the pre-deploy command, production never migrates. Without the healthcheck, Railway
replaces the running engine as soon as the new container starts, even if it then exits.

## Discovery service settings

The engine reads runners from the discovery service (D43, [contract](../../docs/contracts/discovery.md)):
the engine image sets `LPB_DISCOVERY_URL=http://discovery.railway.internal:8939/discovery`, and the
signer only signs, so its config has no orchestrator list. Each Railway environment needs a service
named `discovery`, created once by the project owner, with:

| Setting | Value |
| --- | --- |
| Dockerfile path | `deploy/railway/discovery/Dockerfile` |
| Healthcheck path | `/readyz` (port `8939`) |
| Healthcheck timeout | `120` seconds (ready after the first probe pass) |
| Restart policy | always, 10 retries |
| Public networking | none; the engine reaches it on the private network |

It runs the static directory, [discovery/orchestrators.txt](discovery/orchestrators.txt). To use the
chain instead, set `LPD_DIRECTORY=onchain` and `LPD_ARBITRUM_RPC_URL` (a provider URL; it often carries
a key, so keep it a sealed variable). Orchestrators with untrusted TLS are left out either way.

## Engine database

`LPB_DATABASE_URL` may use Railway's `postgresql://` scheme. The entrypoint rewrites it to
`postgresql+asyncpg://` and does not log the URL. `LPB_AUTO_MIGRATE=false`, so the process does
not migrate as it starts. Railway runs `entrypoint.py migrate` before the new process starts.
On preview that command only checks the revision (`RAILWAY_ENVIRONMENT_NAME=preview`) and leaves
the shared database unchanged. On production it applies the packaged migrations, then checks.
Either way the deploy stops when the database is not at the package head.

Health is `GET /livez` on the engine's public domain. Swagger is at `/docs`. Authorize sends
`Authorization: Bearer`. The engine still enforces its own checks.

Kafka and the authorization webhook stay on the project private network. Do not attach a public
proxy to Batteries or the signer.
