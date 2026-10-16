# Railway preview

This directory deploys the engine to the Railway **preview** environment. Production, Batteries,
and the signer still deploy from
[gateway-integration-lab](https://github.com/NE-Build-Track/gateway-integration-lab).

The image is this checkout (`pip install ".[server,postgres]"`), not a pinned clone of the
repository. The local payment stack remains [deploy/dev-stack](../dev-stack/README.md).

## What runs

| Railway service | This directory | Still deployed from the lab |
| --- | --- | --- |
| engine | `engine/` | |
| batteries | | yes |
| signer | | yes |
| Postgres | Railway database; not a deploy target | yes |

The preview environment already has Batteries, the signer, and the engine's `LPB_*` variables.
This workflow does not redeploy those services and does not set `RAILWAY_TOKEN`.

## Deploy

The workflow is manual (`workflow_dispatch`). Opening or merging a pull request does not deploy.
Add `RAILWAY_PREVIEW_TOKEN` to this repository's Actions secrets first. That value is the Railway
project token already bound to the preview environment, the same one the lab uses. The workflow
does not pass `--project` or `--environment`.

Run **Deploy preview** and enter the pull request number. The job checks out that pull request,
copies `engine/railway.toml` to the repository root for the upload, and runs:

```sh
railway up . --service engine --ci
```

The repository root is the Docker context. Uploading only `engine/` would leave the Python package
out of the image. The copied `railway.toml` is not committed.

`LPB_DATABASE_URL` may use Railway's `postgresql://` scheme. The entrypoint rewrites it to
`postgresql+asyncpg://` and does not log the URL. `LPB_AUTO_MIGRATE=false`: Railway runs
`entrypoint.py migrate` before the new process starts, and the process refuses a database that is
behind the package.

Health is `GET /livez` on the engine's public preview domain. Swagger is at `/docs`. Authorize
sends `Authorization: Bearer`. The engine still enforces its own checks.

Kafka and the authorization webhook stay on the project private network. Do not attach a public
proxy to Batteries or the signer.
