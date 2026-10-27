"""Stable operation identity without storing opaque request bodies."""

import hashlib
import json

from livepeer_builder.contracts import JobRequest


def request_fingerprint(request: JobRequest) -> str:
    """Stable hash of what a retry must repeat exactly. Limits and timeouts may differ between retries."""
    fields = request.model_dump(
        mode="json", include={"app", "path", "method", "payload", "capability", "model"}
    )
    # Keep legacy JSON fingerprints byte-for-byte stable across upgrades.
    if request.raw_body is not None:
        fields["raw_body_sha256"] = hashlib.sha256(request.raw_body).hexdigest()
        fields["raw_body_size"] = len(request.raw_body)
    if request.content_type is not None:
        fields["content_type"] = request.content_type
    if request.headers:
        fields["headers"] = request.headers
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
