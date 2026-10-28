"""Payment evidence on an attempt: stored when the signer signs, and never shrunk (ISS-16, D42).

A transport calls ``on_signed(manifest_id)`` for each session the signer signs, before the paid request
goes out. ``SignedSessionRecorder`` is that callback for one runner call. It puts the session on the
in-memory attempt first, so the engine's error path has it too, then writes it with
``record_payment_evidence`` in a task the caller cannot cancel (D34): a disconnect never cuts the write
off half way. A failed write is logged, not raised: the signer has already charged, so the call goes
on, and the write after the call (``finish_attempt``), or the close after an error or cancel
(``record_payment_evidence`` again), stores the same sessions.

Before ``get_payment``, the transport calls ``remember`` with ``LivePaymentSession.manifest_id`` (D54).
The recorder stores that manifest on the attempt as ``unknown`` before the signer is asked, in a write
the caller cannot cancel. That write is strict: if it fails, the transport signs nothing, because a
charge the engine could not attribute must not happen. A crash during signing then leaves the
``unknown`` manifest for cost sync, so the cost stays ``pending`` instead of ``none`` at 0.
``on_signed`` makes it ``paid`` and marks the attempt paid; a refusal makes it ``unpaid``, which cost
sync never matches. Any other signing error leaves it ``unknown``. A cancel during the intent write
(the caller left, or the open deadline passed) stores it ``unpaid`` once that write ends: the signer
was never asked.
"""

import asyncio
import contextlib
import logging
from collections.abc import Coroutine, Sequence
from typing import Any

from livepeer_builder.contracts import Attempt, PaymentState
from livepeer_builder.protocols import JobRepo
from livepeer_builder.services.streams import sessions_from
from livepeer_builder.utils import run_shielded, spawn_shielded

_LOG = logging.getLogger(__name__)


class SignedSessionRecorder:
    """The ``on_signed`` callback for one runner call. It records on the attempt current when it was
    made, never "the last one", so a report that arrives late cannot land on the next runner's attempt."""

    def __init__(self, store: JobRepo, attempts: list[Attempt]) -> None:
        self._store = store
        self._attempts = attempts
        self._index = len(attempts) - 1

    async def __call__(self, manifest_id: str) -> None:
        await self._record(manifest_id, payment_sent=True, state="paid")

    async def remember(self, manifest_id: str) -> None:
        """Before ``get_payment``: store the manifest as ``unknown`` (D54). Raises when the write fails,
        so the transport signs nothing."""
        attempt = with_evidence(
            self._attempts[self._index], (manifest_id,), payment_sent=False, state="unknown"
        )
        write = spawn_shielded(self._store.record_payment_evidence(attempt))
        try:
            await asyncio.shield(write)  # a failure propagates: nothing stored and nothing signed
        except asyncio.CancelledError:
            # The write may still land, but the signer was never asked: once it ends, store the
            # manifest ``unpaid``, so a cancel or an open deadline never leaves a cost pending forever.
            unasked = with_evidence(attempt, (manifest_id,), payment_sent=False, state="unpaid")
            self._attempts[self._index] = unasked
            spawn_shielded(_after(write, _write(self._store, unasked)))
            raise
        self._attempts[self._index] = attempt

    async def refused(self, manifest_id: str) -> None:
        """The signer refused this manifest: ``unpaid``. Logged, never raised: the stored row stays
        ``unknown`` if this write fails, which keeps the cost ``pending``, never 0."""
        await self._record(manifest_id, payment_sent=False, state="unpaid")

    async def _record(self, manifest_id: str, *, payment_sent: bool, state: PaymentState) -> None:
        attempt = with_evidence(self._attempts[self._index], (manifest_id,), payment_sent, state)
        self._attempts[self._index] = attempt
        await run_shielded(_write(self._store, attempt))


async def store_evidence(store: JobRepo, attempts: Sequence[Attempt]) -> None:
    """Store every attempt's evidence again, for the close after an error or cancel. Appending is
    idempotent, so this is harmless when the write at signing worked."""
    for attempt in attempts:
        if attempt.payment_sent or attempt.sessions:
            await _write(store, attempt)


async def _after(first: "asyncio.Future[None]", then: Coroutine[Any, Any, None]) -> None:
    """Run ``then`` once ``first`` has ended, however it ended (``first``'s failure is the caller's)."""
    with contextlib.suppress(Exception):
        await first
    await then


async def _write(store: JobRepo, attempt: Attempt) -> None:
    """Logged, never raised: the signer has already charged, and a later write stores it again."""
    try:
        await store.record_payment_evidence(attempt)
    except Exception as exc:
        _LOG.warning(
            "could not store payment evidence",
            extra={"event": "jobs.evidence_write_failed", "job_id": str(attempt.job_id), "error": str(exc)},
        )


def with_evidence(
    attempt: Attempt, manifest_ids: Sequence[str], payment_sent: bool, state: PaymentState = "paid"
) -> Attempt:
    """The attempt with these manifests in ``state``: new ones are added in order, and a known one's
    state moves on unless it is ``paid``, which is final (D54). ``payment_sent`` never goes back to
    ``False``: a transport that reports otherwise after the signer signed does not unpay the call."""
    named = set(manifest_ids)
    sessions = tuple(
        s.model_copy(update={"payment_state": state})
        if s.manifest_id in named and s.payment_state != "paid"
        else s
        for s in attempt.sessions
    )
    known = {session.manifest_id for session in sessions}
    fresh = tuple(m for m in dict.fromkeys(manifest_ids) if m not in known)
    added = tuple(s.model_copy(update={"payment_state": state}) for s in sessions_from(fresh))
    return attempt.model_copy(
        update={"sessions": sessions + added, "payment_sent": attempt.payment_sent or payment_sent}
    )
