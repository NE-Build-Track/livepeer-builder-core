"""The handles of a job that outlives its request (D69, ISS-26): the answer to an async submit, the
stored result, and the cancel of a job that is still queued. Work routes, typed against ``WorkApi``.
They are part of the work half's one group: ``mount_work_routes`` adds them with the rest
(``http/work.py``), so they are served wherever the job routes are and are no group of their own in
``http/listeners.py``.

The answers: a submit is 202 with the queued job, ``Location`` and ``Livepeer-Job-Id``, or 200 with
``Idempotent-Replayed: true`` when the reference already named a job. The result is the stored bytes
with their content type; 409 ``job_not_finished`` with ``Retry-After`` while the job has not ended; the
failure ``POST /v1/jobs`` would have answered for a failed or uncertain job; 410 ``result_expired`` for
a result the engine no longer holds, or never held (an immediate job's). A cancel is 200 with the job,
or 409 ``job_not_queued``. The error answers are built in ``http/app.py``.
"""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse, Response

from livepeer_builder.api import WorkApi
from livepeer_builder.contracts import ActorContext, Job, JobRequest
from livepeer_builder.http.auth import actor_dependency
from livepeer_builder.http.replies import JOB_ID_HEADER, replayed

JOB_LOCATION = "/v1/jobs/{job_id}"  # where a submitted job is read back
POLL_AFTER_S = "1"  # Retry-After on a job that has not ended: polling is how a caller learns its end


async def submitted(engine: WorkApi, actor: ActorContext, request: JobRequest) -> Response:
    """Queue the job and answer at once: 202, or 200 for a repeat that named an existing job."""
    job = await engine.jobs.submit(actor, request)
    replay = replayed()
    headers = {"Location": JOB_LOCATION.format(job_id=job.id), JOB_ID_HEADER: str(job.id), **replay}
    return JSONResponse(job.model_dump(mode="json"), status_code=200 if replay else 202, headers=headers)


def add_job_handle_routes(app: FastAPI, engine: WorkApi) -> None:
    """Add the result route and the cancel route to ``app``."""
    Actor = Annotated[ActorContext, Depends(actor_dependency(engine))]

    @app.get("/v1/jobs/{job_id}/result", status_code=200)
    async def get_result(actor: Actor, job_id: UUID) -> Response:
        result = await engine.jobs.result(actor, job_id)
        # as the job route: the stored content type as a header, since media_type would add a charset (C27)
        return Response(content=result.content, headers={"Content-Type": result.content_type})

    @app.post("/v1/jobs/{job_id}/cancel")
    async def cancel_job(actor: Actor, job_id: UUID) -> Job:
        return await engine.jobs.cancel(actor, job_id)
