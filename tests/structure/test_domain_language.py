"""Routes, classes, settings and contracts are named in the domain language (D83, #163).

One id had several names, and several words had more than one meaning. Since #163 the engine says
``tenant`` for the account an enterprise app holds (it said application), ``clearinghouse_admin`` for the
kind that runs the deployment (``admin``), ``allot`` and ``top up`` for what an admin does to an allocation
(``provision``, ``fund``), ``payment key`` for the key that pays for one actor, and the grants token for
the Batteries credential that lists and creates grants (the operator token). The words are in
docs/design-docs/m4-guides/iss-31-payment-configurations.md ("Words used here").

No old name is kept beside the new one: no alias, no deprecated route, no fallback setting. An old
environment variable names no setting and is ignored.

Since #164 the work routes that #163 left under ``/v1/clearinghouse`` are at their own paths: an engine's
own access keys under ``/v1/keys``, and reconciliation and cost sync under ``/v1/costs`` (D87). Their old
paths are old names too.

Not checked here, because they are not these names: an app id (``app`` on offerings and jobs, ISS-48), the
failure kind ``not_provisioned`` (ISS-28), and the words Batteries and the signer use on their own wire.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from pydantic import TypeAdapter, ValidationError

import livepeer_builder
from livepeer_builder import EngineSettings, contracts, testing
from livepeer_builder.contracts import CredentialKind
from livepeer_builder.http import create_app
from livepeer_builder.store import schema
from tests.conftest import EngineFactory
from tests.structure.test_route_params import parameters

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "livepeer_builder"
MIGRATIONS = SRC / "store" / "migrations" / "versions"
OPENAPI = ROOT / "docs" / "generated" / "openapi.json"

# An old name, and what replaced it. Each is matched as written, anywhere in a line.
OLD_NAMES = {
    r"application_id": "tenant_id",
    r"application_claim": "tenant_claim (LPB_JWT_TENANT_CLAIM)",
    r"JWT_APPLICATION": "LPB_JWT_TENANT_CLAIM and LPB_JWT_TENANT_ID",
    r"/v1/admin\b": "/v1/clearinghouse",
    r"/applications/": "/tenants/",
    r"\bProvisionedActor\b": "ActorAllocation",
    r"\bProvision(Request|Body|Amount)\b": "AllotRequest, AllotBody, AllotAmount",
    r"\bFund(Request|ingAmount)\b": "TopUpRequest, TopUpAmount",
    r"\bPaymentCredential\b": "PaymentKey",
    r"(?i)operator_token": "batteries_grants_token (LPB_BATTERIES_GRANTS_TOKEN)",
    r"rotate-key\b": "rotate-payment-key",
    # work routes, which #164 moved out of the clearinghouse prefix (a tenant's keys, from #165, are under
    # /v1/clearinghouse/tenants/{tenant_id}/keys, which none of these matches)
    r"/v1/clearinghouse/keys\b": "/v1/keys (an engine's own access keys are work routes)",
    r"/v1/clearinghouse/reconciliation\b": "/v1/costs/reconciliation",
    r"/v1/clearinghouse/costs\b": "/v1/costs/sync",
}
# Methods of the embedded API and of the payment ports that carried an old word.
OLD_METHODS = {
    "provision": "allot",
    "provision_actor": "allot_actor",
    "fund": "top_up",
    "fund_grant": "top_up_grant",
    "fund_allocation": "top_up_allocation",
    "rotate_key": "rotate_payment_key",
}
OLD_SETTINGS = {
    "jwt_application_claim": "jwt_tenant_claim",
    "jwt_application_id": "jwt_tenant_id",
    "batteries_operator_token": "batteries_grants_token",
}
FIX = (
    "Use the domain-language name (D83 in docs/design-docs/m4-decisions.md, and "
    "tests/structure/test_domain_language.py for the map). Do not keep the old name as an alias."
)

# Where an old name must stay, and why. Nothing else is exempt.
KEPT = {
    # A stored fingerprint is a hash of the command and its request as they were first written
    # (tests/unit/test_hashed_forms.py). The ledger writes that text with the words it had then, so a
    # repeat that spans the upgrade still matches its record.
    SRC / "services" / "ledger.py": (r"application_id",),
}

# The contract documents and the deployment files a reader copies names from.
DOCUMENTS = (
    *sorted((ROOT / "docs" / "contracts").glob("*.md")),
    *sorted((ROOT / "docs" / "guides").glob("*.md")),
    ROOT / "README.md",
    ROOT / "ARCHITECTURE.md",
    ROOT / "AGENTS.md",
    ROOT / "docs" / "RELIABILITY.md",
    ROOT / "docs" / "SECURITY.md",
    ROOT / "deploy" / "dev-stack" / "README.md",
    ROOT / "deploy" / "dev-stack" / ".env.example",
    ROOT / "deploy" / "dev-stack" / "compose.yaml",
    ROOT / "deploy" / "dev-stack" / "config" / "init-secrets.py",
    ROOT / "deploy" / "README.md",
    *sorted((ROOT / "deploy" / "clearinghouse").glob("*.md")),
    *sorted((ROOT / "deploy" / "clearinghouse").glob("*.yaml")),
    ROOT / "deploy" / "clearinghouse" / ".env.example",
    ROOT / "deploy" / "clearinghouse" / "config" / "init-secrets.py",
    *sorted((ROOT / "deploy" / "enterprise").glob("*.md")),
    *sorted((ROOT / "deploy" / "enterprise").glob("*.yaml")),
    ROOT / "deploy" / "enterprise" / ".env.example",
    ROOT / "deploy" / "enterprise" / "allot.py",
)


def old_names(text: str, kept: tuple[str, ...] = ()) -> list[str]:
    found: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for pattern, new in OLD_NAMES.items():
            if pattern not in kept and (match := re.search(pattern, line)):
                found.append(f"line {number}: {match.group(0)!r} is now {new}")
    return found


def outside_upgrade_notes(markdown: str) -> str:
    """The document without its sections headed "Upgrading ...": they say what a name was called. The
    lines are blanked, not removed, so a line number still points into the file."""
    kept: list[str] = []
    depth = 0  # the level of the "Upgrading" heading being skipped; 0 outside one
    for line in markdown.splitlines():
        heading = re.match(r"(#+) +(.*)", line)
        if heading and depth and len(heading.group(1)) <= depth:
            depth = 0
        if heading and not depth and heading.group(2).startswith("Upgrading"):
            depth = len(heading.group(1))
        kept.append("" if depth else line)
    return "\n".join(kept)


def test_the_package_uses_no_old_name() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}, {found}"
        for path in sorted(SRC.rglob("*.py"))
        if MIGRATIONS not in path.parents  # history, and the migration that renames
        for found in old_names(path.read_text(), KEPT.get(path, ()))
    ]
    assert not offenders, f"{FIX}\n" + "\n".join(offenders)


def test_no_method_of_the_package_keeps_an_old_word() -> None:
    declared = re.compile(r"^\s*(?:async )?def (\w+)\(", re.MULTILINE)
    offenders = [
        f"{path.relative_to(ROOT)}: def {name} is now {OLD_METHODS[name]}"
        for path in sorted(SRC.rglob("*.py"))
        if MIGRATIONS not in path.parents
        for name in declared.findall(path.read_text())
        if name in OLD_METHODS
    ]
    assert not offenders, f"{FIX}\n" + "\n".join(offenders)


async def test_no_route_uses_an_old_name(make_engine: EngineFactory) -> None:
    engine = await make_engine()  # API keys and the grant desk on, so every route exists
    routes = [
        route for route in create_app(engine, manage_lifecycle=False).routes if isinstance(route, APIRoute)
    ]
    offenders: list[str] = []
    for route in routes:
        where = f"{sorted(route.methods)} {route.path}"
        offenders += [f"{where}, {found}" for found in old_names(route.path)]
        if re.search(r"/fund\b", route.path):
            offenders.append(f"{where}: '/fund' is now '/top-up'")
        for kind, field in parameters(route.dependant):
            if field.alias in ("application_id", "provisioned"):
                offenders.append(
                    f"{where}: {kind} parameter {field.alias!r} is now 'tenant_id' or 'allotted'"
                )
    assert len(routes) >= 30  # every route today; fewer means the walk missed some
    assert any(route.path.startswith("/v1/clearinghouse/tenants/{tenant_id}/") for route in routes)
    assert {"/v1/keys", "/v1/costs/reconciliation", "/v1/costs/sync"} <= {route.path for route in routes}
    assert not offenders, f"{FIX}\n" + "\n".join(offenders)


def test_the_published_schema_uses_no_old_name() -> None:
    """docs/generated/openapi.json is what a REST client is generated from; ``lpb gen-docs --check`` keeps
    it equal to the served schema."""
    text = OPENAPI.read_text()
    offenders = old_names(text)
    schema_names = set(json.loads(text)["components"]["schemas"])
    offenders += sorted(schema_names & {"ProvisionedActor", "ProvisionBody", "FundRequest"})
    properties = set(re.findall(r'"(provisioned|application_id)":', text))
    offenders += [f"property {name!r}" for name in sorted(properties)]
    assert not offenders, f"{FIX} Then run `uv run lpb gen-docs`.\n" + "\n".join(offenders)
    kinds = json.loads(text)["components"]["schemas"]["AccessKey"]["properties"]["kind"]["enum"]
    assert "clearinghouse_admin" in kinds and "admin" not in kinds, (
        f"the credential kinds are {kinds}: the kind that runs the deployment is clearinghouse_admin. {FIX}"
    )


def test_the_old_admin_kind_is_refused() -> None:
    """Not read as clearinghouse_admin: a key request or an ``ActorContext`` that still says ``admin`` fails
    where it is parsed."""
    kinds = TypeAdapter(CredentialKind)
    assert kinds.validate_python("clearinghouse_admin") == "clearinghouse_admin"
    with pytest.raises(ValidationError):
        kinds.validate_python("admin")


def test_no_setting_keeps_an_old_name() -> None:
    present = sorted(set(OLD_SETTINGS) & set(EngineSettings.model_fields))
    assert not present, f"EngineSettings has {present}; they are now {OLD_SETTINGS}. {FIX}"
    assert set(OLD_SETTINGS.values()) <= set(EngineSettings.model_fields)


def test_an_old_environment_variable_is_ignored() -> None:
    """Like any variable that names no setting: it is not read as the new one, and it is not an error."""
    old = {
        "LPB_DISCOVERY_URL": "http://discovery:8939/discovery",
        "LPB_JWT_APPLICATION_CLAIM": "org",
        "LPB_JWT_APPLICATION_ID": "acme",
        "LPB_BATTERIES_OPERATOR_TOKEN": "not-read",
    }
    settings = EngineSettings.from_env(old)
    assert settings.jwt_tenant_claim is None and settings.jwt_tenant_id is None
    assert settings.batteries_grants_token is None


def test_an_engine_with_jwt_still_set_with_the_old_tenant_variables_refuses_to_start() -> None:
    """#163 review: ignored, the old claim variable left every token in one tenant, so users of different
    organisations shared it. Neither new variable has a default, so the engine refuses to start and
    names them."""
    jwt = {
        "LPB_DISCOVERY_URL": "http://discovery:8939/discovery",
        "LPB_AUTH": "jwt",
        "LPB_JWT_ISSUER": "https://idp.example/",
        "LPB_JWT_AUDIENCE": "engine",
    }
    for old in ({"LPB_JWT_APPLICATION_CLAIM": "org"}, {"LPB_JWT_APPLICATION_ID": "acme"}):
        with pytest.raises(ValidationError, match="LPB_JWT_TENANT_CLAIM and LPB_JWT_TENANT_ID"):
            EngineSettings.from_env(jwt | old)


def test_the_public_api_exports_no_old_name() -> None:
    exported = {
        f"{module.__name__}.{name}"
        for module in (livepeer_builder, contracts, testing)
        for name in module.__all__
    }
    offenders = sorted(name for name in exported if old_names(name))
    assert not offenders, f"{FIX}\n" + "\n".join(offenders)
    assert {"ActorAllocation", "AllotRequest", "TopUpRequest", "PaymentKey"} <= set(contracts.__all__)


def test_no_table_keeps_an_application_column() -> None:
    columns = sorted(
        f"{table.name}.{column.name}"
        for table in schema.metadata.tables.values()
        for column in table.columns
        if "application" in column.name
    )
    assert not columns, (
        f"{columns}: a record names its tenant in tenant_id. Rename the column in store/schema.py, with "
        "a migration that keeps the rows, then run `uv run lpb gen-docs`."
    )


def test_the_contract_documents_use_no_old_name() -> None:
    offenders: list[str] = []
    for path in DOCUMENTS:
        text = path.read_text()
        if path.suffix == ".md":
            text = outside_upgrade_notes(text)
        offenders += [f"{path.relative_to(ROOT)}, {found}" for found in old_names(text)]
    assert not offenders, (
        f"{FIX} A section headed 'Upgrading ...' may say what a name was called.\n" + "\n".join(offenders)
    )
