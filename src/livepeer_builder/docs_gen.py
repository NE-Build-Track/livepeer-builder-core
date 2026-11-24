"""Generators for docs/generated. CI runs ``lpb gen-docs --check`` so committed docs never drift."""

import json
from typing import Any, Literal

from livepeer_builder.config import EngineSettings
from livepeer_builder.runtime import BuilderEngine
from livepeer_builder.store.schema import metadata

# The roles a schema is published for. ``work`` has none of its own: it serves the work routes of the
# ``both`` role's document and nothing else.
PublishedRole = Literal["both", "clearinghouse"]


async def render_openapi(role: PublishedRole = "both") -> str:
    """The schema of the app ``lpb serve`` runs for an engine in ``role``, built the same way.

    ``both``, the default, is ``openapi.json``: every route, on one listener. Its allotment and rotation
    answer the allocation alone, because that engine stores each key for its own jobs.
    ``clearinghouse`` is ``openapi-clearinghouse.json``: every route of a payment clearinghouse. That
    role has two listeners and neither serves them all: a tenant's own routes are on the public one, and
    the paths a clearinghouse admin names a tenant in on the private one (D87 point 3, as amended on 19
    November 2026). So the document is the two listeners' put together (``_of_both_listeners``), and
    each route's tag says which serves it. Its allotment and rotation answer the allocation with its key
    (``allocation_api_key``, ``replayed``, ``resumed``), which only that role hands over (#169). An
    engine in its ``work`` role serves the work routes of ``openapi.json`` and nothing else, so it has
    no file of its own."""
    from livepeer_builder.http.app import create_app
    from livepeer_builder.http.listeners import delivers_allocation_keys
    from livepeer_builder.runtime.assembly import EngineSupply
    from livepeer_builder.testing import FakeAllocationManager, FakeUsageSource

    # Every optional capability on (API keys by default; the tenant desk needs a grants manager),
    # so the committed spec documents the whole API. A deployment without one lacks those routes.
    values: dict[str, Any] = {}
    supply = EngineSupply(grants_manager=FakeAllocationManager())
    standalone = delivers_allocation_keys(role)
    if standalone:  # a clearinghouse has two listeners, allots, and serves each tenant's usage
        provider = FakeAllocationManager()
        values = {"role": role, "http_private_port": 8081}
        supply = EngineSupply(
            allocation_manager=provider, grants_manager=provider, usage_source=FakeUsageSource()
        )
    settings = EngineSettings(
        database_url="sqlite+aiosqlite:///:memory:",
        log_json=False,
        discovery_url="http://127.0.0.1:9/discovery",
        **values,
    )
    engine = await BuilderEngine.create(settings, supply=supply)
    try:
        # The app ``lpb serve`` runs, built the same way, so the spec documents what ships (#186 review).
        if standalone:
            schema = _of_both_listeners(
                create_app(engine, manage_lifecycle=False, listener="public").openapi(),
                create_app(engine, manage_lifecycle=False, listener="private").openapi(),
            )
        else:
            schema = create_app(engine, manage_lifecycle=False).openapi()
    finally:
        await engine.stop()
    schema["info"]["version"] = "generated"  # keep the file stable across dev versions
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def _of_both_listeners(public: dict[str, Any], private: dict[str, Any]) -> dict[str, Any]:
    """One document for an engine with two listeners: every path and every component of either, and the
    tags of both, the public listener's first. What both listeners describe (the runner list, health, a
    model both answer with) must be described the same way by both: a difference is an error here, never
    one side silently kept."""
    merged = dict(private)
    merged["paths"] = _union("path", public["paths"], private["paths"])
    merged["components"] = {
        section: _union(
            section, public["components"].get(section, {}), private["components"].get(section, {})
        )
        for section in sorted({*public["components"], *private["components"]})
    }
    seen = {tag["name"] for tag in public.get("tags", [])}
    more = [tag for tag in private.get("tags", []) if tag["name"] not in seen]
    merged["tags"] = [*public.get("tags", []), *more]
    return merged


def _union(what: str, one: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    differing = sorted(name for name in one.keys() & other.keys() if one[name] != other[name])
    if differing:
        raise ValueError(f"the two listeners describe the same {what} differently: {differing}")
    return {**one, **other}


def render_db_schema() -> str:
    lines = [
        "# Database schema (generated)",
        "",
        "Generated from `src/livepeer_builder/store/schema.py` by `lpb gen-docs`. Do not edit by hand.",
        "Migrations live in `src/livepeer_builder/store/migrations/versions/`.",
        "",
    ]
    lines += [*render_er_diagram(), ""]
    for table in sorted(metadata.tables.values(), key=lambda t: t.name):
        lines += [f"## `{table.name}`", "", "| Column | Type | Null | Key |", "| --- | --- | --- | --- |"]
        for column in table.columns:
            key = "PK" if column.primary_key else ("FK" if column.foreign_keys else "")
            if column.unique:
                key = (key + " unique").strip()
            null = "yes" if column.nullable else "no"
            lines.append(f"| `{column.name}` | {column.type.compile()} | {null} | {key} |")
        indexes = sorted(table.indexes, key=lambda i: i.name or "")
        if indexes:
            lines += ["", "Indexes:", ""]
            for index in indexes:
                cols = ", ".join(col.name for col in index.columns)
                lines.append(f"- `{index.name}` ({cols}){' unique' if index.unique else ''}")
        lines.append("")
    return "\n".join(lines)


# Relationships the schema expresses by convention rather than foreign keys (joined in queries).
_LOGICAL_LINKS = (
    ('lpb_actors ||--o{ lpb_jobs : "owns (tenant_id, actor_id)"'),
    ('lpb_actors ||--o| lpb_secrets : "secret_ref"'),
    ('lpb_payment_sessions }o--o| lpb_usage_rows : "matched payment_session_id"'),
    ('lpb_actors ||--o{ lpb_usage_rows : "allocation_id"'),
)


def render_er_diagram() -> list[str]:
    """A Mermaid ER diagram of every lpb_ table: foreign keys plus the documented logical joins."""
    lines = ["## Entity-relationship diagram", "", "```mermaid", "erDiagram"]
    for table in sorted(metadata.tables.values(), key=lambda t: t.name):
        lines.append(f"    {table.name} {{")
        for column in table.columns:
            kind = type(column.type).__name__.lower().replace("utcdatetime", "datetime")
            key = "PK" if column.primary_key else ("FK" if column.foreign_keys else "")
            lines.append(f"        {kind} {column.name}{(' ' + key) if key else ''}")
        lines.append("    }")
    links = set()
    for table in metadata.tables.values():
        for constraint in table.foreign_key_constraints:
            parent = constraint.referred_table.name
            columns = {c.name for c in constraint.columns}
            pk = {c.name for c in table.primary_key.columns}
            cardinality = "||--o|" if columns == pk else "||--o{"  # FK that is the whole PK: at most one
            links.add(f'    {parent} {cardinality} {table.name} : "{", ".join(sorted(columns))}"')
    lines += sorted(links)
    lines += [f"    {link}" for link in _LOGICAL_LINKS]
    lines += [
        "```",
        "",
        "Solid lines are foreign keys. `lpb_actors` and `lpb_usage_rows` links are logical joins used by",
        "the cost service (a matched `payment_session_id` joins a session-log entry to usage).",
    ]
    return lines
