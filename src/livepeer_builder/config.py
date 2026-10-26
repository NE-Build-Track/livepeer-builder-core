"""Config layer: engine settings from ``LPB_*`` environment variables (docs/RELIABILITY.md#configuration)."""

import base64
import os
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator

from livepeer_builder.contracts import AllocationMode, Contract, Currency, Operation

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


class EngineSettings(Contract):
    engine_id: str = Field(default="livepeer-builder", min_length=1)  # namespaces provider idempotency keys

    # persistence
    database_url: str = DEFAULT_DATABASE_URL  # durable by default; there is no silent in-memory store
    auto_migrate: bool = True  # production can set False and run `lpb migrate` explicitly
    retention_jobs_days: int | None = Field(default=None, ge=1)  # None keeps completed jobs forever
    retention_usage_days: int | None = Field(default=None, ge=1)  # None keeps cached usage rows forever
    retention_interval_s: float = Field(default=6 * 3600, gt=0)  # how often the retention purge runs

    # network
    signer_url: str | None = None
    discovery_url: str | None = None  # livepeer-node /discovery inventory: the discovery service (#45)
    discovery_interval_s: float = Field(default=15.0, gt=0)
    max_payment_challenge_retries: int = Field(default=3, ge=0)

    # admission (checked before any payment)
    max_concurrent_jobs: int | None = Field(default=64, ge=1)
    max_concurrent_jobs_per_app: int | None = Field(default=None, ge=1)

    # streaming (docs/contracts/engine.md#streaming)
    stream_finalize_timeout_s: float = Field(default=10.0, gt=0)  # the most a stream's closing writes take
    stream_drain_timeout_s: float = Field(default=30.0, ge=0)  # engine stop waits this long for open streams
    stream_settle_s: float = Field(default=1.0, ge=0)  # after a disconnect, time for the runner to end
    stream_open_grace_s: float = Field(default=5.0, gt=0)  # how far past its deadline an open may run

    # payments
    allocation_mode: AllocationMode = "per_actor"
    default_currency: Currency = "USD"
    shared_payment_key: SecretStr | None = None  # per_application mode: the one lpg_ key
    batteries_url: str | None = None  # management API and usage feed
    batteries_token: SecretStr | None = None  # allocations and usage; not grant administration
    batteries_operator_token: SecretStr | None = None  # grants.read and grants.create for the admin desk
    batteries_timeout_s: float = Field(default=30.0, gt=0)  # each Batteries HTTP request
    shared_allocation_id: str | None = None  # per_application: the allocation behind shared_payment_key
    secret_key: SecretStr | None = None  # Fernet key for the encrypted-database SecretStore
    cost_sync_interval_s: float = Field(default=30.0, gt=0)

    # access (docs/contracts/access.md#jwt-bearer, D45)
    auth: tuple[AuthMode, ...] = ("api_keys",)  # LPB_AUTH=api_keys | jwt | jwt,api_keys
    jwt_issuer: str | None = None  # matched exactly against iss; its discovery document gives the JWKS
    jwt_jwks_url: str | None = None  # overrides discovery
    jwt_audience: str | None = None  # must be in aud: this engine
    jwt_algorithms: tuple[str, ...] = ("RS256", "ES256")
    jwt_actor_claim: str = Field(default="sub", min_length=1)
    jwt_application_claim: str | None = None  # None: every token is in jwt_application_id
    jwt_application_id: str = Field(default="default", min_length=1)
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

    @field_validator("auth", "jwt_algorithms", "jwt_token_types", mode="before")
    @classmethod
    def _comma_separated(cls, value: Any) -> Any:
        return (
            tuple(part.strip() for part in value.split(",") if part.strip())
            if isinstance(value, str)
            else value
        )

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
        if self.jwt_admin_scope is not None and self.jwt_admin_scope in RESERVED_SCOPES:
            raise ValueError(
                f"jwt_admin_scope {self.jwt_admin_scope!r} is a scope ordinary tokens carry, "
                "so every user would be an admin"
            )
        read = self.jwt_admin_read_scope
        if read is not None and (read in RESERVED_SCOPES or read == self.jwt_admin_scope):
            raise ValueError(
                f"jwt_admin_read_scope {read!r} is a scope ordinary tokens carry, or the admin scope, "
                "so it would not mark a read-only admin"
            )
        return self

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "EngineSettings":
        """Build settings from ``LPB_<FIELD>`` variables; unset variables keep their defaults."""
        source = os.environ if env is None else env
        values: dict[str, str] = {}
        for name in cls.model_fields:
            raw = source.get(ENV_PREFIX + name.upper())
            if raw is not None and raw != "":
                values[name] = raw
        return cls.model_validate(values)


def generate_secret_key() -> str:
    """A new ``LPB_SECRET_KEY``: 32 random bytes, URL-safe base64 (the Fernet key format)."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()
