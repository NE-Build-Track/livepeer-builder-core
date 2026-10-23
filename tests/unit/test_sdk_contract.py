"""The SDK ships no type information, so mypy cannot hold it to the adapter's Protocols.

These tests record the keyword arguments the adapter really passes and check that the real SDK
functions accept every one, so an SDK release that renames or removes one fails here instead of at
the first paid call.
"""

import inspect
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from livepeer_gateway.discovery import discover_runners
from livepeer_gateway.live_runner import call_runner

from livepeer_builder.adapters.sdk import SdkRunnerTransport, SignerDiscoverySource
from livepeer_builder.contracts import JobRequest
from livepeer_builder.testing import runner as make_runner


def _unaccepted(function: Callable[..., Any], names: set[str]) -> set[str]:
    """The names ``function`` does not accept as keyword arguments."""
    kinds = (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    accepted = {n for n, p in inspect.signature(function).parameters.items() if p.kind in kinds}
    return names - accepted


class _Recorder:
    def __init__(self, result: object) -> None:
        self.result = result
        self.keywords: list[set[str]] = []

    async def __call__(self, *args: object, **kwargs: object) -> object:
        self.keywords.append(set(kwargs))
        return self.result


async def test_call_runner_accepts_every_keyword_the_transport_passes() -> None:
    reply = SimpleNamespace(content=b"{}", data={}, content_type="application/json")
    stream = SimpleNamespace(status=200, content_type="text/event-stream")
    calls, opens = _Recorder(reply), _Recorder(stream)
    runner = make_runner()
    await SdkRunnerTransport("https://signer.example", call=calls).call(
        runner, JobRequest(app=runner.app), "lpg_k"
    )
    await SdkRunnerTransport("https://signer.example", call=opens).open(
        runner, JobRequest(app=runner.app), "lpg_k"
    )
    passed = calls.keywords[0] | opens.keywords[0]
    assert "stream" in opens.keywords[0]  # the streaming overload is exercised too
    assert _unaccepted(call_runner, passed) == set()


async def test_discover_runners_accepts_every_keyword_the_source_passes() -> None:
    recorder = _Recorder([])
    await SignerDiscoverySource(signer_url="https://signer.example", discover=recorder).fetch()
    assert _unaccepted(discover_runners, recorder.keywords[0]) == set()
