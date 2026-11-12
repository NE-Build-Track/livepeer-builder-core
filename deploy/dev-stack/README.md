# Dev stack

An optional local stack for driving the engine against real Livepeer payment components. It is not
part of CI. **Orchestrators and runners are assumed to exist on the network**, so the stack runs the
payment side, node's discovery and the engine:

| Service | What it is |
| --- | --- |
| `clearinghouse` | Clearinghouse Batteries v0.0.3 (`03bc9fb`): authorization webhook, management API, embedded Kafka, accounting |
| `signer` | livepeer/node `f45d83f` remote signer (main, 10 commits after v1.0.6). It only signs payments |
| `discovery` | livepeer/node's `livepeer-discovery` from that same commit, static mode, on `127.0.0.1:8939`. The list is `config/discovery.toml`. The engine still only reads `GET /discovery` ([contract](../../docs/contracts/discovery.md)) |
| `engine` | This repository, built from the checkout and served with `lpb serve` on `127.0.0.1:8080`. It reads runners from discovery (`LPB_DISCOVERY_URL`) |

Upstream sources are built from the pinned commits
([upstream-baseline](../../docs/references/upstream-baseline.md)), so the first build takes a while.

## Prerequisites

- A **payer keystore** whose account has a funded TicketBroker deposit and reserve on Arbitrum One.
- An **Arbitrum One RPC URL**. It may contain an API key; it lives only in `secrets/rpc-url`.
- The orchestrators to use, listed in `config/discovery.toml`. Discovery collects each listed URL:
  it GETs `<base>/discovery` at startup and every `RefreshInterval`, and it verifies TLS with the
  system trust store. A runner without `observed_at`, or one stamped more than 30 seconds ahead, is
  dropped, and a TLS failure never enters the catalog. `/readyz` on the metrics port is ready when
  the process listens; it does not report an empty inventory. The engine still refuses an untrusted
  certificate when it calls a runner.

## Start

```sh
cd deploy/dev-stack
python3 config/init-secrets.py            # Batteries creds, engine token, webhook, Kafka users
# add (mode 0600; no trailing newline): secrets/rpc-url, secrets/keystore.json, secrets/keystore-password
docker compose up -d --build clearinghouse signer
docker compose exec clearinghouse clearinghouse --db-path /data/clearinghouse.db \
  grant create --name Dev --sponsor Dev --amount-usd 5 --status active      # note the grant id
cp .env.example .env && chmod 600 .env     # fill LPB_BATTERIES_TOKEN (secrets/engine-token),
                                           # LPB_BATTERIES_OPERATOR_TOKEN (secrets/grant-desk-token;
                                           #   optional, turns on the grant desk),
                                           # LPB_SECRET_KEY
docker compose up -d --build engine
docker compose up -d discovery
```

Check the signer: `docker compose exec clearinghouse curl -s http://127.0.0.1:8938/readyz` (it checks
chain funds and price policy). Check discovery: `curl -s http://127.0.0.1:8939/discovery` (the runners
the engine sees). Readiness is on the metrics port, `127.0.0.1:8940/readyz`, which is not published.

## Use

```sh
docker compose exec engine lpb bootstrap-admin            # prints the admin token once
ADMIN=lpk_...
curl -s -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d '{"kind":"actor","actor_id":"alice","operation_ref":"dev-key-alice"}' \
  http://127.0.0.1:8080/v1/admin/keys                                                  # an actor key
curl -s -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d '{"amount":{"amount":"1","currency":"USD"},"operation_ref":"dev-provision-alice"}' \
  http://127.0.0.1:8080/v1/admin/applications/default/actors/alice/allocation          # explicit funding
ALICE=lpk_...
curl -s http://127.0.0.1:8080/v1/offerings                                             # public, no key
curl -s -i -H "Authorization: Bearer $ALICE" -H 'Content-Type: application/json' \
  -d '{"app":"<app from offerings>","path":"/v1/embeddings","payload":{"model":"...","input":"hi"}}' \
  http://127.0.0.1:8080/v1/jobs
```

