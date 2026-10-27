"""JWT bearer: OAuth 2.0 access tokens from an enterprise identity provider (#49, D45).

The engine is a resource server. It verifies a token's signature against the issuer's published keys
(JWKS) and its ``iss``, ``aud``, ``exp`` and ``nbf``, then maps its claims to an ``ActorContext``
(docs/contracts/access.md#jwt-bearer). It never runs a login. PyJWT is confined to this module.

The chain rule (docs/design-docs/extending-the-engine.md): a credential that is not a JWT, or a JWT from
another issuer, is not ours (``None``); a token from our issuer that fails a check is ``NotAuthenticated``.
When the provider's keys cannot be read, nobody can be authenticated by token: ``ProviderUnavailable``.

Only RFC 9068 access tokens are accepted by default (``typ: at+jwt``, with ``client_id``, ``jti`` and
``iat``), so an ID token or a logout token with the right audience is not an API credential. A token may live
at most ``max_lifetime_s`` (D47, #50 review).
"""

import asyncio
import binascii
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt

from livepeer_builder.adapters.auth import bearer_token
from livepeer_builder.config import EngineSettings
from livepeer_builder.contracts import ActorContext, AuthProvenance, AuthRequest, Operation
from livepeer_builder.errors import NotAuthenticated, ProviderUnavailable

_LOG = logging.getLogger(__name__)

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
    admin_read_scope: str | None = None  # an auditor, a read-only admin (D46); admin_scope wins
    leeway_s: float = 30.0
    realm: str = "livepeer-builder"
    token_types: tuple[str, ...] = ("at+jwt", "application/at+jwt")  # lower case; "jwt" is the opt-out
    max_lifetime_s: float = 3600.0  # exp - iat
    name_claim: str | None = "preferred_username"  # the display name for the operator's actor list (D48)

    @property
    def strict(self) -> bool:
        """RFC 9068 only: a plain JWT is refused, and client_id and jti are required."""
        return "jwt" not in self.token_types


UNAVAILABLE = "the identity provider's signing keys cannot be read"  # the 503 body: no URL, no client error
MAX_DOCUMENT_BYTES = 1024 * 1024  # a discovery document or key set larger than this is refused
SIGNATURE_KEY_TYPES = frozenset({"RSA", "EC", "OKP"})  # never "oct": a shared secret is not a published key


@dataclass(frozen=True)
class SigningKey:
    """A published verification key, and the algorithm it was published for, if it names one."""

    key: jwt.PyJWK
    algorithm: str | None


