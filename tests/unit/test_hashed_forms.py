"""What the engine hashes into a stored record or a provider's idempotency key is a format, not a name.

Three hashes outlive the release that wrote them:

- ``lpb_operations.fingerprint``: a repeat of an admin command is answered from its record only when the
  fingerprint of the repeat equals the stored one (D57). One that differs is a conflict, so the repeat of
  a command whose answer was lost would be refused instead of replayed.
- the idempotency key of each provider write: Batteries replays a write it has seen under the same key
  and applies one it has not, so a command resumed under another key moves the money twice.
- ``lpb_jobs.request_fingerprint``, which decides the same for a job's ``operation_ref``.

So a rename of a command, a field or a credential kind must not change any of them. The digests below
were computed on ``releases/m4`` before #163 renamed anything, by running these same commands. To fix a
failure here, put the hashed text back as it was; never update a digest to make this pass.
"""

from decimal import Decimal

from livepeer_builder import BuilderEngine
from livepeer_builder.contracts import ActorContext, FundRequest, JobRequest, Money, ProvisionRequest
from livepeer_builder.services.grants import GRANT_TAG
from livepeer_builder.services.request_identity import request_fingerprint
from livepeer_builder.store import SqlStore
from livepeer_builder.testing import FakeAllocationManager
from tests.conftest import EngineFactory

USD_5 = Money(amount=Decimal("5"), currency="USD")
WHY = "a digest written before this release no longer matches: see this module's docstring"

FINGERPRINTS = {
    "provision": "8eeeba74ca6295846627a15f9de0b1994cff06254598fdd95e62b0ff000b190d",
    "fund": "73f24f70556e5eac6b2e2e89e8ca57a3299bb20a8347e92b562890039175d088",
    "fund-grant": "5f7818ec743f3f14b9eff1c6e23f5cfd719ba44eeb02b45ce460510f5316a971",
    "create-grant": "ecedcb64dc79c68a37c202c6a71a0f940c1ad97b2200985a678a7cadaaeb8a23",
    "rotate-payment-key": "2ccc34329672956c386e408d05de16352ddd7f2248543f7503ebe2160a39f8fe",
    "issue-admin-key": "d2cd57850aa5dcc09c24b8ffc6d400efb1cb5ed7149d01e048d4b4dd5502c5cd",
    "issue-auditor-key": "54886113fc718a043112897126230ac2b1b4d61dea0d4799ebaa56d740eeeb3f",
    "issue-actor-key": "824da991d86817da4ae6af5cb42cf76d28624496e78a114538a34edf1ad74220",
}
# In the order the commands run: the allocation and its key, the allocation's money, the grant's money, the
# rotation's key, and the tag that finds a created grant again (Batteries takes no key on that call).
SENT_TO_THE_PROVIDER = [
    "kb-bvoOJubMv8opZcjgrQm49UDovkkpD3ieekczrY-w",
    "9t5UMwz545mkGq7vhproaAeAtGGjlDktQthvXUfX9Uo",
    "vVr1U2dYls0QCrgA8mxcTgdcwM1oGYQUgvPS72IOZyo",
    "plNGaYio7xHz7x5rFjYYB100YDPsHF0kjNCRHFip8RY",
    "EyXAIYuvDTcB32ErJvP-ptxlKiw8WkPbCEY48APwkI4",
]


async def run_every_command(engine: BuilderEngine, admin: ActorContext) -> None:
    """Each repeat-safe admin command once, under the reference its digest is keyed by. The rotation of an
    access key is left out: its request is a random key id and a label, and holds no name."""
    payments, keys, desk = engine.payments, engine.keys, engine.payments.grants
    assert keys is not None and desk is not None
    request = ProvisionRequest(
        application_id="default",
        actor_id="alice",
        amount=USD_5,
        label="Alice",
        grant_id="grant_1",
        operation_ref="provision",
    )
    await payments.provision(admin, request)
    await payments.fund(admin, "default", "alice", FundRequest(amount=USD_5, operation_ref="fund"))
    await payments.fund_grant(
        admin, FundRequest(amount=USD_5, operation_ref="fund-grant", grant_id="grant_1")
    )
    await desk.create_grant(admin, "Team", USD_5, operation_ref="create-grant")
    await payments.rotate_key(admin, "default", "alice", "rotate-payment-key")
    for kind in ("admin", "auditor", "actor"):
        await keys.issue(
            admin,
            kind=kind,
            actor_id="alice",
            label="L",
            operation_ref=f"issue-{kind}-key",  # type: ignore[arg-type]
        )


async def test_every_admin_command_keeps_its_fingerprint(
    make_engine: EngineFactory, admin: ActorContext, store: SqlStore
) -> None:
    await run_every_command(await make_engine(engine_id="engine-1"), admin)
    stored = {
        ref: record.fingerprint
        for ref in FINGERPRINTS
        if (record := await store.get_operation("default", "operator", ref)) is not None
    }
    assert stored == FINGERPRINTS, WHY


async def test_every_provider_write_keeps_its_idempotency_key(
    make_engine: EngineFactory, admin: ActorContext, manager: FakeAllocationManager
) -> None:
    engine = await make_engine(engine_id="engine-1")
    await run_every_command(engine, admin)
    sent = [*manager.replays, manager.grants[0].metadata[GRANT_TAG]]
    assert sent == SENT_TO_THE_PROVIDER, WHY


def test_a_job_request_keeps_its_fingerprint() -> None:
    request = JobRequest(app="livepeer-example/hello", path="/run", payload={"prompt": "hi"})
    stored = "e29b846bd1314a69ae0ffc978347354fb5570560f1e47298245d1e418c12b111"
    assert request_fingerprint(request) == stored, WHY
