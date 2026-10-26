"""JWT bearer: OAuth 2.0 access tokens from an enterprise identity provider (#49, D45).

The engine is a resource server. It verifies a token's signature against the issuer's published keys
(JWKS) and its ``iss``, ``aud``, ``exp`` and ``nbf``, then maps its claims to an ``ActorContext``
(docs/contracts/access.md#jwt-bearer). It never runs a login. PyJWT is confined to this module.

The chain rule (docs/design-docs/extending-the-engine.md): a credential that is not a JWT, or a JWT from
another issuer, is not ours (``None``); a token from our issuer that fails a check is ``NotAuthenticated``.
When the provider's keys cannot be read, nobody can be authenticated by token: ``ProviderUnavailable``.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from livepeer_builder.adapters.auth import bearer_token
from livepeer_builder.config import EngineSettings
from livepeer_builder.contracts import ActorContext, AuthProvenance, AuthRequest, Operation
from livepeer_builder.errors import NotAuthenticated, ProviderUnavailable

# What a token's scopes may grant. Admin is a credential kind (LPB_JWT_ADMIN_SCOPE), never a scope string,
# and actors.assert belongs to the trusted-backend credential alone (D14).
GRANTABLE = frozenset(
    {Operation.DISCOVER, Operation.JOBS_RUN, Operation.JOBS_READ, Operation.COSTS_READ, Operation.EVENTS_READ}
)


@dataclass(frozen=True)
class JwtSettings:
    issuer: str
    audience: str
    algorithms: tuple[str, ...] = ("RS256", "ES256")
    actor_claim: str = "sub"
    application_claim: str | None = None  # None: every token is in application_id
    application_id: str = "default"
    scope_claim: str = "scope"
    scope_prefix: str = ""
    admin_scope: str | None = None  # None: no token is an admin credential
    leeway_s: float = 30.0
    realm: str = "livepeer-builder"


class JwksCache:
    """The issuer's signing keys: found through its discovery document (unless ``jwks_url`` is given),
    kept for ``max_age_s``, and fetched again for an unknown ``kid`` at most once per ``refetch_after_s``."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        issuer: str,
        jwks_url: str | None = None,
        *,
        max_age_s: float = 3600.0,
        refetch_after_s: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._issuer = issuer
        self._jwks_url = jwks_url
        self._max_age_s = max_age_s
        self._refetch_after_s = refetch_after_s
        self._monotonic = monotonic
        self._keys: list[jwt.PyJWK] = []
        self._fetched_at: float | None = None
        self._refetched_at: float | None = None  # the last fetch an unknown kid caused
        self._lock = asyncio.Lock()

    async def key(self, kid: str | None) -> jwt.PyJWK | None:
        """The key for ``kid``; with no ``kid``, the only key. ``None`` when the provider has no such key."""
        async with self._lock:
            now = self._monotonic()
            if self._fetched_at is None or now - self._fetched_at >= self._max_age_s:
                await self._fetch(now)
            found = self._find(kid)
            if found is None and kid is not None and self._may_refetch(now):
                self._refetched_at = now
                await self._fetch(now)
                found = self._find(kid)
            return found

    async def aclose(self) -> None:
        await self._client.aclose()

    def _may_refetch(self, now: float) -> bool:
        return self._refetched_at is None or now - self._refetched_at >= self._refetch_after_s

    def _find(self, kid: str | None) -> jwt.PyJWK | None:
        if kid is None:
            return self._keys[0] if len(self._keys) == 1 else None
        return next((key for key in self._keys if key.key_id == kid), None)

    async def _fetch(self, now: float) -> None:
        if self._jwks_url is None:
            document = await self._json(self._issuer.rstrip("/") + "/.well-known/openid-configuration")
            if document.get("issuer") != self._issuer:
                raise ProviderUnavailable("the identity provider's discovery document names another issuer")
            uri = document.get("jwks_uri")
            if not isinstance(uri, str) or not uri:
                raise ProviderUnavailable("the identity provider's discovery document has no jwks_uri")
            self._jwks_url = uri
        try:
            keys = jwt.PyJWKSet.from_dict(await self._json(self._jwks_url)).keys
        except jwt.PyJWKSetError as exc:
            raise ProviderUnavailable(f"the identity provider published no usable keys: {exc}") from exc
        self._keys, self._fetched_at = list(keys), now

    async def _json(self, url: str) -> dict[str, Any]:
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderUnavailable(f"cannot read the identity provider's keys from {url}: {exc}") from exc
        if not isinstance(body, dict):
            raise ProviderUnavailable(f"{url} did not answer a JSON object")
        return body


