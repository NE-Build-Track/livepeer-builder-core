"""The discovery service's settings, from ``LPD_*`` environment variables.

A separate prefix from the engine's ``LPB_*``: the two run as separate processes, and the engine's
own ``LPB_DISCOVERY_URL`` and ``LPB_DISCOVERY_INTERVAL_S`` would otherwise collide.
"""

import os
from collections.abc import Mapping
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from livepeer_builder.discovery.onchain import ARBITRUM_AI_SERVICE_REGISTRY, ARBITRUM_BONDING_MANAGER

ENV_PREFIX = "LPD_"


class DiscoverySettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    directory: Literal["static", "onchain"] = "static"
    orchestrators: str | None = None  # static: comma-separated base URLs, or a file with one per line
    arbitrum_rpc_url: SecretStr | None = None  # onchain: usually carries the provider's key
    bonding_manager: str = ARBITRUM_BONDING_MANAGER
    ai_service_registry: str = ARBITRUM_AI_SERVICE_REGISTRY
    directory_interval_s: float = Field(default=3600.0, gt=0)  # the active set changes per round
    directory_timeout_s: float = Field(default=300.0, gt=0)  # a stuck load gives up; probing goes on
    probe_interval_s: float = Field(default=30.0, gt=0)
    probe_timeout_s: float = Field(default=5.0, gt=0)
    max_failed_refreshes: int = Field(default=6, ge=0)  # 0 evicts on the first failed probe
    http_host: str = "127.0.0.1"
    http_port: int = Field(default=8939, ge=1, le=65535)  # livepeer-node discovery's port
    log_json: bool = True

    @model_validator(mode="after")
    def _directory_inputs(self) -> Self:
        if self.directory == "static" and not self.orchestrators:
            raise ValueError("the static directory needs LPD_ORCHESTRATORS (base URLs, or a file of them)")
        if self.directory == "onchain" and self.arbitrum_rpc_url is None:
            raise ValueError("the on-chain directory needs LPD_ARBITRUM_RPC_URL")
        return self

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "DiscoverySettings":
        """Build settings from ``LPD_<FIELD>`` variables; unset or empty variables keep their defaults."""
        source = os.environ if env is None else env
        values = {
            name: source[ENV_PREFIX + name.upper()]
            for name in cls.model_fields
            if source.get(ENV_PREFIX + name.upper())
        }
        return cls.model_validate(values)
