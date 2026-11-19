"""DefaultPolicy: permissions plus ownership. Enterprises replace it through the AccessPolicy protocol."""

from livepeer_builder.contracts import ActorContext, CredentialKind, Operation, ResourceRef
from livepeer_builder.errors import AccessDenied

ADMIN_OPERATIONS = frozenset(
    {
        Operation.ADMIN_KEYS,
        Operation.ADMIN_ALLOCATIONS,
        Operation.ADMIN_COSTS,
        Operation.ADMIN_READ,
        Operation.ADMIN_TENANTS,
    }
)
# What an auditor may do: read, across every actor of its own tenant, and nothing else (D46).
AUDITOR_OPERATIONS = frozenset(
    {
        Operation.ADMIN_READ,
        Operation.DISCOVER,
        Operation.JOBS_READ,
        Operation.COSTS_READ,
        Operation.EVENTS_READ,
    }
)
# What no tenant admin may do, in any tenant (#165). ``admin.tenants`` enlarges or stops a grant, and a
# tenant admin only divides its own. Cost sync reads the usage of every tenant, and ``actors.assert`` is
# a service credential's.
BEYOND_A_TENANT = frozenset({Operation.ADMIN_TENANTS, Operation.ADMIN_COSTS, Operation.ACTORS_ASSERT})
# The writes a tenant admin makes only on a resource that names its tenant.
TENANT_WRITES = frozenset({Operation.ADMIN_KEYS, Operation.ADMIN_ALLOCATIONS})
# The kinds of key a tenant admin never issues, rotates or revokes.
ADMIN_KINDS: frozenset[CredentialKind] = frozenset({"clearinghouse_admin", "tenant_admin"})


class DefaultPolicy:
    """The engine's built-in AccessPolicy (docs/contracts/access.md#defaultpolicy).

    - Admin operations need an *admin credential*, not just a permission on an actor key.
    - A *clearinghouse admin* credential may act on any tenant's records.
    - A *tenant admin* credential is an admin of its own tenant and of no other: it divides its
      tenant's grant and cannot enlarge it, and it changes no admin key.
    - An *auditor* credential only reads: admin reads, and every actor's records, in its own tenant.
    - Everyone else needs the operation's permission, and may touch only resources owned by
      their own (tenant_id, actor_id).
    """

    def check(self, actor: ActorContext, operation: Operation, resource: ResourceRef | None = None) -> None:
        if actor.credential_kind == "clearinghouse_admin":
            return
        if actor.credential_kind == "tenant_admin":
            _check_tenant_admin(actor, operation, resource)
            return
        if actor.credential_kind == "auditor":
            if operation not in AUDITOR_OPERATIONS:
                raise AccessDenied(f"{operation} is not a read; an auditor credential only reads")
            if resource is not None and resource.tenant_id != actor.tenant_id:
                raise AccessDenied("resource belongs to another tenant")
            return
        if operation in ADMIN_OPERATIONS:
            raise AccessDenied(f"{operation} requires an admin credential")
        if operation not in actor.permissions:
            raise AccessDenied(f"permission {operation!s} required")
        if resource is None:
            return
        if resource.tenant_id != actor.tenant_id:
            raise AccessDenied("resource belongs to another tenant")
        if resource.owner_actor_id is not None and resource.owner_actor_id != actor.actor_id:
            raise AccessDenied("resource belongs to another actor")


def _check_tenant_admin(actor: ActorContext, operation: Operation, resource: ResourceRef | None) -> None:
    """A tenant admin is confined here, and not by each route (#165): every write names the tenant it
    acts on, and one that names another tenant, or none, is refused before any lookup."""
    if operation in BEYOND_A_TENANT:
        raise AccessDenied(f"{operation} requires a clearinghouse admin credential")
    if resource is None:
        if operation in TENANT_WRITES:
            raise AccessDenied(f"{operation} names no tenant; a tenant admin acts on its own tenant only")
        return
    if resource.tenant_id != actor.tenant_id:
        raise AccessDenied("resource belongs to another tenant")
    if operation == Operation.ADMIN_KEYS and resource.credential_kind in ADMIN_KINDS:
        raise AccessDenied(
            f"a tenant admin does not issue, rotate or revoke a {resource.credential_kind} key"
        )
