# REST builder: jobs, history and results

Your application calls the engine's REST service (`lpb serve`) with an engine key. This guide is the
path through a job, its result and your history. The rules live in the contract:
[engine.md, inputs and results in M3](../contracts/engine.md#inputs-and-results-in-m3); this page links
them instead of repeating them. The schema is [generated/openapi.json](../generated/openapi.json).
The other mode, importing the engine in Python, is [embedded-mode.md](embedded-mode.md).

```sh
ENGINE=https://engine.example     # your engine's URL
KEY=lpk_...                       # an actor key with jobs.run, jobs.read and costs.read
```

## 1. Run a job, and keep what it returns

```sh
curl -sS -D headers.txt -o result.bin "$ENGINE/v1/jobs" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"app": "example/echo", "path": "/run", "payload": {"image_url": "https://..."}, "operation_ref": "order-1234"}'
```

- **The response body is the runner's answer, once.** The engine does not keep it, so save it now.
  The job's id is in the `Livepeer-Job-Id` header ([results](../contracts/engine.md#results)).
- **A URL in the payload goes to the runner unchanged**, and the runner fetches it. Make sure the runner
  can reach it ([inputs](../contracts/engine.md#inputs)).
- **A URL in the answer may be the model provider's**, and expire. Save the file when the job returns,
  or ask the runner for inline output where it offers one
  ([provider-hosted assets](../contracts/engine.md#provider-hosted-assets)).
- **Send an `operation_ref`** so a retry after a lost response cannot run the job twice
  ([idempotency](../contracts/engine.md#idempotency)).

Streaming (`?stream=true` or `Accept: text/event-stream`) is the same job delivered as it arrives
([REST streaming](../contracts/engine.md#rest-d37)).

## 2. Read the job

```sh
JOB=$(grep -i '^livepeer-job-id:' headers.txt | cut -d' ' -f2 | tr -d '\r')
curl -sS "$ENGINE/v1/jobs/$JOB" -H "Authorization: Bearer $KEY"
```

The job carries its state, attempts, failure and `result`, a `ResultRef` (size and sha256 of the bytes
you received). To check a saved copy, compare its hash:

```sh
sha256sum result.bin    # equals the job's result.sha256
```

`GET /v1/jobs/{id}/result` answers 501 until stored results arrive in M4 (ISS-26). Another actor's
job is 404 ([access.md](../contracts/access.md#answers)). The job's network cost is
`GET /v1/jobs/{id}/cost` ([payments.md](../contracts/payments.md#cost-classification)).

## 3. Page through your history

```sh
curl -sS "$ENGINE/v1/jobs?limit=50" -H "Authorization: Bearer $KEY"
# next page: the created_at of the last job above, with its offset
curl -sS -G "$ENGINE/v1/jobs" -H "Authorization: Bearer $KEY" \
  --data-urlencode "limit=50" --data-urlencode "before=2026-10-30T12:00:00.123456Z"
```

Newest first, your own jobs only; an admin or auditor key lists the application's
([job history](../contracts/engine.md#job-history)). An empty page is the end.

## 4. When a job fails

A failed `POST /v1/jobs` answers with a status from the failure and a body
`{"error": "...", "job": {...}}`. Branch on `job.failure.kind`, and on `job.failure.reason` for a
payment failure. Each row of [the failure table](../contracts/engine.md#failures) says what it means
and what to do, including whether the same `operation_ref` may run again.
