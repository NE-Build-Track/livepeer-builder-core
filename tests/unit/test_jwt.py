"""JWT bearer (#49, D45): OAuth access tokens from an enterprise identity provider become an ActorContext.

Tokens are signed here with locally generated keys; the provider's discovery document and JWKS are served
by an httpx mock transport, so nothing touches the network.
"""

import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from livepeer_builder.adapters.jwt import JwksCache, JwtBearerAuthenticator, JwtSettings
from livepeer_builder.contracts import AuthProvenance, AuthRequest
from livepeer_builder.errors import NotAuthenticated, ProviderUnavailable

ISSUER = "https://idp.example.com/"
AUDIENCE = "https://engine.example.com"
DISCOVERY = "https://idp.example.com/.well-known/openid-configuration"
JWKS_URL = "https://idp.example.com/keys"
JWKS_URL_2 = "https://idp.example.com/keys-v2"


class Provider:
    """A stand-in identity provider: signing keys, its discovery document and JWKS, and a request count."""

    def __init__(self) -> None:
        self.keys: dict[str, rsa.RSAPrivateKey] = {
            "k1": rsa.generate_private_key(public_exponent=65537, key_size=2048)
        }
        self.published = ["k1"]
        self.issuer = ISSUER
        self.requests: list[str] = []
        self.down = False
        self.jwks_down = False  # only the key set fails, not discovery
        self.jwks_uri = JWKS_URL
        self.extra: dict[str, dict[str, Any]] = {}  # per-kid JWK fields: use, key_ops, alg

    def add_key(self, kid: str, *, publish: bool = True) -> None:
        self.keys[kid] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if publish:
            self.published.append(kid)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        if self.down:
            return httpx.Response(503)
        if str(request.url) == DISCOVERY:
            return httpx.Response(200, json={"issuer": self.issuer, "jwks_uri": self.jwks_uri})
        if str(request.url) in (JWKS_URL, JWKS_URL_2):
            if self.jwks_down or str(request.url) != self.jwks_uri:  # keys only where discovery says
                return httpx.Response(500)
            keys = []
            for kid in self.published:
                public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.keys[kid].public_key()))
                keys.append(public | {"kid": kid, "use": "sig", "alg": "RS256"} | self.extra.get(kid, {}))
            return httpx.Response(
                200, json={"keys": [{k: v for k, v in key.items() if v is not None} for key in keys]}
            )
        return httpx.Response(404)

    def token(
        self, kid: str | None = "k1", *, alg: str = "RS256", drop: tuple[str, ...] = (), **claims: Any
    ) -> str:
        now = int(time.time())
        body: dict[str, Any] = {
            "iss": ISSUER, "aud": AUDIENCE, "sub": "alice", "iat": now, "exp": now + 300, "jti": "t-1",
            "scope": "jobs.run jobs.read",
        } | claims  # fmt: skip
        for name in drop:
            body.pop(name, None)
        headers = {} if kid is None else {"kid": kid}
        return jwt.encode(body, self.keys[kid or "k1"], algorithm=alg, headers=headers)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def provider() -> Provider:
    return Provider()


@pytest.fixture
def clock() -> Clock:
    return Clock()


Make = Callable[..., JwtBearerAuthenticator]


@pytest.fixture
def make(provider: Provider, clock: Clock) -> Make:
    def build(**overrides: Any) -> JwtBearerAuthenticator:
        jwks_url = overrides.pop("jwks_url", None)
        settings = JwtSettings(issuer=ISSUER, audience=AUDIENCE, **overrides)
        client = httpx.AsyncClient(transport=httpx.MockTransport(provider.handler))
        return JwtBearerAuthenticator(settings, JwksCache(client, ISSUER, jwks_url, monotonic=clock))

    return build


FIXED = "the identity provider's signing keys cannot be read"  # the 503 body: never a URL or a client error


def unavailable_reason(caplog: pytest.LogCaptureFixture) -> str:
    """What the server logged about the provider: the detail the 503 body leaves out."""
    found = [r for r in caplog.records if getattr(r, "event", None) == "auth.jwks_unavailable"]
    assert found, "the reason must be logged"
    return f"{found[-1].reason} {getattr(found[-1], 'error', '')}"


