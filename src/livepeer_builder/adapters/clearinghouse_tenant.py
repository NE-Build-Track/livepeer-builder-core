"""A tenant's own routes at a payment clearinghouse, as an enterprise's engine calls them (#165).

The routes name no tenant: the clearinghouse takes it from the tenant admin key (D87 point 3, as amended
on 19 November 2026). So a command about another tenant cannot be sent at all: sent, it would act on this
tenant. ``own_tenant`` refuses it here, before anything is sent, as the clearinghouse refused a path that
named another tenant: 403, "another tenant". The tenant is the wire's, ``LPB_CLEARINGHOUSE_TENANT_ID``.

The remote adapters (``adapters/clearinghouse.py``) and the probes of ``lpb payments check``
(``adapters/payment_probes.py``) take their paths from here.
"""

from livepeer_builder.adapters.batteries import segment
from livepeer_builder.adapters.clearinghouse_wire import ClearinghouseWire
from livepeer_builder.errors import ProviderError

OWN_TENANT = "/v1/clearinghouse"  # the tenant's own routes: no tenant in the path, the key's is used
GRANT_PATH = OWN_TENANT + "/grant"
USAGE_PATH = OWN_TENANT + "/usage"


def allocation_path(actor_id: str, rest: str = "") -> str:
    """The actor's allocation route, among the tenant's own routes."""
    return OWN_TENANT + "/actors/" + segment(actor_id) + "/allocation" + rest


def own_tenant(wire: ClearinghouseWire, tenant_id: str) -> None:
    """Refuse a command about another tenant than the key's, before anything is sent. The clearinghouse
    would apply it to the key's own tenant, whose routes name none. A refusal (403), as the clearinghouse
    refused a path that named another tenant: applied nowhere."""
    if tenant_id != wire.tenant_id:
        raise ProviderError(
            403,
            f"resource belongs to another tenant: this engine's tenant at the clearinghouse is "
            f"{wire.tenant_id!r} (LPB_CLEARINGHOUSE_TENANT_ID), not {tenant_id!r}",
        )
