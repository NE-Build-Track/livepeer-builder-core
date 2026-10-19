# M2: Re-run the attribution evidence on the new baseline

## Status

Owner: John. Follow-up to ISS-01 (decision 6 on #7). **Closed with M2 on 19 October.** Clean funded passes on public orchestrators on 14 October (`auth_id`) and 19 October (session log). The forged-manifest and rejected-ticket live cases carry forward to M3 (ISS-16); both are covered by unit tests.

## Goal

Repeat the 7 October Arbitrum attribution run with the livepeer/node signer, Batteries `ca09e47`
and the SDK with #71, driving it through this engine instead of prototype scripts.

## Acceptance

- [x] Stack from [deploy/dev-stack](../../../deploy/dev-stack/README.md) with pins recorded
- [x] Per-actor spend equals Batteries `spent_*` (reconciliation `balanced`, 12 October, with the T9 shim)
- [x] The same, without the shim (14 October: node `f970be6`, SDK PR 5; reconciliation `balanced`)
- [ ] A forged `manifest_id` on another allocation stays `unattributed` (covered in `test_a_forged_manifest_on_another_allocation_stays_unattributed`; the live re-run carries forward to M3, ISS-16)
- [ ] A rejected-ticket charge is classified `failed_after_payment` (unit-tested; live case carried forward to M3, ISS-16)
- [x] A public orchestrator the node signer accepts (14 October: speedybird and xode)
- [x] Results recorded: [2026-10-12-dev-stack-paid-run](../../references/evidence/2026-10-12-dev-stack-paid-run.md), [2026-10-14-dev-stack-public-orchestrators](../../references/evidence/2026-10-14-dev-stack-public-orchestrators.md), [2026-10-19-dev-stack-session-log](../../references/evidence/2026-10-19-dev-stack-session-log.md)

## Progress

- [x] 2026-10-12: Paid run with livepeer/node, Batteries `ca09e47`, `xode` (guarded) and the mock Live Runner. Found T9.
- [x] 2026-10-14: Paid run on node `f970be6` against public orchestrators: three jobs `delivered`, reconciliation `balanced`, no shims.
- [x] 2026-10-19: Paid run with session-log attribution (#9) on node v1.0.2 and the NE-Build-Track pins: three jobs `delivered`, every session-log entry `matched`, reconciliation `balanced`.

## Decision log

- 2026-10-12: Re-run required because the 7 October run used go-livepeer v0.9.3 and Batteries `9312212`.
