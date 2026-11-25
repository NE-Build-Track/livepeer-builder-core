"""ISS-26: the settings of async jobs and their defaults, locked by D69 (6): 4 workers, 1000 queued, an hour
in the queue, 16 MiB a stored result, results kept 7 days."""

import pytest
from pydantic import ValidationError

from livepeer_builder import EngineSettings

DISCOVERY = {"LPB_DISCOVERY_URL": "http://127.0.0.1:9/discovery"}


def test_the_defaults_are_the_locked_ones() -> None:
    settings = EngineSettings.from_env(DISCOVERY)
    assert (
        settings.async_workers,
        settings.async_max_queued,
        settings.async_queue_ttl_s,
        settings.async_result_max_bytes,
        settings.retention_results_days,
    ) == (4, 1000, 3600.0, 16 * 1024 * 1024, 7)
    assert (settings.async_poll_s, settings.async_drain_timeout_s) == (1.0, 30.0)


def test_each_is_read_from_its_variable() -> None:
    settings = EngineSettings.from_env(
        DISCOVERY
        | {
            "LPB_ASYNC_WORKERS": "2",
            "LPB_ASYNC_MAX_QUEUED": "50",
            "LPB_ASYNC_QUEUE_TTL_S": "600",
            "LPB_ASYNC_RESULT_MAX_BYTES": "1048576",
            "LPB_ASYNC_POLL_S": "0.5",
            "LPB_ASYNC_DRAIN_TIMEOUT_S": "5",
            "LPB_RETENTION_RESULTS_DAYS": "30",
        }
    )
    assert (
        settings.async_workers,
        settings.async_max_queued,
        settings.async_queue_ttl_s,
        settings.async_result_max_bytes,
        settings.async_poll_s,
        settings.async_drain_timeout_s,
        settings.retention_results_days,
    ) == (2, 50, 600.0, 1048576, 0.5, 5.0, 30)


@pytest.mark.parametrize(
    "variable",
    [
        "LPB_ASYNC_WORKERS",
        "LPB_ASYNC_MAX_QUEUED",
        "LPB_ASYNC_QUEUE_TTL_S",
        "LPB_ASYNC_RESULT_MAX_BYTES",
        "LPB_ASYNC_POLL_S",
        "LPB_RETENTION_RESULTS_DAYS",
    ],
)
def test_zero_is_refused(variable: str) -> None:
    with pytest.raises(ValidationError):
        EngineSettings.from_env(DISCOVERY | {variable: "0"})
