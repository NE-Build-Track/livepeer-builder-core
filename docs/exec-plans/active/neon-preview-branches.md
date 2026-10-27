# Neon Postgres, with a database branch per pull request

## Status

- **Owner:** John.
- **Ticket:** none. Operator cutover of the builder-engine database.
- **Branch:** `feat/neon-migration`.
- **Progress:** production is on Neon. PR #60 created `preview/pr-60`; the first preview job
  failed with HTTP 423 while the compute was still locked.

## Goal

Run the builder engine on Neon instead of Railway Postgres, and give every pull request its own
copy of production data. Production stays on one protected branch. A pull request gets a branch of
that data, migrates it, and the branch is deleted when the PR closes.

## Acceptance

- [x] Neon project `builder-engine` (`floral-frog-74114075`) in the Elite org, region
  `aws-eu-central-1`, Postgres 18. Branch `main` is protected, and its compute does not suspend.
  Autoscaling is 0.25–2 CU.
- [x] Production Railway Postgres restored onto `main`. Alembic revision `0005`. Row counts matched
  at cutover. `GET /livez` on the production engine returns 200, and later usage rows are on Neon.
- [x] The engine URL is the direct Neon host. The entrypoint maps `sslmode` to asyncpg's `ssl` and
  drops `channel_binding`. Preview deploys run migrations. Tests cover both.
- [x] `.github/workflows/preview.yml` creates `preview/pr-<number>` from `main` on open, synchronize,
  and reopen, and deletes it when the PR closes. Pushes of other branches no longer deploy the
  preview engine.
- [x] `NEON_API_KEY` and `NEON_PROJECT_ID` are set on the GitHub `preview` environment. PR #60
  created `preview/pr-60` from `main`.
- [x] Railway Postgres is still provisioned in production and preview.

## Progress

- [x] 27 October — Created the Neon project, protected `main`, and restored the production dump.
- [x] 27 October — Pointed the production engine at the direct Neon URL and confirmed `/livez` and
  revision `0005`.
- [x] 27 October — Entrypoint rewrite, preview migrations, and tests.
- [x] 27 October — Preview workflow and the production deploy workflow limited to `main` and
  `releases/*`.
- [x] 27 October — `NEON_PROJECT_ID` and `NEON_API_KEY` on the GitHub `preview` environment.
- [x] 27 October — Preview job retries Neon 423/409 while the compute is still locked.

## Decision log

- 27 October — John: preview branches are normal copies of production, including rows. Records hold
  secret references, not payment keys.
- 27 October — The Neon project is in the Elite org (`org-old-sky-87152913`), the same operator as
  the Railway project. Region `aws-eu-central-1` sits next to the engine in `europe-west4`.
- 27 October — One preview Railway engine remains. The preview job holds a lock, points
  `LPB_DATABASE_URL` at that PR's branch, then deploys. The live preview URL serves the last PR to
  deploy. Closing a PR deletes its branch and does not point the engine at `main`.
- 27 October — The app and Alembic use the direct hostname. A long-running engine needs a real
  session. Preview computes suspend after 5 minutes and cap at 1 CU.
- 27 October — Railway Postgres stays in place after the cutover so production can be pointed back
  at it.
