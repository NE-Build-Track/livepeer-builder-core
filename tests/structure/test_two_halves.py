"""The engine's two halves, and the five payment ports where they meet (D84, #164).

The engine is one program with two halves (docs/design-docs/m4-guides/iss-31-payment-configurations.md,
"The engine's two halves"):

- the **work half**: network capabilities, runner choice, the runner call, streaming, job records and the
  cost of each job;
- the **clearinghouse half**: tenants, grants, allocations, payment keys, usage and the runner list. It
  mints each payment key and hands it over; it signs nothing.

The work half reaches payments through five ports and nothing else: signing inside ``RunnerTransport``,
``CredentialResolver``, ``AllocationManager``, ``AllowanceReader`` and ``UsageSource``. An enterprise that
uses a payment clearinghouse runs the work half alone: allocations, the allowance and usage come from the
clearinghouse, the payment keys from its own store, where the clearinghouse's answers put them, and
signing goes to the clearinghouse's signer. Whatever the work half reached another way would have nothing
behind it there.

The halves live in ``services/`` and ``http/``, and every module of the two is named below as work,
clearinghouse or shared. This file then holds five things:

1. No module of one half imports a module of the other, directly or through what it imports.
2. A work module names no payment protocol except the five ports.
3. Every protocol is classified here, so a sixth way to reach payments cannot arrive unnoticed.
4. Each set of routes is typed against its own half of the facade (``WorkApi``, ``ClearinghouseApi``),
   so mypy refuses a work route that calls a clearinghouse command. Of payments, the work half's facade
   offers the allowance read alone.
5. That read (``GET /v1/allowance``) reaches the allocation through the ``AllowanceReader`` port.

``test_layers.py`` closes the other doors: nothing in ``services/`` or ``http/`` may import an adapter
or an HTTP client, so no module of the work half can call Batteries or the signer itself.
"""

import ast
import typing
from pathlib import Path

import pytest
from fastapi import FastAPI

from livepeer_builder import api, protocols
from livepeer_builder.http.clearinghouse import mount_clearinghouse_routes
from livepeer_builder.http.work import mount_work_routes
from livepeer_builder.protocols import clearinghouse, conditional_writes, persistence
from livepeer_builder.services.costs import CostService, CostSyncWorker
from livepeer_builder.services.jobs import JobService

SRC = Path(__file__).resolve().parents[2] / "src" / "livepeer_builder"
GUIDE = "docs/design-docs/m4-guides/iss-31-payment-configurations.md"

# Network capabilities, runner choice, the runner call, streaming, job records and the cost of each job.
WORK = {
    "services/admission.py",
    "services/costs.py",
    "services/discovery.py",
    "services/evidence.py",
    "services/failover.py",
    "services/failure_codes.py",
    "services/jobs.py",
    "services/operations.py",
    "services/reconciliation.py",
    "services/request_identity.py",
    "services/reserved.py",  # events and sessions: the events feed belongs to the work half
    "services/streams.py",
    "services/usage_intake.py",
    "services/usage_reports.py",
    "http/streaming.py",
    "http/work.py",
}
# Tenants, grants, allocations, payment keys, usage and the runner list.
CLEARINGHOUSE = {
    "services/actor_writes.py",
    "services/allocation_delivery.py",
    "services/grants.py",
    "services/payment_keys.py",
    "services/payments.py",
    "services/runner_list.py",  # the list a clearinghouse publishes: it reaches no work module
    "services/tenant_usage.py",
    "services/tenants.py",
    "http/clearinghouse.py",
    "http/delivered.py",  # a delivered allocation as the clearinghouse's answer
}
# What both halves use: access (policy, access keys, who signed in), the operation ledger, and the app
# that mounts the sets. A shared module may be imported by either half.
SHARED = {
    "services/__init__.py",
    "services/arguments.py",  # an in-process argument checked by the REST body's rule (#106 review)
    "services/keys.py",
    "services/ledger.py",
    "services/ownership.py",
    "services/policy.py",
    "services/sightings.py",
    "http/__init__.py",
    "http/app.py",
    "http/auth.py",
    "http/bodies.py",
    "http/limits.py",
    "http/params.py",
    "http/replies.py",
    "http/server.py",
    "http/unprocessable.py",
}

