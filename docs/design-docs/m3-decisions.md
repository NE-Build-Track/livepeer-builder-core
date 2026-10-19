# M3 decisions

Accepted decisions after the M2 close-out ([m2-decisions](m2-decisions.md) holds D1–D28).

| # | Decision | Where |
| --- | --- | --- |
| D29 | **Streaming is generic and per job** (Mike, 19 October, from the first enterprise reference app's requirement). `jobs.stream` delivers one runner response as bytes plus a content type; the engine never interprets the stream. `mode` stays `immediate`; async (M4) is untouched | [engine.md#streaming](../contracts/engine.md#streaming), [plan](../exec-plans/active/streaming-jobs.md) |
| D30 | **A stream the caller stops reading is `abandoned`, and the job `uncertain`** when payment went out (Mike, 19 October). The work was paid for and probably ran; the engine never pretends otherwise. `abandoned` is both an attempt outcome and a failure kind | [engine.md#streaming](../contracts/engine.md#streaming) |
| D31 | **Payment evidence is written before the first byte** of a stream, on the `in_flight` attempt, so a restart mid-stream resolves the job as `uncertain` (Mike, 19 October) | `services/jobs.py` |
| D32 | **`Runner` carries `version`, `metadata` and `gpu` as the node echoes them** (Mike, 19 October). The engine never interprets `metadata`; the chat runner publishes `{"model_id": …, "max_context": 131072}` there and ChatLPT reads it instead of paying for a models call | [engine.md#discovery](../contracts/engine.md#discovery) |
| D33 | **Streaming is optional for transports**: `StreamingRunnerTransport` adds `open`; a transport without it makes `jobs.stream` answer 400 rather than changing the `RunnerTransport` protocol every integrator implemented (Mike, 19 October) | `protocols/__init__.py` |
