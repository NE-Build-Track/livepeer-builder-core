# Deploy: Compose projects

Optional Compose projects for driving the engine against real Livepeer payment components. None is
part of CI, and this repository does not deploy.

| Project | Folder | The engine's role | What it is |
| --- | --- | --- | --- |
| `livepeer-builder-dev` | [dev-stack/](dev-stack/README.md) | `both` | Self-hosted payments: one deployment with the engine, Batteries, the signer and discovery |
| `lpb-clearinghouse` | [clearinghouse/](clearinghouse/README.md) | `clearinghouse` | A payment clearinghouse: the engine in front of Batteries, the signer and discovery. It does payments only |
| `lpb-enterprise` | [enterprise/](enterprise/README.md) | `work` | An enterprise that uses that clearinghouse: its own engine, which runs its jobs |

The last two prove the payment clearinghouse on one host (ISS-31, D82, D87). They are an arrangement to
copy from, not a service: see [Limits](#limits).

## Two names that are not the same

- **The payment clearinghouse** is the hosted service an enterprise's engine pays through: an engine in
  its `clearinghouse` role, Batteries, the signer and a wallet. In these projects it is the whole
  `lpb-clearinghouse` project.
- **Batteries** is the upstream program
  [livepeer/clearinghouse-batteries](https://github.com/livepeer/clearinghouse-batteries). Its binary,
  its config keys and its environment variables say `clearinghouse`, and the dev stack names its service
  `clearinghouse` for that reason. In the two new projects it is always the service `batteries`, with
  the config `config/batteries.toml` and the credential file `secrets/batteries.toml`.

## The arrangement

```text
 lpb-enterprise                                lpb-clearinghouse
 ┌──────────────────────────┐   edge network   ┌───────────────────────────────────────────────────┐
 │ engine, role work        │ ───────────────► │ engine, role clearinghouse                        │
 │  jobs, runners, costs    │  payment calls   │  public listener :8080   (tenants, signer paths)  │
 │  127.0.0.1:8280          │  only            │  private listener 127.0.0.1:8081 (its admin)      │
 └────────────┬─────────────┘                  │        │ inside network                           │
              │ jobs, straight to orchestrators │        ▼                                          │
              ▼                                │  batteries + signer (wallet)      discovery       │
        orchestrators ◄────────────────────────┼── the runner list ──────────────────┘            │
                                               └───────────────────────────────────────────────────┘
```

| | `lpb-clearinghouse` | `lpb-enterprise` |
| --- | --- | --- |
| Services | `engine`, `batteries`, `signer`, `discovery`, and `private` (a curl for the private listener, started on demand) | `engine` |
| Published on the host | `127.0.0.1:8180`: the engine's public listener, and nothing else | `127.0.0.1:8280`: the engine's one listener |
| Never published | Batteries, the signer, discovery and the engine's private listener | |
| Networks | `lpb-clearinghouse_inside` (the engine, Batteries with the signer, discovery) and `lpb-clearinghouse-edge` (the engine alone) | `lpb-enterprise_default`, and it joins `lpb-clearinghouse-edge` |
| Volumes | `lpb-clearinghouse_engine-data`, `lpb-clearinghouse_paid-state` | `lpb-enterprise_engine-data` |
| Holds | Batteries' credentials, the wallet's key folder (mounted read-only), an RPC address, the orchestrators' addresses | The clearinghouse's address, a tenant admin key, the tenant's id, and one payment key for each actor |

- **What crosses the edge network is payment calls only:** the tenant's routes (allotment, allowance,
  usage), the runner list and the signer's two paths. A prompt or a result never does: the enterprise's
  engine sends each job straight to an orchestrator.
- **The private listener is on the clearinghouse engine's own loopback.** No network reaches it, the
  enterprise project included. The clearinghouse admin reaches it with the `private` service, a curl
  that runs in the engine's network namespace:
  `docker compose run --rm private http://127.0.0.1:8081/readyz`.
- **Each side has its own database, its own `LPB_SECRET_KEY` and its own credential files.**

## Secrets and addresses

Nothing real is committed. Each project's `.env`, the clearinghouse's `secrets/` and its
`config/discovery.toml` are git-ignored, and they are kept out of the engine image's build context.

| What | Where it comes from | Committed stand-in |
| --- | --- | --- |
| The wallet's key file and its password file | The folder `WALLET_KEY_DIR` names, mounted read-only into the signer; the two file names are `WALLET_KEYSTORE_FILE` and `WALLET_PASSWORD_FILE`. Nothing is copied: the signer reads the password from its own memory, without the newline a password file usually ends in | Placeholders in `clearinghouse/.env.example` |
| The wallet's public address | `WALLET_ADDRESS` in `clearinghouse/.env`, for Batteries' on-chain listener | The zero address |
| The RPC address | `clearinghouse/secrets/rpc-url` (mode 0600, no trailing newline). The signer and Batteries' on-chain listener read it | None |
| The orchestrators' addresses | `clearinghouse/config/discovery.toml` | `config/discovery.example.toml`, which names `orchestrator.example` |
| Batteries' credentials, the engine's two tokens, the clearinghouse's `LPB_SECRET_KEY` | `clearinghouse/config/init-secrets.py` generates them into `secrets/` and `.env` | Placeholders |
| The tenant admin key, the tenant's id, the enterprise's `LPB_SECRET_KEY` | `enterprise/.env`: the first two are the answer to onboarding | Placeholders |
| The access keys the runbook issues (each admin's, and an actor's) | Files under each project's `secrets/`, written by the runbook's commands | None |

## Limits

- One host, and the same people run both sides. The boundary is real (separate networks, volumes and
  credential files), and it is not a second organisation.
- No TLS and no proxy: the public listener is published on `127.0.0.1` and reached over a Docker
  network. A clearinghouse that others reach puts a TLS proxy in front of the public listener and
  publishes only that (tech debt T43).
- One signer for the wallet, and only one signer may use a wallet at a time: stop any other stack that
  signs with the same key before this one starts.
- Onboarding, grant top-ups and wallet funding are manual steps. Nothing here watches the wallet.
- No availability promise, no backups, and SQLite on a named volume.
- An engine server in its `work` role serves no route that allots, so the enterprise project allots with
  `enterprise/allot.py`, through the engine's embedded API (tech debt T41).

## Proof runbook

Three paid proofs, for ISS-31's "Done when" (#169): (a) an engine in its `work` role runs a paid fixed
job through an engine in its `clearinghouse` role that holds two tenants; (b) the same for a live job;
(c) an engine in its `both` role still runs a paid job by itself.

**Money.** Only a job moves money: each paid job sends the orchestrator payment tickets from the
wallet's deposit, for the price the runner asked. A grant and an allocation are entries in Batteries'
ledger, which cap what the signer will sign; creating them moves nothing on-chain. So the spend is the
price of one fixed job, a few payment cycles of one live job, and one more fixed job. Keep `GRANT` and
`ALLOT` to the smallest amounts that cover one job at the rates `GET /v1/offerings` shows: an
orchestrator may charge many times what another does for the same capability.

**What you supply** (values never go in git, a pull request or a terminal):

| Input | Where it goes |
| --- | --- |
| The folder with the funded wallet's key file and password file, and the two file names | `WALLET_KEY_DIR`, `WALLET_KEYSTORE_FILE`, `WALLET_PASSWORD_FILE` in `clearinghouse/.env` |
| The wallet's public address | `WALLET_ADDRESS` in `clearinghouse/.env` |
| An Arbitrum One RPC address | `clearinghouse/secrets/rpc-url` |
| The orchestrators to list | `clearinghouse/config/discovery.toml` |
| A fixed job to run, and a live one if a live runner is listed: `app`, `path` and `payload` | The variables `FIXED_JOB` and `LIVE_JOB` below |
| `GRANT` and `ALLOT`, in USD | The variables below |

The commands keep every access key in a file under a git-ignored `secrets/` folder and print none; no
payment key is kept or printed at all. They need
`docker compose`, `curl` and `jq`, and are run from this folder (`deploy/`).

### 0. Before anything is started

Only one signer may use the wallet. Stop every other stack that signs with it, and leave it stopped
until [Stop everything](#stop-everything).

```sh
umask 077
GRANT=0.50   # each tenant's grant, USD: a ledger entry, not a payment
ALLOT=0.25   # each actor's allocation, USD: the most its jobs can cost
FIXED_JOB='{"app":"<app from offerings>","path":"<path>","payload":{}}'
LIVE_JOB='{"app":"<live app from offerings>","path":"<path>","payload":{},"timeout_s":15}'

(cd clearinghouse && python3 config/init-secrets.py)       # secrets/ and .env
# then: clearinghouse/secrets/rpc-url, clearinghouse/config/discovery.toml, and the four WALLET_
# variables in clearinghouse/.env
```

### 1. The clearinghouse

```sh
cd clearinghouse
docker compose up -d --build
docker compose ps                                           # batteries healthy; signer, discovery, engine up
docker compose exec -T engine lpb payments check            # role clearinghouse; every probe ok; exit 0
curl -s http://127.0.0.1:8180/readyz | jq '{role, ready}'   # "clearinghouse", true
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8180/v1/jobs   # 404: no job route

private() { docker compose --progress quiet run --rm -T private "$@"; }   # curl on the private listener
docker compose exec -T engine lpb bootstrap-admin | jq -r .token > secrets/admin-key
admin() { private -H "Authorization: Bearer $(cat secrets/admin-key)" "$@"; }

admin http://127.0.0.1:8081/v1/clearinghouse/wallet | jq    # the deposit and reserve, and signer.ready
```

Check before going on: `lpb payments check` exits 0; the wallet report's `signer.ready` is `true` and
its `escrow` shows the wallet's deposit and reserve (Batteries' on-chain listener reports them once it
has read to the head: an empty `escrow` a minute after start means the listener is not on, or
`WALLET_ADDRESS` is not the signer's). The signer's own readiness, which checks chain funds and price
policy: `docker compose exec batteries curl -s http://127.0.0.1:8938/readyz`.

### 2. Two tenants, on the private listener

```sh
onboard() {  # onboard NAME: the tenant's id and its first tenant admin key, kept in secrets/
  admin -H 'Content-Type: application/json' http://127.0.0.1:8081/v1/clearinghouse/tenants \
    -d "{\"name\":\"$1\",\"amount\":{\"amount\":\"$GRANT\",\"currency\":\"USD\"},\"operation_ref\":\"proof-onboard-$1\"}" \
    > secrets/tenant-$1.json
  jq -r .tenant_id secrets/tenant-$1.json > secrets/tenant-$1-id
  jq -r .admin_key.token secrets/tenant-$1.json > secrets/tenant-$1-key
  rm secrets/tenant-$1.json
}
onboard one && onboard two
ONE=$(cat secrets/tenant-one-id) TWO=$(cat secrets/tenant-two-id)
admin http://127.0.0.1:8081/v1/clearinghouse/tenants | jq 'map({tenant_id, name, total, unallocated, spent})'
```

Costs nothing. Check: two tenants, each with `total` and `unallocated` of `GRANT` and `spent` of 0.

Tenant two allots its actor itself, on the public listener, with its own admin key:

```sh
PUBLIC=http://127.0.0.1:8180
tenant() { curl -sS -H "Authorization: Bearer $(cat secrets/tenant-$1-key)" "${@:2}"; }   # tenant one|two ...
tenant two -H 'Content-Type: application/json' \
  -d "{\"amount\":{\"amount\":\"$ALLOT\",\"currency\":\"USD\"},\"operation_ref\":\"proof-allot-bob\"}" \
  $PUBLIC/v1/clearinghouse/tenants/$TWO/actors/bob/allocation | jq '{allocation_id, status}'
```

Costs nothing. The answer's `payment_key` is tenant two's to keep; this proof does not use it, so the
command leaves it out.

### 3. The enterprise, as tenant one

```sh
cd ../enterprise
cp .env.example .env
sed -i "s|^LPB_CLEARINGHOUSE_TOKEN=.*|LPB_CLEARINGHOUSE_TOKEN=$(cat ../clearinghouse/secrets/tenant-one-key)|" .env
sed -i "s|^LPB_CLEARINGHOUSE_TENANT_ID=.*|LPB_CLEARINGHOUSE_TENANT_ID=$ONE|" .env
sed -i "s|^LPB_SECRET_KEY=.*|LPB_SECRET_KEY=$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')|" .env
docker compose up -d --build
docker compose exec -T engine lpb payments check            # role work; every probe ok; exit 0
WORK=http://127.0.0.1:8280
curl -s $WORK/readyz | jq '{role, ready}'                   # "work", true
curl -s $WORK/v1/offerings | jq 'map({app, rate_low, rate_high, stale})'   # the clearinghouse's runner list
curl -s -o /dev/null -w '%{http_code}\n' $WORK/v1/clearinghouse/tenants/$ONE/grant   # 404: no clearinghouse route

mkdir -p secrets
docker compose exec -T engine lpb bootstrap-admin | jq -r .token > secrets/admin-key
curl -sS -H "Authorization: Bearer $(cat secrets/admin-key)" -H 'Content-Type: application/json' \
  -d "{\"kind\":\"actor\",\"actor_id\":\"alice\",\"tenant_id\":\"$ONE\",\"operation_ref\":\"proof-key-alice\"}" \
  $WORK/v1/keys | jq -r .token > secrets/alice-key
docker compose exec -T engine python /opt/enterprise/allot.py alice $ALLOT proof-allot-alice
```

Costs nothing. Check: `lpb payments check` exits 0 and its `tenant` is tenant one's grant; the offerings
are not empty and not `stale`; the allotment prints alice's allocation, `active`. Read the rates in the
offerings now, and stop here if `ALLOT` does not cover one job.

### 4. Proof (a): a paid fixed job from the enterprise's engine

```sh
alice() { curl -sS -H "Authorization: Bearer $(cat secrets/alice-key)" "$@"; }
alice -D secrets/fixed-headers -o secrets/fixed-result -H 'Content-Type: application/json' -d "$FIXED_JOB" $WORK/v1/jobs
JOB=$(grep -i '^livepeer-job-id:' secrets/fixed-headers | tr -d '\r' | cut -d' ' -f2)
alice $WORK/v1/jobs/$JOB | jq '{state, failure, attempts: [.attempts[] | {outcome, payment_sent, allocation_id, orchestrator_url}]}'
```

**Costs** the price of one fixed job. Check: `state` is `succeeded` and the attempt's `payment_sent` is
`true`. Then, once Batteries has accounted the payment (a few seconds):

```sh
curl -sS -X POST -H "Authorization: Bearer $(cat secrets/admin-key)" $WORK/v1/costs/sync | jq
alice $WORK/v1/jobs/$JOB/cost | jq '{status, fees, billable}'                 # observed, with the fee
alice "$WORK/v1/allowance" | jq                                               # spent is the fee
cd ../clearinghouse
tenant one $PUBLIC/v1/clearinghouse/tenants/$ONE/usage | jq '.items | length'     # 1: tenant one's payment
tenant two $PUBLIC/v1/clearinghouse/tenants/$TWO/usage | jq '.items | length'     # 0: tenant two is untouched
tenant two $PUBLIC/v1/clearinghouse/tenants/$TWO/actors/bob/allocation | jq .allowance   # spent 0
admin http://127.0.0.1:8081/v1/clearinghouse/tenants | jq 'map({name, spent})'       # one: the fee; two: 0
admin http://127.0.0.1:8081/v1/clearinghouse/wallet | jq                             # the wallet after the job

# tenant one's admin on tenant two: 403 on each
for path in grant actors usage actors/bob/allocation keys; do
  tenant one -o /dev/null -w "$path %{http_code}\n" $PUBLIC/v1/clearinghouse/tenants/$TWO/$path
done
# and the clearinghouse admin's key does nothing on the public listener: 403
curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $(cat secrets/admin-key)" \
  $PUBLIC/v1/clearinghouse/tenants/$ONE/grant
cd ../enterprise
```

### 5. Proof (b): a paid live job, a few payment cycles

Only if the offerings list a runner priced by the second (`rate_low.unit` is `seconds`). Live work pays
every 3 seconds for as long as it runs, so pick the shortest input the runner takes; `timeout_s` in
`LIVE_JOB` caps it at five cycles.

```sh
alice -D secrets/live-headers -o secrets/live-result -H 'Content-Type: application/json' -d "$LIVE_JOB" $WORK/v1/jobs
JOB=$(grep -i '^livepeer-job-id:' secrets/live-headers | tr -d '\r' | cut -d' ' -f2)
alice $WORK/v1/jobs/$JOB | jq '{state, failure, attempts: [.attempts[] | {outcome, payment_sent, sessions}]}'
```

**Costs** the runner's price for each second the job ran. Check as for (a): the job's state and
`payment_sent`, then cost sync, the job's cost, tenant one's usage (one more payment session) and
tenant two's usage, still 0. A job the timeout ended is `failed` with `timeout` and is still paid for
what ran: its cost says so.

### 6. Proof (c): an engine in its `both` role still pays by itself

The dev stack is that engine: [dev-stack/README.md](dev-stack/README.md). It signs with the same
wallet, so the clearinghouse's signer is stopped first.

```sh
(cd ../clearinghouse && docker compose stop signer)
cd ../dev-stack
docker compose up -d --build                    # as its README says, with its own secrets/ and .env
docker compose exec -T engine lpb payments check            # role both; exit 0
```

Then its README's "Use" section, with one tenant, one actor, an allocation of `ALLOT` and `FIXED_JOB`.
**Costs** the price of one fixed job. Check: the job `succeeded` with `payment_sent`, its cost is
`observed` after `POST /v1/costs/sync`, and `GET /v1/costs/reconciliation` for the actor is `balanced`.

### Stop everything

```sh
(cd dev-stack && docker compose down)            # keeps its volumes, as before the proof
(cd enterprise && docker compose down)
(cd clearinghouse && docker compose down)
```

`down` keeps the named volumes: the clearinghouse's ledger and both engines' records, which are the
evidence. To remove them as well, once the evidence is written down: `docker compose down -v` in
`enterprise/` and `clearinghouse/`, then delete each project's `.env` and `secrets/`. Start the stacks
that were stopped in step 0 again only after the clearinghouse's signer is down.
