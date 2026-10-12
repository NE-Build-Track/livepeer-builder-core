# M2: Re-run the attribution evidence on the new baseline

## Status

Owner: John. Follow-up to ISS-01 (decision 6 on #7). **First run done on 12 October; blocked on T9 for a clean pass.**

## Goal

Repeat the 7 October Arbitrum attribution run with the livepeer/node signer, Batteries `ca09e47`
and the SDK with #71, driving it through this engine instead of prototype scripts.

## Acceptance

- [x] Stack from [deploy/dev-stack](../../../deploy/dev-stack/README.md) with pins recorded
- [x] Per-actor spend equals Batteries `spent_*` (reconciliation `balanced`, 12 October, with the T9 shim)
- [ ] The same, without the shim, after SDK #71 reads node's `"state"` key (T9)
- [ ] A forged `manifest_id` is `unattributed`, not attributed
- [ ] A rejected-ticket charge is classified `failed_after_payment`
- [ ] A public orchestrator the node signer accepts (T10)
- [x] Results recorded: [2026-10-12-dev-stack-paid-run](../../references/evidence/2026-10-12-dev-stack-paid-run.md)

## Progress

- [x] 2026-10-12: Paid run with livepeer/node, Batteries `ca09e47`, `xode` (guarded) and the mock Live Runner. Found T9.

## Decision log

- 2026-10-12: Re-run required because the 7 October run used go-livepeer v0.9.3 and Batteries `9312212`.
