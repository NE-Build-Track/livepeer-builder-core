"""Generated class diagrams of the extension interfaces and core contracts (``lpb gen-docs``).

Generated from the code so they can't drift: CI runs ``lpb gen-docs --check``. Every protocol must be
listed in ``DEFAULTS`` (its default implementations), or generation fails with how to fix it.
"""

import inspect
import types
import typing
from typing import Any

from pydantic import BaseModel

from livepeer_builder import protocols
from livepeer_builder.adapters.auth import ApiKeyAuthenticator, AuthenticatorChain
from livepeer_builder.adapters.batteries import BatteriesClient, BatteriesUsageSource
from livepeer_builder.adapters.clock import SystemClock
from livepeer_builder.adapters.sdk import SdkRunnerStream, SdkRunnerTransport, SignerDiscoverySource
from livepeer_builder.adapters.secrets import EncryptedDbSecretStore, RoutingSecretStore, SettingsSecretStore
from livepeer_builder.adapters.static import StaticDiscoverySource
from livepeer_builder.contracts import (
    ActorContext,
    Allowance,
    Attempt,
    AttemptCost,
    AuthProvenance,
    AuthRequest,
    Failure,
    Fees,
    Job,
    JobCost,
    JobRequest,
    JobResult,
    MeasuredUsage,
    Money,
    NetworkRate,
    Offering,
    ProvisionedActor,
    ResourceRef,
    ResultRef,
    Runner,
    RunnerGPU,
    SecretRef,
)
from livepeer_builder.services.discovery import MostCapacityFirst
from livepeer_builder.services.payments import PaymentsService
from livepeer_builder.services.policy import DefaultPolicy
from livepeer_builder.services.reserved import ReservedEventFeed, ReservedSessions
from livepeer_builder.store import SqlStore
from livepeer_builder.store.maintenance import SqlMaintenance

# Extension protocols shown in the diagram, with their default implementations.
DEFAULTS: dict[str, tuple[type, ...]] = {
    "Authenticator": (ApiKeyAuthenticator, AuthenticatorChain),
    "AccessPolicy": (DefaultPolicy,),
    "SecretStore": (RoutingSecretStore, SettingsSecretStore, EncryptedDbSecretStore),
    "DiscoverySource": (SignerDiscoverySource, StaticDiscoverySource),
    "SelectionPolicy": (MostCapacityFirst,),
    "RunnerTransport": (SdkRunnerTransport,),
    "StreamingRunnerTransport": (SdkRunnerTransport,),
    "RunnerStream": (SdkRunnerStream,),
    "CredentialResolver": (PaymentsService,),
    "AllocationManager": (BatteriesClient,),
    "UsageSource": (BatteriesUsageSource,),
    "AllowanceReader": (PaymentsService,),
    "EngineStore": (SqlStore,),
    "StoreMaintenance": (SqlMaintenance,),
    "Clock": (SystemClock,),
    "EventFeed": (ReservedEventFeed,),
    "SessionManager": (ReservedSessions,),
}
# Persistence sub-interfaces: implemented through EngineStore, listed in a table instead of the diagram.
REPOS = ("ActorRepo", "KeyRepo", "JobRepo", "CostRepo", "SecretRepo")

CONTRACTS: tuple[type[BaseModel], ...] = (
    AuthRequest, ActorContext, AuthProvenance, ResourceRef,
    JobRequest, Job, Attempt, Failure, ResultRef, MeasuredUsage, JobResult,
    Runner, RunnerGPU, NetworkRate, Offering,
    JobCost, AttemptCost, Fees,
    ProvisionedActor, SecretRef, Allowance, Money,
)  # fmt: skip


def protocol_classes() -> dict[str, type]:
    return {
        name: obj
        for name, obj in vars(protocols).items()
        if inspect.isclass(obj)
        and getattr(obj, "_is_protocol", False)
        and obj.__module__ == protocols.__name__
    }


def type_name(annotation: Any) -> str:
    """A compact, Mermaid-safe type name (no pipes, commas or semicolons)."""
    if annotation is inspect.Parameter.empty or annotation is None or annotation is type(None):
        return "None"
    origin = typing.get_origin(annotation)
    args = [a for a in typing.get_args(annotation) if a is not Ellipsis]
    if origin in (typing.Union, types.UnionType):
        present = [a for a in args if a is not type(None)]
        name = " or ".join(type_name(a) for a in present)
        return name + "?" if len(present) < len(args) else name
    if origin is typing.Literal:
        return "enum"
    if origin in (tuple, list, set, frozenset) or (origin and getattr(origin, "__name__", "") == "Sequence"):
        return (type_name(args[0]) if args else "any") + "[]"
    if origin in (dict,) or (origin and getattr(origin, "__name__", "") == "Mapping"):
        return "map"
    return getattr(annotation, "__name__", str(annotation)).replace(",", " ")


