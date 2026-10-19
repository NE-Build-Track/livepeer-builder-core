"""DefaultPolicy: permissions plus ownership. Enterprises replace it through the AccessPolicy protocol."""

from livepeer_builder.contracts import ActorContext, Operation, ResourceRef
from livepeer_builder.errors import AccessDenied

ADMIN_OPERATIONS = frozenset({Operation.ADMIN_KEYS, Operation.ADMIN_ALLOCATIONS, Operation.ADMIN_COSTS})


class DefaultPolicy:
    """The engine's built-in AccessPolicy (docs/contracts/access.md#defaultpolicy).

    - Admin operations need an *admin credential*, not just a permission on an actor key.
    - An admin credential may act on any application's records.
    - Everyone else needs the operation's permission, and may touch only resources owned by
      their own (application_id, actor_id).
    """

    def check(self, actor: ActorContext, operation: Operation, resource: ResourceRef | None = None) -> None:
        if actor.credential_kind == "admin":
            return
        if operation in ADMIN_OPERATIONS:
            raise AccessDenied(f"{operation} requires an admin credential")
        if operation not in actor.permissions:
            raise AccessDenied(f"permission {operation!s} required")
        if resource is None:
            return
        if resource.application_id != actor.application_id:
            raise AccessDenied("resource belongs to another application")
        if resource.owner_actor_id is not None and resource.owner_actor_id != actor.actor_id:
            raise AccessDenied("resource belongs to another actor")
