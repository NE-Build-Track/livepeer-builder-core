# Railway

This directory deploys the engine from this checkout. Batteries and the signer still deploy from
[gateway-integration-lab](https://github.com/NE-Build-Track/gateway-integration-lab). The local
payment stack remains [deploy/dev-stack](../dev-stack/README.md).

The image is this checkout (`pip install ".[server,postgres]"`), not a pinned clone of the
repository.

## What runs

| Railway service | This directory | Still deployed from the lab |
| --- | --- | --- |
| engine | `engine/` | |
| batteries | | yes |
| signer | | yes |
| Postgres | Railway database; not a deploy target | yes |

Each Railway environment already has Batteries, the signer, and the engine's `LPB_*` variables.
This workflow does not redeploy those services.

## Environments

The workflow reads `RAILWAY_TOKEN` from a GitHub environment. The token is a Railway project token,
so the workflow does not pass `--project` or `--environment`. Put the secret on the GitHub
environment, not on the repository: a repository secret of the same name is used when the
environment secret is missing.

| Git ref | GitHub environment | Railway token |
| --- | --- | --- |
| `main`, `releases/*` | `production` | production project token |
| any other branch | `preview` | preview project token |

A push deploys the engine. **Deploy** can also be run by hand for the selected branch. Limit the
`production` environment's deployment branches to `main` and `releases/*` so a branch job cannot
select it. Required reviewers on `production` gate that deploy.

The lab still has its own production engine deploy. Until that workflow stops deploying the engine,
either repository can replace the production engine.

## Upload

The job runs:

```sh
railway up --service engine --ci
```

The repository root is the Docker context. Uploading only `engine/` would leave the Python package
out of the image.

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
