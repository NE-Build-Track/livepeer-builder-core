"""Reconcile one allocation at a time (#115, C9).

The provider reports ``spent`` per allocation, and an actor revoked and provisioned again has a new one.
Reconciliation used to compare the current allocation's ``spent`` with every charge of every job the actor
ever ran, so after a re-provision it never balanced again. The engine side now counts only the attempts
the reconciled allocation paid: each attempt records its allocation when it starts.

The default is the actor's current allocation. An earlier one is named by the caller, and must be one the
engine knows for that actor: the current one, or one that paid an attempt. The provider still reports
``spent`` for a revoked allocation, so an earlier one reconciles like the current one.

Which allocation paid each attempt is an optional read of the store's cost snapshot
(``AllocationCostSnapshot``). With a supplied store without it, every attempt of the actor is counted
against the current allocation, as before, and no other allocation is known.
"""

from collections.abc import Mapping, Sequence
from uuid import UUID

from livepeer_builder.contracts import AttemptCost, JobCost
from livepeer_builder.errors import NotFound
from livepeer_builder.protocols import AllocationCostSnapshot, CostSnapshot


class PaidAttempts:
    """The allocation that paid each attempt of an actor's jobs, read in the reconciliation's snapshot.
    Unknown (``None``) when the store's snapshot cannot say."""

    def __init__(self, by_attempt: Mapping[tuple[UUID, int], str] | None) -> None:
        self._by_attempt = by_attempt

    @classmethod
    async def read(cls, reads: CostSnapshot, application_id: str, actor_id: str | None) -> "PaidAttempts":
        if isinstance(reads, AllocationCostSnapshot):
            return cls(await reads.attempt_allocations(application_id, actor_id))
        return cls(None)

    def _known(self, current: str | None) -> set[str]:
        """Every allocation the engine knows for the actor: the current one and those that paid."""
        paid = set(self._by_attempt.values()) if self._by_attempt is not None else set()
        return paid | ({current} if current is not None else set())

    def reconciled(self, current: str | None, requested: str | None) -> str:
        """The allocation to reconcile: ``requested`` when it is one of the actor's, else the current one.
        An allocation that is not the actor's is not found, so no caller reads another's spend."""
        chosen = requested or current
        if chosen is None:
            raise NotFound("no allocation pays for that actor or application")
        if chosen not in self._known(current):
            raise NotFound(f"allocation {chosen!r} never paid for that actor or application")
        return chosen

    def others(self, current: str | None, reconciled: str) -> tuple[str, ...]:
        """The actor's other allocations, sorted: each reconciles by naming it."""
        return tuple(sorted(self._known(current) - {reconciled}))

    def by(self, allocation_id: str, cost: JobCost) -> Sequence[AttemptCost]:
        """The job's attempt costs that ``allocation_id`` paid; all of them when the store cannot say."""
        if self._by_attempt is None:
            return cost.attempts
        return [
            attempt
            for attempt in cost.attempts
            if self._by_attempt.get((cost.job_id, attempt.attempt_number)) == allocation_id
        ]
