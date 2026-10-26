"""DefaultPolicy: permissions plus ownership. Enterprises replace it through the AccessPolicy protocol."""

from livepeer_builder.contracts import ActorContext, Operation, ResourceRef
from livepeer_builder.errors import AccessDenied

ADMIN_OPERATIONS = frozenset(
    {Operation.ADMIN_KEYS, Operation.ADMIN_ALLOCATIONS, Operation.ADMIN_COSTS, Operation.ADMIN_READ}
)
# What an auditor may do: read, across every actor of its own application, and nothing else (D46).
AUDITOR_OPERATIONS = frozenset(
    {
        Operation.ADMIN_READ,
        Operation.DISCOVER,
        Operation.JOBS_READ,
        Operation.COSTS_READ,
        Operation.EVENTS_READ,
    }
)


class DefaultPolicy:
    """The engine's built-in AccessPolicy (docs/contracts/access.md#defaultpolicy).

    - Admin operations need an *admin credential*, not just a permission on an actor key.
    - An admin credential may act on any application's records.
    - An *auditor* credential only reads: admin reads, and every actor's records, in its own application.
    - Everyone else needs the operation's permission, and may touch only resources owned by
      their own (application_id, actor_id).
    """

    def check(self, actor: ActorContext, operation: Operation, resource: ResourceRef | None = None) -> None:
        if actor.credential_kind == "admin":
            return
        if actor.credential_kind == "auditor":
            if operation not in AUDITOR_OPERATIONS:
                raise AccessDenied(f"{operation} is not a read; an auditor credential only reads")
            if resource is not None and resource.application_id != actor.application_id:
                raise AccessDenied("resource belongs to another application")
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
