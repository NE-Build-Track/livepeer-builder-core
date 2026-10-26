"""The discovery service (``pip install livepeer-builder[server]``): which orchestrators exist and what
their runners advertise, served in livepeer-node's ``GET /discovery`` format for engines to read through
``LPB_DISCOVERY_URL`` (#45).

It runs as its own process (``lpb discovery``) and depends on nothing in the engine. Change who is
discovered by writing an ``OrchestratorDirectory``; ``StaticDirectory`` and ``OnChainDirectory`` are
the two this package ships.
"""

from livepeer_builder.discovery.catalog import Catalog, CatalogStatus
from livepeer_builder.discovery.directory import Orchestrator, OrchestratorDirectory
from livepeer_builder.discovery.onchain import OnChainDirectory
from livepeer_builder.discovery.probe import Probe, Prober
from livepeer_builder.discovery.rpc import JsonRpcChain
from livepeer_builder.discovery.server import create_app, directory_from, serve
from livepeer_builder.discovery.settings import DiscoverySettings
from livepeer_builder.discovery.static import StaticDirectory

__all__ = [
    "Catalog",
    "CatalogStatus",
    "DiscoverySettings",
    "JsonRpcChain",
    "OnChainDirectory",
    "Orchestrator",
    "OrchestratorDirectory",
    "Probe",
    "Prober",
    "StaticDirectory",
    "create_app",
    "directory_from",
    "serve",
]
