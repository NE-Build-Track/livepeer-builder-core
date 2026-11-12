"""People an identity provider signs in become known actors before any allocation (#52, D48).

Under JWT bearer an actor is the provider's immutable subject (``sub``), which an operator cannot guess.
``SightingRecorder`` wraps the engine's authenticator chain: when a credential from an identity provider
names a builder (an actor credential, not an operator or the console's reader, and not an engine key,
whose actors the key list already shows), it records that actor with the provider's display name. The
operator's actor list then shows "alice, not provisioned" and provisions her by name.

A request writes nothing after the first: one write per person per process, then one an hour to move
``last_seen_at``, and one at once when the name changes. Recording never fails a request.
"""

import logging
from typing import Protocol

from livepeer_builder.contracts import ActorContext, AuthRequest, SeenActor
from livepeer_builder.protocols import Authenticator, Clock

_LOG = logging.getLogger(__name__)

REFRESH_AFTER_S = 3600.0


class SightingStore(Protocol):
    async def save_sighting(self, seen: SeenActor) -> None: ...


def is_person(actor: ActorContext) -> bool:
    """A builder an identity provider vouched for: the people the operator's list needs to learn about."""
    return actor.credential_kind == "actor" and actor.auth is not None and actor.auth.method != "api_key"


class SightingRecorder:
    def __init__(
        self,
        inner: Authenticator,
        store: SightingStore,
        clock: Clock,
        *,
        refresh_after_s: float = REFRESH_AFTER_S,
    ) -> None:
        self._inner = inner
        self._store = store
        self._clock = clock
        self._refresh_after_s = refresh_after_s
        self._recorded: dict[tuple[str, str], tuple[str, float]] = {}  # (application, actor) -> (name, when)

    async def authenticate(self, request: AuthRequest) -> ActorContext | None:
        actor = await self._inner.authenticate(request)
        if actor is not None and is_person(actor):
            await self._record(actor)
        return actor

    def challenge(self) -> str | None:
        return self._inner.challenge()

    async def _record(self, actor: ActorContext) -> None:
        key = (actor.application_id, actor.actor_id)
        name = actor.display_name or ""
        now = self._clock.now()
        recorded = self._recorded.get(key)
        if (
            recorded is not None
            and recorded[0] == name
            and now.timestamp() - recorded[1] < self._refresh_after_s
        ):
            return
        seen = SeenActor(
            application_id=actor.application_id,
            actor_id=actor.actor_id,
            name=name,
            first_seen_at=now,  # the store keeps the first one
            last_seen_at=now,
        )
        try:
            await self._store.save_sighting(seen)
        except Exception as exc:  # the request goes on: an unrecorded sighting only delays the list
            _LOG.warning(
                "an actor's sighting could not be recorded",
                extra={
                    "event": "actors.sighting_failed",
                    "actor_id": actor.actor_id,
                    "error": type(exc).__name__,
                },
            )
            return
        self._recorded[key] = (name, now.timestamp())
