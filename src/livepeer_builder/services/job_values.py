"""Stable request identity and inline result metadata for immediate jobs."""

import hashlib
import json

from livepeer_builder.contracts import JobRequest, ResultRef, RunnerReply


def request_fingerprint(request: JobRequest) -> str:
    """Stable hash of what a retry must repeat exactly. Limits and timeouts may differ between retries."""
    fields = request.model_dump(
        mode="json", include={"app", "path", "method", "payload", "capability", "model"}
    )
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def result_ref(reply: RunnerReply) -> ResultRef:
    return ResultRef(
        storage="inline",
        content_type=reply.content_type,
        size_bytes=len(reply.content),
        sha256=hashlib.sha256(reply.content).hexdigest(),
    )
