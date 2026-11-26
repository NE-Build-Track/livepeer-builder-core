"""The operation ledger (D57, ISS-11): repeat-safe admin commands over the ``OperationRepo`` port.

A command that moves money or creates a credential runs under the caller's ``operation_ref``. The ledger
records it ``started`` before the first provider call and ``done`` with the public result after the last
step, so a repeat is answered from that record and never applies twice. Provider calls keep their own
derived idempotency keys, so a resumed command replays them at the provider. An internal service, not
an extension point: an enterprise replaces the store, not the ledger.
"""

import asyncio
import hashlib
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import TypeAdapter

from livepeer_builder.contracts import (
    OPERATION_REPLAYED,
    OPERATION_RESUMED,
    ActorContext,
    OperationCommand,
    OperationRecord,
)
from livepeer_builder.errors import (
    AccessDenied,
    AlreadyAllotted,
    InvalidRequest,
    NotFound,
    OperationConflict,
    OperationInProgress,
    ProviderError,
    ProviderUnavailable,
)
from livepeer_builder.protocols import Clock, OperationRepo

_REQUEST = TypeAdapter(dict[str, Any])


# What a fingerprint hashes is a stored format, not a name: ``lpb_operations.fingerprint`` holds digests
# written before #163 renamed the words below, and no migration can recompute a digest. The hashed text
# keeps the word it was first written with, so the repeat of a command recorded before the upgrade still
# matches its record (tests/unit/test_hashed_forms.py). Nothing reads these words back.
_HASHED_COMMANDS: dict[str, str] = {"allot": "provision", "top_up": "fund", "top_up_grant": "fund_grant"}
_HASHED_FIELDS = {"tenant_id": "application_id"}
_HASHED_KINDS = {"clearinghouse_admin": "admin"}  # the kind of key an ``issue_key`` asked for


def fingerprint(command: OperationCommand, request: Mapping[str, Any]) -> str:
    """sha256 of the command and its target and validated request, the reference excluded."""
    values = _REQUEST.dump_python(dict(request), mode="json")  # models and decimals as JSON
    if "kind" in values:
        values["kind"] = _HASHED_KINDS.get(values["kind"], values["kind"])
    hashed = {_HASHED_FIELDS.get(name, name): value for name, value in values.items()}
    canonical = json.dumps(
        {"command": _HASHED_COMMANDS.get(command, command), "request": hashed}, sort_keys=True
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def definite_refusal(error: Exception) -> bool:
    """The command was refused and applied nothing: invalid, not found, denied, already allotted, or a
    provider 4xx. A
    timeout, a transport error, a 5xx or a crash is an unknown outcome, never a refusal."""
    if isinstance(error, ProviderError):
        return 400 <= error.status_code < 500 and error.status_code != 408
    return isinstance(error, InvalidRequest | NotFound | AccessDenied | AlreadyAllotted)


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
        tenant_id=caller.tenant_id,
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
        earlier_request: Callable[[], Awaitable[Mapping[str, Any]]] | None = None,
        resumable: Callable[[OperationRecord], None] | None = None,
    ) -> T:
        """Run ``steps`` once per ``operation_ref``; a repeat gets the first run's result back.

        ``resumable`` is shown a held record of this command that has not finished, before it is resumed
        or reported in progress, and raises when this call must not finish that run. Nothing was sent or
        written by then, so the record stays as it was for the call that may resume it.

        ``earlier_request`` is the request as an earlier release recorded it, for a command whose request
        lost a field since. It is asked only when a held record does not match ``request``: a finished
        record that matches the earlier form is this command's too, and is replayed as its own. One
        still ``started`` is a conflict however old, and is never resumed: the earlier release's run is
        not one this release can finish (docs/contracts/index.md, the upgrade notes of #165)."""
        OPERATION_REPLAYED.set(False)
        OPERATION_RESUMED.set(False)
        record = OperationRecord(
            tenant_id=caller.tenant_id,
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
            form = await _form_held(held, record, earlier_request)
            if form is None or (form == "earlier" and held.state != "done"):
                raise OperationConflict(held)  # before any provider call
            if held.state == "done":
                return _replayed(held, answer)
            if resumable is not None:
                resumable(held)  # before the take-over, which is a write and restarts the stale window
            taken = await self._resume(held)
            if taken is None:  # another repeat resumed it first: answer as the record is now
                return await self._recorded(held, answer)
            record = taken
            OPERATION_RESUMED.set(True)  # this run follows an attempt whose outcome is unknown
        running = RunningOperation(self._repo, record)
        # A run never outlives half the stale window, so none is still calling a provider when a repeat
        # resumes it: fencing guards the records, not a call in flight (grant creation has no provider
        # idempotency key). Past the budget the call is cancelled and the outcome is unknown.
        budget = asyncio.timeout(self._stale_s / 2)
        try:
            async with budget:
                result = await steps(running)
        except TakenOver:
            return await self._recorded(record, answer)
        except Exception as refused:
            if budget.expired():
                raise ProviderUnavailable(
                    f"{command} ran past its {self._stale_s / 2:g} s budget; repeat it to resume"
                ) from refused
            # Freed only when nothing was applied: a step noted in the context (by this run or an earlier
            # one) means the command is half done, and a resume must finish it from that context. A resumed
            # run follows an attempt whose outcome is unknown (a provider call that timed out may have been
            # applied, with nothing noted), so its refusal never frees the reference: freed, the next repeat
            # would run as new, and another request could take the reference. An actor already allotted
            # is refused for good: the earlier run's actor was revoked or is another's.
            unchanged = running.record.context == record.context == dict(context or {})
            untouched = unchanged and not running.resumed
            if definite_refusal(refused) and (untouched or isinstance(refused, AlreadyAllotted)):
                await self._repo.abandon_operation(record)
            raise  # otherwise the record stays started, for a resume
        if await self._repo.complete_operation(
            record, answer.dump_python(result, mode="json"), self._clock.now()
        ):
            return result
        return await self._recorded(record, answer)

    async def _recorded[T](self, record: OperationRecord, answer: TypeAdapter[T]) -> T:
        """A resume took this slow run over (its token is fenced off): answer as the record does now."""
        held = await self._repo.get_operation(record.tenant_id, record.actor_id, record.operation_ref)
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


async def _form_held(
    held: OperationRecord,
    record: OperationRecord,
    earlier_request: Callable[[], Awaitable[Mapping[str, Any]]] | None,
) -> Literal["current", "earlier"] | None:
    """Which form of this command the record holding a reference is: it has the command's fingerprint
    (``current``), or only the fingerprint of the request as an earlier release recorded it
    (``earlier``). ``None`` when it is another command's."""
    if held.fingerprint == record.fingerprint:
        return "current"
    if earlier_request is None:
        return None
    earlier = fingerprint(record.command, await earlier_request())
    return "earlier" if held.fingerprint == earlier else None


def _replayed[T](held: OperationRecord, answer: TypeAdapter[T]) -> T:
    OPERATION_REPLAYED.set(True)
    return answer.validate_python(held.result)
