"""The SDK pieces the engine still calls, and the ones it copied (D53).

The SDK ships no type information, so mypy cannot hold it to the adapters. These tests check that the SDK
functions the engine calls accept what it passes, and fail when the pinned SDK's own ``call_runner`` path
changes: adapters/runner_call.py is the engine's copy of it, so every pin bump reviews that copy.
"""

import hashlib
import inspect
from collections.abc import Callable
from typing import Any

import pytest
from livepeer_gateway import http, live_runner
from livepeer_gateway.discovery import discover_runners
from livepeer_gateway.remote_signer import (
    GetPaymentResponse,
    LivePaymentChallenge,
    LivePaymentSession,
    get_signer_info,
)

from livepeer_builder.adapters import runner_call
from livepeer_builder.adapters.sdk import HttpDiscoverySource

COPIED_AT = "2017a80"
# The first 16 hex digits of each function's source sha256 at COPIED_AT. When one fails: read the SDK
# diff, carry what matters into adapters/runner_call.py, then update the digest and COPIED_AT.
COPIED = {
    "live_runner.call_runner": "deff844bd224b1a6",
    "live_runner._parse_runner_payment_challenge": "379451580ba148b3",
    "live_runner._runner_payment_type": "19d390438f197d34",
    "live_runner._pad_runner_price": "6d9b23d21957da77",
    "live_runner._start_funding": "109226506dcb7277",
    "live_runner._stop_funding": "f518f7d17f4b7b83",
    "live_runner._is_json_content_type": "5e8dad829aed9d1f",
    "http._open_request": "c523ab00c1072621",
    "http._request_parts": "9b572a482cc870b0",
}
_MODULES = {"live_runner": live_runner, "http": http}


def _accepts(function: Callable[..., Any], *keywords: str, positional: int = 0) -> bool:
    parameters = list(inspect.signature(function).parameters.values())
    names = {p.name for p in parameters if p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)}
    takes_positional = [p for p in parameters if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return set(keywords) <= names and len(takes_positional) >= positional


@pytest.mark.parametrize("name", sorted(COPIED))
def test_the_copied_sdk_call_path_is_unchanged(name: str) -> None:
    module, function = name.split(".")
    source = inspect.getsource(getattr(_MODULES[module], function))
    digest = hashlib.sha256(source.encode()).hexdigest()[:16]
    assert digest == COPIED[name], (
        f"livepeer_gateway {name} changed since {COPIED_AT}: review the diff, update "
        "adapters/runner_call.py to match, then update its digest in this test"
    )


def test_the_copied_protocol_constants_match() -> None:
    assert runner_call._PAYMENT_TYPES_BY_UNIT == live_runner._RUNNER_PAYMENT_TYPES_BY_UNIT
    assert runner_call.PAYER_ADDRESS_HEADER == live_runner._LIVE_RUNNER_PAYER_ADDRESS_HEADER


def test_the_signer_client_accepts_what_the_call_path_passes() -> None:
    assert _accepts(
        LivePaymentSession, "signer_headers", "type", "challenge", "app", "max_price", positional=1
    )
    assert _accepts(get_signer_info, positional=2)
    assert _accepts(LivePaymentChallenge, "payment_params", "manifest_id", "payment_url")
    assert {"payment", "seg_creds"} <= set(inspect.signature(GetPaymentResponse).parameters)
    for method in ("get_payment", "run_payments"):
        assert inspect.iscoroutinefunction(getattr(LivePaymentSession, method))
    assert isinstance(inspect.getattr_static(LivePaymentSession, "manifest_id"), property)


async def test_discover_runners_accepts_every_keyword_the_source_passes() -> None:
    seen: list[set[str]] = []

    async def recorder(**kwargs: object) -> list[dict[str, Any]]:
        seen.append(set(kwargs))
        return []

    await HttpDiscoverySource("http://discovery.example/discovery", discover=recorder).fetch()
    assert _accepts(discover_runners, *seen[0])
