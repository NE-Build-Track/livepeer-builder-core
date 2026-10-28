"""Payment evidence on an attempt: stored when the signer signs, and never shrunk (ISS-16, D42).

A transport calls ``on_signed(manifest_id)`` for each session the signer signs, before the paid request
goes out. ``SignedSessionRecorder`` is that callback for one runner call. It puts the session on the
in-memory attempt first, so the engine's error path has it too, then writes it with
``record_payment_evidence`` in a task the caller cannot cancel (D34): a disconnect never cuts the write
off half way. A failed write is logged, not raised: the signer has already charged, so the call goes
on, and the write after the call (``finish_attempt``), or the close after an error or cancel
(``record_payment_evidence`` again), stores the same sessions.

Before ``get_payment``, the transport calls ``remember`` (D53) with ``LivePaymentSession.manifest_id``.
The recorder keeps that id in memory. It does not append a session and it does not write. A row means
``on_signed`` ran. An expired challenge, a lost reply and a cancel leave no session row.
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
        self._remembered: list[str] = []

    async def __call__(self, manifest_id: str) -> None:
        attempt = with_evidence(self._attempts[self._index], (manifest_id,), payment_sent=True)
        self._attempts[self._index] = attempt
        await run_shielded(_write(self._store, attempt))

    async def remember(self, manifest_id: str) -> None:
        """Before ``get_payment``: keep the manifest id here. No session on the attempt and no write."""
        if manifest_id not in self._remembered:
            self._remembered.append(manifest_id)


async def store_evidence(store: JobRepo, attempts: Sequence[Attempt]) -> None:
    """Store every attempt's evidence again, for the close after an error or cancel. Appending is
    idempotent, so this is harmless when the write at signing worked."""
    for attempt in attempts:
        if attempt.payment_sent or attempt.sessions:
            await _write(store, attempt)


async def _write(store: JobRepo, attempt: Attempt) -> None:
    """Logged, never raised: the signer has already charged, and a later write stores it again."""
    try:
        await store.record_payment_evidence(attempt)
    except Exception as exc:
        _LOG.warning(
            "could not store payment evidence",
            extra={"event": "jobs.evidence_write_failed", "job_id": str(attempt.job_id), "error": str(exc)},
        )


def with_evidence(attempt: Attempt, manifest_ids: Sequence[str], payment_sent: bool) -> Attempt:
    """The attempt with the manifests not recorded yet, in order. ``payment_sent`` never goes back to
    ``False``: a transport that reports otherwise after the signer signed does not unpay the call."""
    known = {session.manifest_id for session in attempt.sessions}
    fresh = tuple(m for m in dict.fromkeys(manifest_ids) if m not in known)
    return attempt.model_copy(
        update={
            "sessions": attempt.sessions + sessions_from(fresh),
            "payment_sent": attempt.payment_sent or payment_sent,
        }
    )
