# Events contract (definition only)

**Decision:** D10. Defined in M2; implemented in M3. **Code:** `contracts/reserved.py`
(`EngineEvent`, `EventPage`), `protocols.EventFeed`, `services/reserved.py` (answers 501).

Enterprises keep their retail records in step with the engine by consuming a versioned event feed.

## Event

| Field | Meaning |
| --- | --- |
| `sequence` | The cursor; strictly increasing |
| `id` | Stable; consumers drop duplicates by `id` |
| `type` | `job.created`, `job.finished`, `usage.reported`, `cost.observed`, `cost.corrected` |
| `schema_version` | Starts at 1; a breaking payload change increments it |
| `occurred_at`, `application_id`, `actor_id`, `job_id`, `operation_ref` | Correlation |
| `payload` | The `Job` for job events. The `JobCost` (with per-attempt cost classes) for cost events |

## Reading

`GET /v1/events?after=<cursor>&limit=<n>` (and `EventFeed.after`) returns `{events, next_cursor}`.
The consumer applies a page, saves `next_cursor`, and de-duplicates by `id`.

- **Scope:** an actor reads its own application's events with `events.read`. An admin reads every
  application.
- **Retention window:** events are kept for a configurable window. A cursor older than the window gets
  **410 Gone**, and the consumer resyncs from current state (`GET /v1/jobs`, `/cost`). There is **no**
  "replay from zero" promise.
- **Completeness:** a durable engine feed cannot restore evidence an upstream producer never sent.
  Missing cost stays `pending`.
