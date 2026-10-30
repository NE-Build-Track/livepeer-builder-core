"""Services defined in M2 and implemented later, or held by a decision. They fail loudly, never silently."""

from uuid import UUID

from livepeer_builder.contracts import ActorContext, EventPage, Session, SessionRequest
from livepeer_builder.errors import NotImplementedInMilestone


class ReservedEventFeed:
    """EventFeed placeholder: versioned events with a retention window and 410 on an expired cursor.
    Held, with no milestone: the M3 scope decision of 21 October 2026 (item 4), kept for M3 by D60."""

    async def after(self, actor: ActorContext, cursor: int, limit: int = 500) -> EventPage:
        del actor, cursor, limit
        raise NotImplementedInMilestone(
            "the engine events feed",
            "held",
            "ISS-18",
            held_by="the M3 scope decision of 21 October 2026 (item 4, D60)",
        )


class ReservedSessions:
    """SessionManager placeholder: engine-proxied persistent sessions (M4, ISS-27)."""

    async def open(self, actor: ActorContext, request: SessionRequest) -> Session:
        del actor, request
        raise NotImplementedInMilestone("persistent sessions", "M4", "ISS-27")

    async def close(self, actor: ActorContext, session_id: UUID) -> Session:
        del actor, session_id
        raise NotImplementedInMilestone("persistent sessions", "M4", "ISS-27")