# The five ports. Signing has no protocol of its own: it happens inside the runner transport.
SIGNING_INSIDE = {"RunnerTransport", "StreamingRunnerTransport", "RunnerStream"}
PORTS = SIGNING_INSIDE | {"CredentialResolver", "AllocationManager", "AllowanceReader", "UsageSource"}
# Payment protocols that are not ports: where payment keys and allocation records are kept. The work
# half reads both through CredentialResolver and AllowanceReader. EngineStore is here because it holds
# both: a work module asks for the narrow repo it needs (JobRepo, CostRepo). TenantUsageSource is how a
# clearinghouse reads one tenant's usage at the provider: the work half reads usage through UsageSource.
BEHIND_THE_PORTS = {
    "SecretStore",
    "WritableSecretStore",
    "SecretRepo",
    "ActorRepo",
    "ActorCommandRepo",
    "TenantUsageSource",
    "EngineStore",
}
NOT_PAYMENTS = {
    "Clock",
    "Authenticator",
    "AccessPolicy",
    "DiscoverySource",
    "DiscoveryDocumentSource",
    "SelectionPolicy",
    "KeyRepo",
    "JobRepo",
    "OperationRepo",
    "UsageOnceRepo",
    "UsageSetAsideRepo",
    "CostRepo",
    "CostSnapshot",
    "AllocationCostSnapshot",
    "StoreMaintenance",
    "EventFeed",
    "SessionManager",
}
FIVE = (
    "The work half reaches payments only through five ports: signing inside RunnerTransport, "
    f"CredentialResolver, AllocationManager, AllowanceReader and UsageSource (D84, {GUIDE})."
)


def module_file(name: str) -> str | None:
    """The package file a dotted module name is, as a path under the package; ``None`` outside it."""
    parts = name.split(".")
    if parts[0] != "livepeer_builder":
        return None
    for candidate in (SRC.joinpath(*parts[1:]).with_suffix(".py"), SRC.joinpath(*parts[1:], "__init__.py")):
        if len(parts) > 1 and candidate.is_file():
            return str(candidate.relative_to(SRC))
    return "__init__.py" if len(parts) == 1 else None


def imports_of(relative: str) -> set[str]:
    """The package files ``relative`` imports, wherever in the file the import is written."""
    found: set[str] = set()
    for node in ast.walk(ast.parse((SRC / relative).read_text())):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            # ``from package import module`` imports the module, not the package's own names
            names = [
                f"{node.module}.{alias.name}" if module_file(f"{node.module}.{alias.name}") else node.module
                for alias in node.names
            ]
        else:
            continue
        found.update(file for name in names if (file := module_file(name)) is not None)
    return found


def path_to(start: str, targets: set[str]) -> list[str] | None:
    """The shortest chain of imports from ``start`` to one of ``targets``, or ``None``."""
    came_from: dict[str, str | None] = {start: None}
    queue = [start]
    for current in queue:
        if current in targets and current != start:
            chain = [current]
            while (previous := came_from[chain[-1]]) is not None:
                chain.append(previous)
            return chain[::-1]
        for target in sorted(imports_of(current)):
            if target not in came_from:
                came_from[target] = current
                queue.append(target)
    return None


def protocol_names(relative: str) -> set[str]:
    """The names ``relative`` takes from ``livepeer_builder.protocols``, imported or as an attribute."""
    tree = ast.parse((SRC / relative).read_text())
    named: set[str] = set()
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.startswith("livepeer_builder.protocols"):
                named.update(alias.name for alias in node.names)
            elif node.module == "livepeer_builder":
                aliases.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "protocols"
                )
        elif isinstance(node, ast.Import):
            aliases.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name.startswith("livepeer_builder.protocols")
            )
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and ast.unparse(node.value) in aliases:
            named.add(node.attr)
    return named