class JwtBearerAuthenticator:
    def __init__(self, settings: JwtSettings, keys: JwksCache) -> None:
        self._settings = settings
        self._keys = keys

    async def authenticate(self, request: AuthRequest) -> ActorContext | None:
        token = bearer_token(request)
        if token is None or token.count(".") != 2:
            return None
        try:
            header = jwt.get_unverified_header(token)
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError:
            return None  # not a JWT: another authenticator's credential
        if unverified.get("iss") != self._settings.issuer:
            return None
        claims = await self._verified(token, header)
        return self._actor(claims)

    def challenge(self) -> str | None:
        return f'Bearer realm="{self._settings.realm}"'

    async def aclose(self) -> None:
        await self._keys.aclose()

    async def _verified(self, token: str, header: dict[str, Any]) -> dict[str, Any]:
        settings = self._settings
        algorithm = header.get("alg")
        if algorithm not in settings.algorithms:
            raise NotAuthenticated(f"the token's algorithm {algorithm!r} is not allowed")
        kid = header.get("kid")
        key = await self._keys.key(kid)
        if key is None:
            if kid is None:
                raise NotAuthenticated("the token has no kid and the identity provider has several keys")
            raise NotAuthenticated(f"unknown signing key {kid!r}")
        try:
            return jwt.decode(
                token,
                key.key,
                algorithms=[algorithm],
                audience=settings.audience,
                issuer=settings.issuer,
                leeway=settings.leeway_s,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise NotAuthenticated("the token has expired") from exc
        except jwt.ImmatureSignatureError as exc:
            raise NotAuthenticated("the token is not yet valid") from exc
        except jwt.InvalidAudienceError as exc:
            raise NotAuthenticated("the token's audience is not this engine") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise NotAuthenticated(f"the token has no {exc.claim} claim") from exc
        except jwt.InvalidSignatureError as exc:
            raise NotAuthenticated("the token's signature does not verify") from exc
        except jwt.PyJWTError as exc:
            raise NotAuthenticated(f"invalid token: {type(exc).__name__}") from exc

    def _actor(self, claims: dict[str, Any]) -> ActorContext:
        settings = self._settings
        actor_id = _text(claims, settings.actor_claim)
        application_id = (
            _text(claims, settings.application_claim)
            if settings.application_claim
            else settings.application_id
        )
        scopes = _scopes(claims.get(settings.scope_claim))
        named = {
            scope.removeprefix(settings.scope_prefix)
            for scope in scopes
            if scope.startswith(settings.scope_prefix)
        }
        admin = settings.admin_scope is not None and settings.admin_scope in scopes
        jti = claims.get("jti")
        return ActorContext(
            actor_id=actor_id,
            application_id=application_id,
            permissions=frozenset(operation.value for operation in GRANTABLE if operation.value in named),
            credential_kind="admin" if admin else "actor",
            auth=AuthProvenance(
                method="jwt", issuer=settings.issuer, credential_id=jti if isinstance(jti, str) else None
            ),
        )


def jwt_authenticator(settings: EngineSettings, *, timeout_s: float = 10.0) -> JwtBearerAuthenticator:
    """The authenticator ``LPB_AUTH=jwt`` builds from ``LPB_JWT_*``; the caller closes it (``aclose``)."""
    if settings.jwt_issuer is None or settings.jwt_audience is None:
        raise ValueError(
            "LPB_JWT_ISSUER and LPB_JWT_AUDIENCE are required for jwt"
        )  # the settings check it first
    config = JwtSettings(
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        algorithms=settings.jwt_algorithms,
        actor_claim=settings.jwt_actor_claim,
        application_claim=settings.jwt_application_claim,
        application_id=settings.jwt_application_id,
        scope_claim=settings.jwt_scope_claim,
        scope_prefix=settings.jwt_scope_prefix,
        admin_scope=settings.jwt_admin_scope,
        leeway_s=settings.jwt_leeway_s,
    )
    client = httpx.AsyncClient(timeout=timeout_s, follow_redirects=False)
    return JwtBearerAuthenticator(config, JwksCache(client, settings.jwt_issuer, settings.jwt_jwks_url))


def _text(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value:
        raise NotAuthenticated(f"the token has no {name} claim")
    return value


def _scopes(value: object) -> set[str]:
    """RFC 9068's space-separated ``scope``, or a list such as Entra's ``scp``."""
    if isinstance(value, str):
        return set(value.split())
    if isinstance(value, list):
        return {item for item in value if isinstance(item, str)}
    return set()
