"""Who could run a job, and with which credential: the checks the paid call makes before its first
attempt, and so before any payment (D52, D62, #114 C17, ISS-26). ``services/dispatch.py`` asks.

A job that waits for its answer (an immediate job, a stream) is failed by the first check that says
no, in the order it always was: no runner listed (``no_offering``), no allocation
(``not_provisioned``), every runner it would pay full (``no_capacity``), no runner selected
(``no_offering``).

A queued job is asked about its actor first, because that is an answer and the rest may pass: an actor
with no allocation fails the job ``not_provisioned`` whatever the runners say. After that nothing fails
it that may pass: a credential that cannot be read, no runner listed, and, for a job that pays, every
priced runner full are each ``NotNow``, and the worker lets the job wait. A job that does not pay goes on
to its runners, full or not: advertised capacity is a hint, and its refusal costs nothing.
"""

from typing import NoReturn
from uuid import UUID

from livepeer_builder.contracts import ActorContext, FailureKind, JobRequest, PaymentKey, Runner
from livepeer_builder.errors import NotProvisioned
from livepeer_builder.protocols import CredentialResolver, RunnerTransport, SelectionPolicy
from livepeer_builder.services.discovery import DiscoveryService, capped_first, paid_candidates
from livepeer_builder.services.failover import Candidates


class NotNow(Exception):
    """A claimed async job cannot be dispatched for a reason that passes, and nothing was attempted: its
    actor's credential could not be read, no runner is listed for its app, or it pays and every runner
    it would pay advertises no room. The job is not failed: its worker gives the claim back."""


class Nobody(Exception):
    """Nobody can run the job, and it does not wait for somebody: the kind it fails with, unpaid."""

    def __init__(self, kind: FailureKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class JobRunners:
    def __init__(
        self,
        discovery: DiscoveryService,
        transport: RunnerTransport,
        selection: SelectionPolicy,
        credentials: CredentialResolver,
        max_paid_attempts: int,
    ) -> None:
        self._discovery = discovery
        self._transport = transport
        self._selection = selection
        self._credentials = credentials
        self._max_paid_attempts = max_paid_attempts  # D55

    @property
    def _pays(self) -> bool:
        """Whether a credential pays a runner through the transport: no signer, no pay."""
        return bool(getattr(self._transport, "pays", True))

    def waits(self, app: str) -> bool:
        """Whether a queued job of ``app`` that pays could not be dispatched now, for a reason that
        passes: no runner is listed for it, or every runner an attempt would pay advertises no room.
        It is what can be known without the job's credential, so it is not the last word on one job:
        the async worker still claims one job of such an app each poll interval, and ``choose`` decides
        for that job (ISS-26)."""
        candidates = self._discovery.candidates(app)
        return not candidates or (self._pays and not paid_candidates(candidates))

    async def choose(
        self, job_id: UUID, actor: ActorContext, request: JobRequest, *, queued: bool
    ) -> tuple[Candidates, PaymentKey]:
        """The runners to try, in order, and the credential to try them with. Raises ``Nobody`` when
        the job fails before payment, and ``NotNow`` when a queued job has to wait."""
        if queued:
            credential = await self._readable(actor)
            candidates = self._listed(request.app, queued)
        else:
            candidates = self._listed(request.app, queued)
            credential = await self._provisioned(actor)
        paid = credential.token is not None and self._pays
        # A runner advertising zero capacity stays a candidate (#114, C17): the hint can lag, selection
        # tries it last, and if every runner refuses the job fails ``refused`` through failover. A job
        # that pays is not tried on runners that all advertise zero: a full runner's refusal can cost a
        # ticket.
        if paid and not (candidates := paid_candidates(candidates)):
            _nobody(queued, "no_capacity", f"every runner for {request.app!r} is busy")
        ordered = tuple(self._selection.order(candidates, request))
        if paid:  # a runner with no usable ceiling is refused at its 402 (D62)
            ordered = capped_first(ordered)
        if not ordered:  # an enterprise SelectionPolicy may filter every runner out
            raise Nobody("no_offering", f"no runner for {request.app!r} selected")
        return Candidates(ordered, request.max_attempts, self._max_paid_attempts, job_id), credential

    def _listed(self, app: str, queued: bool) -> list[Runner]:
        candidates = self._discovery.candidates(app)
        if not candidates:
            _nobody(queued, "no_offering", f"no runner advertises app {app!r}")
        return candidates

    async def _provisioned(self, actor: ActorContext) -> PaymentKey:
        """The actor's credential. An actor with no allocation fails the job; any other error is the
        caller's, as it was."""
        try:
            return await self._credentials.credential(actor)
        except NotProvisioned as missing:
            raise Nobody("not_provisioned", str(missing)) from missing

    async def _readable(self, actor: ActorContext) -> PaymentKey:
        """``_provisioned`` for a queued job: a lookup that fails for any other reason may pass."""
        try:
            return await self._provisioned(actor)
        except Nobody:
            raise
        except Exception as blip:
            raise NotNow(f"the actor's credential could not be read ({type(blip).__name__})") from blip


def _nobody(queued: bool, kind: FailureKind, message: str) -> NoReturn:
    """No runner can take the job now. A queued job waits for one; any other fails with ``kind``."""
    if queued:
        raise NotNow(message)
    raise Nobody(kind, message)
