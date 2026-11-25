"""The answer rule for one actor-owned record (D44, docs/contracts/access.md#answers).

A route checks the operation's permission first, before any lookup, so a missing permission is
``AccessDenied`` (403) and says nothing about any record. Ownership comes second: another actor's or
tenant's record reads exactly like a missing one, ``NotFound`` (404).

Who owns a record is the ``AccessPolicy``'s to say, with one exception that is the engine's own: the
bytes of a job's result go to the actor the job belongs to and to nobody else (``check_result_owner``).
"""

from livepeer_builder.contracts import ActorContext, Job, Operation, ResourceRef
from livepeer_builder.errors import AccessDenied, NotFound
from livepeer_builder.protocols import AccessPolicy


def check_owner(
    policy: AccessPolicy, actor: ActorContext, operation: Operation, resource: ResourceRef, missing: str
) -> None:
    """Ownership of a record that exists; a refusal is reported as ``NotFound(missing)``."""
    try:
        policy.check(actor, operation, resource)
    except AccessDenied as exc:
        raise NotFound(missing) from exc


def sees_every_actor(actor: ActorContext) -> bool:
    """An admin or an auditor reads every actor's records (a tenant admin's and an auditor's, of its
    own tenant)."""
    return actor.credential_kind in ("clearinghouse_admin", "tenant_admin", "auditor")


def job_resource(job: Job) -> ResourceRef:
    return ResourceRef(kind="job", tenant_id=job.tenant_id, owner_actor_id=job.actor_id, id=str(job.id))


def check_result_owner(actor: ActorContext, job: Job) -> None:
    """A job's result is its owner's alone: the actor it belongs to, in its tenant. Anybody else gets
    the ``NotFound`` of a job that is not theirs or does not exist, whatever their kind of credential.

    This is the engine's rule and not the policy's (docs/contracts/engine.md#results-of-an-async-job):
    an admin and an auditor read another actor's job as a record, and a supplied ``AccessPolicy`` may
    let anybody, but a result's bytes, and any URL inside them, are promised to the owner without
    condition. It is asked after the policy's own checks, so a policy can narrow it and cannot widen it."""
    if (actor.tenant_id, actor.actor_id) != (job.tenant_id, job.actor_id):
        raise NotFound(f"job {job.id} not found")
