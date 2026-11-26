# Deploy: Compose projects

Optional Compose projects for driving the engine against real Livepeer payment components. None is
part of CI, and this repository does not deploy.

| Project | Folder | The engine's role | What it is |
| --- | --- | --- | --- |
| `livepeer-builder-dev` | [dev-stack/](dev-stack/README.md) | `both` | Self-hosted payments: one deployment with the engine, Batteries, the signer and discovery |
| `lpb-clearinghouse` | [clearinghouse/](clearinghouse/README.md) | `clearinghouse` | A payment clearinghouse: the engine in front of Batteries, with the signer beside it and discovery. It does payments only |
| `lpb-enterprise` | [enterprise/](enterprise/README.md) | `work` | An enterprise that uses that clearinghouse: its own engine, which runs its jobs |

The last two prove the payment clearinghouse on one host (ISS-31, D82, D87). They are an arrangement to
copy from, not a service: see [Limits](#limits).

## Two names that are not the same

- **The payment clearinghouse** is the hosted service an enterprise's engine pays with: an engine in
  its `clearinghouse` role, Batteries, the signer and a wallet. In these projects it is the whole
  `lpb-clearinghouse` project.
- **Batteries** is the upstream program
  [livepeer/clearinghouse-batteries](https://github.com/livepeer/clearinghouse-batteries). Its binary,
  its config keys and its environment variables say `clearinghouse`, and the dev stack names its service
  `clearinghouse` for that reason. In the two new projects it is always the service `batteries`, with
  the config `config/batteries.toml` and the credential file `secrets/batteries.toml`.

## The arrangement

```text
 lpb-enterprise                               lpb-clearinghouse
 ┌───────────────────────┐   edge network     ┌────────────────────────────────────────────────────┐
 │ engine, role work     │ ─────────────────► │ engine, role clearinghouse                         │
 │  jobs, runners, costs │  allot, allowance, │  public listener :8080    (a tenant's own routes)  │
 │  stores each key it   │  usage, runner list│  private listener 127.0.0.1:8081   (its admin)     │
 │  is handed            │                    │        │ inside network                            │
 │  127.0.0.1:8280       │   edge network     │        ▼                                           │
 │                       │ ─────────────────► │  signer-relay :8937 ──► signer ──► batteries       │
 └───────────┬───────────┘  each payment,     │   (the published signer)  (wallet)  (webhook,      │
             │              signed with the   │                                      ledger)       │
             │              handed key        │  discovery ──► the runner list the engine serves   │
             ▼                                └────────────────────────────────────────────────────┘
       orchestrators   (jobs go straight there, with the signed payment)
```

| | `lpb-clearinghouse` | `lpb-enterprise` |
| --- | --- | --- |
| Services | `engine`, `batteries`, `signer`, `signer-relay`, `discovery`, and `private` (a curl for the private listener, started on demand) | `engine` |
| Published on the host | `127.0.0.1:8180`: the engine's public listener. `127.0.0.1:8137`: the signer. Nothing else | `127.0.0.1:8280`: the engine's one listener |
| Never published | Batteries (its management API, its webhook and its Kafka), discovery, the signer's metrics and readiness port, and the engine's private listener | |
| Networks | `lpb-clearinghouse_inside` (the engine, Batteries with the signer, the relay, discovery) and `lpb-clearinghouse-edge` (the engine and the relay) | `lpb-enterprise_default`, and it joins `lpb-clearinghouse-edge` |
| Volumes | `lpb-clearinghouse_engine-data`, `lpb-clearinghouse_paid-state` | `lpb-enterprise_engine-data` |
| Holds | Batteries' credentials, the wallet's key folder (mounted read-only), an RPC address, the orchestrators' addresses. No allocation's key, and no `LPB_SECRET_KEY` | The clearinghouse's address, a tenant admin key, the tenant's id, the published signer's address, and the key of each actor's allocation, stored under its own `LPB_SECRET_KEY` |

- **Who signs.** The enterprise's engine signs each payment itself, at the published signer, with the
  key of the actor's allocation. The clearinghouse hands that key over once, in the answer to the
  allotment or the rotation that minted it, and stores none. The clearinghouse's engine is not in the
  signing path and serves no signer path.
- **The signer's webhook is the only check at signing.** The signer asks Batteries' authorization
  webhook for every payment: which allocation the key pays from, and whether it has money left. It is
  configured in `clearinghouse/config/signer.toml` and must stay: a signer published without it would
  sign for anybody.
- **How the signer is published.** The signer runs in the `batteries` network namespace, because
  Batteries' Kafka and webhook listen on that namespace's loopback. Putting that namespace on the edge
  network would publish Batteries' management API too. So `signer-relay`, a TCP relay, forwards one port
  to the signer, `8937`, and is the only thing of that pair on the edge network.
- **What crosses the edge network.** To the engine: the tenant's own routes (allotment, the allowance,
  usage), which name no tenant, and the runner list. To the signer: the two signing calls. A prompt or a result crosses to
  neither: the enterprise's engine sends each job straight to an orchestrator.
- **The private listener is on the clearinghouse engine's own loopback.** No network reaches it, the
  enterprise project included. The clearinghouse admin reaches it with the `private` service, a curl
  that runs in the engine's network namespace:
  `docker compose run --rm --no-deps private http://127.0.0.1:8081/readyz`.
- **The wallet report reads the signer inside the project.** The clearinghouse engine's
  `LPB_SIGNER_URL` is `http://batteries:8937`, and `signer.ready` in the wallet report says the signer
  answers there. It does not say the published address works: the enterprise's
  `lpb payments check` does, from the enterprise's side.
- **Each side has its own database and its own credential files.**

## Secrets and addresses

Nothing real is committed. Each project's `.env`, the clearinghouse's `secrets/` and its
`config/discovery.toml` are git-ignored, and they are kept out of the engine image's build context.

No container is given `clearinghouse/secrets/` as a folder. Each service is given the files its own
configuration names, each mounted by itself and read-only: Batteries its credential registry and the RPC
address, the signer the RPC address and its webhook and Kafka files. The signer answers the internet
through the relay, and it is given no Batteries credential. The keys the runbook saves in that folder,
the clearinghouse admin's and each tenant admin's, reach no container at all.

| What | Where it comes from | Committed stand-in |
| --- | --- | --- |
| The wallet's key file and its password file | The folder `WALLET_KEY_DIR` names, mounted read-only into the signer; the two file names are `WALLET_KEYSTORE_FILE` and `WALLET_PASSWORD_FILE`. Nothing is copied: the signer reads the password from its own memory, without the newline a password file usually ends in | Placeholders in `clearinghouse/.env.example` |
| The wallet's public address | `WALLET_ADDRESS` in `clearinghouse/.env`, for Batteries' on-chain listener | The zero address |
| The RPC address | `clearinghouse/secrets/rpc-url` (mode 0600, no trailing newline). The signer and Batteries' on-chain listener read it | None |
| The orchestrators' addresses | `clearinghouse/config/discovery.toml` | `config/discovery.example.toml`, which names `orchestrator.example` |
| Batteries' credentials and the engine's two tokens | `clearinghouse/config/init-secrets.py` generates them into `secrets/` and `.env` | Placeholders |
| The clearinghouse admin's key, and each tenant's id and admin key | The runbook saves them in `clearinghouse/secrets/` as they are issued, through `keep`: each is shown once, so a repeat of the command that issued one leaves the saved file as it is | None |
| The tenant admin key, the tenant's id, the enterprise's `LPB_SECRET_KEY` | `enterprise/.env`: the first two are the answer to onboarding | Placeholders |
| The published signer's address | `LPB_SIGNER_URL` in `enterprise/.env`: `http://signer:8937` on one host | That address |
| The key of each actor's allocation | The enterprise engine's own secret store, encrypted under its `LPB_SECRET_KEY`. Written by the engine when it allots or rotates | None |
| The access keys the runbook issues (each admin's, and an actor's) | Files under each project's `secrets/`, written by the runbook's commands | None |

## Limits

- One host, and the same people run both sides. The boundary is real (separate networks, volumes and
  credential files), and it is not a second organisation.
- No TLS and no proxy: the public listener and the signer are published on `127.0.0.1` and reached over
  a Docker network, in the clear. An allocation's key crosses that network when it is handed over and
  with every payment. A clearinghouse that others reach puts TLS in front of both and publishes only
  that (tech debt T43).
- One signer for the wallet, and only one signer may use a wallet at a time: stop any other stack that
  signs with the same key before this one starts.
- Onboarding, grant top-ups and wallet funding are manual steps. Nothing here watches the wallet.
- No availability promise, no backups, and SQLite on a named volume.
- An engine server in its `work` role serves no route that allots, so the enterprise project allots with
  `enterprise/allot.py`, through the engine's embedded API (tech debt T41).

## Proof runbook

The paid proofs for ISS-31's "Done when" (#169), on the path where the enterprise's engine signs at the
published signer itself:

- **(a)** an engine in its `work` role runs a paid fixed job with an engine in its `clearinghouse` role
  that holds two tenants;
- **(b)** the same for a live job;
- **(c)** an engine in its `both` role still runs a paid job by itself.

All three were run from this runbook on 26 November 2026, on `releases/m4` with every change of ISS-31
merged: [the evidence](../docs/references/evidence/2026-11-26-payment-clearinghouse-proofs.md). That run
started a second enterprise project as tenant two and topped up a grant, which steps 2 and 3 show. Its
jobs were short: a job of several payment cycles and a fixed-price job that succeeds are in the record
of [23 November](../docs/references/evidence/2026-11-23-payment-clearinghouse-proofs.md), which ran
proofs (a) and (b) before a tenant's routes took the tenant from the credential (#165) and records the
paths of that day. An earlier run, on 19 November, used a design that was withdrawn the same day, in
which the clearinghouse's engine signed for the enterprise's.
[Its record](../docs/references/evidence/2026-11-19-payment-clearinghouse-proofs.md) says what of it
stands.

**Money.** Only a job moves money: each paid job sends the orchestrator payment tickets from the
wallet's deposit, for the price the runner asked. A grant and an allocation are entries in Batteries'
ledger, which cap what the signer will sign; creating them moves nothing on-chain. So the spend is the
price of one fixed job and a few payment cycles of one live job. Keep `GRANT` and `ALLOT` to the smallest
amounts that cover one job at the rates `GET /v1/offerings` shows: an orchestrator may charge many times
what another does for the same capability. Each step below says what it costs.

**What you supply** (values never go in git, a pull request or a terminal):

| Input | Where it goes |
| --- | --- |
| The folder with the funded wallet's key file and password file, and the two file names | `WALLET_KEY_DIR`, `WALLET_KEYSTORE_FILE`, `WALLET_PASSWORD_FILE` in `clearinghouse/.env` |
| The wallet's public address | `WALLET_ADDRESS` in `clearinghouse/.env` |
| An Arbitrum One RPC address | `clearinghouse/secrets/rpc-url` |
| The orchestrators to list | `clearinghouse/config/discovery.toml` |
| A fixed job to run, and a live one if a live runner is listed: `app`, `path` and `payload` | The variables `FIXED_JOB` and `LIVE_JOB` below |
| `GRANT` and `ALLOT`, in USD | The variables below |

The commands keep every access key in a file under a git-ignored `secrets/` folder and print none. No
allocation's key is printed or kept by a command at all: the enterprise's engine stores its own, and
the one handed to tenant two in step 2 is dropped as it arrives. The commands need `docker compose`,
`curl` and `jq`, and are run from this folder (`deploy/`), in one shell.

### 0. Before anything is started

Only one signer may use the wallet. Stop every other stack that signs with it, and leave it stopped
until [Stop everything](#stop-everything).

Run this block first, and again in any new shell: every later step uses what it defines.

```sh
umask 077
D=$PWD                                                       # this folder: deploy/
GRANT=0.50   # each tenant's grant, USD: a ledger entry, not a payment
ALLOT=0.25   # each actor's allocation, USD: the most its jobs can cost
FIXED_JOB='{"app":"<app from offerings>","path":"<path>","payload":{}}'
LIVE_JOB='{"app":"<live app from offerings>","path":"<path>","payload":{},"timeout_s":15}'

ch()  { docker compose -f "$D/clearinghouse/compose.yaml" "$@"; }   # the clearinghouse project
ent() { docker compose -f "$D/enterprise/compose.yaml" "$@"; }      # the enterprise project
CS=$D/clearinghouse/secrets ES=$D/enterprise/secrets
PUBLIC=http://127.0.0.1:8180 SIGNER=http://127.0.0.1:8137 WORK=http://127.0.0.1:8280
OWN=/v1/clearinghouse                 # a tenant's own routes, on the public listener: they name no tenant
TENANTS=/v1/clearinghouse/tenants     # the clearinghouse admin's, on the private listener: they name one

# curl on the private listener. --no-deps: it never starts or recreates the engine it is run beside
private() { ch --progress quiet run --rm --no-deps -T private "$@"; }
keep() {  # keep FILE: save the token on stdin as FILE. With no token, a key FILE already holds stays
  local token; token=$(cat)
  case "$token" in
    ''|null) [ -s "$1" ] && echo "no token came back: $1 is left as it is" >&2 && return 0
             echo "no token came back, and $1 holds none" >&2; return 1 ;;
  esac
  (umask 077; printf '%s\n' "$token" > "$1")
}
admin()   { private -H "Authorization: Bearer $(cat $CS/admin-key)" "$@"; }       # the clearinghouse admin
tenant()  { curl -sS -H "Authorization: Bearer $(cat $CS/tenant-$1-key)" "${@:2}"; }   # tenant one|two ...
actor()   { curl -sS -H "Authorization: Bearer $(cat $ES/$1-key)" "${@:2}"; }     # actor alice|carol ...
stored()  {  # stored ch|ent: how many secrets that engine's store holds, and who keeps each actor's key
  $1 exec -T engine python -c "
import sqlite3
db = sqlite3.connect('file:/data/livepeer-builder.db?mode=ro', uri=True)
print('secrets stored:', db.execute('select count(*) from lpb_secrets').fetchone()[0])
print(db.execute('select actor_id, credential_custody from lpb_actors order by 1').fetchall())"
}
```

**Start from new volumes.** A database from an earlier run of these projects cannot be used: its schema
is the withdrawn design's, and the engine's revision check does not tell the two apart. Batteries'
ledger from that run would also list its old grants as tenants. Remove them, which costs nothing and
touches nothing on-chain:

```sh
ent down -v 2>/dev/null; ch down -v 2>/dev/null     # stops both projects and removes their volumes
docker volume rm lpb-enterprise_engine-data lpb-clearinghouse_engine-data lpb-clearinghouse_paid-state 2>/dev/null
docker volume ls --format '{{.Name}}' | grep -E '^lpb-(clearinghouse|enterprise)_' || echo "no volumes left"
```

Check: the last line prints `no volumes left`. The second line is for a project whose `.env` is gone,
which `down` cannot read; it removes the same three volumes by name, and says nothing when they are
already gone. If a volume is still listed, a container of an earlier run still holds it:
`docker ps -a --filter name=lpb-` shows it, and `docker rm -f` removes it.

Then the files `clearinghouse/` needs. If `clearinghouse/.env`, `clearinghouse/secrets/` and
`clearinghouse/config/discovery.toml` are there from an earlier run, they still fit: keep them and skip
this block. (`LPB_SECRET_KEY` in that `.env` is no longer read.)

```sh
(cd clearinghouse && python3 config/init-secrets.py)        # secrets/ and .env
# then, by hand: clearinghouse/secrets/rpc-url (mode 0600, no trailing newline),
# clearinghouse/config/discovery.toml (from config/discovery.example.toml), and the four WALLET_
# variables in clearinghouse/.env
```

### 1. The clearinghouse

```sh
ch up -d --build
ch ps                                                       # see the check below
ch exec -T engine lpb payments check                        # role clearinghouse; exit 0
curl -s $PUBLIC/readyz | jq '{role, ready}'                 # "clearinghouse", true
curl -s -o /dev/null -w '%{http_code}\n' -X POST $PUBLIC/v1/jobs                  # 404: no job route
curl -s -o /dev/null -w '%{http_code}\n' -X POST $PUBLIC/sign-orchestrator-info   # 404: the engine signs for nobody
curl -s -o /dev/null -w '%{http_code}\n' $SIGNER/sign-orchestrator-info           # 405: the published signer

ch exec -T engine lpb bootstrap-admin | jq -r '.token // empty' | keep $CS/admin-key
admin http://127.0.0.1:8081/v1/clearinghouse/wallet | jq   # the deposit and reserve, and signer.ready
```

Costs nothing. Check before going on:

- `ch ps`: `batteries` is healthy, and `signer`, `signer-relay`, `discovery` and `engine` are up. In the
  `PORTS` column only two entries begin with `127.0.0.1:`: `8180->8080` on `engine` and `8137->8937` on
  `signer-relay`.
- `lpb payments check` exits 0 with three probes, each `ok`: `signer`, `batteries`, `batteries_grants`.
- The wallet report's `signer.ready` is `true`, and its `escrow` shows the wallet's deposit and reserve.
  Batteries' on-chain listener reports them once it has read to the head: an empty `escrow` a minute
  after start means the listener is not on, or `WALLET_ADDRESS` is not the signer's.
- The signer's own readiness, which checks chain funds and price policy, on a port that is published
  nowhere: `ch exec batteries curl -s http://127.0.0.1:8938/readyz`.

### 2. Two tenants, on the private listener

```sh
onboard() {  # onboard NAME: the tenant's id and its first tenant admin key, kept in secrets/
  local answer
  answer=$(admin -H 'Content-Type: application/json' http://127.0.0.1:8081$TENANTS \
    -d "{\"name\":\"$1\",\"amount\":{\"amount\":\"$GRANT\",\"currency\":\"USD\"},\"operation_ref\":\"proof-onboard-$1\"}")
  jq -r '.tenant_id // empty' <<<"$answer" | keep $CS/tenant-$1-id &&
    jq -r '.admin_key.token // empty' <<<"$answer" | keep $CS/tenant-$1-key  # the answer is in no file
}
onboard one && onboard two
ONE=$(cat $CS/tenant-one-id) TWO=$(cat $CS/tenant-two-id)
admin http://127.0.0.1:8081$TENANTS | jq 'map({tenant_id, name, total, unallocated, spent})'
curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $(cat $CS/admin-key)" \
  $PUBLIC$TENANTS                                            # 404: onboarding is not on the public listener
```

Costs nothing. Check: two tenants, each with `total` and `unallocated` of `GRANT` and `spent` of 0. In a
new shell, set `ONE` and `TWO` again with the line above.

A grant is topped up by hand, by the clearinghouse admin, on the private listener. The answer is a 204
with no body, and a repeat under the same reference adds nothing:

```sh
admin -o /dev/null -w '%{http_code}\n' -H 'Content-Type: application/json' \
  -d '{"amount":{"amount":"0.10","currency":"USD"},"operation_ref":"proof-topup-two"}' \
  http://127.0.0.1:8081$TENANTS/$TWO/grant/top-up            # 204
admin http://127.0.0.1:8081$TENANTS | jq 'map({name, total})'   # two: GRANT + 0.10; one: GRANT
tenant two -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' \
  -d '{"amount":{"amount":"0.10","currency":"USD"},"operation_ref":"proof-topup-self"}' \
  $PUBLIC$OWN/grant/top-up                                   # 404: a tenant tops up no grant
```

Costs nothing: a top-up is an entry in Batteries' ledger.

Tenant two allots its actor itself, on the public listener, with its own admin key. Its routes there
name no tenant: the clearinghouse takes the tenant from the key. The answer hands the allocation's key
to the caller, once. This proof does not use that key, so the command reads the answer and drops the
key:

```sh
allot_bob() {
  tenant two -H 'Content-Type: application/json' \
    -d "{\"amount\":{\"amount\":\"$ALLOT\",\"currency\":\"USD\"},\"operation_ref\":\"proof-allot-bob\"}" \
    $PUBLIC$OWN/actors/bob/allocation \
    | jq '{allocation_id, status, credential_custody, handed_a_key: (.allocation_api_key != null), replayed}'
}
allot_bob            # "active", "work_engine", handed_a_key true, replayed false
allot_bob            # the repeat: the same allocation, handed_a_key false, replayed true
stored ch            # secrets stored: 0, and [('bob', 'work_engine')]
```

Costs nothing. Check: the first answer hands a key over and the repeat does not; the clearinghouse
stores no secret, and its record says the work engine keeps bob's key.

### 3. The enterprise, as tenant one

```sh
cp enterprise/.env.example enterprise/.env
sed -i "s|^LPB_CLEARINGHOUSE_TOKEN=.*|LPB_CLEARINGHOUSE_TOKEN=$(cat $CS/tenant-one-key)|" enterprise/.env
sed -i "s|^LPB_CLEARINGHOUSE_TENANT_ID=.*|LPB_CLEARINGHOUSE_TENANT_ID=$ONE|" enterprise/.env
sed -i "s|^LPB_SECRET_KEY=.*|LPB_SECRET_KEY=$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')|" enterprise/.env
ent up -d --build
ent exec -T engine lpb payments check                       # role work; exit 0
curl -s $WORK/readyz | jq '{role, ready}'                   # "work", true
curl -s $WORK/v1/offerings | jq 'map({app, rate_low, rate_high, stale})'   # the clearinghouse's runner list
curl -s -o /dev/null -w '%{http_code}\n' $WORK$OWN/grant                    # 404: no clearinghouse route

mkdir -p $ES
ent exec -T engine lpb bootstrap-admin | jq -r '.token // empty' | keep $ES/admin-key
issue() {  # issue ACTOR: an access key for an actor of tenant one, at the enterprise's engine
  curl -sS -H "Authorization: Bearer $(cat $ES/admin-key)" -H 'Content-Type: application/json' \
    -d "{\"kind\":\"actor\",\"actor_id\":\"$1\",\"tenant_id\":\"$ONE\",\"operation_ref\":\"proof-key-$1\"}" \
    $WORK/v1/keys | jq -r '.token // empty' | keep $ES/$1-key
}
issue alice
ent exec -T engine python /opt/enterprise/allot.py alice $ALLOT proof-allot-alice
stored ent           # secrets stored: 1, and [('alice', 'secret_store')]
stored ch            # secrets stored: 0, and [('alice', 'work_engine'), ('bob', 'work_engine')]
tenant one $PUBLIC$OWN/actors/alice/allocation | jq '{status, credential_custody, allowance}'
```

Costs nothing. Check:

- `lpb payments check` exits 0 with four probes, each `ok`: `clearinghouse_grant`,
  `clearinghouse_usage`, `signer` and `clearinghouse_runner_list`. Its `tenant` is tenant one's grant.
  The `signer` probe is the published signer, asked from the enterprise's side.
- The offerings are not empty and not `stale`. Read the rates now, and stop here if `ALLOT` does not
  cover one job.
- The allotment prints alice's allocation, `active`, with `key_kept_by` `secret_store`.
- **The key is stored at the enterprise and nowhere at the clearinghouse:** the enterprise's store holds
  one secret and its record says `secret_store`; the clearinghouse's store holds none and its record
  says `work_engine`. The read of the allocation carries no key.

**A second enterprise, as tenant two.** The same project runs once more under another name, with
tenant two's key and id, a secret key of its own and another port. Compose reads a variable set in the
shell before the one in `.env`, so nothing of tenant one's project changes:

```sh
ES2=$D/enterprise/secrets/two WORK2=http://127.0.0.1:8281   # git-ignored, as secrets/ is
mkdir -p $ES2 && python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())' > $ES2/secret-key
ent2() {  # the enterprise project again, as tenant two, with its own volume
  LPB_CLEARINGHOUSE_TOKEN=$(cat $CS/tenant-two-key) LPB_CLEARINGHOUSE_TENANT_ID=$TWO \
  LPB_SECRET_KEY=$(cat $ES2/secret-key) ENTERPRISE_PORT=8281 \
    docker compose -p lpb-enterprise-two -f "$D/enterprise/compose.yaml" "$@"
}
ent2 up -d
ent2 exec -T engine lpb payments check                      # role work; exit 0; its tenant is tenant two's grant
ent2 exec -T engine lpb bootstrap-admin | jq -r '.token // empty' | keep $ES2/admin-key
curl -sS -H "Authorization: Bearer $(cat $ES2/admin-key)" -H 'Content-Type: application/json' \
  -d "{\"kind\":\"actor\",\"actor_id\":\"dave\",\"tenant_id\":\"$TWO\",\"operation_ref\":\"proof-key-dave\"}" \
  $WORK2/v1/keys | jq -r '.token // empty' | keep $ES2/dave-key
ent2 exec -T engine python /opt/enterprise/allot.py dave $ALLOT proof-allot-dave
stored ent2          # secrets stored: 1, and [('dave', 'secret_store')]
```

Costs nothing. Check: each enterprise's store holds its own actor's key and no other, and the
clearinghouse's holds none. A job of dave's is run as alice's is in step 4, with `$ES2/dave-key` and
`$WORK2`: it **costs** the price of that job, it is in tenant two's usage and not in tenant one's, and
alice's key is a 401 on this engine.

**A runner is paid before it reads the request.** A job the runner refuses (a path it does not serve,
an input it does not take) is still charged, and the engine records it as failed after payment. Run a
job whose `path` and `payload` are known to work on that runner, or accept the price of a refusal. The
run of 19 November 2026 met both kinds of refusal:
[the evidence](../docs/references/evidence/2026-11-19-payment-clearinghouse-proofs.md).

### 4. Proof (a): a paid fixed job, signed at the published signer

```sh
actor alice -D $ES/fixed-headers -o $ES/fixed-result -H 'Content-Type: application/json' -d "$FIXED_JOB" $WORK/v1/jobs
JOB=$(grep -i '^livepeer-job-id:' $ES/fixed-headers | tr -d '\r' | cut -d' ' -f2)
actor alice $WORK/v1/jobs/$JOB | jq '{state, failure, attempts: [.attempts[] | {outcome, payment_sent, allocation_id, orchestrator_url}]}'
```

**Costs** the price of one fixed job. Check: `state` is `succeeded` and the attempt's `payment_sent` is
`true`. Then, once Batteries has accounted the payment (a few seconds):

```sh
curl -sS -X POST -H "Authorization: Bearer $(cat $ES/admin-key)" $WORK/v1/costs/sync | jq
actor alice $WORK/v1/jobs/$JOB/cost | jq '{status, fees, billable}'           # observed, with the fee
actor alice $WORK/v1/allowance | jq                                           # spent is the fee
tenant one $PUBLIC$OWN/usage | jq '.items | length'                           # 1: tenant one's payment
tenant two $PUBLIC$OWN/usage | jq '.items | length'                           # 0: tenant two is untouched
tenant two $PUBLIC$OWN/actors/bob/allocation | jq .allowance                  # spent 0
admin http://127.0.0.1:8081$TENANTS | jq 'map({name, spent})'                 # one: the fee; two: 0
admin http://127.0.0.1:8081$TENANTS/$TWO/actors/bob/allocation | jq .allowance   # the admin names the tenant
admin http://127.0.0.1:8081/v1/clearinghouse/wallet | jq                     # the wallet after the job

# tenant one's key reads tenant one, and has no bob: its routes name no tenant, so they reach no other
tenant one $PUBLIC$OWN/grant | jq -r .tenant_id                               # tenant one's id
tenant one -o /dev/null -w '%{http_code}\n' $PUBLIC$OWN/actors/bob/allocation   # 404
# no path that names a tenant is on the public listener: 404 on each, tenant two's or tenant one's own
for path in grant actors usage actors/bob/allocation keys; do
  tenant one -o /dev/null -w "$path %{http_code}\n" $PUBLIC$TENANTS/$TWO/$path
done
# on the private listener those paths are the clearinghouse admin's: tenant one's key is 403 there
private -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $(cat $CS/tenant-one-key)" \
  http://127.0.0.1:8081$TENANTS/$TWO/grant
# and the clearinghouse admin's key does nothing on the public listener: 403
curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $(cat $CS/admin-key)" \
  $PUBLIC$OWN/grant
```

Costs nothing more. **What shows the payment was signed at the published signer by the enterprise's
engine:** the job's `payment_sent` is `true`; the payment is in tenant one's usage at the clearinghouse,
which Batteries accounts from that signer's own events; and the clearinghouse's engine serves no signer
path (the 404 of step 1), so nothing else could have signed it.

### 5. Proof (b): a paid live job, a few payment cycles

Only if the offerings list a runner priced by the second (`rate_low.unit` is `seconds`). Live work pays
every 3 seconds for as long as it runs, so pick the shortest input the runner takes; `timeout_s` in
`LIVE_JOB` caps it at five cycles.

```sh
actor alice -D $ES/live-headers -o $ES/live-result -H 'Content-Type: application/json' -d "$LIVE_JOB" $WORK/v1/jobs
JOB=$(grep -i '^livepeer-job-id:' $ES/live-headers | tr -d '\r' | cut -d' ' -f2)
actor alice $WORK/v1/jobs/$JOB | jq '{state, failure, attempts: [.attempts[] | {outcome, payment_sent, sessions}]}'
```

**Costs** the runner's price for each second the job ran. Check as for (a): the job's state and
`payment_sent`, then cost sync, the job's cost, tenant one's usage (one more payment session, with a row
for each cycle) and tenant two's usage, still 0. A job the timeout ended is `failed` with `timeout` and
is still paid for what ran: its cost says so.

### 6. An empty allocation is refused by the signer

The signer's Batteries authorization webhook is the only check at signing. An actor whose allocation
holds nothing shows it: the engine has a key for the actor, the signer asks Batteries, and Batteries
refuses.

```sh
issue carol
ent exec -T engine python /opt/enterprise/allot.py carol 0 proof-allot-carol   # status "exhausted"
actor carol -D $ES/empty-headers -o $ES/empty-result -w '%{http_code}\n' \
  -H 'Content-Type: application/json' -d "$FIXED_JOB" $WORK/v1/jobs              # 402
JOB=$(grep -i '^livepeer-job-id:' $ES/empty-headers | tr -d '\r' | cut -d' ' -f2)
actor carol $WORK/v1/jobs/$JOB | jq '{state, failure: (.failure | {kind, reason, status_code, payment_sent})}'
tenant one $PUBLIC$OWN/usage | jq '.items | length'                             # as after step 5
```

**Costs nothing:** the signer signs nothing, so no ticket is sent. Check: the answer is `402`; the job is
`failed` with `kind` `payment`, `reason` `allowance_exhausted`, `status_code` 402 and `payment_sent`
`false`; tenant one's usage has no new row.

### 7. Proof (c): an engine in its `both` role still pays by itself

This proof never used the withdrawn path. It was run on 19 November 2026 and again on 26 November, with
the other two.

The dev stack is that engine: [dev-stack/README.md](dev-stack/README.md). It signs with the same
wallet, so the clearinghouse's signer is stopped first.

```sh
ch stop signer-relay signer
cd dev-stack
docker compose up -d --build                    # as its README says, with its own secrets/ and .env
docker compose exec -T engine lpb payments check            # role both; exit 0
```

The dev stack's README copies the wallet's two files into its `secrets/` folder. To copy nothing, keep
an override file outside the repository and name it after `compose.yaml` with a second `-f`. It mounts
the key folder read-only into the signer and gives the signer the same start as the clearinghouse
project's, with the two file names written out:

```yaml
services:
  signer:
    entrypoint: ["/bin/sh", "-c"]
    command:
      - |
        umask 077
        printf %s "$$(cat /run/wallet/<password file>)" > /dev/shm/wallet-password
        exec /usr/local/bin/livepeer-signer --data-dir /data/node --config /etc/signer.toml \
          --keystore-file /run/wallet/<key file> --keystore-password-file /dev/shm/wallet-password
    volumes:
      - {type: bind, source: <the wallet's key folder>, target: /run/wallet, read_only: true}
```

A project name of its own (`docker compose -p <name> ...`) gives the proof new volumes and leaves an
existing dev stack's records alone. A dev stack database from before 23 November 2026 cannot be used
either, for the reason in step 0.

Then its README's "Use" section, with one tenant, one actor, an allocation of `ALLOT` and `FIXED_JOB`.
**Costs** the price of one fixed job. Check: the job `succeeded` with `payment_sent`, its cost is
`observed` after `POST /v1/costs/sync`, and `GET /v1/costs/reconciliation` for the actor is `balanced`.

### What to write down

For the evidence document, with no address, credential or tenant id: the commit the engine image was
built from; each check of steps 1 to 6 and what it showed; for each job its kind, how long it ran, its
state, whether a payment was sent and its cost after sync; the two `stored` outputs of step 3; tenant
one's and tenant two's usage counts; the 402 of step 6; and the total charged to allocations.

### Stop everything

```sh
(cd "$D/dev-stack" && docker compose down)       # only if step 7 was run; keeps its volumes
ent2 down                                        # only if the second enterprise was started
ent down
ch down
```

`down` keeps the named volumes: the clearinghouse's ledger and both engines' records, which are the
evidence. To remove them as well, once the evidence is written down: `ent down -v` and `ch down -v`,
then delete each project's `.env` and `secrets/`. Start the stacks that were stopped in step 0 again
only after the clearinghouse's signer is down.
