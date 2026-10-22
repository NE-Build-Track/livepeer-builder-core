"""Payment evidence on an attempt: written when the signer signs, and never shrunk (D42).

A transport reports each signed session through ``protocols.report_signed_session`` before the paid
request goes out. ``SignedSessionRecorder`` puts it on the in-memory attempt first, so a cancellation
during the write still leaves it for the engine's error path, then writes it with ``record_payment_evidence``
(append-only, D39). A failed write is logged, not raised: the signer has already charged, so the call
goes on, and the write after the call stores the same rows.
"""

import logging
from collections.abc import Sequence

from livepeer_builder.contracts import Attempt, SignedSession
from livepeer_builder.protocols import JobRepo

_LOG = logging.getLogger(__name__)


class SignedSessionRecorder:
    """The ``report_signed_session`` hook for one runner call: records on the attempt current at creation.

    It holds the attempt's index, not "the last one", so a report that arrives late (from a task the
    transport started) can never land on the next runner's attempt.
    """

    def __init__(self, store: JobRepo, attempts: list[Attempt]) -> None:
        self._store = store
        self._attempts = attempts
        self._index = len(attempts) - 1

    async def __call__(self, manifest_id: str) -> None:
        attempt = with_evidence(self._attempts[self._index], (manifest_id,), payment_sent=True)
        self._attempts[self._index] = attempt
        try:
            await self._store.record_payment_evidence(attempt)  # never rewrites a stored outcome
        except Exception as exc:
            _LOG.warning(
                "could not write payment evidence before the paid request",
                extra={
                    "event": "jobs.evidence_write_failed",
                    "job_id": str(attempt.job_id),
                    "error": str(exc),
                },
            )


def with_evidence(attempt: Attempt, manifest_ids: Sequence[str], payment_sent: bool) -> Attempt:
    """Adds the manifests not recorded yet, in order. ``payment_sent`` never goes back to ``False``."""
    known = {session.manifest_id for session in attempt.sessions}
    added = sessions_from(tuple(m for m in dict.fromkeys(manifest_ids) if m not in known))
    return attempt.model_copy(
        update={"sessions": attempt.sessions + added, "payment_sent": attempt.payment_sent or payment_sent}
    )


def sessions_from(manifest_ids: tuple[str, ...]) -> tuple[SignedSession, ...]:
    return tuple(SignedSession(manifest_id=manifest_id) for manifest_id in manifest_ids if manifest_id)
