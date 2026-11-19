"""What a route adds to its answer: the job's id as a header, and the mark of a repeated command."""

from fastapi import Response

from livepeer_builder.contracts import OPERATION_REPLAYED

JOB_ID_HEADER = "Livepeer-Job-Id"


def replay[T](response: Response, answer: T) -> T:
    """Mark an admin command answered from the operation ledger: ``Idempotent-Replayed: true`` (D57)."""
    if OPERATION_REPLAYED.get():
        response.headers["Idempotent-Replayed"] = "true"
    return answer
