"""What ``FakeEngine`` answers with: a ``Reply``, a ``Fails`` or a ``Raises`` per ``(app, path)``."""

from dataclasses import dataclass
from typing import Any, cast

from livepeer_builder.contracts import AttemptOutcome, FailureKind
from livepeer_builder.errors import EngineError

NOT_DISPATCHED = frozenset({"no_offering", "no_capacity", "not_provisioned"})  # failed before any attempt
UNCERTAIN_OUTCOMES = frozenset({"timeout", "unreachable", "in_flight"})  # paid, and the runner may have run


def attempt_outcome(kind: FailureKind) -> AttemptOutcome:
    """The attempt outcome the engine records for a failure of this kind (the inverse of failure_kind)."""
    if kind in ("runner_error", "runner_rejected"):
        return "http"
    if kind == "payment":
        return "payment_refused"
    if kind == "interrupted":
        return "in_flight"  # the engine stopped mid-call; recovery leaves the attempt as it was
    return cast(AttemptOutcome, kind)


@dataclass(frozen=True)
class Fails:
    """The job fails: ``run`` and ``stream`` raise ``JobFailed`` with this failure.

    ``kind`` sets the attempt outcome the engine would record (``runner_error`` is ``http``). With
    ``payment_sent=True`` the job fails on that runner (no failover); a paid ``timeout`` or
    ``unreachable`` leaves it ``uncertain``, since the runner may have run. ``no_offering``,
    ``no_capacity`` and ``not_provisioned`` fail before any attempt. ``body`` is the runner's own
    words. Among stream chunks the stream was paid when it opened, so ``payment_sent`` does not apply.
    """

    kind: FailureKind = "runner_error"
    message: str = "scripted failure"
    status_code: int | None = 500
    payment_sent: bool = False
    body: str | None = None


@dataclass(frozen=True)
class Reply:
    """A successful runner reply. ``chunks`` is what a stream yields."""

    content: bytes = b'{"ok": true}'
    status_code: int = 200
    content_type: str = "application/json"
    chunks: tuple[bytes | Fails, ...] = (b'{"ok": true}',)
    data: dict[str, Any] | None = None


@dataclass(frozen=True)
class Raises:
    """The call raises ``error``, as the engine does before any job exists: ``NoCapacity`` at admission,
    ``ProviderUnavailable``, and so on. Scripted for a reply, or for a job's cost."""

    error: EngineError
