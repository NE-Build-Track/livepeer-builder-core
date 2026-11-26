"""The catalog and the allowance of ``FakeEngine``: what it is given, behind the engine's policy checks."""

from collections.abc import Iterable

from livepeer_builder.contracts import ActorContext, Allowance, Offering, Operation, ResourceRef, Runner
from livepeer_builder.contracts.discovery import is_kept
from livepeer_builder.errors import NotProvisioned
from livepeer_builder.protocols import AccessPolicy
from livepeer_builder.services.policy import DefaultPolicy


class FakeDiscovery:
    """The offerings as given, and of the runners given the ones an engine keeps (``is_kept``, #158):
    an engine lists no runner whose text, URL or ``app`` breaks the discovery contract's rules,
    whichever source handed it over, so the fake lists none either."""

    def __init__(
        self, offerings: Iterable[Offering], runners: Iterable[Runner], policy: AccessPolicy | None = None
    ) -> None:
        self._offerings = list(offerings)
        self._runners = [runner for runner in runners if is_kept(runner)]
        self._policy = policy or DefaultPolicy()

    def offerings(self, *, app: str | None = None) -> list[Offering]:
        return [offering for offering in self._offerings if app is None or offering.app == app]

    def runners(self, actor: ActorContext, app: str) -> list[Runner]:
        self._policy.check(actor, Operation.DISCOVER)
        return self.candidates(app)

    def candidates(self, app: str) -> list[Runner]:
        return [runner for runner in self._runners if runner.app == app]


class FakeAllowance:
    def __init__(self, allowance: Allowance | None, policy: AccessPolicy | None = None) -> None:
        self._allowance = allowance
        self._policy = policy or DefaultPolicy()

    async def allowance(  # NOSONAR - async by protocol (AllowanceApi); the fake awaits nothing
        self, actor: ActorContext, actor_id: str | None = None
    ) -> Allowance:
        target = actor_id or actor.actor_id
        owner = ResourceRef(kind="allocation", tenant_id=actor.tenant_id, owner_actor_id=target)
        self._policy.check(actor, Operation.COSTS_READ, owner)  # as the engine, before any lookup
        if self._allowance is None:
            raise NotProvisioned("no allowance scripted")
        return self._allowance
