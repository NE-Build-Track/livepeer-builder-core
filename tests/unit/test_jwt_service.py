"""JWT bearer chosen by configuration (#49, D45): LPB_AUTH and LPB_JWT_* build the authenticator, and the
REST service accepts the provider's tokens. The identity provider is a local aiohttp server publishing a
discovery document and a JWKS, so the real wiring (settings, default_parts, an httpx client) runs."""

import base64
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import jwt
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from livepeer_builder import EngineSettings, EngineSupply
from livepeer_builder.adapters.jwt import JwtSettings
from livepeer_builder.contracts import ActorContext, AuthRequest
from livepeer_builder.http import create_app
from livepeer_builder.testing import make_engine

AUDIENCE = "https://engine.example.com"


class IdentityProvider:
    def __init__(self, server: TestServer, key: rsa.RSAPrivateKey) -> None:
        self.issuer = str(server.make_url("/"))
        self._key = key

    def token(self, **claims: Any) -> str:
        now = int(time.time())
        body = {
            "iss": self.issuer,
            "aud": AUDIENCE,
            "sub": "alice",
            "iat": now,
            "exp": now + 300,
            "jti": "t-1",
            "client_id": "console",
            "scope": "discover jobs.read",
        }
        return jwt.encode(body | claims, self._key, algorithm="RS256", headers={"kid": "k1", "typ": "at+jwt"})


@pytest.fixture
async def idp() -> AsyncIterator[IdentityProvider]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    holder: dict[str, str] = {}

    async def discovery(_: web.Request) -> web.Response:
        return web.json_response({"issuer": holder["issuer"], "jwks_uri": holder["issuer"] + "keys"})

    async def keys(_: web.Request) -> web.Response:
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        return web.json_response({"keys": [public | {"kid": "k1", "use": "sig"}]})

    app = web.Application()
    app.router.add_get("/.well-known/openid-configuration", discovery)
    app.router.add_get("/keys", keys)
    async with TestServer(app) as server:
        provider = IdentityProvider(server, key)
        holder["issuer"] = provider.issuer
        yield provider


def jwt_settings(idp: IdentityProvider, auth: str = "jwt", **extra: str) -> dict[str, Any]:
    # the local identity provider is plain http, which needs the dev override (D47); and every token is in
    # one tenant unless a claim names it (exactly one, #163 review)
    tenant = {} if "jwt_tenant_claim" in extra else {"jwt_tenant_id": "default"}
    return {
        "auth": auth,
        "jwt_issuer": idp.issuer,
        "jwt_audience": AUDIENCE,
        "jwt_allow_http": True,
        **tenant,
        **extra,
    }


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# -- settings ----------------------------------------------------------------------------------------


def test_api_keys_stay_the_default() -> None:
    assert EngineSettings(discovery_url="http://127.0.0.1:9/discovery").auth == ("api_keys",)


def test_jwt_settings_come_from_the_environment() -> None:
    settings = EngineSettings.from_env(
        {
            "LPB_DISCOVERY_URL": "http://127.0.0.1:9/discovery",
            "LPB_AUTH": "jwt, api_keys",
            "LPB_JWT_ISSUER": "https://idp.example.com/",
            "LPB_JWT_AUDIENCE": AUDIENCE,
            "LPB_JWT_TENANT_CLAIM": "org",
            "LPB_JWT_ALGORITHMS": "RS256,PS256",
            "LPB_JWT_ADMIN_SCOPE": "livepeer:admin",
            "LPB_JWT_ADMIN_READ_SCOPE": "livepeer:read",
        }
    )
    assert (settings.jwt_admin_scope, settings.jwt_admin_read_scope) == ("livepeer:admin", "livepeer:read")
    assert settings.auth == ("jwt", "api_keys")
    assert settings.jwt_algorithms == ("RS256", "PS256")
    assert (settings.jwt_actor_claim, settings.jwt_scope_claim, settings.jwt_leeway_s) == (
        "sub",
        "scope",
        30.0,
    )