def bearer(token: str) -> AuthRequest:
    return AuthRequest(method="GET", path="/v1/jobs", headers={"authorization": f"Bearer {token}"})


# -- a valid token -------------------------------------------------------------------------------------


async def test_a_valid_token_becomes_an_actor(make: Make, provider: Provider) -> None:
    actor = await make().authenticate(bearer(provider.token()))
    assert actor is not None
    assert (actor.actor_id, actor.application_id, actor.credential_kind) == ("alice", "default", "actor")
    assert actor.permissions == frozenset({"jobs.run", "jobs.read"})
    assert actor.auth == AuthProvenance(method="jwt", issuer=ISSUER, credential_id="t-1")
    assert provider.requests == [DISCOVERY, JWKS_URL]  # found through the issuer's discovery document


async def test_claims_are_mapped_as_configured(make: Make, provider: Provider) -> None:
    authenticator = make(
        actor_claim="email", application_claim="tenant", scope_claim="scp", scope_prefix="api://engine/"
    )
    token = provider.token(
        email="alice@example.com",
        tenant="acme",
        scp=["api://engine/costs.read", "openid", "api://engine/discover"],
    )
    actor = await authenticator.authenticate(bearer(token))
    assert actor is not None
    assert (actor.actor_id, actor.application_id) == ("alice@example.com", "acme")
    assert actor.permissions == frozenset({"costs.read", "discover"})


async def test_only_the_admin_scope_makes_an_admin(make: Make, provider: Provider) -> None:
    authenticator = make(admin_scope="livepeer:admin")
    admin = await authenticator.authenticate(bearer(provider.token(scope="livepeer:admin")))
    assert admin is not None and admin.credential_kind == "admin"
    actor = await authenticator.authenticate(bearer(provider.token(scope="admin.keys admin.allocations")))
    assert actor is not None and actor.credential_kind == "actor"  # admin is a kind, never a scope string


async def test_actors_assert_and_unknown_scopes_are_never_granted(make: Make, provider: Provider) -> None:
    actor = await make().authenticate(bearer(provider.token(scope="actors.assert jobs.run openid email")))
    assert actor is not None and actor.permissions == frozenset({"jobs.run"})


async def test_a_token_without_kid_uses_the_only_key(make: Make, provider: Provider) -> None:
    assert await make().authenticate(bearer(provider.token(kid=None))) is not None


async def test_clock_skew_within_the_leeway_is_accepted(make: Make, provider: Provider) -> None:
    late = provider.token(exp=int(time.time()) - 10)
    assert await make(leeway_s=30).authenticate(bearer(late)) is not None
    with pytest.raises(NotAuthenticated, match="expired"):
        await make(leeway_s=0).authenticate(bearer(late))


async def test_the_configured_jwks_url_skips_discovery(make: Make, provider: Provider) -> None:
    assert await make(jwks_url=JWKS_URL).authenticate(bearer(provider.token())) is not None
    assert provider.requests == [JWKS_URL]


# -- not ours: the next authenticator gets a turn ----------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": "Bearer lpk_engine_key"},
        {"authorization": "Basic YWxpY2U6cHc="},
        {"authorization": "Bearer not.a-jwt"},
        {"authorization": "Bearer three.dotted.parts"},
    ],
    ids=["no header", "engine key", "basic", "garbage", "three parts, not a jwt"],
)
async def test_a_credential_that_is_not_a_jwt_is_not_ours(make: Make, headers: dict[str, str]) -> None:
    assert await make().authenticate(AuthRequest(method="GET", path="/", headers=headers)) is None


async def test_another_issuers_token_is_not_ours(make: Make, provider: Provider) -> None:
    assert await make().authenticate(bearer(provider.token(iss="https://other.example.com/"))) is None
    assert provider.requests == []  # recognized without fetching any key


# -- ours, but invalid: the chain stops with 401 ---------------------------------------------------------


