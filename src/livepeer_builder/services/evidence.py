"""Payment evidence on an attempt: stored when the signer signs, and never shrunk (ISS-16, D42).

A transport calls ``on_signed(manifest_id)`` for each session the signer signs, before the paid request
goes out. ``SignedSessionRecorder`` is that callback for one runner call. It puts the session on the
in-memory attempt first, so the engine's error path has it too, then writes it with
``record_payment_evidence`` in a task the caller cannot cancel (D34): a disconnect never cuts the write
off half way. A failed write is logged, not raised: the signer has already charged, so the call goes
on, and the write after the call stores the same sessions.
"""

import logging
from collections.abc import Sequence

from livepeer_builder.contracts import Attempt
from livepeer_builder.protocols import JobRepo
from livepeer_builder.services.streams import sessions_from
from livepeer_builder.utils import run_shielded

_LOG = logging.getLogger(__name__)


class SignedSessionRecorder:
    """The ``on_signed`` callback for one runner call. It records on the attempt current when it was
    made, never "the last one", so a report that arrives late cannot land on the next runner's attempt."""

    def __init__(self, store: JobRepo, attempts: list[Attempt]) -> None:
        self._store = store
        self._attempts = attempts
        self._index = len(attempts) - 1

    async def __call__(self, manifest_id: str) -> None:
        attempt = with_evidence(self._attempts[self._index], (manifest_id,), payment_sent=True)
        self._attempts[self._index] = attempt
        await run_shielded(self._write(attempt))

    async def _write(self, attempt: Attempt) -> None:
        try:
            await self._store.record_payment_evidence(attempt)
        except Exception as exc:
            _LOG.warning(
                "could not store payment evidence before the paid request",
                extra={
                    "event": "jobs.evidence_write_failed",
                    "job_id": str(attempt.job_id),
                    "error": str(exc),
                },
            )


def with_evidence(attempt: Attempt, manifest_ids: Sequence[str], payment_sent: bool) -> Attempt:
    """The attempt with the manifests not recorded yet, in order. ``payment_sent`` never goes back to
    ``False``: a transport that reports otherwise after the signer signed does not unpay the call."""
    known = {session.manifest_id for session in attempt.sessions}
    added = sessions_from(tuple(m for m in dict.fromkeys(manifest_ids) if m not in known))
    return attempt.model_copy(
        update={"sessions": attempt.sessions + added, "payment_sent": attempt.payment_sent or payment_sent}
    )
