# Runtime evidence: paid dev-stack run, 12 October 2026

> The dev stack was restructured later on 12 October. It now has no local orchestrator or runner, `paid/` became `config/`, and there is a single `compose.yaml`. Paths below are as they were at the time of the run.

**Stack:** `deploy/dev-stack` with `compose.paid.yaml`, on Arbitrum One, through the public RPC
`arb1.arbitrum.io`. The engine was driven over REST.

| Component | Revision / identity |
| --- | --- |
| Engine | this repository, `releases/m2` working tree (uncommitted scaffold) |
| Signer and orchestrator | livepeer/node `1432123` (eliteprox fork) |
| Batteries | `ca09e47`, embedded Kafka |
| SDK | fork `3250c08` (SDK #71 head) |
| Payer (signer) | `0x5ae4e42db3671370a0c25aff451e7482aaec3d0b`: deposit 0.335 ETH, reserve 0.3366 ETH |
| Orchestrator | `0xd00354656922168815fcd1e51cbddb9e359e3c7f` (xode): active, with the redemption guard `RedeemerMaxFeePerGas = "1"` |
| Runner | mock Live Runner `dev/echo`, single-shot, **$0.001 fixed** per request, registered through SDK `register_runner` |

## Results

1. **No implicit money.** Before provisioning, `alice`'s job was refused with **402 `not_provisioned`**. There was no fallback to a shared key.
2. **Explicit provisioning.** One atomic Batteries call created allocation `AaEx8zzgcXmF_CeYUZiSDA` with $1 USD. The key is stored encrypted (`SecretRef` `db:…`). The allowance read showed granted $1, spent $0.
3. **Paid jobs.** Three `dev/echo` calls went engine → SDK → signer → Batteries webhook → orchestrator → runner. Each returned 200 with the echoed body.
4. **Charges.** Batteries applied one usage row per call, each on alice's allocation, with a `payment_session_id`, and `computed_fee_usd` ≈ **$0.001** (the runner price).
5. **Attribution gap found (SDK #71 with livepeer/node).** The first job recorded **no `auth_ids`**, so its cost stayed `pending`:
   - SDK `auth_id_from_signer_state` reads `state["State"]`, which is go-livepeer's shape.
   - livepeer/node returns `{"state": …, "sig": …}`, with a lowercase key.
   - The inner payload still carries `AuthID`.
   - Reproduced in isolation: `{"State": …}` gives the ID; `{"state": …}` gives `''`.
   - Tracked as tech debt **T9**; the fix belongs in SDK #71.
6. **With a one-line, container-only shim** (also read `"state"`), jobs 2 and 3 recorded their `auth_id`s. After cost sync, both are **`observed`**, classified **`delivered`**, at $0.001 each.
7. **Reconciliation balanced exactly** for alice:
   - Batteries `spent` = **$0.003000000000005082**;
   - delivered = **$0.002000000000003388** (jobs 2 and 3);
   - unattributed = **$0.001000000000001694** (job 1);
   - `balanced: true`.
8. **Redemption guard.** The `xode` pending nonce was **3136 before and after** and its balance was unchanged, so no transaction was sent. The local redemption queue was empty; no ticket won in this short run.

## Public orchestrators

The local orchestrator was then removed. The signer was pointed at four public orchestrators:
`orch-us-central.xodeapp.xyz:18935`, `us-central-metrics.speedybird.xyz:28935`,
`orch-eu-central.open-pool.com:443` and `hky.eliteencoder.net:8936`.

1. **Discovery.** Three of the four serve self-signed TLS certificates, and the node signer (which
   has no skip-verify option) dropped them. After pinning their certificates
   (`paid/pin-orchestrator-certs.py`, mounted through `SSL_CERT_DIR`), all four were discovered:
   7 runners. These were chat `qwen3.8-27b` on three orchestrators, `embeddings/ollama`,
   `whisper-large-v3` (plus translate), and `speech/kokoro`, all metered per second.
2. **Paid job refused by the signer.** `blueclaw/embeddings/ollama` → `POST /generate-live-payment`
   → **400 `incomplete or expired orchestrator payment params`**. The engine classified it `payment`
   with `payment_sent: false` and returned 503 with `Retry-After`. Nothing was charged.
3. **Root cause (tech debt T10).** Decoding each orchestrator's 402 challenge showed `address != recipient`
   on all four:

   | Orchestrator | Address | Recipient |
   | --- | --- | --- |
   | xode us-central | `0x52cf…` | `0xd003…` |
   | speedybird | `0xa286…` | `0xdef1…` |
   | open-pool eu-central | `0x52cf…` | `0x5263…` |
   | hky.eliteencoder | `0x0074…` | `0x104a…` |

   The node signer requires them to be equal. Every other field was valid (auth token, session,
   round 4368, block hash, price, recipient random hash).
