"""Config layer: engine settings from ``LPB_*`` environment variables (docs/RELIABILITY.md#configuration)."""

import base64
import os
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, SecretStr, field_validator, model_validator

from livepeer_builder.contracts import Contract, Currency, Operation
from livepeer_builder.contracts.common import GrantId

ENV_PREFIX = "LPB_"
# The clearinghouse's runner list, after its address: what an enterprise's engine reads by default (D87).
CLEARINGHOUSE_RUNNER_LIST = "/v1/clearinghouse/discovery"
# What selects a payment clearinghouse: set together or not at all (D84, #168).
_CLEARINGHOUSE = ("clearinghouse_url", "clearinghouse_token", "clearinghouse_tenant_id")
# What self-hosted payments sets. An engine that uses a clearinghouse holds none of them.
_SELF_HOSTED = ("batteries_url", "batteries_token", "batteries_grants_token", "signer_url")
DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./livepeer-builder.db"
AuthMode = Literal["api_keys", "jwt"]
# Scopes an ordinary token carries: an admin scope must be none of these, or every user is an admin (D47).
RESERVED_SCOPES = frozenset(
    {"openid", "profile", "email", "address", "phone", "offline_access"}
    | {operation.value for operation in Operation}
)
# JWT signatures the engine accepts: asymmetric only, so a verification key can never sign (D45).
ASYMMETRIC_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)


def _reserved(scope: str, prefix: str) -> bool:
    """A scope ordinary tokens carry, as written or once the scope prefix is removed (#50 review):
    with LPB_JWT_SCOPE_PREFIX=api:, api:jobs.run is the jobs.run every builder holds."""
    return scope in RESERVED_SCOPES or (bool(prefix) and scope.removeprefix(prefix) in RESERVED_SCOPES)


def _env(name: str) -> str:
    return ENV_PREFIX + name.upper()


def _runner_list_by_default(values: dict[str, Any]) -> dict[str, Any]:
    """``values`` with the clearinghouse's runner list as the discovery address, when a clearinghouse is
    named and no discovery address is (D87). An address that cannot be read is left for its validator."""
    address = values.get("clearinghouse_url")
    if values.get("discovery_url") or not isinstance(address, str) or not address.strip():
        return values
    return {**values, "discovery_url": address.strip().rstrip("/") + CLEARINGHOUSE_RUNNER_LIST}


