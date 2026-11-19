"""Authentication for the REST routes: the engine's authenticator chain as a FastAPI dependency.

Both sets of routes use it, and so do the routes an operator adds through ``extend``.
"""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from livepeer_builder.api import ClearinghouseApi, WorkApi
from livepeer_builder.contracts import ActorContext, AuthRequest
from livepeer_builder.errors import NotAuthenticated


def actor_dependency(engine: WorkApi | ClearinghouseApi) -> Callable[..., Coroutine[Any, Any, ActorContext]]:
    """The FastAPI dependency every authenticated route uses: the engine's authenticator chain.

    Extra endpoints added through ``livepeer_builder.http.server`` (``extend=``) use it too, so they
    authenticate like the built-in routes: ``Annotated[ActorContext, Depends(actor_dependency(engine))]``.

    When the engine accepts API keys, it also declares the ``lpk_`` bearer scheme in OpenAPI, so Swagger
    UI shows Authorize. With keys off it declares a JWT bearer scheme when the authenticator chain announces
    a ``Bearer`` challenge (``LPB_AUTH=jwt``, D45), and otherwise none: the engine cannot know what another
    authenticator expects, so a service that wants Swagger to send it declares it in ``extend``.
    """
    # Declares the scheme only; authentication itself stays with engine.authenticator (raw headers).
    if engine.keys is not None:
        bearer = HTTPBearer(auto_error=False, bearerFormat="lpk", description="An engine API key: `lpk_...`")
    elif "bearer" in (engine.authenticator.challenge() or "").lower():  # JWT bearer (D45), keys off
        bearer = HTTPBearer(
            auto_error=False,
            bearerFormat="JWT",
            description="An access token from the engine's identity provider",
        )
    else:

        async def actor_from(request: Request) -> ActorContext:
            return await _authenticated(engine, request)

        return actor_from

    async def actor_from_key(
        request: Request,
        _declared: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
    ) -> ActorContext:
        return await _authenticated(engine, request)

    return actor_from_key


async def _authenticated(engine: WorkApi | ClearinghouseApi, request: Request) -> ActorContext:
    auth = AuthRequest(
        method=request.method,
        path=request.url.path,
        headers={key.lower(): value for key, value in request.headers.items()},
        client_address=request.client.host if request.client else None,
    )
    actor = await engine.authenticator.authenticate(auth)
    if actor is None:
        raise NotAuthenticated("a credential is required")
    return actor
