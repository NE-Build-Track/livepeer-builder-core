"""HTTP layer: the REST service (``pip install livepeer-builder[server]``). FastAPI is confined here."""

from livepeer_builder.http.app import create_app

__all__ = ["create_app"]
