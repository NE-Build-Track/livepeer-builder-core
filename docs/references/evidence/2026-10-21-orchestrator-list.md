# Runtime evidence: engine-owned orchestrator discovery, 21 October 2026

This is the live check for [#27](https://github.com/NE-Build-Track/livepeer-builder-core/issues/27)
(D42, [plan](../../exec-plans/active/orchestrator-discovery.md)). The engine read its own orchestrator
list and called `GET /discovery` on every listed orchestrator, with TLS verified against the system
trust store. The signer was not on the discovery path.

| Component | Revision / identity |
| --- | --- |
| Engine | this repository, branch `tasks/m3/orchestrator-list` from `releases/m3` `ad6bba9` (uncommitted working tree) |
| SDK | NE-Build-Track/livepeer-python-gateway `3238731` (TLS verification from livepeer-python-gateway#76) |
| List (file) | `deploy/railway/engine/orchestrators.json`: the 28 addresses of the Railway signer's `orchestrators.txt` |
| List (URL) | `https://signer.pymthouse.com/discover-orchestrators` (23 entries with an `address`) |
| Trust store | the host's system CAs; inside the Railway engine image, `python:3.12.7-slim`'s CA certificates |

## Results

1. **Certificates, before the run.** A handshake against the system trust store found 7 of the 28
   listed orchestrators with publicly trusted (Let's Encrypt) certificates: `ai.`, `ai1.`, `ai-mdw.`
   and `hky.eliteencoder.net`, `lpt.sundara.tv`, `livepeerservice.world` and
   `router.livepeerservice.world`. 19 served self-signed certificates and 2 did not answer.
2. **List from a file** (`lpb serve` with `LPB_ORCHESTRATOR_LIST` set to the file):
   - `/readyz`: `discovery_fresh: true`, `discovery_error: null`.
   - `GET /v1/offerings` listed 89 apps. `GET /v1/runners?app=…` for each returned runners from
     three orchestrators, all with trusted certificates: `ai1.eliteencoder.net` (77 apps),
     `hky.eliteencoder.net` (11) and `lpt.sundara.tv` (1).
   - The log had `discovery.orchestrator_excluded` once for each orchestrator left out: 19 with
     reason `untrusted_certificate`, and 4 `failed`. The four are the two unreachable hosts,
     `ai-mdw.eliteencoder.net` (HTTP 404, `live runners are not supported`) and
     `router.livepeerservice.world` (HTTP 502, `upstream unavailable`). `ai.eliteencoder.net` and
     `livepeerservice.world` answered without runners.
3. **List from an https URL** (the same engine code, with `LPB_ORCHESTRATOR_LIST` set to the URL):
   `discovery_fresh: true`, no `discovery_error`, 89 runners from the same three orchestrators; 19
   excluded for untrusted certificates and 1 for another failure.
4. **Railway engine image.** `docker build -f deploy/railway/engine/Dockerfile` succeeded. In the
   image, `LPB_ORCHESTRATOR_LIST` is `/etc/livepeer-builder/orchestrators.json` (28 entries); a
   default-context handshake to `hky.eliteencoder.net:8936` succeeded (TLSv1.3) and one to
   `ai.stronk.rocks:9994` was rejected (`self-signed certificate`). No certificate bundle was added.

## Not covered by this run

- A paid job. Discovery no longer goes through the signer, but payment is unchanged: the signer pays
  `TicketParams.Recipient` within its price ceilings and the authorization webhook, whatever its own
  `Orchestrators` list says. A paid dev-stack job on a trusted orchestrator would confirm that end to
  end.
- The Railway deployment itself. Before deploying, confirm that no Railway environment sets
  `LPB_DISCOVERY_URL` on the engine: with both settings the engine refuses to start.
