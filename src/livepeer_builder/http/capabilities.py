"""Read-only capability schemas matched against authenticated model discovery."""

import json
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query

from livepeer_builder.api import EngineApi
from livepeer_builder.contracts import ActorContext
from livepeer_builder.contracts.capabilities import CapabilityProfile


def attach_capabilities(
    app: FastAPI,
    engine: EngineApi,
    profiles: tuple[CapabilityProfile, ...],
    actor_from: Callable[..., Coroutine[Any, Any, ActorContext]],
) -> None:
    Actor = Annotated[ActorContext, Depends(actor_from)]

    @app.get("/v1/capabilities", response_model=list[CapabilityProfile])
    async def capabilities(
        actor: Actor, app_name: Annotated[str | None, Query(alias="app")] = None
    ) -> list[CapabilityProfile]:
        result = []
        for profile in profiles:
            if app_name is not None and profile.app != app_name:
                continue
            for runner in engine.discovery.runners(actor, profile.app):
                try:
                    metadata = json.loads(runner.metadata or "{}")
                    matches = (
                        metadata.get("model_name") == profile.model
                        and metadata.get("deployment_revision") == profile.deployment_revision
                        and any(
                            isinstance(value, dict) and value.get("path") == profile.path
                            for value in metadata.get("endpoints", {}).values()
                        )
                    )
                except (ValueError, TypeError, AttributeError):
                    matches = False
                if matches:
                    result.append(profile)
                    break
        return result