@pytest.mark.parametrize(
    ("claims", "drop", "reason"),
    [
        ({"aud": "https://someone-else.example.com"}, (), "audience"),
        ({"exp": int(time.time()) - 3600}, (), "expired"),
        ({"nbf": int(time.time()) + 3600}, (), "not yet valid"),
        ({}, ("exp",), "exp"),
        ({}, ("aud",), "aud"),
        ({}, ("sub",), "sub"),
    ],
    ids=["wrong audience", "expired", "not yet valid", "no exp", "no aud", "no actor claim"],
)
async def test_an_invalid_token_from_our_issuer_is_refused(
    make: Make, provider: Provider, claims: dict[str, Any], drop: tuple[str, ...], reason: str
) -> None:
    with pytest.raises(NotAuthenticated, match=reason):
        await make().authenticate(bearer(provider.token(drop=drop, **claims)))


async def test_a_forged_signature_is_refused(make: Make, provider: Provider) -> None:
    provider.add_key("forger", publish=False)
    forged = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "alice", "exp": int(time.time()) + 300},
        provider.keys["forger"],
        algorithm="RS256",
        headers={"kid": "k1"},  # claims the provider's key id, signed with another key
    )
    with pytest.raises(NotAuthenticated, match="signature"):
        await make().authenticate(bearer(forged))


async def test_alg_none_is_refused(make: Make) -> None:
    unsigned = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "alice", "exp": int(time.time()) + 300},
        None,
        algorithm="none",
    )
    with pytest.raises(NotAuthenticated, match="algorithm"):
        await make().authenticate(bearer(unsigned))


