"""The answer rule for one actor-owned record (D44, docs/contracts/access.md#answers).

A route checks the operation's permission first, before any lookup, so a missing permission is
``AccessDenied`` (403) and says nothing about any record. Ownership comes second: another actor's or
application's record reads exactly like a missing one, ``NotFound`` (404).
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
    """An admin or an auditor reads every actor's records (in an auditor's case, of its application)."""
    return actor.credential_kind in ("admin", "auditor")


def job_resource(job: Job) -> ResourceRef:
    return ResourceRef(
        kind="job", application_id=job.application_id, owner_actor_id=job.actor_id, id=str(job.id)
    )