def _methods(cls: type, own_only: bool = False) -> list[str]:
    """Public properties and methods, inherited included unless ``own_only`` (EngineStore lists its repos
    separately). ``__aiter__`` counts as public: it is how a stream is read."""
    lines = []
    for name, prop in inspect.getmembers(cls, predicate=lambda m: isinstance(m, property)):
        if not name.startswith("_") and prop.fget is not None:
            lines.append(f"        +{name} {type_name(typing.get_type_hints(prop.fget).get('return'))}")
    for name, member in inspect.getmembers(cls, predicate=inspect.isfunction):
        if (name.startswith("_") and name != "__aiter__") or (own_only and name not in vars(cls)):
            continue
        hints = typing.get_type_hints(member)
        params = [p for p in inspect.signature(member).parameters if p != "self"]
        prefix = "async " if inspect.iscoroutinefunction(member) else ""
        lines.append(f"        +{prefix}{name}({', '.join(params)}) {type_name(hints.get('return'))}")
    return lines


def render_interfaces() -> str:
    found = protocol_classes()
    missing = sorted(set(found) - set(DEFAULTS) - set(REPOS))
    if missing:
        hint = "Add each one to docs_diagrams.DEFAULTS with its default implementations."
        raise RuntimeError(f"protocols without documented defaults: {missing}. {hint}")
    lines = [
        "# Extension interfaces (generated)",
        "",
        "Generated by `lpb gen-docs` from `src/livepeer_builder/protocols/`. Do not edit by hand. Every",
        "method is `async` unless marked otherwise. The extension guide is",
        "[../design-docs/extending-the-engine.md](../design-docs/extending-the-engine.md).",
        "",
        "## Interfaces and their default implementations",
        "",
        "```mermaid",
        "classDiagram",
        "    direction LR",
    ]
    for name in DEFAULTS:
        lines.append(f"    class {name} {{")
        lines.append("        <<interface>>")
        if name == "EngineStore":
            lines.append("        +async ...ActorRepo, KeyRepo, JobRepo, CostRepo, SecretRepo methods() None")
        lines += _methods(found[name], own_only=name == "EngineStore")
        lines.append("    }")
    implementations = sorted({cls.__name__ for defaults in DEFAULTS.values() for cls in defaults})
    for impl in implementations:
        lines.append(f"    class {impl}")
    for name, defaults in DEFAULTS.items():
        for cls in defaults:
            lines.append(f"    {name} <|.. {cls.__name__}")
    lines += ["```", "", "## Default implementations", ""]
    lines += ["| Interface | Default | Module |", "| --- | --- | --- |"]
    for name, defaults in DEFAULTS.items():
        for cls in defaults:
            lines.append(f"| `{name}` | `{cls.__name__}` | `{cls.__module__}` |")
    lines += ["", "## Persistence sub-interfaces", ""]
    lines += ["`EngineStore` combines these. `SqlStore` implements all of them.", ""]
    lines += ["| Interface | Methods |", "| --- | --- |"]
    for repo in REPOS:
        names = [n for n, m in vars(found[repo]).items() if inspect.isfunction(m) and not n.startswith("_")]
        lines.append(f"| `{repo}` | {', '.join(f'`{n}`' for n in names)} |")
    return "\n".join(lines) + "\n"


def render_contracts() -> str:
    lines = [
        "# Core contracts (generated)",
        "",
        "Generated by `lpb gen-docs` from `src/livepeer_builder/contracts/`. Do not edit by hand. Every",
        "contract is a frozen pydantic model, the same class in-process and in the REST schema. `X?` means",
        "optional, `X[]` a list. Composition edges link a contract to contracts it contains.",
        "",
        "```mermaid",
        "classDiagram",
    ]
    names = {cls.__name__ for cls in CONTRACTS}
    edges: set[str] = set()
    for cls in CONTRACTS:
        lines.append(f"    class {cls.__name__} {{")
        for field, info in cls.model_fields.items():
            lines.append(f"        +{type_name(info.annotation)} {field}")
            for inner in _referenced(info.annotation):
                if inner in names and inner != cls.__name__:
                    edges.add(f"    {cls.__name__} *-- {inner} : {field}")
        lines.append("    }")
    lines += sorted(edges)
    lines.append("```")
    return "\n".join(lines) + "\n"


def _referenced(annotation: Any) -> set[str]:
    found = {getattr(annotation, "__name__", "")}
    for arg in typing.get_args(annotation):
        found |= _referenced(arg)
    return found