@pytest.mark.parametrize(
    ("values", "complaint"),
    [
        ({"auth": "jwt"}, "LPB_JWT_ISSUER"),
        ({"auth": "jwt", "jwt_issuer": "https://idp/"}, "LPB_JWT_AUDIENCE"),
        ({"auth": "passwords"}, "auth"),
        ({"auth": "jwt,jwt", "jwt_issuer": "https://idp/", "jwt_audience": "a"}, "twice"),
        ({"auth": ""}, "auth"),
        (
            {"auth": "jwt", "jwt_issuer": "https://idp/", "jwt_audience": "a", "jwt_algorithms": "HS256"},
            "asymmetric",
        ),
        (
            {"auth": "jwt", "jwt_issuer": "https://idp/", "jwt_audience": "a", "jwt_algorithms": "none"},
            "asymmetric",
        ),
        (
            {
                "auth": "jwt",
                "jwt_issuer": "https://idp/",
                "jwt_audience": "a",
                "jwt_tenant_id": "a" * 201,
            },
            "jwt_tenant_id",
        ),
    ],
    ids=["no issuer", "no audience", "unknown mode", "repeated mode", "empty", "hmac", "none", "long app id"],
)
def test_bad_auth_settings_fail_at_startup(values: dict[str, Any], complaint: str) -> None:
    with pytest.raises(ValidationError, match=complaint):
        EngineSettings.model_validate({"discovery_url": "http://127.0.0.1:9/discovery", **values})


JWT = {
    "discovery_url": "http://127.0.0.1:9/discovery",
    "auth": "jwt",
    "jwt_issuer": "https://idp.example/",
    "jwt_audience": AUDIENCE,
}
ONE_TENANT_SOURCE = "exactly one of LPB_JWT_TENANT_CLAIM"


@pytest.mark.parametrize(
    ("tenant", "said"),
    [({}, "neither is set"), ({"jwt_tenant_claim": "org", "jwt_tenant_id": "acme"}, "both are set")],
    ids=["neither", "both"],
)
def test_jwt_needs_exactly_one_source_of_a_tokens_tenant(tenant: dict[str, str], said: str) -> None:
    """#163 review: neither setting has a default, so an engine that names no tenant source refuses to
    start rather than put every token in one tenant; and one that names both is ambiguous."""
    with pytest.raises(ValidationError) as refused:
        EngineSettings.model_validate(JWT | tenant)
    message = str(refused.value)
    assert ONE_TENANT_SOURCE in message and "LPB_JWT_TENANT_ID" in message and said in message


@pytest.mark.parametrize(
    "tenant", [{"jwt_tenant_claim": "org"}, {"jwt_tenant_id": "acme"}], ids=["claim", "id"]
)
def test_either_source_alone_starts(tenant: dict[str, str]) -> None:
    settings = EngineSettings.model_validate(JWT | tenant)
    assert (settings.jwt_tenant_claim, settings.jwt_tenant_id) == (
        tenant.get("jwt_tenant_claim"),
        tenant.get("jwt_tenant_id"),
    )


def test_without_jwt_neither_tenant_source_is_needed_and_neither_has_a_default() -> None:
    settings = EngineSettings(discovery_url="http://127.0.0.1:9/discovery")
    assert (settings.jwt_tenant_claim, settings.jwt_tenant_id) == (None, None)


@pytest.mark.parametrize(
    "tenant", [{}, {"tenant_claim": "org", "tenant_id": "acme"}], ids=["neither", "both"]
)
def test_the_authenticator_needs_exactly_one_source_of_a_tokens_tenant(tenant: dict[str, str]) -> None:
    """An embedder that builds the authenticator itself gets no implicit tenant either."""
    with pytest.raises(ValueError, match="exactly one of tenant_claim and tenant_id"):
        JwtSettings(issuer="https://idp/", audience="a", **tenant)


def test_an_embedder_s_tenant_id_longer_than_the_store_keeps_is_refused() -> None:
    """Every token takes this id when no claim names one, and every record stores it in 200 characters."""
    assert JwtSettings(issuer="https://idp/", audience="a", tenant_id="a" * 200)
    with pytest.raises(ValueError, match="200 characters"):
        JwtSettings(issuer="https://idp/", audience="a", tenant_id="a" * 201)


# -- the engine -------------------------------------------------------------------------------------


async def test_jwt_alone_replaces_engine_keys(idp: IdentityProvider) -> None:
    async with make_engine(**jwt_settings(idp)) as engine:
        assert engine.keys is None  # no key routes, no bootstrap
        request = AuthRequest(
            method="GET", path="/v1/jobs", headers={"authorization": f"Bearer {idp.token()}"}
        )
        actor = await engine.authenticator.authenticate(request)
        assert actor is not None and actor.actor_id == "alice"
        assert actor.permissions == frozenset({"discover", "jobs.read"})


