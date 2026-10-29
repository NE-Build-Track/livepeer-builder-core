"""JWT bearer chosen by configuration (#49, D45): LPB_AUTH and LPB_JWT_* build the authenticator, and the
REST service accepts the provider's tokens. The identity provider is a local aiohttp server publishing a
discovery document and a JWKS, so the real wiring (settings, default_parts, an httpx client) runs."""

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
    # the local identity provider is plain http, which needs the dev override (D47)
    return {"auth": auth, "jwt_issuer": idp.issuer, "jwt_audience": AUDIENCE, "jwt_allow_http": True, **extra}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# -- settings ----------------------------------------------------------------------------------------


def test_api_keys_stay_the_default() -> None:
    assert EngineSettings().auth == ("api_keys",)


def test_jwt_settings_come_from_the_environment() -> None:
    settings = EngineSettings.from_env(
        {
            "LPB_AUTH": "jwt, api_keys",
            "LPB_JWT_ISSUER": "https://idp.example.com/",
            "LPB_JWT_AUDIENCE": AUDIENCE,
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
    ],
    ids=["no issuer", "no audience", "unknown mode", "repeated mode", "empty", "hmac", "none"],
)
def test_bad_auth_settings_fail_at_startup(values: dict[str, Any], complaint: str) -> None:
    with pytest.raises(ValidationError, match=complaint):
        EngineSettings.model_validate(values)


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
            ActorContext(actor_id="op", credential_kind="admin"),
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
                await client.get("/v1/admin/reconciliation?actor_id=alice", headers=admin)
            ).status_code in (200, 404)
            assert (
                await client.get("/v1/admin/reconciliation", headers=bearer(idp.token()))
            ).status_code == 403
            assert (await client.post("/v1/admin/keys", json={}, headers=admin)).status_code in (404, 405)
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
    settings = EngineSettings(auth=("jwt",), jwt_issuer="https://idp/", jwt_audience="a")
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
    base = {"auth": "jwt", "jwt_issuer": "https://idp/", "jwt_audience": "a"}
    with pytest.raises(ValidationError, match=complaint):
        EngineSettings.model_validate(base | values)


def test_token_types_are_read_as_a_lower_case_list() -> None:
    base = {"auth": "jwt", "jwt_issuer": "https://idp/", "jwt_audience": "a"}
    settings = EngineSettings.model_validate(base | {"jwt_token_types": "AT+JWT, jwt"})
    assert settings.jwt_token_types == ("at+jwt", "jwt")


def test_the_dev_override_allows_an_http_issuer() -> None:
    settings = EngineSettings(
        auth=("jwt",), jwt_issuer="http://keycloak:8080/", jwt_audience="a", jwt_allow_http=True
    )
    assert settings.jwt_issuer == "http://keycloak:8080/"


async def test_a_builders_first_sign_in_puts_them_on_the_operators_list(idp: IdentityProvider) -> None:
    """Registration on first sign-in (#52, D48): the operator sees alice by name before provisioning her."""
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
            listed = (await client.get("/v1/admin/actors", headers=operator)).json()
            assert [(a["actor_id"], a["label"], a["provisioned"]) for a in listed] == [
                ("3c446b40-7123", "alice", False)
            ]  # the operator signing in is not an actor, so is not listed
