"""Config layer: engine settings from ``LPB_*`` environment variables (docs/RELIABILITY.md#configuration)."""

import base64
import os
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from livepeer_builder.contracts import Contract, Currency, Operation

ENV_PREFIX = "LPB_"
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


# What a refused admin scope is, in the refusal's words. No refusal repeats the value it was given.
_ORDINARY_SCOPE = (
    "a scope ordinary tokens carry (an operation's name or a standard scope, as written or after "
    "LPB_JWT_SCOPE_PREFIX)"
)


def _reserved(scope: str, prefix: str) -> bool:
    """A scope ordinary tokens carry, as written or once the scope prefix is removed (#50 review):
    with LPB_JWT_SCOPE_PREFIX=api:, api:jobs.run is the jobs.run every builder holds."""
    return scope in RESERVED_SCOPES or (bool(prefix) and scope.removeprefix(prefix) in RESERVED_SCOPES)


class EngineSettings(Contract):
    # A refused value is never echoed: several settings are secrets, or may carry one (signer_url). This
    # hides it from str(error); _a_refusal_holds_no_value, below every rule, from every other form. That
    # keeps each rule's words, so no rule's words quote a value given: they name the setting and say what
    # it needs (tests/unit/test_settings_refusals.py tries every refusal with a value of its own).
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
    batteries_operator_token: SecretStr | None = None  # grants.read and grants.create for the admin desk
    batteries_timeout_s: float = Field(default=30.0, gt=0)  # each Batteries HTTP request
    secret_key: SecretStr | None = None  # Fernet key for the encrypted-database SecretStore
    cost_sync_interval_s: float = Field(default=30.0, gt=0)
    # an admin command still started after this long is resumed by a repeat (D57, Q3: 4x batteries_timeout_s)
    operation_stale_s: float = Field(default=120.0, gt=0)

    # access (docs/contracts/access.md#jwt-bearer, D45)
    auth: tuple[AuthMode, ...] = ("api_keys",)  # LPB_AUTH=api_keys | jwt | jwt,api_keys
    jwt_issuer: str | None = None  # matched exactly against iss; its discovery document gives the JWKS
    jwt_jwks_url: str | None = None  # overrides discovery
    jwt_audience: str | None = None  # must be in aud: this engine
    jwt_algorithms: tuple[str, ...] = ("RS256", "ES256")
    jwt_actor_claim: str = Field(default="sub", min_length=1)
    jwt_application_claim: str | None = None  # None: every token is in jwt_application_id
    jwt_application_id: str = Field(default="default", min_length=1, max_length=200)  # as records store it
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
                "LPB_DISCOVERY_URL must be an http:// or https:// URL with a host and no whitespace, such "
                "as http://discovery:8939/discovery"
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
        if not value or set(value) - ASYMMETRIC_ALGORITHMS:
            raise ValueError(
                "jwt_algorithms takes asymmetric algorithms only, at least one of: "
                + ", ".join(sorted(ASYMMETRIC_ALGORITHMS, key=str.lower))
            )
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
            raise ValueError("LPB_OPERATION_STALE_S must be at least four times LPB_BATTERIES_TIMEOUT_S")
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
            raise ValueError(f"jwt_admin_scope is {_ORDINARY_SCOPE}, so every user would be an admin")
        read = self.jwt_admin_read_scope
        if read is not None and (_reserved(read, self.jwt_scope_prefix) or read == self.jwt_admin_scope):
            raise ValueError(
                f"jwt_admin_read_scope is {_ORDINARY_SCOPE}, or the admin scope, so it would not mark a "
                "read-only admin"
            )
        return self

    # The last model validator: pydantic runs each one defined after it outside it, where its refusal would
    # carry every setting given (tests/unit/test_settings_refusals.py fails if one is).
    @model_validator(mode="wrap")
    @classmethod
    def _a_refusal_holds_no_value(
        cls, values: Any, handler: ModelWrapValidatorHandler["EngineSettings"]
    ) -> "EngineSettings":
        """A refusal names each setting, with its type and its words, and holds no value given (#162
        review). ``hide_input_in_errors`` hides values from ``str(error)`` only: ``errors()`` and
        ``json()`` keep them, and a whole-settings rule's or a missing setting's is every setting given,
        credentials too. A rule's exception is kept as its words: its traceback holds the settings. The
        words are the rule's own, so a rule that quoted what it refused would put the value back."""
        try:
            return handler(values)
        except ValidationError as refused:
            title, details = refused.title, refused.errors(include_url=False)
        # Raised here, outside the except block, so the refusal does not chain the error that holds them.
        raise ValidationError.from_exception_data(title, [_without_input(detail) for detail in details])

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, defaults: Mapping[str, Any] | None = None
    ) -> "EngineSettings":
        """Build settings from ``LPB_<FIELD>`` variables; unset variables keep their defaults. ``defaults``
        fills fields the environment leaves unset, by field name: the CLI commands that serve nothing give
        ``discovery_url`` one (#112, C23)."""
        source = os.environ if env is None else env
        values: dict[str, Any] = dict(defaults or {})
        for name in cls.model_fields:
            raw = source.get(ENV_PREFIX + name.upper())
            if raw is not None and raw != "":
                values[name] = raw
        return cls.model_validate(values)

    @classmethod
    def database_url_from_env(cls, env: Mapping[str, str] | None = None) -> str:
        """``LPB_DATABASE_URL`` alone, read as ``from_env`` reads it: for the commands that open only the
        store (``lpb migrate``, ``revision``, ``backup``, ``restore``), which need no other setting
        (#112, C23)."""
        source = os.environ if env is None else env
        return source.get(ENV_PREFIX + "DATABASE_URL") or DEFAULT_DATABASE_URL


_HIDDEN_INPUT = "[hidden]"  # what a refusal's errors() and json() say was given


def _without_input(detail: Mapping[str, Any]) -> Any:  # one of pydantic's InitErrorDetails
    """A refused setting as pydantic reported it, with ``_HIDDEN_INPUT`` for what was given. A rule's
    exception in the context becomes its message, so the refusal's words stay the rule's own."""
    line = {"type": detail["type"], "loc": detail["loc"], "input": _HIDDEN_INPUT}
    if "ctx" in detail:
        line["ctx"] = {
            name: str(value) if isinstance(value, BaseException) else value
            for name, value in detail["ctx"].items()
        }
    return line


def generate_secret_key() -> str:
    """A new ``LPB_SECRET_KEY``: 32 random bytes, URL-safe base64 (the Fernet key format)."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()
