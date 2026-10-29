"""The operation ledger (D57, ISS-11): repeat-safe admin commands over the ``OperationRepo`` port.

A command that moves money or creates a credential runs under the caller's ``operation_ref``. The ledger
records it ``started`` before the first provider call and ``done`` with the public result after the last
step, so a repeat is answered from that record and never applies twice. Provider calls keep their own
derived idempotency keys, so a resumed command replays them at the provider. An internal service, not
an extension point: an enterprise replaces the store, not the ledger.
"""

import hashlib
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter

from livepeer_builder.contracts import OPERATION_REPLAYED, ActorContext, OperationCommand, OperationRecord
from livepeer_builder.errors import (
    AccessDenied,
    AlreadyProvisioned,
    InvalidRequest,
    NotFound,
    OperationConflict,
    OperationInProgress,
    ProviderError,
)
from livepeer_builder.protocols import Clock, OperationRepo

_REQUEST = TypeAdapter(dict[str, Any])


def fingerprint(command: OperationCommand, request: Mapping[str, Any]) -> str:
    """sha256 of the command and its target and validated request, the reference excluded."""
    values = _REQUEST.dump_python(dict(request), mode="json")  # models and decimals as JSON
    canonical = json.dumps({"command": command, "request": values}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def definite_refusal(error: Exception) -> bool:
    """The command was refused and applied nothing: invalid, not found, denied, already provisioned, or a
    provider 4xx. A
    timeout, a transport error, a 5xx or a crash is an unknown outcome, never a refusal."""
    if isinstance(error, ProviderError):
        return 400 <= error.status_code < 500 and error.status_code != 408
    return isinstance(error, InvalidRequest | NotFound | AccessDenied | AlreadyProvisioned)


class TakenOver(Exception):
    """A resume took this run over (its fencing token is stale): stop, and answer as the record does."""


class RunningOperation:
    """The run a command's steps belong to: the record as this run holds it."""

    def __init__(self, repo: OperationRepo, record: OperationRecord) -> None:
        self._repo = repo
        self.record = record

    @property
    def context(self) -> dict[str, str]:
        return self.record.context

    @property
    def resumed(self) -> bool:
        """An earlier run of this command may have done some steps (the record was taken over)."""
        return self.record.fencing_token > 1

    async def remember(self, **values: str) -> None:
        """Add to the context before the next step, so a resume finds it. Raises ``TakenOver`` when a resume
        took this run over: undo what only this run made and re-raise; the other run owns the command now."""
        noted = self.record.model_copy(update={"context": self.record.context | values})
        if not await self._repo.remember_operation(noted):
            raise TakenOver
        self.record = noted


def done(
    caller: ActorContext,
    command: OperationCommand,
    operation_ref: str,
    request: Mapping[str, Any],
    result: Any,
    now: datetime,
) -> OperationRecord:
    """The record of a command whose effect and record commit in one transaction (an access key): it is
    never ``started``, so its outcome is never unknown."""
    return OperationRecord(
        application_id=caller.application_id,
        actor_id=caller.actor_id,
        operation_ref=operation_ref,
        command=command,
        fingerprint=fingerprint(command, request),
        state="done",
        result=result,
        started_at=now,
        completed_at=now,
    )


def repeat[T](held: OperationRecord, fingerprint: str, answer: TypeAdapter[T]) -> T:
    """The answer to a repeat of a held reference that is not resumed: a conflict for another request, the
    recorded result once done, otherwise in progress."""
    if held.fingerprint != fingerprint:
        raise OperationConflict(held)
    if held.state != "done":
        raise OperationInProgress(held)
    return _replayed(held, answer)


class OperationLedger:
    def __init__(self, repo: OperationRepo, clock: Clock, stale_s: float) -> None:
        self._repo = repo
        self._clock = clock
        self._stale_s = stale_s

    async def run[T](
        self,
        caller: ActorContext,
        command: OperationCommand,
        operation_ref: str,
        request: Mapping[str, Any],
        steps: Callable[[RunningOperation], Awaitable[T]],
        answer: TypeAdapter[T],
        context: Mapping[str, str] | None = None,
    ) -> T:
        """Run ``steps`` once per ``operation_ref``; a repeat gets the first run's result back."""
        OPERATION_REPLAYED.set(False)
        record = OperationRecord(
            application_id=caller.application_id,
            actor_id=caller.actor_id,
            operation_ref=operation_ref,
            command=command,
            fingerprint=fingerprint(command, request),
            state="started",
            context=dict(context or {}),
            started_at=self._clock.now(),
        )
        held = await self._repo.begin_operation(record)
        if held is not None:
            if held.fingerprint != record.fingerprint:
                raise OperationConflict(held)  # before any provider call
            if held.state == "done":
                return _replayed(held, answer)
            taken = await self._resume(held)
            if taken is None:  # another repeat resumed it first: answer as the record is now
                return await self._recorded(held, answer)
            record = taken
        running = RunningOperation(self._repo, record)
        try:
            result = await steps(running)
        except TakenOver:
            return await self._recorded(record, answer)
        except Exception as refused:
            # Freed only when nothing was applied: a step noted in the context (by this run or an earlier
            # one) means the command is half done, and a resume must finish it from that context.
            if definite_refusal(refused) and running.record.context == record.context == dict(context or {}):
                await self._repo.abandon_operation(record)
            raise  # otherwise the record stays started, for a resume
        if await self._repo.complete_operation(
            record, answer.dump_python(result, mode="json"), self._clock.now()
        ):
            return result
        return await self._recorded(record, answer)

    async def _recorded[T](self, record: OperationRecord, answer: TypeAdapter[T]) -> T:
        """A resume took this slow run over (its token is fenced off): answer as the record does now."""
        held = await self._repo.get_operation(record.application_id, record.actor_id, record.operation_ref)
        if held is None or held.state != "done":
            raise OperationInProgress(held or record, self._until_stale(held or record))
        return _replayed(held, answer)

    def _until_stale(self, held: OperationRecord) -> int:
        """Whole seconds until a repeat may resume ``held`` (``Retry-After``), at least 1."""
        age = (self._clock.now() - held.started_at).total_seconds()
        return max(1, math.ceil(self._stale_s - age))

    async def _resume(self, held: OperationRecord) -> OperationRecord | None:
        """Take over a run older than ``LPB_OPERATION_STALE_S``: it is presumed dead, and every step is safe
        to repeat. A younger one may still run. Taking over is a compare-and-set on the fencing token;
        ``None`` when another repeat took it over first."""
        now = self._clock.now()
        if (now - held.started_at).total_seconds() < self._stale_s:
            raise OperationInProgress(held, self._until_stale(held))
        return await self._repo.resume_operation(held, now)


def _replayed[T](held: OperationRecord, answer: TypeAdapter[T]) -> T:
    OPERATION_REPLAYED.set(True)
    return answer.validate_python(held.result)
