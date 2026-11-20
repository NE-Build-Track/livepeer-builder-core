"""The kind ``tenant_admin`` in ``DefaultPolicy`` (#165, D83): an admin of one tenant.

A tenant admin is confined by the policy, not by each route: every write names the tenant it acts on, and
the policy refuses one that names another tenant, or none. It divides its tenant's grant and cannot
enlarge it, and it changes no admin key. The other kinds are as they were.
"""

import pytest

from livepeer_builder.contracts import ActorContext, CredentialKind, Operation, ResourceRef
from livepeer_builder.errors import AccessDenied
from livepeer_builder.services.ownership import sees_every_actor
from livepeer_builder.services.policy import ADMIN_OPERATIONS, DefaultPolicy

POLICY = DefaultPolicy()
TENANT_ADMIN = ActorContext(actor_id="tenant-admin", tenant_id="t-1", credential_kind="tenant_admin")
OWN = ResourceRef(kind="allocation", tenant_id="t-1", owner_actor_id="alice")
OTHER = ResourceRef(kind="allocation", tenant_id="t-2", owner_actor_id="alice")
BEYOND = [Operation.ADMIN_TENANTS, Operation.ADMIN_COSTS, Operation.ACTORS_ASSERT]
WITHIN = [operation for operation in Operation if operation not in BEYOND]


def allowed(actor: ActorContext, operation: Operation, resource: ResourceRef | None) -> bool:
    try:
        POLICY.check(actor, operation, resource)
    except AccessDenied:
        return False
    return True


@pytest.mark.parametrize("operation", WITHIN)
def test_a_tenant_admin_does_in_its_own_tenant_what_an_admin_does_and_nothing_in_another(
    operation: Operation,
) -> None:
    assert allowed(TENANT_ADMIN, operation, OWN)
    assert not allowed(TENANT_ADMIN, operation, OTHER)


@pytest.mark.parametrize("operation", BEYOND)
@pytest.mark.parametrize("resource", [None, OWN, OTHER, ResourceRef(kind="tenant", tenant_id="t-1")])
def test_what_reaches_beyond_one_tenant_is_a_clearinghouse_admins_alone(
    operation: Operation, resource: ResourceRef | None
) -> None:
    """Onboarding, the tenant list, a grant's top-up and a tenant's status (``admin.tenants``) enlarge or
    stop a grant; cost sync reads every tenant's usage; ``actors.assert`` is a service credential's."""
    assert not allowed(TENANT_ADMIN, operation, resource)
    everywhere = ActorContext(actor_id="operator", credential_kind="clearinghouse_admin")
    assert allowed(everywhere, operation, resource)


@pytest.mark.parametrize("operation", [Operation.ADMIN_KEYS, Operation.ADMIN_ALLOCATIONS])
def test_a_write_that_names_no_tenant_is_refused_to_a_tenant_admin(operation: Operation) -> None:
    """Fail closed: a command that forgot to name its tenant would let the path or the body choose it."""
    with pytest.raises(AccessDenied, match="names no tenant"):
        POLICY.check(TENANT_ADMIN, operation)
    assert allowed(TENANT_ADMIN, Operation.ADMIN_READ, None)  # a read is confined by its ownership check
    assert allowed(TENANT_ADMIN, Operation.JOBS_READ, None)


@pytest.mark.parametrize(
    ("kind", "may"),
    [("auditor", True), ("actor", True), ("service", True), ("tenant_admin", False), ("clearinghouse_admin", False)],
)  # fmt: skip
def test_a_tenant_admin_changes_no_admin_key(kind: CredentialKind, may: bool) -> None:
    key = ResourceRef(kind="key", tenant_id="t-1", credential_kind=kind)
    assert allowed(TENANT_ADMIN, Operation.ADMIN_KEYS, key) is may
    assert not allowed(TENANT_ADMIN, Operation.ADMIN_KEYS, key.model_copy(update={"tenant_id": "t-2"}))
    assert allowed(TENANT_ADMIN, Operation.ADMIN_READ, key)  # it reads its tenant's keys, of every kind


def test_the_other_kinds_are_as_they_were() -> None:
    assert Operation.ADMIN_TENANTS in ADMIN_OPERATIONS
    auditor = ActorContext(actor_id="dashboard", tenant_id="t-1", credential_kind="auditor")
    actor = ActorContext(
        actor_id="alice", tenant_id="t-1", permissions=frozenset(operation.value for operation in Operation)
    )
    for operation in ADMIN_OPERATIONS - {Operation.ADMIN_READ}:
        assert not allowed(auditor, operation, OWN)  # an auditor writes nothing
    for operation in ADMIN_OPERATIONS:
        assert not allowed(actor, operation, OWN)  # a permission string is not an admin credential
    assert allowed(auditor, Operation.ADMIN_READ, OWN) and not allowed(auditor, Operation.ADMIN_READ, OTHER)
    assert allowed(actor, Operation.JOBS_RUN, OWN) and not allowed(actor, Operation.JOBS_RUN, OTHER)


def test_a_tenant_admin_reads_every_actor_of_its_tenant() -> None:
    assert sees_every_actor(TENANT_ADMIN)
