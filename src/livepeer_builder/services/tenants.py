"""Tenants at the clearinghouse: onboarding, the tenant list, a tenant's grant and its status (D83, #165).

A tenant is one enterprise app's account. A tenant and its grant share one id: onboarding creates the
grant at the payment provider, and the id the provider gives it is the tenant's id from then on. Nothing
maps one to the other, and no command takes a separate grant id. A tenant has one grant for life: it is
topped up as needed, paused when the tenant must stop, and never closed (D82).

Only a clearinghouse admin onboards a tenant, lists every tenant and pauses one (``admin.tenants``). A
tenant's own admin or auditor reads its grant. The desk is an optional capability: it is present only
with a grants manager (``LPB_BATTERIES_GRANTS_TOKEN``, or one supplied), because the engine's own
provider credential can top up a grant but not create, read or pause one.
"""

from pydantic import TypeAdapter

from livepeer_builder.contracts import (
    OPERATION_REPLAYED,
    ActorContext,
    Currency,
    IssuedKey,
    Money,
    OnboardedTenant,
    Operation,
    ResourceRef,
    TenantGrant,
    TenantState,
    TenantStatus,
)
from livepeer_builder.contracts.common import AllotAmount, StoredText
from livepeer_builder.errors import InvalidRequest
from livepeer_builder.protocols import AccessPolicy, AllocationManager
from livepeer_builder.services.arguments import checked
from livepeer_builder.services.grants import GRANT_TAG, idempotency_key, stored_id, tagged_grant
from livepeer_builder.services.keys import KeyService
from livepeer_builder.services.ledger import OperationLedger, RunningOperation

_ONBOARDED = TypeAdapter(OnboardedTenant)
_STATE = TypeAdapter(TenantState)
_AMOUNT = TypeAdapter(AllotAmount)
_TEXT = TypeAdapter(StoredText)
FIRST_ADMIN = "tenant-admin"  # the actor a tenant's first admin key is issued to, unless the caller names one


