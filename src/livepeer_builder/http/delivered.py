"""A delivered allocation as the HTTP answer: the one place its key is written out in plain.

``DeliveredAllocation`` holds the key as a ``SecretStr``, so a dump or log of it shows it masked. This body
exists only while a route builds its answer, and carries the key as text for the work engine to keep.
"""

from pydantic import Field

from livepeer_builder.contracts import ActorAllocation, DeliveredAllocation


class DeliveredAllocationBody(ActorAllocation):
    allocation_api_key: str | None = Field(default=None, repr=False)  # shown once; null when ``replayed``
    replayed: bool = False  # this answer carries no key
    resumed: bool = False  # this answer finishes a run an earlier attempt left unfinished


def delivered_body(answer: DeliveredAllocation) -> DeliveredAllocationBody:
    key = answer.allocation_api_key
    return DeliveredAllocationBody(
        **answer.model_dump(exclude={"allocation_api_key"}),
        allocation_api_key=key.get_secret_value() if key is not None else None,
    )