Check attribution with `GET /v1/jobs/{id}/cost` and `GET /v1/admin/reconciliation?actor_id=alice`.

## Status (19 October, at the pins of that day)

- **Session-log attribution works live** on node v1.0.2 and the NE-Build-Track pins: entries
  `matched`, reconciliation `balanced` ([evidence](../../docs/references/evidence/2026-10-19-dev-stack-session-log.md)).
- **Stacks created before 16 October** lack the `grant-desk` credential, and their `builder-engine`
  credential lacks `grants.fund`. Add both to `secrets/clearinghouse.toml` (as `config/init-secrets.py`
  writes them), save the grant-desk secret as `secrets/grant-desk-token`, set
  `LPB_BATTERIES_OPERATOR_TOKEN` in `.env`, and recreate `clearinghouse` and `engine`. Without the
  operator token the engine starts with the grant desk off (`/v1/admin/grants` is absent).
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
- **Stacks on Batteries `ca09e47` need a rebuild** (`docker compose up -d --build clearinghouse`, then
  recreate the signer as below). v0.0.3 refuses a non-loopback bind without `unsafe@`, so the old
  `config/clearinghouse.toml` no longer starts it. On its first start it applies migrations `003` and
  `004` to the `paid-state` database; `ca09e47` then refuses to start on it ("unknown migration")
  ([the Batteries pin](../../docs/references/upstream-baseline.md#the-batteries-pin)).
- **Restarting `clearinghouse` needs the signer recreated.** The signer runs in clearinghouse's network
  namespace (`network_mode: service:clearinghouse`), so after clearinghouse restarts it stays on the old
  namespace and every payment fails `signer_unavailable`. Recreate it: `docker compose up -d
  --force-recreate --no-build signer` (seen live on 30 October,
  [evidence](../../docs/references/evidence/2026-10-30-m3-live-suite.md)).
- **The signer container runs as root** (`user: "0:0"`) so bind-mounted secrets stay readable.
  Discovery and clearinghouse run as their images' own users. This is dev only.
- **The engine image runs as `lpb` (uid 10001)**, and `/.dockerignore` keeps `secrets/`, `.env`,
  `.venv` and caches out of the build context. An `engine-data` volume created by an older, root-run
  image needs its ownership fixed once:
  `docker compose run --rm --user 0 engine chown -R 10001:10001 /data`.
- **The engine's management credential** (`builder-engine` in `secrets/clearinghouse.toml`) may
  `grants.fund` and has no `grants.*`. Batteries does not scope that permission to one grant, and an
  engine admin may fund any grant it names. `grant-desk` has `grants.read` and `grants.create` for
  `LPB_BATTERIES_OPERATOR_TOKEN`. A stack created before that credential existed needs it added to
  `secrets/clearinghouse.toml`, plus `secrets/grant-desk-token`, before the engine's grant desk can
  list or create grants.
  Any deployment gives the engine the same permissions, which `config/init-secrets.py` writes:
  - `builder-engine` (`LPB_BATTERIES_TOKEN`): `grants.fund`, `allocations.create`, `allocations.fund`,
    `allocations.read`, `allocations.status`, `allocations.revoke`, `api_keys.create`, `api_keys.read`,
    `api_keys.revoke` and `usage.read`;
  - `grant-desk` (`LPB_BATTERIES_OPERATOR_TOKEN`, optional): `grants.read` and `grants.create`.
- **No pinned certificates.** The signer no longer discovers, so it needs no orchestrator certificates
  (T12 closed by D43). `config/pinned-certs/` is gone. The engine still verifies runner TLS against
  the system trust store.
- **Docker network error:** if Docker reports "all predefined address pools have been fully
  subnetted", run `docker network prune`.