def declared_protocols() -> set[str]:
    return {
        name
        for module in (protocols, clearinghouse, conditional_writes, persistence)
        for name, value in vars(module).items()
        if isinstance(value, type)
        and getattr(value, "_is_protocol", False)
        and value.__module__ == module.__name__
    }


def members(protocol: type) -> set[str]:
    """What a protocol offers, with what the protocols it combines offer."""
    bases = [base for base in protocol.__mro__ if getattr(base, "_is_protocol", False)]
    return {name for base in bases for name in vars(base) if not name.startswith("_")}


def test_every_service_and_route_module_belongs_to_a_half_or_is_shared() -> None:
    found = {
        path.relative_to(SRC).as_posix()
        for folder in ("services", "http")
        for path in (SRC / folder).glob("*.py")
    }
    named = WORK | CLEARINGHOUSE | SHARED
    assert not (WORK & CLEARINGHOUSE) and not (WORK & SHARED) and not (CLEARINGHOUSE & SHARED), (
        "a module belongs to one of WORK, CLEARINGHOUSE and SHARED in tests/structure/test_two_halves.py"
    )
    assert found == named, (
        f"not named: {sorted(found - named)}; named but gone: {sorted(named - found)}. Every module of "
        "services/ and http/ is listed in tests/structure/test_two_halves.py as WORK (jobs, runners, "
        "streaming, costs), CLEARINGHOUSE (tenants, grants, allocations, payment keys) or SHARED (both "
        f"halves use it). The halves are described in {GUIDE}."
    )


@pytest.mark.parametrize("module", sorted(WORK))
def test_the_work_half_imports_nothing_of_the_clearinghouse_half(module: str) -> None:
    chain = path_to(module, CLEARINGHOUSE)
    assert chain is None, (
        f"{module} is in the work half and reaches the clearinghouse half: {' -> '.join(chain or [])}. "
        f"{FIVE} In services/, take the port as a constructor argument, typed as its Protocol from "
        "livepeer_builder.protocols, and let runtime/assembly.py pass the implementation. In http/, call "
        "the engine through WorkApi. If both halves need the code, move it to a module both may import "
        "and list it under SHARED in this file."
    )


@pytest.mark.parametrize("module", sorted(CLEARINGHOUSE))
def test_the_clearinghouse_half_imports_nothing_of_the_work_half(module: str) -> None:
    chain = path_to(module, WORK)
    assert chain is None, (
        f"{module} is in the clearinghouse half and reaches the work half: {' -> '.join(chain or [])}. "
        "A payment clearinghouse never runs jobs, so its half stands without the work half. If both "
        "halves need the code, move it to a module both may import and list it under SHARED in this file."
    )


@pytest.mark.parametrize("module", sorted(WORK))
def test_the_work_half_names_no_payment_protocol_but_the_five_ports(module: str) -> None:
    kept = sorted(protocol_names(module) & BEHIND_THE_PORTS)
    assert not kept, (
        f"{module} is in the work half and names {kept}, which stay behind the payment ports. {FIVE} "
        "Read a payment key through CredentialResolver and an allocation through AllowanceReader; ask "
        "the store for the narrow repo the module needs (JobRepo, CostRepo), not for EngineStore."
    )


def test_every_protocol_is_a_payment_port_or_named_as_not_one() -> None:
    """A new protocol is classified here before it is used, so a sixth way for the work half to reach
    payments is a decision somebody made, not a side effect."""
    groups = (PORTS, BEHIND_THE_PORTS, NOT_PAYMENTS)
    named = set().union(*groups)
    assert sum(len(group) for group in groups) == len(named), "a protocol is named in two groups"
    declared = declared_protocols()
    assert declared == named, (
        f"not classified: {sorted(declared - named)}; classified but gone: {sorted(named - declared)}. "
        "Name each protocol of livepeer_builder.protocols in tests/structure/test_two_halves.py: under "
        "NOT_PAYMENTS when it has nothing to do with payments, or under BEHIND_THE_PORTS when it holds "
        f"payment keys or allocation records, which the work half must not reach. {FIVE} A sixth port "
        "changes D84 in docs/design-docs/m4-decisions.md: ask before adding one to PORTS."
    )