class TenantDesk:
    def __init__(
        self,
        manager: AllocationManager,
        policy: AccessPolicy,
        currency: Currency,
        ledger: OperationLedger,
        engine_id: str,
        keys: KeyService | None = None,
    ) -> None:
        self._manager = manager
        self._policy = policy
        self._currency = currency
        self._ledger = ledger
        self._engine_id = engine_id
        self._keys = keys

    async def onboard(
        self,
        admin: ActorContext,
        name: str,
        amount: Money,
        *,
        operation_ref: str,
        admin_actor_id: str = FIRST_ADMIN,
    ) -> OnboardedTenant:
        """Onboard a tenant, on the ledger (D57): create its grant, then issue its first tenant admin key.

        The record is scoped to the caller and the reference, as every command's is, so it exists before
        the provider is asked: the tenant has no id until the provider answers with the grant's. That id
        is noted in the record as soon as it is known, and the key's id before the key is stored. A
        repeat of the reference gets the first tenant back and never creates a second grant or a second
        key: a finished command is answered from its record, and a resumed one finds the grant by the
        tag it carries (the provider takes no idempotency key on a grant) and the key by its noted id.
        A grant whose id the engine's records cannot keep is no tenant: it is refused as an answer the
        engine cannot use (``stored_id``), with nothing noted and no key issued.

        The key's token is in the answer of the run that issued the key, and nowhere else: the engine
        keeps its hash. A repeat, or a resume after the key was stored, answers ``token: null``; a
        clearinghouse admin then issues another tenant admin key."""
        self._policy.check(admin, Operation.ADMIN_TENANTS)
        # The body's rules, for an in-process call (#112, C11), refused as InvalidRequest (#106 review)
        name = checked(_TEXT, name.strip(), "name")
        if not name:
            raise InvalidRequest("a tenant name is required")
        if not checked(_TEXT, admin_actor_id, "admin_actor_id"):
            raise InvalidRequest("the tenant admin key needs an actor id")
        if amount.currency != self._currency:
            raise InvalidRequest(f"a grant's amount must be in {self._currency}")
        amount = checked(_AMOUNT, amount, "amount")
        # This text is hashed into the tag, so it keeps its first wording, as every provider key does
        # (tests/unit/test_hashed_forms.py).
        tag = idempotency_key(
            self._engine_id, f"create-grant:{admin.tenant_id}:{admin.actor_id}:{operation_ref}", "grant"
        )
        minted: list[str] = []  # the token, which the record never holds

        async def steps(run: RunningOperation) -> OnboardedTenant:
            tenant_id = run.context.get("tenant_id")
            if tenant_id is None:
                found = await tagged_grant(self._manager, run, tag) if run.resumed else None
                grant = found or await self._manager.create_grant(name, amount, {GRANT_TAG: tag})
                tenant_id = stored_id(grant.id, "a grant id")  # before the record or a key names it
                await run.remember(tenant_id=tenant_id)
            key = await self._first_admin_key(run, admin, tenant_id, admin_actor_id, name, minted)
            return OnboardedTenant(tenant_id=tenant_id, name=name, admin_key=key)

        request = {"name": name, "amount": amount, "admin_actor_id": admin_actor_id}
        onboarded = await self._ledger.run(admin, "onboard_tenant", operation_ref, request, steps, _ONBOARDED)
        if onboarded.admin_key is None:
            return onboarded
        token = minted[0] if minted and not OPERATION_REPLAYED.get() else None
        shown = onboarded.admin_key.model_copy(update={"token": token, "replayed": token is None})
        return onboarded.model_copy(update={"admin_key": shown})

    async def _first_admin_key(
        self,
        run: RunningOperation,
        admin: ActorContext,
        tenant_id: str,
        actor_id: str,
        label: str,
        minted: list[str],
    ) -> IssuedKey | None:
        """The tenant's first admin key, without its token (the record keeps this answer). The key's id is
        noted before the key is stored, so a resume finds the key an earlier run issued."""
        if self._keys is None:
            return None  # engine access keys are off: the tenant's admin is another kind of credential
        key_id = run.context.get("admin_key_id")
        if key_id is None:
            key_id = self._keys.new_key_id()
            await run.remember(admin_key_id=key_id)
        issued = await self._keys.issue_noted(
            admin, key_id, kind="tenant_admin", actor_id=actor_id, tenant_id=tenant_id, label=label
        )
        if issued.token is not None:
            minted.append(issued.token)
        return issued.model_copy(update={"token": None})

    async def list_tenants(self, admin: ActorContext) -> list[TenantGrant]:
        """Every tenant, with its grant's balance: every grant the provider holds."""
        self._policy.check(admin, Operation.ADMIN_TENANTS)
        return await self._manager.grant_balances()

    async def grant(self, admin: ActorContext, tenant_id: str) -> TenantGrant:
        """The tenant's grant: its total, its unallocated balance and its status."""
        self._policy.check(admin, Operation.ADMIN_READ, _tenant(tenant_id))  # before the provider (D44)
        return await self._manager.read_grant(tenant_id)

    async def set_status(
        self, admin: ActorContext, tenant_id: str, status: TenantStatus, *, operation_ref: str
    ) -> TenantState:
        """Pause or resume a tenant by pausing or resuming its grant, on the ledger (D57). What a paused
        grant stops is the provider's rule, not the engine's (docs/contracts/payments.md#tenants). A
        late repeat of a reference is answered from its record, so it never pauses a tenant again that
        was resumed since."""
        self._policy.check(admin, Operation.ADMIN_TENANTS, _tenant(tenant_id))
        if status not in ("active", "paused"):
            raise InvalidRequest("status must be active or paused; a grant is never closed")

        async def steps(_: RunningOperation) -> TenantState:
            await self._manager.set_grant_status(tenant_id, status)
            return TenantState(tenant_id=tenant_id, status=status)

        request = {"tenant_id": tenant_id, "status": status}
        return await self._ledger.run(admin, "set_tenant_status", operation_ref, request, steps, _STATE)


def _tenant(tenant_id: str) -> ResourceRef:
    return ResourceRef(kind="tenant", tenant_id=tenant_id)
