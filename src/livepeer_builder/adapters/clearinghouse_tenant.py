"""A tenant's own routes at a payment clearinghouse, as an enterprise's engine calls them, and whose
tenant admin key it calls them with (#165).

The routes name no tenant: the clearinghouse takes it from the tenant admin key (D87 point 3, as amended
on 19 November 2026). Two things follow, and both are settled here, for the remote adapters
(``adapters/clearinghouse.py``), the discovery reader that carries the key (``adapters/sdk.py``) and the
probes of ``lpb payments check`` (``adapters/payment_probes.py``), which takes its paths from here.

**A command about another tenant cannot be sent.** Sent, it would act on this tenant. ``own_tenant``
refuses it before anything is sent, as the clearinghouse refused a path that named another tenant: 403,
"another tenant". The tenant is the wire's, ``LPB_CLEARINGHOUSE_TENANT_ID``.

**Nothing at the clearinghouse compares that setting with the key.** A key that belongs to another
tenant is answered for that tenant: an allotment would come from its grant, a usage page would be its
payments, and the engine would record all of it under its own tenant. So every request of the adapters
goes through ``TenantWire``, which holds the engine to one tenant:

- **Before the first request of a process** it reads the key's grant, which states the tenant
  (``confirm``). Another tenant than the setting's is a refusal of whatever was asked (403), and nothing
  of it is sent. This is what keeps an answer that states no tenant, a usage page or the runner list,
  from being another tenant's. A read that fails says nothing about the key: its error is the caller's,
  and the next request asks again. A process asks once more only until one read has answered: the key
  of a process does not change.
- **Every answer that states a tenant is read** (``tenant_id``: an allocation, a grant). Another tenant
  is never a success. After a read nothing was applied, and it is the same refusal. After a command the
  clearinghouse has applied it at the other tenant: a ``ProviderError`` with the answer's 2xx, which is
  no refusal, so the operation ledger keeps the reference, as for a command applied to another
  allocation. Nothing is recorded in the engine, and a key the answer handed over is dropped with the
  answer: it is in no error, no log line and no store.
"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from livepeer_builder.adapters.batteries import segment
from livepeer_builder.adapters.clean_errors import raised
from livepeer_builder.adapters.clearinghouse_wire import ClearinghouseWire
from livepeer_builder.contracts import TenantGrant
from livepeer_builder.errors import ProviderError

OWN_TENANT = "/v1/clearinghouse"  # the tenant's own routes: no tenant in the path, the key's is used
GRANT_PATH = OWN_TENANT + "/grant"
USAGE_PATH = OWN_TENANT + "/usage"
_ALLOCATION = "/allocation"
# What a command leaves at the tenant the key belongs to, by the end of its path, and who puts it right.
_APPLIED = {
    "": "An allocation was allotted there, from that tenant's grant, and its key is not kept here: that "
    "tenant's admin revokes the allocation at the clearinghouse, which returns what it has not spent to "
    "the grant",
    "/rotate-payment-key": "That tenant's actor was given a new key, which is not kept here, and the key "
    "it had is ended: that tenant's admin rotates the actor's payment key again",
    "/top-up": "The amount is on that tenant's allocation: revoking that allocation at the clearinghouse "
    "returns what it has not spent to the grant",
    "/status": "That tenant's admin sets the allocation's status again at the clearinghouse",
    "/revoke": "That tenant's allocation is ended, and what it had not spent is back in its grant: that "
    "tenant's admin allots the actor again",
}


def allocation_path(actor_id: str, rest: str = "") -> str:
    """The actor's allocation route, among the tenant's own routes."""
    return OWN_TENANT + "/actors/" + segment(actor_id) + _ALLOCATION + rest


class TenantWire:
    """The wire to a clearinghouse, held to the one tenant the engine is there: it sends nothing until
    it knows the key is that tenant's, and takes no answer that states another."""

    def __init__(self, wire: ClearinghouseWire) -> None:
        self._wire = wire
        self._confirmed = False

    @classmethod
    def over(cls, wire: "ClearinghouseWire | TenantWire") -> "TenantWire":
        """``wire`` itself when it is held to its tenant already: the adapters of one engine share one,
        so the key's grant is read once for all three."""
        return wire if isinstance(wire, TenantWire) else cls(wire)

    @property
    def tenant_id(self) -> str:
        return self._wire.tenant_id

    async def confirm(self) -> None:
        """Know that the key is this tenant's, or refuse (403). The key's grant is read until one read
        has answered for this tenant, and never after."""
        if not self._confirmed:
            await self.grant()

    async def grant(self) -> TenantGrant:
        """The grant of the key's tenant, which must be this tenant: the read that says whose key it is."""
        grant = raised(_of(self.tenant_id, await self._wire.read_as(TenantGrant, GRANT_PATH), None))
        self._confirmed = True
        return grant

    async def read_as[T: BaseModel](
        self, model: type[T], path: str, params: Mapping[str, str] | None = None
    ) -> T:
        await self.confirm()
        return raised(_of(self.tenant_id, await self._wire.read_as(model, path, params), None))

    async def write_as[T: BaseModel](
        self, model: type[T], path: str, body: Mapping[str, Any] | None = None
    ) -> T:
        await self.confirm()
        command = path.rpartition(_ALLOCATION)[2]
        return raised(_of(self.tenant_id, await self._wire.write_as(model, path, body), command))


def own_tenant(wire: TenantWire, tenant_id: str) -> None:
    """Refuse a command about another tenant than the key's, before anything is sent. The clearinghouse
    would apply it to the key's own tenant, whose routes name none. A refusal (403), as the clearinghouse
    refused a path that named another tenant: applied nowhere."""
    if tenant_id != wire.tenant_id:
        raise ProviderError(
            403,
            f"resource belongs to another tenant: this engine's tenant at the clearinghouse is "
            f"{wire.tenant_id!r} (LPB_CLEARINGHOUSE_TENANT_ID), not {tenant_id!r}",
        )


def _of[T: BaseModel](own: str, answer: T, command: str | None) -> T | ProviderError:
    """``answer``, unless it states another tenant than ``own``: then the error that comes to, for
    ``raised``. ``command`` is the end of the path of the command the answer is to, and ``None`` for a
    read. The error is returned and never raised here, where the answer, which may hold a handed key, is
    a local: no frame of its traceback holds the answer, and the answer is not kept."""
    stated = getattr(answer, "tenant_id", own)
    if stated == own:
        return answer
    whose = (
        "the tenant admin key in LPB_CLEARINGHOUSE_TOKEN belongs to another tenant than "
        f"LPB_CLEARINGHOUSE_TENANT_ID: this engine's tenant is {own!r}, and the clearinghouse"
    )
    fix = "Set the key and the id of one tenant, and start the engine again"
    if command is None:
        return ProviderError(
            403, f"{whose} answers for {stated!r}. Nothing was applied, and nothing is recorded here. {fix}"
        )
    return ProviderError(
        200,
        f"{whose} applied this command to tenant {stated!r}. Nothing of it is recorded here. "
        f"{_APPLIED.get(command, 'That tenant holds what the command did')}. {fix}",
    )