class EngineSettings(Contract):
    # A refused value is never echoed: several settings are secrets, or may carry one (signer_url).
    model_config = ConfigDict(hide_input_in_errors=True)  # merged with Contract's frozen, extra="forbid"

    engine_id: str = Field(default="livepeer-builder", min_length=1)  # namespaces provider idempotency keys

    # persistence
    database_url: str = DEFAULT_DATABASE_URL  # durable by default; there is no silent in-memory store
    auto_migrate: bool = True  # production can set False and run `lpb migrate` explicitly
    retention_jobs_days: int | None = Field(default=None, ge=1)  # None keeps completed jobs forever
    retention_usage_days: int | None = Field(default=None, ge=1)  # None keeps cached usage rows forever
    retention_interval_s: float = Field(default=6 * 3600, gt=0)  # how often the retention purge runs

    # network
    signer_url: str | None = None
    # Required. GET this URL for the livepeer/node /discovery contract (D63). The engine does not start
    # without it, and it does not fall back to a built-in list.
    discovery_url: str
    discovery_interval_s: float = Field(default=15.0, gt=0)
    # How old an orchestrator-stamped rate may be before its offering is stale (D62 (3)). D63: the reader
    # keeps price_info as advertised, and this age is catalog policy.
    rate_max_age_s: float = Field(default=600.0, gt=0)
    max_payment_challenge_retries: int = Field(default=3, ge=0)

    # admission (checked before any payment)
    max_concurrent_jobs: int | None = Field(default=64, ge=1)
    max_concurrent_jobs_per_app: int | None = Field(default=None, ge=1)

    # paid failover (D55): how many paid attempts a job may fail over from; 1 turns paid failover off
    max_paid_attempts: int = Field(default=2, ge=1, le=10)

    # streaming (docs/contracts/engine.md#streaming)
    stream_finalize_timeout_s: float = Field(default=10.0, gt=0)  # the most a stream's closing writes take
    stream_drain_timeout_s: float = Field(default=30.0, ge=0)  # engine stop waits this long for open streams
    stream_settle_s: float = Field(default=1.0, ge=0)  # after a disconnect, time for the runner to end
    stream_open_grace_s: float = Field(default=5.0, gt=0)  # how far past its deadline an open may run

    # payments: one allocation and key per actor, the only mode (D82); no setting holds a payment key
    default_currency: Currency = "USD"
    batteries_url: str | None = None  # management API and usage feed
    batteries_token: SecretStr | None = None  # allocations and usage; not grant administration
    batteries_grants_token: SecretStr | None = None  # grants.read and grants.create for the admin desk
    batteries_timeout_s: float = Field(default=30.0, gt=0)  # each Batteries HTTP request
    secret_key: SecretStr | None = None  # Fernet key for the encrypted-database SecretStore
    cost_sync_interval_s: float = Field(default=30.0, gt=0)
    # An engine that uses a payment clearinghouse (D84, #168): its address, the tenant admin key and the
    # tenant's id, which is its grant's id (D83). Set together or not at all, and never with the
    # Batteries or signer settings: the payment ports then reach the clearinghouse and nothing else.
    clearinghouse_url: str | None = None
    clearinghouse_token: SecretStr | None = None  # the tenant admin key; sent as a bearer, never logged
    clearinghouse_tenant_id: GrantId | None = None
    clearinghouse_timeout_s: float = Field(default=30.0, gt=0)  # each request to the clearinghouse
    # an admin command still started after this long is resumed by a repeat (D57, Q3: 4x batteries_timeout_s)
    operation_stale_s: float = Field(default=120.0, gt=0)

    # access (docs/contracts/access.md#jwt-bearer, D45)
    auth: tuple[AuthMode, ...] = ("api_keys",)  # LPB_AUTH=api_keys | jwt | jwt,api_keys
    jwt_issuer: str | None = None  # matched exactly against iss; its discovery document gives the JWKS
    jwt_jwks_url: str | None = None  # overrides discovery
    jwt_audience: str | None = None  # must be in aud: this engine
    jwt_algorithms: tuple[str, ...] = ("RS256", "ES256")
    jwt_actor_claim: str = Field(default="sub", min_length=1)
    jwt_tenant_claim: str | None = None  # None: every token is in jwt_tenant_id
    jwt_tenant_id: str = Field(default="default", min_length=1, max_length=200)  # as records store it
    jwt_scope_claim: str = Field(default="scope", min_length=1)
    jwt_scope_prefix: str = ""  # stripped before scopes are matched to operations
    jwt_admin_scope: str | None = None  # None: no token is an admin credential
    jwt_admin_read_scope: str | None = None  # an auditor: a read-only admin (D46); the admin scope wins
    jwt_leeway_s: float = Field(default=30.0, ge=0)
    # RFC 9068 access tokens only; add "jwt" for a provider that cannot send at+jwt (weaker, D47)
    jwt_token_types: tuple[str, ...] = ("at+jwt", "application/at+jwt")
    jwt_max_lifetime_s: float = Field(default=3600.0, gt=0)  # exp - iat; longer tokens are refused (D47)
    # The person's display name, for the operator's actor list (D48); empty turns it off
    jwt_name_claim: str | None = "preferred_username"
    jwt_allow_http: bool = False  # dev only: an http issuer or key URL, such as a local Keycloak (D47)

    # operations
    log_json: bool = True
    http_host: str = "127.0.0.1"
    http_port: int = Field(default=8080, ge=1, le=65535)
    # The private listener (D84, #167). Unset: one listener, which serves every route. Set: http_host and
    # http_port are the public listener, without the routes only a clearinghouse admin calls, and this
    # one serves every route to a clearinghouse admin only. Loopback unless the deployment says otherwise.
    http_private_host: str = "127.0.0.1"
    http_private_port: int | None = Field(default=None, ge=1, le=65535)
    # A /v1/ body over this is refused with 413 before it is read (D59); fits an 8 MiB raw_body as base64
    http_max_request_bytes: int = Field(default=16 * 1024 * 1024, ge=1024)

    @field_validator("auth", "jwt_algorithms", "jwt_token_types", mode="before")
    @classmethod
    def _comma_separated(cls, value: Any) -> Any:
        return (
            tuple(part.strip() for part in value.split(",") if part.strip())
            if isinstance(value, str)
            else value
        )

    @field_validator("signer_url")
    @classmethod
    def _signer_url_holds_no_secret(cls, value: str | None) -> str | None:
        """Error text and logs quote the signer URL (D58), so it may carry no userinfo, query or fragment;
        the signer's credential is the actor's payment key, sent as a header."""
        if value is None:
            return None
        # A base URL never needs @, ? or #, wherever they sit: a scheme-relative //user:pw@host puts the
        # userinfo in the path, where urlsplit does not look, and the SDK's error would quote it (#78).
        if any(mark in value for mark in "@?#"):
            raise ValueError("signer_url must not carry userinfo, a query or a fragment")
        parts = urlsplit(value if "://" in value else f"https://{value}")  # the SDK's reading of host:port
        if not parts.hostname:
            raise ValueError("signer_url needs a host, as http(s)://host[:port] or host:port")
        return value

    @model_validator(mode="before")
    @classmethod
    def _clearinghouse_runner_list_is_the_default(cls, values: Any) -> Any:
        return _runner_list_by_default(values) if isinstance(values, dict) else values

    @field_validator("clearinghouse_url")
    @classmethod
    def _clearinghouse_url_is_an_origin(cls, value: str | None) -> str | None:
        """A scheme, a host and an optional port, and nothing after them. The engine signs at the
        clearinghouse through the gateway SDK, which keeps that much of a signer's address and drops the
        rest: with a path, the tenant's routes would be called under it and the signer's two paths at
        the root. Error text and logs quote the address, so it carries no userinfo either: the tenant
        admin key is a header. A trailing slash alone names no path and is taken off. The value is not
        echoed."""
        if value is None:
            return None
        value = value.strip()
        try:
            parts = urlsplit(value)
            valid = (
                parts.scheme in ("http", "https")
                and bool(parts.hostname)
                and parts.path in ("", "/")
                and not any(mark in value for mark in "@?#")
                and not any(char.isspace() for char in value)
            )
        except ValueError:  # an unclosed IPv6 literal, for example
            valid = False
        if not valid:
            raise ValueError(
                "LPB_CLEARINGHOUSE_URL must be http:// or https://, a host and an optional port, with no "
                "path, query, fragment or userinfo, such as https://clearinghouse.example: the signer's "
                "two paths are called at its root"
            )
        return f"{parts.scheme}://{parts.netloc}"

    @field_validator("discovery_url")
    @classmethod
    def _http_discovery_url(cls, value: str) -> str:
        # Checked here, not on the first poll: a scheme-less host:port would fail every refresh, quietly.
        value = value.strip()  # urlsplit ignores padding; httpx refuses a newline and sends a space as %20
        # A failed refresh's error quotes this URL in the log (/readyz shows only a code, #113 C7);
        # httpx also sends a URL's userinfo as Basic auth. So, like signer_url (D58), it carries no userinfo,
        # query or fragment, and this error does not echo it.
        if any(mark in value for mark in "@?#"):
            raise ValueError("LPB_DISCOVERY_URL must not carry userinfo, a query or a fragment")
        try:
            parts = urlsplit(value)
            valid = (
                parts.scheme in ("http", "https")
                and bool(parts.hostname)
                and not any(char.isspace() for char in value)
            )
        except ValueError:  # an unclosed IPv6 literal, for example
            valid = False
        if not valid:
            raise ValueError(
                f"LPB_DISCOVERY_URL must be an http:// or https:// URL with a host, such as "
                f"http://discovery:8939/discovery; got {value!r}"
            )
        return value

    @field_validator("auth")
    @classmethod
    def _one_of_each(cls, value: tuple[AuthMode, ...]) -> tuple[AuthMode, ...]:
        if not value:
            raise ValueError("auth needs at least one mode: api_keys, jwt, or jwt,api_keys")
        if len(set(value)) != len(value):
            raise ValueError("auth names a mode twice")
        return value

    @field_validator("jwt_algorithms")
    @classmethod
    def _asymmetric(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        refused = sorted(set(value) - ASYMMETRIC_ALGORITHMS)
        if not value or refused:
            raise ValueError(f"jwt_algorithms takes asymmetric algorithms only; refused {refused}")
        return value

    @field_validator("jwt_token_types")
    @classmethod
    def _token_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("jwt_token_types needs at least one token type, such as at+jwt")
        return tuple(kind.lower() for kind in value)

    @model_validator(mode="after")
    def _stale_outlasts_batteries_calls(self) -> "EngineSettings":
        # A resume is a lease, not a lock (D57): it must not start while the presumed-dead run may still have
        # a Batteries call in flight. The ledger cancels any run at half the stale window; the longest
        # ordinary command makes two calls (create grant then read it back; rotate: create, then revoke),
        # each under one deadline of the timeout (adapters/batteries.py), so they fit in that half. Grant
        # creation has no provider idempotency key, so an overlap there would create a second funded grant.
        # A process paused longer than that (a frozen VM) is the lease's limit: only a provider idempotency
        # key would close it.
        if self.operation_stale_s < 4 * self.batteries_timeout_s:
            raise ValueError(
                f"LPB_OPERATION_STALE_S ({self.operation_stale_s:g}) must be at least four times "
                f"LPB_BATTERIES_TIMEOUT_S ({self.batteries_timeout_s:g})"
            )
        return self

    @model_validator(mode="after")
    def _a_clearinghouse_is_whole_and_alone(self) -> "EngineSettings":
        """A half setting, or a clearinghouse beside self-hosted payments, is refused before the engine
        starts (#168). Only names are said: three of these settings are secrets."""
        named = [name for name in _CLEARINGHOUSE if getattr(self, name) is not None]
        if not named:
            return self
        if len(named) < len(_CLEARINGHOUSE):
            missing = ", ".join(_env(name) for name in _CLEARINGHOUSE if name not in named)
            raise ValueError(
                f"{', '.join(map(_env, _CLEARINGHOUSE))} are set together or not at all; missing: {missing}"
            )
        mixed = [_env(name) for name in _SELF_HOSTED if getattr(self, name) is not None]
        if mixed:
            raise ValueError(
                f"{', '.join(mixed)} cannot be set with LPB_CLEARINGHOUSE_URL: an engine that uses a "
                "payment clearinghouse reaches Batteries and the signer through it, and holds no address "
                "or credential of theirs"
            )
        if self.operation_stale_s < 4 * self.clearinghouse_timeout_s:  # as for Batteries, just above
            raise ValueError(
                f"LPB_OPERATION_STALE_S ({self.operation_stale_s:g}) must be at least four times "
                f"LPB_CLEARINGHOUSE_TIMEOUT_S ({self.clearinghouse_timeout_s:g})"
            )
        return self

    @property
    def uses_clearinghouse(self) -> bool:
        """Whether the payment ports reach a payment clearinghouse, and not Batteries and a signer."""
        return self.clearinghouse_url is not None

    @property
    def clearinghouse_runner_list_token(self) -> str | None:
        """The tenant admin key, when the discovery address is the clearinghouse's runner list, which
        needs it (D87); otherwise ``None``. The address must be that route exactly, scheme, host, port
        and path: the key is sent to no other."""
        if self.clearinghouse_url is None or self.clearinghouse_token is None:
            return None
        if self.discovery_url != self.clearinghouse_url + CLEARINGHOUSE_RUNNER_LIST:
            return None
        return self.clearinghouse_token.get_secret_value()

    @model_validator(mode="after")
    def _two_listeners_have_two_addresses(self) -> "EngineSettings":
        if (self.http_private_host, self.http_private_port) == (self.http_host, self.http_port):
            raise ValueError(
                f"LPB_HTTP_PRIVATE_PORT ({self.http_private_port}) is the public listener's port on the "
                "same host: the private listener needs an address of its own"
            )
        return self

    @model_validator(mode="after")
    def _jwt_is_configured(self) -> "EngineSettings":
        if "jwt" in self.auth:
            for name in ("jwt_issuer", "jwt_audience"):
                if not getattr(self, name):
                    raise ValueError(f"LPB_{name.upper()} is required when LPB_AUTH includes jwt")
        if not self.jwt_allow_http:
            for name in ("jwt_issuer", "jwt_jwks_url"):
                value = getattr(self, name)
                if value and not value.startswith("https://"):
                    raise ValueError(
                        f"LPB_{name.upper()} must be https (LPB_JWT_ALLOW_HTTP=true for local development)"
                    )
        if self.jwt_admin_scope is not None and _reserved(self.jwt_admin_scope, self.jwt_scope_prefix):
            raise ValueError(
                f"jwt_admin_scope {self.jwt_admin_scope!r} is a scope ordinary tokens carry, "
                "so every user would be an admin"
            )
        read = self.jwt_admin_read_scope
        if read is not None and (_reserved(read, self.jwt_scope_prefix) or read == self.jwt_admin_scope):
            raise ValueError(
                f"jwt_admin_read_scope {read!r} is a scope ordinary tokens carry, or the admin scope, "
                "so it would not mark a read-only admin"
            )
        return self

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, defaults: Mapping[str, Any] | None = None
    ) -> "EngineSettings":
        """Build settings from ``LPB_<FIELD>`` variables; unset variables keep their defaults. ``defaults``
        fills fields the environment leaves unset, by field name: the CLI commands that serve nothing give
        ``discovery_url`` one (#112, C23)."""
        source = os.environ if env is None else env
        values: dict[str, Any] = {}
        for name in cls.model_fields:
            raw = source.get(ENV_PREFIX + name.upper())
            if raw is not None and raw != "":
                values[name] = raw
        # before the caller's defaults: a clearinghouse's runner list comes before a command's stand-in
        values = _runner_list_by_default(values)
        return cls.model_validate(dict(defaults or {}) | values)

    @classmethod
    def database_url_from_env(cls, env: Mapping[str, str] | None = None) -> str:
        """``LPB_DATABASE_URL`` alone, read as ``from_env`` reads it: for the commands that open only the
        store (``lpb migrate``, ``revision``, ``backup``, ``restore``), which need no other setting
        (#112, C23)."""
        source = os.environ if env is None else env
        return source.get(ENV_PREFIX + "DATABASE_URL") or DEFAULT_DATABASE_URL


def generate_secret_key() -> str:
    """A new ``LPB_SECRET_KEY``: 32 random bytes, URL-safe base64 (the Fernet key format)."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()
