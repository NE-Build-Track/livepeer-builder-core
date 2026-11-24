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
  and the next request asks again. Once one read has answered for this tenant, no other is made while
  every answer agrees with it. An answer that states another tenant ends that: the next request reads
  the grant again first, a read of the grant that was already on its way does not count, and an answer
  that states no tenant and was on its way is not taken. Requests that arrive together share one read.
  A command on the operation ledger does not wait for its first request
  to ask: the payments service awaits ``ready_for_commands`` of the allocation manager before it begins
  the command's record (``ReadyBeforeCommands``), which is ``confirm``, so the read spends none of the
  ledger's budget, which is sized for the command's own calls. Asked through the port alone, the
  adapters still confirm before their first request, as every read does.
- **Every answer that states a tenant is read** (``tenant_id``: an allocation, a grant). Another tenant
  is never a success. After a read nothing was applied, and it is the same refusal. After a command the
  clearinghouse has applied it at the other tenant: a ``ProviderError`` with the answer's 2xx, which is
  no refusal, so the operation ledger keeps the reference, as for a command applied to another
  allocation. Nothing is recorded in the engine, and a key the answer handed over is dropped with the
  answer: it is in no error, no log line and no store.
"""

import asyncio
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from livepeer_builder.adapters.batteries import segment
from livepeer_builder.adapters.clean_errors import bare, raised
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
        self._questioned = 0  # how many answers have stated another tenant: it only grows
        self._asking = asyncio.Lock()  # one read of the key's grant at a time
        self._asked = 0  # how many such reads have ended
        self._refused: Exception | None = None  # what the last one failed with, as an error with no frames

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
        has answered for this tenant, and not again while every answer agrees with it.

        Requests that arrive together share one read (#185 review): each read has the clearinghouse read
        the grant's balances, and a read for each request of a cold start made them time out. Those that
        waited for a read that failed fail with it, and the next request reads again."""
        if self._confirmed:
            return
        asked = self._asked
        async with self._asking:
            if self._confirmed:
                return
            if self._asked != asked and self._refused is not None:  # the read this one waited for
                raise _again(self._refused)
            await self.grant()
            if not self._confirmed:  # an answer for another tenant arrived while the grant was read
                raise _in_question(self.tenant_id)

    async def grant(self) -> TenantGrant:
        """The grant of the key's tenant, which must be this tenant: the read that says whose key it is.
        It says so only if no answer stated another tenant while it was read: such a read is older word
        than the contradiction, and the next request reads again."""
        questioned = self._questioned
        try:
            grant = raised(self._held(await self._wire.read_as(TenantGrant, GRANT_PATH), None, questioned))
        except Exception as error:
            self._asked, self._refused = self._asked + 1, _again(error)
            raise
        self._asked, self._refused = self._asked + 1, None
        self._confirmed = self._questioned == questioned
        return grant

    async def read_as[T: BaseModel](
        self, model: type[T], path: str, params: Mapping[str, str] | None = None
    ) -> T:
        await self.confirm()
        questioned = self._questioned
        return raised(self._held(await self._wire.read_as(model, path, params), None, questioned))

    async def write_as[T: BaseModel](
        self, model: type[T], path: str, body: Mapping[str, Any] | None = None
    ) -> T:
        await self.confirm()
        questioned, command = self._questioned, path.rpartition(_ALLOCATION)[2]
        return raised(self._held(await self._wire.write_as(model, path, body), command, questioned))

    def _held[T: BaseModel](self, answer: T, command: str | None, questioned: int) -> T | ProviderError:
        """``answer``, held to this tenant, or the error it comes to, for ``raised``. ``questioned`` is
        the count of contradictions when its request was sent.

        The error is returned and never raised here: this frame holds the answer, which for an allotment
        or a rotation holds a handed key, and a frame an error passed through stays reachable from it
        (review of ``f71101e``).

        An answer that states another tenant ends what was known of the key. One that states none, a
        usage page, is taken on that knowledge alone: if it was contradicted while the answer was on its
        way, the answer was sent for with the key that has just failed, and it is not taken, whatever a
        later read of the grant has said since."""
        checked = _of(self.tenant_id, answer, command)
        if isinstance(checked, ProviderError):
            self._confirmed, self._questioned = False, self._questioned + 1
            return checked
        if self._questioned != questioned and not hasattr(answer, "tenant_id"):
            return _in_question(self.tenant_id)
        return checked


def _in_question(own: str) -> ProviderError:
    return ProviderError(
        403,
        f"whose key LPB_CLEARINGHOUSE_TOKEN is came into question while this was asked: the clearinghouse "
        f"answered another request for another tenant than {own!r} (LPB_CLEARINGHOUSE_TENANT_ID). Nothing "
        "of this answer is used. The key's grant is read again before the next request",
    )


def _again(error: BaseException) -> Exception:
    """``error`` as a new error of its kind and words, with no traceback: what a failed read of the
    grant is kept as for those that waited for it, and what each of them is given. An error that was
    raised keeps every frame it passed through, and each raise adds to them."""
    if isinstance(error, ProviderError):
        return ProviderError(error.status_code, error.message)
    return bare(error)


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