async def test_both_modes_accept_tokens_and_keys(idp: IdentityProvider) -> None:
    async with make_engine(**jwt_settings(idp, auth="jwt,api_keys")) as engine:
        assert engine.keys is not None
        issued = await engine.keys.issue(
            ActorContext(actor_id="op", credential_kind="clearinghouse_admin"),
            kind="actor",
            actor_id="bob",
            operation_ref="issue-jwt_service-145",
        )
        for token, who in ((idp.token(), "alice"), (issued.token, "bob")):
            request = AuthRequest(method="GET", path="/", headers={"authorization": f"Bearer {token}"})
            actor = await engine.authenticator.authenticate(request)
            assert actor is not None and actor.actor_id == who
        assert engine.authenticator.challenge() == 'Bearer realm="livepeer-builder"'  # announced once


async def test_supplied_authenticators_still_win(idp: IdentityProvider) -> None:
    class Nobody:
        async def authenticate(self, request: AuthRequest) -> ActorContext | None:
            return None

        def challenge(self) -> str | None:
            return None

    async with make_engine(supply=EngineSupply(authenticators=(Nobody(),)), **jwt_settings(idp)) as engine:
        request = AuthRequest(method="GET", path="/", headers={"authorization": f"Bearer {idp.token()}"})
        assert await engine.authenticator.authenticate(request) is None  # no JWT authenticator was built


# -- the REST service ----------------------------------------------------------------------------------


async def test_the_rest_service_accepts_the_providers_tokens(idp: IdentityProvider) -> None:
    async with make_engine(**jwt_settings(idp, jwt_admin_scope="livepeer:admin")) as engine:
        app = create_app(engine, manage_lifecycle=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://engine"
        ) as client:
            # offerings is public (#5); runners still requires a token
            runners = "/v1/runners"
            params = {"app": "example/echo"}
            assert (await client.get(runners, params=params, headers=bearer(idp.token()))).status_code == 200
            missing = await client.get(runners, params=params)
            assert (
                missing.status_code == 401
                and missing.headers["www-authenticate"] == 'Bearer realm="livepeer-builder"'
            )
            expired = await client.get(
                runners, params=params, headers=bearer(idp.token(exp=int(time.time()) - 3600))
            )
            assert expired.status_code == 401 and "expired" in expired.json()["error"]
            assert (
                await client.get("/v1/jobs", headers=bearer(idp.token(scope="discover")))
            ).status_code == 403
            admin = bearer(idp.token(scope="livepeer:admin"))
            assert (
                await client.get("/v1/clearinghouse/reconciliation?actor_id=alice", headers=admin)
            ).status_code in (200, 404)
            assert (
                await client.get("/v1/clearinghouse/reconciliation", headers=bearer(idp.token()))
            ).status_code == 403
            assert (await client.post("/v1/clearinghouse/keys", json={}, headers=admin)).status_code in (
                404,
                405,
            )
            schemes = app.openapi()["components"]["securitySchemes"]
            assert [(s["scheme"], s.get("bearerFormat")) for s in schemes.values()] == [("bearer", "JWT")]


async def test_without_a_bearer_challenge_no_scheme_is_declared(idp: IdentityProvider) -> None:
    class Header:
        async def authenticate(self, request: AuthRequest) -> ActorContext | None:
            return None

        def challenge(self) -> str | None:
            return None

    async with make_engine(supply=EngineSupply(authenticators=(Header(),), api_keys=None)) as engine:
        assert "securitySchemes" not in create_app(engine, manage_lifecycle=False).openapi().get(
            "components", {}
        )


def test_review_defaults_are_strict() -> None:
    settings = EngineSettings(
        discovery_url="http://127.0.0.1:9/discovery",
        auth=("jwt",),
        jwt_issuer="https://idp/",
        jwt_audience="a",
        jwt_tenant_id="default",
    )
    assert settings.jwt_token_types == ("at+jwt", "application/at+jwt")
    assert (settings.jwt_max_lifetime_s, settings.jwt_allow_http) == (3600, False)