class JwksCache:
    """The issuer's signing keys: found through its discovery document (OpenID, then RFC 8414) unless
    ``jwks_url`` is given, and fetched again for an unknown ``kid`` at most once per ``refetch_after_s``.

    Keys older than ``max_age_s`` are refreshed in the background while requests keep using them, so a key
    the provider withdrew stops verifying within minutes and a slow provider never stalls a request. Keys
    older than ``stale_limit_s`` are not served: the request waits for a fetch (D47, #50 review).

    Only asymmetric keys for signatures count: ``kty: oct``, ``use: enc``, or ``key_ops`` without ``verify``,
    is skipped. A discovered key URL is kept only once it has served a usable key set, so a bad first one is
    discovered again; it must be https unless ``allow_http``. A document over ``MAX_DOCUMENT_BYTES`` is
    refused. After a failed fetch the provider is left alone for ``pause_after_failure_s``, counted from the
    failure, and the request is answered 503 meanwhile. Reasons go to the log (``auth.jwks_unavailable``),
    never to the response.

    One fetch at a time (D49): a background refresh, a cold start and an unknown-``kid`` refetch share one
    task, and only that task updates the cache, so an older answer can never replace a newer key set. No lock
    is held while the provider answers, and each document has one ``fetch_timeout_s`` deadline in all.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        issuer: str,
        jwks_url: str | None = None,
        *,
        max_age_s: float = 600.0,
        stale_limit_s: float = 3600.0,
        refetch_after_s: float = 60.0,
        pause_after_failure_s: float = 5.0,
        fetch_timeout_s: float = 3.0,
        allow_http: bool = False,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._issuer = issuer
        self._configured_url = jwks_url  # the operator's: never replaced by discovery
        self._discovered_url: str | None = None  # kept only after it served a usable key set
        self._max_age_s = max_age_s
        self._stale_limit_s = stale_limit_s
        self._allow_http = allow_http
        self._refetch_after_s = refetch_after_s
        self._pause_s = pause_after_failure_s
        self._timeout_s = fetch_timeout_s
        self._monotonic = monotonic
        self._keys: list[SigningKey] = []
        self._fetched_at: float | None = None  # when the cached set was requested
        self._refetched_at: float | None = None  # the last successful fetch an unknown kid caused
        self._failed_at: float | None = None  # when the last fetch failed, measured when it failed
        self._fetching: asyncio.Task[None] | None = None  # the one fetch in flight (D49)

    async def key(self, kid: str | None) -> SigningKey | None:
        """The key for ``kid``; with no ``kid``, the only key. ``None`` when the provider has no such key."""
        now = self._monotonic()
        if self._fetched_at is None or now - self._fetched_at >= self._stale_limit_s:
            await self._shared_fetch()
        elif now - self._fetched_at >= self._max_age_s:
            self._refresh_in_background()
        found = self._find(kid)
        if found is None and kid is not None and self._may_refetch(now):
            # a fetch already in flight may predate the rotation that minted this kid: wait, then ask again
            await self._settled()
            found = self._find(kid)
            if found is None:
                await self._shared_fetch()
                self._refetched_at = self._monotonic()  # only a fetch that worked uses up the minute
                found = self._find(kid)
        return found

    async def aclose(self) -> None:
        if self._fetching is not None:
            self._fetching.cancel()
            await asyncio.gather(self._fetching, return_exceptions=True)
        await self._client.aclose()

    def _refresh_in_background(self) -> None:
        if self._paused():
            return
        self._start()  # joins a fetch already in flight; nobody waits on it

    async def _shared_fetch(self) -> None:
        """The fetch in flight, or a new one. Concurrent callers share one request, and only that fetch
        publishes, so an older response can never replace a newer key set (#50 review)."""
        if self._fetching is None and self._paused():
            raise ProviderUnavailable(UNAVAILABLE)  # a fetch just failed: leave the provider alone a moment
        await asyncio.shield(self._start())  # a caller that goes away does not cancel the shared fetch

    async def _settled(self) -> None:
        """Wait for the fetch in flight, if any, whatever its outcome."""
        if self._fetching is not None:
            await asyncio.gather(asyncio.shield(self._fetching), return_exceptions=True)

    def _start(self) -> asyncio.Task[None]:
        if self._fetching is None:
            task = asyncio.create_task(self._fetch(self._monotonic()))
            task.add_done_callback(self._finished)
            self._fetching = task
        return self._fetching

    def _finished(self, task: asyncio.Task[None]) -> None:
        if self._fetching is task:
            self._fetching = None
        if not task.cancelled():
            task.exception()  # a background refresh's failure was logged by _fetch; mark it retrieved

    def _paused(self) -> bool:
        return self._failed_at is not None and self._monotonic() - self._failed_at < self._pause_s

    def _may_refetch(self, now: float) -> bool:
        return self._refetched_at is None or now - self._refetched_at >= self._refetch_after_s

    def _find(self, kid: str | None) -> SigningKey | None:
        if kid is None:
            return self._keys[0] if len(self._keys) == 1 else None
        return next((signing for signing in self._keys if signing.key.key_id == kid), None)

    async def _fetch(self, started: float) -> None:
        try:
            url = self._configured_url or self._discovered_url or await self._discover()
            keys = _signing_keys(await self._json(url))
            if not keys:
                raise _Unreadable("the key set has no usable signing keys", url)
        except Exception as exc:  # anything at all becomes 503 and a pause, never a 500
            unreadable = (
                exc
                if isinstance(exc, _Unreadable)
                else _Unreadable("unexpected error", "", f"{type(exc).__name__}: {exc}"[:300])
            )
            self._failed_at, self._discovered_url = self._monotonic(), None  # discovered again next time
            _LOG.warning(
                "the identity provider's keys cannot be read",
                extra={
                    "event": "auth.jwks_unavailable",
                    "reason": unreadable.reason,
                    "url": unreadable.url,
                    "error": unreadable.error,
                },
            )
            raise ProviderUnavailable(UNAVAILABLE) from None
        if self._configured_url is None:
            self._discovered_url = url
        self._keys, self._fetched_at, self._failed_at = keys, started, None

    async def _discover(self) -> str:
        """The ``jwks_uri`` from the OpenID configuration, or else from RFC 8414 metadata."""
        openid = self._issuer.rstrip("/") + "/.well-known/openid-configuration"
        parts = urlsplit(self._issuer)
        rfc8414 = (
            f"{parts.scheme}://{parts.netloc}/.well-known/oauth-authorization-server{parts.path.rstrip('/')}"
        )
        try:
            where, document = openid, await self._json(openid)
        except _Unreadable as missing:
            if missing.reason != "the request failed":
                raise
            try:
                where, document = rfc8414, await self._json(rfc8414)
            except _Unreadable:
                raise missing from None  # neither answered: report the OpenID one, the usual
        if document.get("issuer") != self._issuer:
            raise _Unreadable("the discovery document names another issuer", where)
        uri = document.get("jwks_uri")
        if not isinstance(uri, str) or not uri:
            raise _Unreadable("the discovery document has no jwks_uri", where)
        allowed = ("http", "https") if self._allow_http else ("https",)
        if urlsplit(uri).scheme not in allowed:
            raise _Unreadable(f"the jwks_uri scheme is not {' or '.join(allowed)}", where)
        return uri

    async def _json(self, url: str) -> dict[str, Any]:
        """One document, within ``fetch_timeout_s`` in all: a provider that trickles bytes is cut off."""
        try:
            async with asyncio.timeout(self._timeout_s), self._client.stream("GET", url) as response:
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content += chunk
                    if len(content) > MAX_DOCUMENT_BYTES:
                        raise _Unreadable(f"the answer is larger than {MAX_DOCUMENT_BYTES} bytes", url)
            body = json.loads(content)
        except (httpx.HTTPError, ValueError, TimeoutError) as exc:
            raise _Unreadable("the request failed", url, f"{type(exc).__name__}: {exc}"[:300]) from None
        if not isinstance(body, dict):
            raise _Unreadable("the answer is not a JSON object", url)
        return body


class _Unreadable(Exception):
    """Why the keys could not be read: for the log only."""

    def __init__(self, reason: str, url: str, error: str = "") -> None:
        super().__init__(reason)
        self.reason, self.url, self.error = reason, url, error


def _signing_keys(document: dict[str, Any]) -> list[SigningKey]:
    """The keys that may verify a signature: asymmetric, not ``use: enc``, and ``key_ops`` (when given) with
    ``verify``. A key PyJWT cannot load is skipped, as PyJWKSet does."""
    entries = document.get("keys")
    keys: list[SigningKey] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("use", "sig") != "sig":
            continue
        kind, declared = entry.get("kty"), entry.get("alg")
        if not isinstance(kind, str) or kind not in SIGNATURE_KEY_TYPES:
            continue
        operations = entry.get("key_ops")
        if operations is not None and (not isinstance(operations, list) or "verify" not in operations):
            continue
        try:
            key = jwt.PyJWK(entry)
        except (jwt.PyJWTError, TypeError, ValueError, KeyError):  # e.g. "alg": ["RS256"] (#50 review)
            continue
        keys.append(SigningKey(key=key, algorithm=declared if isinstance(declared, str) else None))
    return keys


class JwtBearerAuthenticator:
    def __init__(self, settings: JwtSettings, keys: JwksCache) -> None:
        self._settings = settings
        self._keys = keys

    async def authenticate(self, request: AuthRequest) -> ActorContext | None:
        token = bearer_token(request)
        if token is None or token.count(".") != 2:
            return None
        if _unverified_issuer(token) != self._settings.issuer:
            return None  # not a JWT, or another issuer's: another authenticator's credential
        try:  # ours from here: a header PyJWT refuses (an unknown crit, a kid that is not text) is a 401
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise NotAuthenticated(f"the token's header is invalid: {type(exc).__name__}") from exc
        claims = await self._verified(token, header)
        return self._actor(claims)

    def challenge(self) -> str | None:
        return f'Bearer realm="{self._settings.realm}"'

    async def aclose(self) -> None:
        await self._keys.aclose()

    def _check_type(self, header: dict[str, Any]) -> None:
        """RFC 9068 4: an access token says ``at+jwt``; anything else (an ID token says ``JWT``) is refused
        unless the operator opted in to plain JWTs, which may also omit ``typ``."""
        kind = header.get("typ")
        if kind is None and not self._settings.strict:
            return
        if not isinstance(kind, str) or kind.lower() not in self._settings.token_types:
            raise NotAuthenticated(f"the token type {kind!r} is not an access token (at+jwt)")

    async def _verified(self, token: str, header: dict[str, Any]) -> dict[str, Any]:
        settings = self._settings
        algorithm = header.get("alg")
        if algorithm not in settings.algorithms:
            raise NotAuthenticated(f"the token's algorithm {algorithm!r} is not allowed")
        self._check_type(header)
        kid = header.get("kid")
        signing = await self._keys.key(kid)
        if signing is None:
            if kid is None:
                raise NotAuthenticated("the token has no kid and the identity provider has several keys")
            raise NotAuthenticated(f"unknown signing key {kid!r}")
        if signing.algorithm is not None and signing.algorithm != algorithm:  # RFC 8725 3.1
            raise NotAuthenticated(
                f"the token's algorithm {algorithm!r} is not the one its key was published for"
            )
        try:
            claims = jwt.decode(
                token,
                signing.key.key,
                algorithms=[algorithm],
                audience=settings.audience,
                issuer=settings.issuer,
                leeway=settings.leeway_s,
                options={"require": _required(settings)},
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
        if "cnf" in claims:  # bound to a certificate (RFC 8705) or a DPoP key (RFC 9449): fail closed
            raise NotAuthenticated("the token is sender-constrained (cnf), which this engine cannot verify")
        expires, issued = claims["exp"], claims["iat"]
        if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in (expires, issued)):
            raise NotAuthenticated("the token's exp and iat must each be a NumericDate (a number)")
        lifetime = expires - issued
        if lifetime > settings.max_lifetime_s:
            raise NotAuthenticated(
                f"the token's lifetime of {lifetime:.0f}s is longer than {settings.max_lifetime_s:.0f}s"
            )
        return claims

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
        auditor = settings.admin_read_scope is not None and settings.admin_read_scope in scopes
        jti = claims.get("jti")
        name = claims.get(settings.name_claim) if settings.name_claim else None
        return ActorContext(
            actor_id=actor_id,
            application_id=application_id,
            permissions=frozenset(operation.value for operation in GRANTABLE if operation.value in named),
            credential_kind="admin" if admin else "auditor" if auditor else "actor",
            auth=AuthProvenance(
                method="jwt", issuer=settings.issuer, credential_id=jti if isinstance(jti, str) else None
            ),
            display_name=name[:200] if isinstance(name, str) and name else None,
        )


def jwt_authenticator(settings: EngineSettings, *, timeout_s: float = 3.0) -> JwtBearerAuthenticator:
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
        admin_read_scope=settings.jwt_admin_read_scope,
        leeway_s=settings.jwt_leeway_s,
        token_types=settings.jwt_token_types,
        max_lifetime_s=settings.jwt_max_lifetime_s,
        name_claim=settings.jwt_name_claim,
    )
    client = httpx.AsyncClient(timeout=timeout_s, follow_redirects=False)
    keys = JwksCache(
        client,
        settings.jwt_issuer,
        settings.jwt_jwks_url,
        fetch_timeout_s=timeout_s,
        allow_http=settings.jwt_allow_http,
    )
    return JwtBearerAuthenticator(config, keys)


def _required(settings: JwtSettings) -> list[str]:
    """Claims a token must carry: ``iat`` always (the lifetime cap needs it), and RFC 9068's when strict."""
    common = ["exp", "iss", "aud", "iat"]
    return [*common, "client_id", "jti"] if settings.strict else common


def _unverified_issuer(token: str) -> object:
    """The payload's ``iss`` before anything is verified, or ``None`` when the payload is not a JSON object.
    Read without PyJWT so a header it refuses cannot make our token look like someone else's."""
    try:
        claims = json.loads(jwt.utils.base64url_decode(token.split(".")[1]))
    except (binascii.Error, ValueError):
        return None
    return claims.get("iss") if isinstance(claims, dict) else None


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
