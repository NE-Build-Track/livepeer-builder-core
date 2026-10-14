"""Engine errors. Each maps to one HTTP status in livepeer_builder.http (see docs/contracts/engine.md)."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from livepeer_builder.contracts import AttemptOutcome, Job


class EngineError(Exception):
    """Base error for the engine."""


class NotAuthenticated(EngineError):
    """No credential, or a credential an authenticator recognized but rejected (401)."""


class AccessDenied(EngineError):
    """Authenticated, but the policy refuses this operation or resource (403)."""


class NotFound(EngineError):
    """No such record visible to this actor (404). Never reveals another actor's records."""


class InvalidRequest(EngineError):
    """The request is malformed or names an unsupported option (400)."""


class OperationExists(EngineError):
    """This (application, actor, operation_ref) already dispatched a job with the same request (409)."""

    def __init__(self, job: "Job") -> None:
        super().__init__(f"operation_ref {job.operation_ref!r} already used by job {job.id}")
        self.job = job


class OperationConflict(EngineError):
    """The operation_ref was reused with a different request body (409)."""

    def __init__(self, job: "Job") -> None:
        super().__init__(f"operation_ref {job.operation_ref!r} was used by job {job.id} with another request")
        self.job = job


class JobFailed(EngineError):
    """The job ended without a result. Switch on ``job.failure.kind``."""

    def __init__(self, job: "Job") -> None:
        failure = job.failure
        super().__init__(failure.message if failure is not None else f"job {job.id} {job.state}")
        self.job = job


class NoCapacity(EngineError):
    """Admission limits refused the job before dispatch (503 with Retry-After)."""

    def __init__(self, message: str, retry_after_s: int = 5) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class NotProvisioned(EngineError):
    """per_actor mode and this actor has no allocation. Provisioning is always explicit."""


class ProviderUnavailable(EngineError):
    """The payment provider or its usage feed cannot be reached or is not configured (503)."""


class ProviderError(EngineError):
    """The payment provider answered with an error status."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"provider HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message


class CursorMismatch(EngineError):
    """The provider rejected a usage cursor for this route or filter set."""


class NotImplementedInMilestone(EngineError):
    """A contract defined in M2 whose implementation belongs to a later milestone (501)."""

    def __init__(self, feature: str, milestone: str, ticket: str) -> None:
        super().__init__(f"{feature} is defined but not implemented until {milestone} ({ticket})")
        self.feature = feature
        self.milestone = milestone
        self.ticket = ticket


class UnsupportedForBackend(EngineError):
    """The operation is not available for this database backend (for example SQLite-only backup)."""


class RunnerCallError(EngineError):
    """One runner call failed. Raised by a RunnerTransport.

    ``payment_sent`` is True once tickets went out with the request: the job is then bound to this
    runner and must not fail over. ``manifest_ids`` lists the orchestrator sessions the signer
    signed, one per session in signing order. It can be non-empty even when ``payment_sent`` is
    False, because the signer charges when it signs.
    """

    def __init__(
        self,
        kind: "AttemptOutcome",
        message: str,
        *,
        payment_sent: bool,
        manifest_ids: tuple[str, ...] = (),
        status_code: int | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.payment_sent = payment_sent
        self.manifest_ids = manifest_ids
        self.status_code = status_code
        self.body = body
