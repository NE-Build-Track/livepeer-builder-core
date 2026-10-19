# Runtime evidence: streamed jobs on the network, 19 October 2026

The first live run of `jobs.stream` ([plan](../../exec-plans/active/streaming-jobs.md), branch
`tasks/m3/mz/stream-support` at `ebedff1`), against the payment stack the ChatLPT reference app
runs: Batteries `ca09e47` and node signer `406b248`, exposed on loopback for the check. The engine
ran in-process with its own SQLite database, in `per_application` mode on ChatLPT's shared
allocation `AaFWEnucfhuOCn95t02zMw`.

| Component | Identity |
| --- | --- |
| Runners discovered for `blueclaw/chat/qwen3.8-27b` | xode us-central, speedybird, hky.eliteencoder (3) |
| `Runner.metadata` (new) | xode and hky: `{"model_id":"Qwen3.8-27B", …, "max_context":131072}`; speedybird: the same with `"max_context":40960` |
| `Runner.version`, `Runner.gpu` | not published by these runners (`None`) |

## 1. A stream read to the end

`POST /v1/chat/completions` with `stream: true`, `stream_options.include_usage: true`, thinking off,
"Count from one to twelve in words, one per line."

| Measure | Value |
| --- | --- |
| `open` returned (402 paid, headers in) | 956 ms |
| Attempt row before the first byte | `in_flight`, `payment_sent=True`, session `440ddeeb` |
| Chunks | 12; first byte at 1086 ms, last at 1291 ms; deltas every 15–35 ms |
| Text | `one` … `twelve`, one per line, as asked |
| Usage in the final event | 24 prompt, 26 completion, 0 reasoning |
| Job | `succeeded`; attempt `succeeded`; `ResultRef` inline, 3722 bytes, sha `fe178293ff76…` |
| Cost | `observed`, **$0.000111275**, 1 usage row, class `delivered`; session `440ddeeb` → `AaFWOt1Wehm17rB97LEJLw` `matched` |

## 2. A stream abandoned after the first chunk

The same request with a longer answer; the consumer read one chunk (484 bytes) and called `aclose()`.

| Measure | Value |
| --- | --- |
| Job | `uncertain`; attempt `abandoned`; failure kind `abandoned` |
| Cost | `observed`, **$0.000111275**, 1 usage row, class `uncertain`; session `cb533e2b` → `AaFWOuHZemqIj4hLW8DjYw` `matched` |

The work was paid for and the engine says so. Nothing failed over, nothing was retried.

## 3. Reconciliation

`balanced: true`. The allocation's `spent` ($0.002405) exceeds this engine's attributed total because
the allocation is shared with ChatLPT's own engine, whose jobs live in another database; the
difference is `unattributed` here, as the contract defines. Within this engine every usage row of
both jobs matched a logged session.

## Observations

- Streaming adds no payment round trips: the 402 is paid once at `open`, the stream funds itself
  under the same session, and cost sync attributes it exactly like a buffered job.
- `max_context` differs per orchestrator for the same app. An application deriving a context window
  from `Runner.metadata` should take the smallest value across the runners the engine may choose.
- Spend for this evidence: about $0.00022.

## Not covered

- A mid-stream orchestrator failure or an idle timeout on the real network (both covered by unit
  tests with the scripted transport).
- A restart with a stream open (unit-tested through `recover_interrupted`).