async def test_an_hmac_token_keyed_with_the_public_key_is_refused(make: Make, provider: Provider) -> None:
    """The classic confusion attack: HS256 with the RSA public key as the shared secret."""
    public_pem = (
        provider.keys["k1"]
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    header = jwt.utils.base64url_encode(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "alice", "exp": int(time.time()) + 300}
    payload = jwt.utils.base64url_encode(json.dumps(claims).encode())
    import hashlib
    import hmac

    signature = hmac.new(public_pem, header + b"." + payload, hashlib.sha256).digest()
    token = b".".join([header, payload, jwt.utils.base64url_encode(signature)]).decode()
    with pytest.raises(NotAuthenticated, match="algorithm"):
        await make().authenticate(bearer(token))


async def test_an_algorithm_outside_the_allow_list_is_refused(make: Make, provider: Provider) -> None:
    with pytest.raises(NotAuthenticated, match="algorithm"):
        await make(algorithms=("ES256",)).authenticate(bearer(provider.token(alg="RS384")))


async def test_a_token_without_kid_is_refused_when_the_provider_has_several_keys(
    make: Make, provider: Provider
) -> None:
    provider.add_key("k2")
    with pytest.raises(NotAuthenticated, match="kid"):
        await make().authenticate(bearer(provider.token(kid=None)))


# -- signing keys change -------------------------------------------------------------------------------


async def test_a_new_signing_key_is_fetched_once_and_then_rate_limited(
    make: Make, provider: Provider, clock: Clock
) -> None:
    authenticator = make()
    assert await authenticator.authenticate(bearer(provider.token())) is not None
    provider.add_key("k2")  # the provider rotates its signing key
    assert await authenticator.authenticate(bearer(provider.token(kid="k2"))) is not None
    assert provider.requests.count(JWKS_URL) == 2
    provider.add_key("k3", publish=False)
    with pytest.raises(NotAuthenticated, match="unknown signing key"):
        await authenticator.authenticate(bearer(provider.token(kid="k3")))
    with pytest.raises(NotAuthenticated, match="unknown signing key"):
        await authenticator.authenticate(bearer(provider.token(kid="k3")))
    assert provider.requests.count(JWKS_URL) == 2  # a stream of unknown kids cannot hammer the provider
    clock.now += 61
    provider.published.append("k3")
    assert await authenticator.authenticate(bearer(provider.token(kid="k3"))) is not None


async def test_keys_are_fetched_again_after_an_hour(make: Make, provider: Provider, clock: Clock) -> None:
    authenticator = make()
    await authenticator.authenticate(bearer(provider.token()))
    clock.now += 3601
    await authenticator.authenticate(bearer(provider.token()))
    assert provider.requests.count(JWKS_URL) == 2


# -- the provider ----------------------------------------------------------------------------------------


async def test_an_unreachable_provider_is_unavailable_not_unauthenticated(
    make: Make, provider: Provider
) -> None:
    provider.down = True
    with pytest.raises(ProviderUnavailable):
        await make().authenticate(bearer(provider.token()))


async def test_a_discovery_document_for_another_issuer_is_refused(
    make: Make, provider: Provider, caplog: pytest.LogCaptureFixture
) -> None:
    provider.issuer = "https://impostor.example.com/"
    with pytest.raises(ProviderUnavailable, match=FIXED):
        await make().authenticate(bearer(provider.token()))
    assert "issuer" in unavailable_reason(caplog)


@pytest.mark.parametrize(
    ("discovery", "jwks", "complaint"),
    [
        ({"issuer": ISSUER}, None, "jwks_uri"),
        ({"issuer": ISSUER, "jwks_uri": JWKS_URL}, {"keys": []}, "no usable signing keys"),
        ({"issuer": ISSUER, "jwks_uri": JWKS_URL}, ["not", "an", "object"], "JSON object"),
        ({"issuer": ISSUER, "jwks_uri": "ftp://idp.example.com/keys"}, None, "scheme"),
    ],
    ids=["no jwks_uri", "no keys", "not an object", "not http(s)"],
)
async def test_a_provider_that_publishes_nothing_usable_is_unavailable(
    provider: Provider, discovery: dict[str, Any], jwks: Any, complaint: str, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=discovery if str(request.url) == DISCOVERY else jwks)

    keys = JwksCache(httpx.AsyncClient(transport=httpx.MockTransport(handler)), ISSUER)
    authenticator = JwtBearerAuthenticator(JwtSettings(issuer=ISSUER, audience=AUDIENCE), keys)
    with pytest.raises(ProviderUnavailable, match=FIXED):
        await authenticator.authenticate(bearer(provider.token()))
    assert complaint in unavailable_reason(caplog)
    await authenticator.aclose()


async def test_a_malformed_scope_claim_grants_nothing(make: Make, provider: Provider) -> None:
    actor = await make().authenticate(bearer(provider.token(scope=42)))
    assert actor is not None and actor.permissions == frozenset()


def test_the_challenge_is_a_bearer_realm(make: Make) -> None:
    assert make().challenge() == 'Bearer realm="livepeer-builder"'


# -- PR #50 review ---------------------------------------------------------------------------------------


async def test_a_sender_constrained_token_is_refused(make: Make, provider: Provider) -> None:
    """cnf binds the token to a certificate (RFC 8705) or a DPoP key (RFC 9449); this engine verifies
    neither, so accepting it as a bearer token would be the downgrade a stolen token needs."""
    for cnf in (
        {"jkt": "0ZcOCORZNYy-DWpqq30jZyJGHTN0d2HglBV3uiguA4I"},
        {"x5t#S256": "bwcK0esc3ACC3DB2Y5_lESsXE8o9ltc05O89jdN-dg2"},
    ):
        with pytest.raises(NotAuthenticated, match="sender-constrained"):
            await make().authenticate(bearer(provider.token(cnf=cnf)))


async def test_the_503_never_carries_the_key_url_or_the_client_error(
    make: Make, provider: Provider, caplog: pytest.LogCaptureFixture
) -> None:
    provider.down = True
    with pytest.raises(ProviderUnavailable) as raised:
        await make().authenticate(bearer(provider.token()))
    assert str(raised.value) == FIXED
    assert DISCOVERY in unavailable_reason(caplog) or "openid-configuration" in unavailable_reason(caplog)


async def test_a_discovered_key_url_that_fails_is_discovered_again(
    make: Make, provider: Provider, clock: Clock
) -> None:
    """A first jwks_uri that fails must not stick until restart: the issuer may fix or move it."""
    authenticator = make()
    provider.jwks_down = True
    with pytest.raises(ProviderUnavailable):
        await authenticator.authenticate(bearer(provider.token()))
    provider.jwks_down, provider.jwks_uri = False, JWKS_URL_2  # the issuer moves its keys
    clock.now += 6  # past the short pause after a failed fetch
    assert await authenticator.authenticate(bearer(provider.token())) is not None
    assert provider.requests.count(DISCOVERY) == 2 and provider.requests[-1] == JWKS_URL_2


async def test_a_configured_key_url_is_never_replaced_by_discovery(
    make: Make, provider: Provider, clock: Clock
) -> None:
    authenticator = make(jwks_url=JWKS_URL)
    provider.jwks_down = True
    with pytest.raises(ProviderUnavailable):
        await authenticator.authenticate(bearer(provider.token()))
    provider.jwks_down = False
    clock.now += 6
    assert await authenticator.authenticate(bearer(provider.token())) is not None
    assert DISCOVERY not in provider.requests


async def test_a_failed_refetch_does_not_use_up_the_minute(
    make: Make, provider: Provider, clock: Clock
) -> None:
    """While the provider is failing, an unknown kid is 503 (the keys could not be read), not 401, and the
    next minute is not lost: once a pause has passed, the refetch is tried again."""
    authenticator = make()
    assert await authenticator.authenticate(bearer(provider.token())) is not None
    provider.add_key("k2")
    provider.jwks_down = True
    with pytest.raises(ProviderUnavailable):
        await authenticator.authenticate(bearer(provider.token(kid="k2")))
    fetches = provider.requests.count(JWKS_URL)
    with pytest.raises(ProviderUnavailable):  # within the pause: 503 again, and the provider is left alone
        await authenticator.authenticate(bearer(provider.token(kid="k2")))
    assert provider.requests.count(JWKS_URL) == fetches
    provider.jwks_down = False
    clock.now += 6
    assert await authenticator.authenticate(bearer(provider.token(kid="k2"))) is not None


@pytest.mark.parametrize(
    "fields", [{"use": "enc"}, {"key_ops": ["encrypt", "wrapKey"]}], ids=["use enc", "key_ops without verify"]
)
async def test_a_key_not_for_signatures_never_verifies_one(
    make: Make, provider: Provider, fields: dict[str, Any]
) -> None:
    provider.extra["k1"] = fields
    with pytest.raises((NotAuthenticated, ProviderUnavailable)):  # no signing key left at all
        await make().authenticate(bearer(provider.token()))
    provider.add_key("k2")  # with a real signing key published beside it
    authenticator = make()
    with pytest.raises(NotAuthenticated, match="unknown signing key"):
        await authenticator.authenticate(bearer(provider.token(kid="k1")))
    # no kid: the only *signing* key is k2, so a token signed with the encryption key k1 does not verify
    with pytest.raises(NotAuthenticated, match="signature"):
        await authenticator.authenticate(bearer(provider.token(kid=None)))
    assert await authenticator.authenticate(bearer(provider.token(kid="k2"))) is not None


async def test_a_key_without_use_still_signs(make: Make, provider: Provider) -> None:
    provider.extra["k1"] = {"use": None}  # several issuers omit it
    assert await make().authenticate(bearer(provider.token())) is not None


async def test_a_token_must_use_the_algorithm_its_key_declares(make: Make, provider: Provider) -> None:
    """The key is published for RS256; the same RSA key must not verify an RS384 token (RFC 8725)."""
    with pytest.raises(NotAuthenticated, match="algorithm"):
        await make(algorithms=("RS256", "RS384")).authenticate(bearer(provider.token(alg="RS384")))


async def test_a_key_url_that_worked_and_then_fails_is_discovered_again(
    make: Make, provider: Provider, clock: Clock
) -> None:
    """The issuer moves its keys: the URL that served them yesterday must not be kept once it fails."""
    authenticator = make()
    assert await authenticator.authenticate(bearer(provider.token())) is not None
    provider.jwks_uri = JWKS_URL_2  # the old URL now fails; discovery names the new one
    clock.now += 3601  # the cached keys are due for a refresh
    with pytest.raises(ProviderUnavailable):
        await authenticator.authenticate(bearer(provider.token()))
    clock.now += 6
    assert await authenticator.authenticate(bearer(provider.token())) is not None
    assert provider.requests[-2:] == [DISCOVERY, JWKS_URL_2]


async def test_the_admin_read_scope_makes_an_auditor_and_admin_wins(make: Make, provider: Provider) -> None:
    """A dashboard's service account gets a read-only credential (#52, D46)."""
    authenticator = make(admin_scope="livepeer:admin", admin_read_scope="livepeer:read")
    reader = await authenticator.authenticate(bearer(provider.token(scope="livepeer:read")))
    assert reader is not None and reader.credential_kind == "auditor"
    both = await authenticator.authenticate(bearer(provider.token(scope="livepeer:read livepeer:admin")))
    assert both is not None and both.credential_kind == "admin"
