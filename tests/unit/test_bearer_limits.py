"""#113 C22: an oversized or malformed bearer is a 401, never a 500.

A deeply nested JSON segment made ``json.loads`` raise ``RecursionError`` in the JWT authenticator's
unverified read and in PyJWT's header read, which no handler caught: a 500, with no credential needed.
Both now treat it like any other unreadable token. Separately, the engine refuses a bearer longer than
16 KiB before any authenticator sees it, the API-key path and supplied authenticators included. The cap
is a fixed constant, not a setting.
"""

import base64
import json

import httpx
import pytest

from livepeer_builder.adapters.auth import MAX_BEARER_LENGTH, AuthenticatorChain
from livepeer_builder.adapters.jwt import JwksCache, JwtBearerAuthenticator, JwtSettings
from livepeer_builder.contracts import ActorContext, AuthRequest
from livepeer_builder.errors import NotAuthenticated
from tests.conftest import EngineFactory

ISSUER = "https://idp.example.com/"
NESTED = b"[" * 12000  # deep enough for RecursionError on Python 3.12, and still under the cap once encoded


def segment(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def bearer(token: str) -> AuthRequest:
    return AuthRequest(method="GET", path="/v1/jobs", headers={"authorization": f"Bearer {token}"})


class Recording:
    """A supplied authenticator: records every credential it is shown."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def authenticate(self, request: AuthRequest) -> ActorContext | None:
        self.seen.append(request.header("authorization") or "")
        return ActorContext(actor_id="someone")

    def challenge(self) -> str | None:
        return None


def jwt_authenticator() -> JwtBearerAuthenticator:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no key is fetched for an unreadable token: {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(unreachable))
    return JwtBearerAuthenticator(JwtSettings(issuer=ISSUER, audience="aud"), JwksCache(client, ISSUER))


def test_the_cap_is_16_kib_and_a_nested_token_fits_under_it() -> None:
    assert MAX_BEARER_LENGTH == 16 * 1024
    with pytest.raises(RecursionError):  # what the tests below rely on
        json.loads(NESTED)


async def test_a_nested_payload_is_not_ours_rather_than_a_recursion_error() -> None:
    """The unverified read finds no issuer: another authenticator's credential, so the chain gives 401."""
    token = f"{segment(b'{}')}.{segment(NESTED)}.sig"
    assert len(token) < MAX_BEARER_LENGTH
    assert await jwt_authenticator().authenticate(bearer(token)) is None


async def test_a_nested_header_on_our_token_is_a_401_rather_than_a_recursion_error() -> None:
    """PyJWT 2.15 already turns this RecursionError into a DecodeError; this pins the 401."""
    token = f"{segment(NESTED)}.{segment(json.dumps({'iss': ISSUER}).encode())}.sig"
    assert len(token) < MAX_BEARER_LENGTH
    with pytest.raises(NotAuthenticated, match="header"):
        await jwt_authenticator().authenticate(bearer(token))


async def test_a_header_read_that_raises_recursion_error_is_a_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """The header read itself treats RecursionError as unreadable, whatever the PyJWT release (>= 2.13)."""

    def recursing(token: str) -> dict[str, object]:
        raise RecursionError("maximum recursion depth exceeded while decoding a JSON array")

    monkeypatch.setattr("livepeer_builder.adapters.jwt.jwt.get_unverified_header", recursing)
    token = f"{segment(b'{}')}.{segment(json.dumps({'iss': ISSUER}).encode())}.sig"
    with pytest.raises(NotAuthenticated, match="header"):
        await jwt_authenticator().authenticate(bearer(token))


async def test_a_bearer_over_16_kib_never_reaches_an_authenticator() -> None:
    supplied = Recording()
    chain = AuthenticatorChain([supplied])
    with pytest.raises(NotAuthenticated, match="16 KiB"):
        await chain.authenticate(bearer("x" * (MAX_BEARER_LENGTH + 1)))
    assert supplied.seen == []
    assert await chain.authenticate(bearer("x" * MAX_BEARER_LENGTH)) == ActorContext(actor_id="someone")
    assert len(supplied.seen) == 1


async def test_the_engine_refuses_an_oversized_api_key_before_the_key_store(
    make_engine: EngineFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = await make_engine()
    looked_up: list[str] = []

    async def lookup(token_hash: str) -> None:
        looked_up.append(token_hash)

    monkeypatch.setattr(engine.store, "access_key_by_hash", lookup)
    with pytest.raises(NotAuthenticated, match="16 KiB"):
        await engine.authenticator.authenticate(bearer("lpk_" + "x" * MAX_BEARER_LENGTH))
    assert looked_up == []


@pytest.mark.parametrize(
    "header",
    [
        "Bearer\t"
        + "x" * 40_000,  # a tab: no bearer to the engine, one to an authenticator that splits on any space
        "Bearer\x0b" + "x" * 40_000,
        "bearer  " + "x" * (MAX_BEARER_LENGTH + 1),
        "Basic " + "x" * 40_000,  # any scheme: a supplied authenticator may read another
        "x" * 40_000,
        " " * 40_000 + "Bearer x",
    ],
    ids=["tab", "vertical tab", "two spaces", "another scheme", "no scheme", "leading spaces"],
)
async def test_the_cap_is_on_the_whole_header_so_no_spelling_of_it_gets_past(header: str) -> None:
    """The cap was on the engine's own reading of the header (one space after ``Bearer``), so a credential
    it did not read as a bearer went on to a supplied authenticator at any length."""
    supplied = Recording()
    request = AuthRequest(method="GET", path="/v1/jobs", headers={"authorization": header})
    with pytest.raises(NotAuthenticated, match="16 KiB"):
        await AuthenticatorChain([supplied]).authenticate(request)
    assert supplied.seen == []


async def test_a_short_header_of_another_scheme_still_reaches_a_supplied_authenticator() -> None:
    supplied = Recording()
    request = AuthRequest(method="GET", path="/v1/jobs", headers={"authorization": "Basic dXNlcjpwYXNz"})
    assert await AuthenticatorChain([supplied]).authenticate(request) == ActorContext(actor_id="someone")
    assert supplied.seen == ["Basic dXNlcjpwYXNz"]
