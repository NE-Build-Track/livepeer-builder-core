"""Config layer: engine settings from ``LPB_*`` environment variables (docs/RELIABILITY.md#configuration)."""

import os
from collections.abc import Mapping

from pydantic import Field, SecretStr

from livepeer_builder.contracts import AllocationMode, Contract, Currency

ENV_PREFIX = "LPB_"
DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./livepeer-builder.db"


class EngineSettings(Contract):
    engine_id: str = Field(default="livepeer-builder", min_length=1)  # namespaces provider idempotency keys

    # persistence
    database_url: str = DEFAULT_DATABASE_URL  # durable by default; there is no silent in-memory store
    auto_migrate: bool = True  # production can set False and run `lpb migrate` explicitly
    retention_jobs_days: int | None = Field(default=None, ge=1)  # None keeps completed jobs forever
    retention_usage_days: int | None = Field(default=None, ge=1)  # None keeps cached usage rows forever

    # network
    signer_url: str | None = None
    discovery_url: str | None = None  # overrides the signer's discovery endpoint
    discovery_interval_s: float = Field(default=15.0, gt=0)
    max_payment_challenge_retries: int = Field(default=3, ge=0)

    # admission (checked before any payment)
    max_concurrent_jobs: int | None = Field(default=64, ge=1)
    max_concurrent_jobs_per_app: int | None = Field(default=None, ge=1)

    # payments
    allocation_mode: AllocationMode = "per_actor"
    default_currency: Currency = "USD"
    shared_payment_key: SecretStr | None = None  # per_application mode: the one lpg_ key
    batteries_url: str | None = None  # management API and usage feed
    batteries_token: SecretStr | None = None  # grants.fund for the configured grant; no grants.*
    grant_id: str | None = None  # the operator-created grant the engine provisions under
    shared_allocation_id: str | None = None  # per_application: the allocation behind shared_payment_key
    secret_key: SecretStr | None = None  # Fernet key for the encrypted-database SecretStore
    cost_sync_interval_s: float = Field(default=30.0, gt=0)

    # operations
    log_json: bool = True
    http_host: str = "127.0.0.1"
    http_port: int = Field(default=8080, ge=1, le=65535)

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
