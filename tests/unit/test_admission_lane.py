"""ISS-26: the one gate an async job passes before it is claimed (``AdmissionLimiter.async_lane``).

The worker's cap (``LPB_ASYNC_WORKERS``), the engine's admission slot, the job's own id and "closed" are
checked and taken in one call that never awaits, and given back in one place. So two steps of the
worker that overlap cannot both take the last worker, cannot both hold one job, and take nothing once
the engine has begun to stop.
"""

from uuid import uuid4

import pytest

from livepeer_builder.errors import NoCapacity
from livepeer_builder.services.admission import AdmissionLimiter

APP, OTHER = "example/echo", "example/other"


def test_a_reservation_takes_a_worker_and_an_admission_slot_and_gives_both_back_once() -> None:
    admission = AdmissionLimiter(None, None, async_workers=2)
    lane = admission.async_lane
    assert (lane.room, admission.running) == (2, 0)

    slot = lane.reserve(APP, uuid4())
    assert slot is not None
    assert (lane.room, admission.running) == (1, 1)
    slot.release()
    slot.release()  # the end of a job and the stop of the engine may both give it back: once counts
    assert (lane.room, admission.running) == (2, 0)
    assert admission.full_apps() == frozenset()


def test_the_worker_cap_is_checked_where_the_slot_is_taken() -> None:
    """With one worker, a second reservation is refused though the engine has room: no step can read
    "a worker is free" and act on it after another step took that worker."""
    admission = AdmissionLimiter(10, None, async_workers=1)
    lane = admission.async_lane
    held = lane.reserve(APP, uuid4())
    assert held is not None

    assert lane.reserve(OTHER, uuid4()) is None
    assert (lane.room, admission.running) == (0, 1)
    held.release()
    assert lane.reserve(OTHER, uuid4()) is not None


def test_a_job_is_held_by_one_reservation_at_a_time() -> None:
    lane = AdmissionLimiter(None, None, async_workers=4).async_lane
    job_id = uuid4()
    held = lane.reserve(APP, job_id)
    assert held is not None and lane.holds(job_id)

    assert lane.reserve(APP, job_id) is None  # another step read the same page of the queue
    held.release()
    assert not lane.holds(job_id)
    assert lane.reserve(APP, job_id) is not None


def test_the_lane_is_inside_the_engines_admission_limits() -> None:
    admission = AdmissionLimiter(2, 1, async_workers=4)
    lane = admission.async_lane
    assert lane.room == 2  # never more than the engine has room for
    first = lane.reserve(APP, uuid4())
    assert first is not None
    assert lane.reserve(APP, uuid4()) is None  # the app is at its limit
    assert admission.full_apps() == frozenset({APP})
    second = lane.reserve(OTHER, uuid4())
    assert second is not None
    assert lane.room == 0 and lane.reserve("example/third", uuid4()) is None  # the engine is full
    first.release()
    assert lane.room == 1


async def test_an_immediate_job_and_an_async_one_share_the_engines_slots() -> None:
    admission = AdmissionLimiter(1, None, async_workers=4)
    async with admission.admit(APP):
        assert admission.async_lane.room == 0
        assert admission.async_lane.reserve(APP, uuid4()) is None
    slot = admission.async_lane.reserve(APP, uuid4())
    assert slot is not None
    with pytest.raises(NoCapacity):
        async with admission.admit(APP):
            pass
    slot.release()


async def test_a_closed_lane_refuses_every_claim_and_admits_immediate_jobs_as_before() -> None:
    admission = AdmissionLimiter(None, None, async_workers=4)
    lane = admission.async_lane
    held = lane.reserve(APP, uuid4())
    assert held is not None and not lane.closed

    lane.close()

    assert lane.closed and lane.room == 0
    assert lane.reserve(APP, uuid4()) is None
    held.release()  # what was held before is still given back
    assert admission.running == 0
    async with admission.admit(APP):  # the lane is the async worker's: nothing else is closed
        assert admission.running == 1
