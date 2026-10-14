# M2: Re-run the attribution evidence on the new baseline

## Status

Owner: John. Follow-up to ISS-01 (decision 6 on #7). **Clean funded pass on public orchestrators done on 14 October (T9 and T10 resolved). The forged-manifest and rejected-ticket cases remain.**

## Goal

Repeat the 7 October Arbitrum attribution run with the livepeer/node signer, Batteries `ca09e47`
and the SDK with #71, driving it through this engine instead of prototype scripts.

## Acceptance

- [x] Stack from [deploy/dev-stack](../../../deploy/dev-stack/README.md) with pins recorded
- [x] Per-actor spend equals Batteries `spent_*` (reconciliation `balanced`, 12 October, with the T9 shim)
- [x] The same, without the shim (14 October: node `f970be6`, SDK PR 5; reconciliation `balanced`)
- [ ] A forged `manifest_id` is `unattributed`, not attributed
- [ ] A rejected-ticket charge is classified `failed_after_payment`
- [x] A public orchestrator the node signer accepts (14 October: speedybird and xode)
- [x] Results recorded: [2026-10-12-dev-stack-paid-run](../../references/evidence/2026-10-12-dev-stack-paid-run.md), [2026-10-14-dev-stack-public-orchestrators](../../references/evidence/2026-10-14-dev-stack-public-orchestrators.md)

## Progress

- [x] 2026-10-12: Paid run with livepeer/node, Batteries `ca09e47`, `xode` (guarded) and the mock Live Runner. Found T9.
- [x] 2026-10-14: Paid run on node `f970be6` against public orchestrators: three jobs `delivered`, reconciliation `balanced`, no shims.

## Decision log

- 2026-10-12: Re-run required because the 7 October run used go-livepeer v0.9.3 and Batteries `9312212`.
