"""Operational contracts: ``BuilderEngine.health()`` (``GET /readyz``), ``BuilderEngine.purge()`` and
the read-only payments check (``lpb payments check``)."""

from datetime import datetime
from typing import Literal

from livepeer_builder.contracts.common import Contract, FailureCode, SourceFailureCode
from livepeer_builder.contracts.payments import TenantGrant

# Which half of the engine a deployment runs (D84, ``LPB_ROLE``). "both": the work half and the
# clearinghouse half, for self-hosted payments. "work": the work half alone, paying through a payment
# clearinghouse. "clearinghouse": the clearinghouse half alone; it never runs jobs.
Role = Literal["both", "work", "clearinghouse"]


class EngineHealth(Contract):
    role: Role = "both"  # the role this engine runs in (#169)
    ready: bool
    discovery_fresh: bool
    discovery_observed_at: datetime | None
    discovery_error: SourceFailureCode | None  # a code, never an error's text (#113, C7)
    cost_sync_at: datetime | None
    cost_sync_error: FailureCode | None  # the text is logged
    # usage rows cost sync could not read or store and left out, in all (#114, C13); None until the first
    # cost sync of this process
    cost_sync_set_aside: int | None = None
    running_jobs: int
    # Async jobs waiting for a worker, and how long the oldest has waited (ISS-26). Alert on the age, not
    # on the depth: a deep queue that drains is healthy, and one old job means nothing is being claimed.
    # Counted when a job is submitted or cancelled and at each step of the worker.
    queued_jobs: int = 0
    oldest_queued_s: float | None = None
    version: str


class PurgeReport(Contract):
    """What a retention purge deleted (docs/contracts/persistence.md#retention)."""

    jobs_deleted: int
    usage_rows_deleted: int


# What one probe of the payments check found. "ok": the answer the probe looks for. "refused": the other
# side answered and refused the engine's credential or its address (401, 403, 404). "unavailable": it
# was not reached, or said it cannot answer now. "unexpected": it answered with something the probe
# cannot read as what it asked for. "not_configured": the setting the probe needs is not set.
ProbeOutcome = Literal["ok", "refused", "unavailable", "unexpected", "not_configured"]
PaymentConfiguration = Literal["self_hosted", "clearinghouse"]


class PaymentProbe(Contract):
    """One read the payments check made (D87, #168). Never a credential, an address or the other side's
    own words: ``setting`` names the setting that says where the probe went."""

    name: str
    setting: str  # such as LPB_SIGNER_URL
    request: str | None  # such as "GET /v1/usage": the one request sent, without its host. None: none sent
    outcome: ProbeOutcome
    status_code: int | None = None
    detail: str  # the engine's words


class PaymentCheck(Contract):
    """What ``lpb payments check`` found (D87, #168): every probe a ``GET``, and nothing written
    anywhere. ``ok`` when every probe is. ``role`` is the engine's, and ``configuration`` what its
    payments reach: Batteries and a signer of its own (``self_hosted``, the ``both`` and the
    ``clearinghouse`` role), or a payment clearinghouse (``clearinghouse``, the ``work`` role).
    ``tenant`` is who an engine in its ``work`` role is at the clearinghouse, with its grant, when the
    clearinghouse answered that read."""

    role: Role  # the engine's role, which says what it probes (#169)
    configuration: PaymentConfiguration
    ok: bool
    probes: tuple[PaymentProbe, ...]
    tenant: TenantGrant | None = None