@pytest.mark.parametrize(
    ("values", "complaint"),
    [
        ({"jwt_issuer": "http://idp/"}, "https"),
        ({"jwt_jwks_url": "http://idp/keys"}, "https"),
        ({"jwt_admin_scope": "jobs.run"}, "admin"),
        ({"jwt_admin_scope": "openid"}, "admin"),
        ({"jwt_token_types": ""}, "token_types"),
        ({"jwt_admin_read_scope": "jobs.read"}, "admin_read"),
        ({"jwt_admin_read_scope": "profile"}, "admin_read"),
        ({"jwt_admin_scope": "ops", "jwt_admin_read_scope": "ops"}, "admin_read"),
        ({"jwt_scope_prefix": "api:", "jwt_admin_read_scope": "api:jobs.read"}, "admin_read"),
        ({"jwt_scope_prefix": "api:", "jwt_admin_scope": "api:jobs.run"}, "admin"),
        ({"jwt_scope_prefix": "api:", "jwt_admin_scope": "api:openid"}, "admin"),
    ],
    ids=[
        "http issuer",
        "http key URL",
        "admin scope is an operation",
        "admin scope is openid",
        "no token type",
        "read scope is an operation",
        "read scope is profile",
        "read scope is the admin scope",
        "prefixed read scope is an operation",
        "prefixed admin scope is an operation",
        "prefixed admin scope is openid",
    ],
)
def test_risky_jwt_settings_fail_at_startup(values: dict[str, Any], complaint: str) -> None:
    """An admin scope every user token carries would make every user an admin (#50 review)."""
    base = {
        "discovery_url": "http://127.0.0.1:9/discovery",
        "auth": "jwt",
        "jwt_issuer": "https://idp/",
        "jwt_audience": "a",
        "jwt_tenant_id": "default",
    }
    with pytest.raises(ValidationError, match=complaint):
        EngineSettings.model_validate(base | values)


def test_token_types_are_read_as_a_lower_case_list() -> None:
    base = {
        "discovery_url": "http://127.0.0.1:9/discovery",
        "auth": "jwt",
        "jwt_issuer": "https://idp/",
        "jwt_audience": "a",
        "jwt_tenant_id": "default",
    }
    settings = EngineSettings.model_validate(base | {"jwt_token_types": "AT+JWT, jwt"})
    assert settings.jwt_token_types == ("at+jwt", "jwt")


def test_the_dev_override_allows_an_http_issuer() -> None:
    settings = EngineSettings(
        discovery_url="http://127.0.0.1:9/discovery",
        auth=("jwt",),
        jwt_issuer="http://keycloak:8080/",
        jwt_audience="a",
        jwt_tenant_id="default",
        jwt_allow_http=True,
    )
    assert settings.jwt_issuer == "http://keycloak:8080/"


async def test_a_builders_first_sign_in_puts_them_on_the_operators_list(idp: IdentityProvider) -> None:
    """Registration on first sign-in (#52, D48): the operator sees alice by name before allotment her."""
    async with make_engine(**jwt_settings(idp, jwt_admin_scope="livepeer:admin")) as engine:
        app = create_app(engine, manage_lifecycle=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://engine"
        ) as client:
            alice = bearer(idp.token(sub="3c446b40-7123", preferred_username="alice"))
            assert (await client.get("/v1/jobs", headers=alice)).status_code == 200
            operator = bearer(
                idp.token(sub="b715dc36", scope="livepeer:admin", preferred_username="operator")
            )
            listed = (await client.get("/v1/clearinghouse/actors", headers=operator)).json()
            assert [(a["actor_id"], a["label"], a["allotted"]) for a in listed] == [
                ("3c446b40-7123", "alice", False)
            ]  # the operator signing in is not an actor, so is not listed


async def test_an_oversized_or_nested_bearer_is_a_401_not_a_500(idp: IdentityProvider) -> None:
    """#113 C22: json.loads raised RecursionError on a deeply nested segment, and no handler caught it."""

    def segment(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    nested, ours = segment(b"[" * 12000), segment(json.dumps({"iss": idp.issuer}).encode())
    tokens = {
        "40 kB, a nested payload": f"e30.{segment(b'[' * 30000)}.sig",
        "a nested payload": f"e30.{nested}.sig",
        "a nested header on our token": f"{nested}.{ours}.sig",
    }
    async with make_engine(**jwt_settings(idp)) as engine:
        app = create_app(engine, manage_lifecycle=False)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://engine") as client:
            for name, token in tokens.items():
                answer = await client.get("/v1/jobs", headers=bearer(token))
                assert answer.status_code == 401, (name, answer.status_code, answer.text)
