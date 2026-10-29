"""The SDK pieces the engine still calls, and the ones it copied (D53, D58).

The SDK ships no type information, so mypy cannot hold it to the adapters. These tests check that the SDK
functions the engine calls accept what it passes, and fail when the pinned SDK's own ``call_runner`` path
or its payment session changes: adapters/runner_call.py is the engine's copy of the first, and
adapters/payment_funding.py overrides the session's renewal and refresh and replaces its loop, so every
pin bump reviews both.
"""

import hashlib
import inspect
from collections.abc import Callable
from typing import Any

import pytest
from livepeer_gateway import http, live_runner, remote_signer
from livepeer_gateway.discovery import discover_runners
from livepeer_gateway.remote_signer import (
    GetPaymentResponse,
    LivePaymentChallenge,
    LivePaymentSession,
    get_signer_info,
)

from livepeer_builder.adapters import payment_funding, runner_call
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
    # D58: EnginePaymentSession overrides send_payment and _refresh_payment_params and fund() replaces
    # run_payments; get_payment calls the refresh, __init__ sets what the overrides read, and renewal
    # errors are raised by _raise_http_json_error.
    "remote_signer.LivePaymentSession.__init__": "a16e3f978e6a251d",
    "remote_signer.LivePaymentSession.get_payment": "fcf2e8befd22498d",
    "remote_signer.LivePaymentSession.send_payment": "de84259d1c7cfe55",
    "remote_signer.LivePaymentSession.run_payments": "e6692417a9cffb83",
    "remote_signer.LivePaymentSession._refresh_payment_params": "b7a2a53c68f02338",
    "http._raise_http_json_error": "5baaf95e296f86bb",
}
_MODULES = {"live_runner": live_runner, "http": http, "remote_signer": remote_signer}


def _accepts(function: Callable[..., Any], *keywords: str, positional: int = 0) -> bool:
    parameters = list(inspect.signature(function).parameters.values())
    names = {p.name for p in parameters if p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)}
    takes_positional = [p for p in parameters if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return set(keywords) <= names and len(takes_positional) >= positional


@pytest.mark.parametrize("name", sorted(COPIED))
def test_the_copied_sdk_call_path_is_unchanged(name: str) -> None:
    module, path = name.split(".", 1)
    target: Any = _MODULES[module]
    for part in path.split("."):  # "Class.method" too
        target = getattr(target, part)
    source = inspect.getsource(target)
    digest = hashlib.sha256(source.encode()).hexdigest()[:16]
    assert digest == COPIED[name], (
        f"livepeer_gateway {name} changed since {COPIED_AT}: review the diff, update "
        "adapters/runner_call.py to match, then update its digest in this test"
    )


@pytest.mark.parametrize("copy", [runner_call, payment_funding], ids=lambda module: module.__name__)
def test_the_copy_keeps_the_sdk_s_mit_notice(copy: Any) -> None:
    """MIT asks every copy of a substantial portion to carry the copyright and permission notice."""
    source = inspect.getsource(copy)
    assert "Copyright (c) 2026 Livepeer Foundation" in source
    assert "The above copyright notice and this permission notice shall be included in all" in source


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
