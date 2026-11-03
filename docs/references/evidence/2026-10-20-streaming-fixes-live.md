# Runtime evidence: the stream lifecycle fixes on the network, 20 October 2026

The live rerun for the review fixes D34–D39 ([plan](../../exec-plans/active/streaming-jobs.md#review-fixes-20-october-d34d39)).
It covers what the [19 October run](2026-10-19-streaming-live.md) could not: a stream funded in several
rounds, a stream longer than a cost-sync cycle, a client disconnect through a real server, and
`complete()`.

| Component | Identity |
| --- | --- |
| Engine | branch `tasks/m3/mz/stream-support` with the D34–D39 changes (uncommitted, awaiting review), image `engine:stream-fixes`, served by uvicorn on loopback |
| Payment stack | ChatLPT's: Batteries `ca09e47`, node signer `406b248` (unchanged images) |
| Mode | `per_application` on ChatLPT's shared allocation `AaFWEnucfhuOCn95t02zMw`; the engine has its own SQLite database and `LPB_ENGINE_ID` |
| Runners for `blueclaw/chat/qwen3.8-27b` | xode us-central, speedybird, hky.eliteencoder; 4,666,430,889 wei per second |
| Requests | `POST /v1/chat/completions` with `stream: true`, thinking off, through `POST /v1/jobs?stream=true` |

## 1. A long stream, funded in several rounds (D35, D39)

"Write the numbers from one to one thousand two hundred in English words, one per line." Two runs,
both on xode.

| Measure | Run A | Run B |
| --- | --- | --- |
| Headers and first chunk (D37: headers wait for the first chunk) | 0.82 s | 0.79 s |
| Stream | 2,537 chunks, 581,460 bytes, 54.7 s | 2,371 chunks, 543,583 bytes, 51.3 s |
| At 25 s, after a forced cost sync, stream still open | job `running`, attempt `in_flight`, session `81f8cc2d` **`matched`** | session `2082ab37` **`matched`** |
| Right after the end, **before any cost sync** | — | attempt `succeeded`, session `2082ab37` still **`matched`** |
| Job | `succeeded`, `ResultRef` 581,460 bytes | `succeeded` |
| Cost | **16 usage rows** under one session, class `delivered`, $0.000782 | 15 rows, `delivered` |

- **Several funding rounds attribute correctly.** The SDK re-signed under one payment session every
  few seconds, and cost sync put every row on the one job. An earlier, shorter run (19.7 s, 890
  chunks) gave 6 rows, $0.000335.
- **D39 holds.** The end-of-stream write ran after cost sync had matched the session, and the match
  was still there before the next sync. Before D39, that write deleted and reinserted the row as
  `unmatched`.

## 2. A client disconnect through uvicorn (D34, D38)

A long story request; the client read five chunks and closed the connection. The runner kept going,
so the 1 s settle window ran out.

| Measure | Value |
| --- | --- |
| `running_jobs` 3 s after the disconnect | **0** (the slot was released) |
| Job | `uncertain`; attempt `abandoned`; failure `abandoned`, "the caller stopped reading the stream" |
| Cost | **1 usage row**, $0.000112, class `uncertain`; session `a35616eb` `matched` |

On the code reviewed on 20 October, this disconnect left the job `running` and the slot held until a
restart. One usage row means the runner stream was closed and funding stopped when the client left.

## 3. `complete()` from an embedded app (D36, D38)

The engine in-process (`BuilderEngine.from_settings`, its own database file), inside `async with
stream:`. The prompt asked for a count to three hundred; the app's own rule called `complete()` as soon
as the text it had received contained "twenty".

| Measure | Value |
| --- | --- |
| `complete()` | after 5.71 s and 52,881 bytes, well before the runner would have finished |
| Job | `succeeded`; attempt `succeeded`; `ResultRef` 52,881 bytes |
| Cost | 2 usage rows, $0.000156, class **`delivered`** (not `uncertain`); session `082c76a8` `matched` |

## 4. Reconciliation and logs

This engine's reconciliation for the shared allocation: `balanced: true`. Delivered 37 rows
($0.001855), uncertain 1 row ($0.000112), failed-after-payment and pending 0. The rest of the
allocation's `spent` is `unattributed` here because ChatLPT's engine and the embedded run keep their
jobs in other databases, as in the 19 October run.

The server log has one `jobs.stream_ended` line per stream and no errors, tracebacks or
`jobs.stream_finish_failed`.

Spend for this evidence: about $0.0021.

## Not covered

- A failure after the first byte on the real network (the connection abort, D37) and a runner that
  never answers (the first-byte deadline, D35). Both are unit-tested with the scripted transport, and
  the abort through ASGI on both server code paths.
- `engine.stop()` with open streams (D36), unit-tested.
