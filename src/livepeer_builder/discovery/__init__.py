"""The discovery service: which orchestrators exist, what their runners advertise, served in
livepeer-node's ``GET /discovery`` format for engines to read through ``LPB_DISCOVERY_URL`` (#45).

It runs as its own process (``lpb discovery``) and depends on nothing in the engine. Change who is
discovered by writing an ``OrchestratorDirectory``; ``StaticDirectory`` and ``OnChainDirectory`` are
the two this package ships.
"""

from livepeer_builder.discovery.catalog import Catalog, CatalogStatus
from livepeer_builder.discovery.directory import Orchestrator, OrchestratorDirectory
from livepeer_builder.discovery.onchain import OnChainDirectory
from livepeer_builder.discovery.probe import Probe, Prober
from livepeer_builder.discovery.static import StaticDirectory

__all__ = [
    "Catalog",
    "CatalogStatus",
    "OnChainDirectory",
    "Orchestrator",
    "OrchestratorDirectory",
    "Probe",
    "Prober",
    "StaticDirectory",
]
