"""HTTP layer: the REST service (``pip install livepeer-builder[server]``). FastAPI is confined here."""

from livepeer_builder.http.app import create_app
from livepeer_builder.http.auth import actor_dependency
from livepeer_builder.http.server import Extend, build_app, serve

__all__ = ["Extend", "actor_dependency", "build_app", "create_app", "serve"]