def test_the_work_half_holds_the_ports_it_pays_and_reads_through() -> None:
    """The ports are in use where the guide says, so the checks above are not passing on nothing."""
    job = typing.get_type_hints(JobService.__init__)
    assert job["transport"] is protocols.RunnerTransport  # signing happens inside it
    assert job["credentials"] is protocols.CredentialResolver
    assert typing.get_type_hints(CostService.__init__)["allowances"] == protocols.AllowanceReader | None
    assert typing.get_type_hints(CostSyncWorker.__init__)["source"] is protocols.UsageSource


def test_each_set_of_routes_is_typed_against_its_own_half() -> None:
    """Each set is one function that adds its routes to an app, so a deployment can mount one set. Typed
    against its half, mypy refuses a work route that calls a clearinghouse command, and the other way
    round."""
    for mount, half in ((mount_work_routes, api.WorkApi), (mount_clearinghouse_routes, api.ClearinghouseApi)):
        hints = typing.get_type_hints(mount)
        expected = {"app": FastAPI, "engine": half, "return": type(None)}
        if mount is mount_clearinghouse_routes:
            expected["deliver_allocation_keys"] = bool
        assert hints == expected, (
            f"{mount.__name__}(app, engine) takes engine: {half.__name__}, the facade of its half "
            "(src/livepeer_builder/api.py). Typed as EngineApi, the set could call the other half."
        )


def test_the_work_half_of_the_facade_offers_of_payments_the_allowance_read_alone() -> None:
    offered = typing.get_type_hints(api.WorkApi.payments.fget)["return"]
    assert offered is api.AllowanceApi and members(api.AllowanceApi) == {"allowance"}, (
        f"WorkApi.payments is {offered.__name__} with {sorted(members(offered))}. {FIVE} The work "
        "routes read an allowance (GET /v1/allowance) and call no other payments method: put a "
        "clearinghouse command on ClearinghouseApi and its route in http/clearinghouse.py."
    )


def test_the_clearinghouse_half_of_the_facade_offers_no_work() -> None:
    work_only = {"jobs", "discovery", "costs", "events", "sessions"}
    assert work_only <= members(api.WorkApi)
    offered = sorted(work_only & members(api.ClearinghouseApi))
    assert not offered, (
        f"ClearinghouseApi offers {offered}. A payment clearinghouse never runs jobs: these are members "
        "of WorkApi, and their routes belong in http/work.py."
    )
    assert {api.WorkApi, api.ClearinghouseApi} <= set(api.EngineApi.__mro__)  # the whole engine is both


def test_the_allowance_read_goes_through_the_allowance_reader_port() -> None:
    """``GET /v1/allowance`` is the one payments call a work route makes. The engine answers it in
    ``PaymentsService.allowance``, which may use the policy and the two methods of ``AllowanceReader``,
    and nothing else of the service: not the actor records, the secret store or the provider."""
    port = members(protocols.AllowanceReader)
    assert port == {"allocation_for", "read_allowance"}
    tree = ast.parse((SRC / "services" / "payments.py").read_text())
    read = next(
        node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "allowance"
    )
    used = {
        node.attr
        for node in ast.walk(read)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self"
    }
    assert port <= used, "the allowance read no longer goes through AllowanceReader's two methods"
    beside = sorted(used - port - {"_policy"})
    assert not beside, (
        f"PaymentsService.allowance uses self.{', self.'.join(beside)}. {FIVE} The allowance read is a "
        "work route's: it finds the allocation with allocation_for and reads it with read_allowance, the "
        "two methods of AllowanceReader, so an engine that uses a payment clearinghouse can answer it "
        "from the clearinghouse. Put anything else it needs behind that port."
    )
