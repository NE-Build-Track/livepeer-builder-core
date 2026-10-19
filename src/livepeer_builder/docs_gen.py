"""Generators for docs/generated. CI runs ``lpb gen-docs --check`` so committed docs never drift."""

import json

from livepeer_builder.config import EngineSettings
from livepeer_builder.runtime import BuilderEngine
from livepeer_builder.store.schema import metadata


async def render_openapi() -> str:
    from livepeer_builder.http import create_app

    settings = EngineSettings(database_url="sqlite+aiosqlite:///:memory:", log_json=False)
    engine = await BuilderEngine.from_settings(settings)
    try:
        schema = create_app(engine, manage_lifecycle=False).openapi()
    finally:
        await engine.stop()
    schema["info"]["version"] = "generated"  # keep the file stable across dev versions
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


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
    ('lpb_actors ||--o{ lpb_jobs : "owns (application_id, actor_id)"'),
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
