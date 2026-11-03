"""Compose operator-installed OpenAPI profiles. Never fetch runner-advertised URLs."""

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from livepeer_builder.contracts.capabilities import CapabilityProfile

_MISSING = object()


def _resolve(
    value: Any, document: dict[str, Any], seen: tuple[str, ...] = (), budget: list[int] | None = None
) -> Any:
    if budget is None:
        budget = [10000]
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError("Expanded schema exceeds the supported size")
    if len(seen) > 32:
        raise ValueError("Schema references exceed the supported depth")
    if isinstance(value, list):
        return [_resolve(item, document, seen, budget) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        ref = value["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/components/schemas/") or ref in seen:
            raise ValueError("Only non-recursive local schema references are supported")
        target: Any = document
        for part in ref[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        resolved = _resolve(target, document, (*seen, ref), budget)
        if set(value) != {"$ref"}:
            raise ValueError("Schema reference siblings require an explicit resolved schema")
        return resolved
    return {key: _resolve(item, document, seen, budget) for key, item in value.items()}


def _default(schema: dict[str, Any]) -> Any:
    for key in ("const", "default"):
        if key in schema:
            return copy.deepcopy(schema[key])
    result = {}
    for key, child in schema.get("properties", {}).items():
        value = _default(child)
        if value is not _MISSING:
            result[key] = value
    return result if result else _MISSING


def parse_capability_openapi(document: dict[str, Any]) -> tuple[CapabilityProfile, ...]:
    """Each operation's x-livepeer pins its app, exact model, deployment and public path."""
    if document.get("openapi") != "3.1.0":
        raise ValueError("Capability catalog requires OpenAPI 3.1.0")
    profiles = []
    identities = set()
    for path_item in document["paths"].values():
        operation = path_item.get("post")
        if not operation or "x-livepeer" not in operation:
            continue
        binding = operation["x-livepeer"]
        content = operation["requestBody"]["content"]
        content_type = next(
            (mime for mime in ("application/json", "multipart/form-data") if mime in content), None
        )
        if content_type is None:
            raise ValueError("Unsupported capability request body")
        schema = _resolve(content[content_type]["schema"], document)
        if schema.get("type") != "object":
            raise ValueError("Capability input must be an object")
        if schema.get("properties", {}).get("model", {}).get("const") != binding["model"]:
            raise ValueError("Schema model must match the deployment binding")
        default = _default(schema)
        defaults: dict[str, JsonValue] = {} if default is _MISSING else default
        profile = CapabilityProfile(
            id=operation["operationId"],
            app=binding["app"],
            model=binding["model"],
            deployment_revision=binding["deployment_revision"],
            path=binding["path"],
            content_type=content_type,
            input_schema=schema,
            defaults=defaults,
            schema_digest="sha256:" + hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest(),
        )
        identity = (profile.app, profile.id)
        if identity in identities or ".." in profile.path.split("/"):
            raise ValueError("Duplicate or invalid capability operation")
        identities.add(identity)
        profiles.append(profile)
    return tuple(profiles)


def load_capability_openapi(path: str | None) -> tuple[CapabilityProfile, ...]:
    if path is None:
        return ()
    with Path(path).open("rb") as handle:
        raw = handle.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("Capability catalog exceeds 4 MiB")
    return parse_capability_openapi(json.loads(raw))
