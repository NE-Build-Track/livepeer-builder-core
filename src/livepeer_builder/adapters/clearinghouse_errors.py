"""What the remote adapters say when a command at a payment clearinghouse cannot be reported as done, or
is not theirs to send (#168): one error for each case, with what happened and what to do next.

Split from ``clearinghouse.py``, which holds the adapters and decides which case an answer is.
"""

from typing import NoReturn

from livepeer_builder.errors import AccessDenied, InvalidRequest, ProviderError


def never_arrived(actor_id: str, allocation_id: str, *, rotation: bool) -> InvalidRequest:
    """The refusal of a command the clearinghouse finished before and answers without the key (D87 point
    1). A refusal, so a first run frees its reference: this engine applied nothing. A resumed run never
    frees its own (``services/ledger.py``), so the record of a lost answer stays ``started``."""
    lost = (
        f"the clearinghouse finished this {'rotation' if rotation else 'allotment'} before (allocation "
        f"{allocation_id!r}) and handed its key to an answer that never reached this engine. It hands a "
        "key over once and keeps none, so that key is lost"
    )
    if rotation:
        return InvalidRequest(
            f"{lost}. A lost key is replaced by a rotation: rotate the payment key of actor {actor_id!r} "
            "again, under a new operation_ref"
        )
    return InvalidRequest(
        f"{lost}, and this engine has no record of actor {actor_id!r} to rotate it from. Revoke the "
        "allocation at the clearinghouse, which returns what it has not spent to the grant, then allot "
        "again here under a new operation_ref"
    )


def acted_elsewhere(actor_id: str, named: str, acted_on: str, did: str, then: str) -> ProviderError:
    """What a command the clearinghouse applied to another allocation comes to: which allocation was
    named, which one was acted on, that nothing is recorded here, and what to do next (``then``)."""
    return ProviderError(
        200,
        f"the clearinghouse {did} allocation {acted_on!r}, which actor {actor_id!r} holds there now, and "
        f"not {named!r}, which this command named: the actor was revoked and allotted again at the "
        f"clearinghouse while the command was on its way. Nothing of it is recorded here. {then}",
    )


def not_held(actor_id: str, allocation_id: str) -> ProviderError:
    return ProviderError(
        404, f"the clearinghouse holds another allocation for actor {actor_id!r}, not {allocation_id!r}"
    )


def clearinghouse_admin_only(what: str) -> NoReturn:
    raise AccessDenied(
        f"only a clearinghouse admin {what}, at the clearinghouse: this engine holds a tenant admin key"
    )


def needs_the_actor(member: str) -> NoReturn:
    raise InvalidRequest(
        f"{member} needs the actor at a clearinghouse, whose routes name one: call it on "
        "for_actor(tenant_id, actor_id)"
    )
