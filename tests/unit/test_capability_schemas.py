"""Schema reads spend nothing and remain pinned to current model/deployment identity."""

import copy
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from livepeer_builder.http import create_app
from livepeer_builder.runtime.capability_schemas import load_capability_openapi, parse_capability_openapi
from livepeer_builder.testing import runner
from tests.conftest import EngineFactory

_OPERATION = "/private-prefix/v1/images/generations"


def document() -> dict:
    return {
        "openapi": "3.1.0",
        "paths": {
            "/private-prefix/v1/images/generations": {
                "post": {
                    "operationId": "image",
                    "x-livepeer": {
                        "app": "example/echo",
                        "model": "image-model",
                        "deployment_revision": "sha256:" + "a" * 64,
                        "path": "/v1/images/generations",
                    },
                    "requestBody": {
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Input"}}}
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "Input": {
                    "type": "object",
                    "required": ["model", "prompt"],
                    "properties": {
                        "model": {"const": "image-model"},
                        "prompt": {"type": "string"},
                        "seed": {"type": "integer", "default": 0},
                        "enabled": {"type": "boolean", "default": False},
                        "settings": {"type": "object", "properties": {"steps": {"default": 4}}},
                    },
                }
            }
        },
    }


def test_schema_local_refs_and_nested_defaults_are_copied_without_fabricating_required_inputs() -> None:
    source = document()
    original = copy.deepcopy(source)
    profile = parse_capability_openapi(source)[0]
    assert profile.path == "/v1/images/generations"
    assert profile.defaults == {"model": "image-model", "seed": 0, "enabled": False, "settings": {"steps": 4}}
    assert "prompt" not in profile.defaults
    assert source == original
    profile.defaults["seed"] = 7
    assert parse_capability_openapi(source)[0].defaults["seed"] == 0


@pytest.mark.parametrize("reference", ["https://private.example/schema", "#/components/schemas/Input"])
def test_external_and_recursive_references_fail_without_network_reads(reference: str) -> None:
    source = document()
    source["components"]["schemas"]["Input"] = {"$ref": reference}
    with pytest.raises(ValueError, match="Only non-recursive local schema references"):
        parse_capability_openapi(source)


def _post(source: dict) -> dict:
    return source["paths"][_OPERATION]["post"]


def test_catalog_requires_openapi_31() -> None:
    source = document()
    source["openapi"] = "3.0.3"
    with pytest.raises(ValueError, match=r"OpenAPI 3\.1\.0"):
        parse_capability_openapi(source)


def test_non_capability_operations_are_ignored() -> None:
    source = document()
    source["paths"]["/health"] = {"get": {"operationId": "health"}}
    source["paths"]["/plain"] = {"post": {"operationId": "plain"}}
    assert [profile.id for profile in parse_capability_openapi(source)] == ["image"]


def test_unsupported_request_body_is_rejected() -> None:
    source = document()
    _post(source)["requestBody"]["content"] = {"text/plain": {"schema": {"type": "object"}}}
    with pytest.raises(ValueError, match="Unsupported capability request body"):
        parse_capability_openapi(source)


def test_non_object_input_is_rejected() -> None:
    source = document()
    _post(source)["requestBody"]["content"]["application/json"]["schema"] = {"type": "string"}
    with pytest.raises(ValueError, match="must be an object"):
        parse_capability_openapi(source)


def test_schema_model_must_match_the_deployment_binding() -> None:
    source = document()
    source["components"]["schemas"]["Input"]["properties"]["model"]["const"] = "other-model"
    with pytest.raises(ValueError, match="Schema model must match"):
        parse_capability_openapi(source)


def test_reference_siblings_are_rejected() -> None:
    source = document()
    _post(source)["requestBody"]["content"]["application/json"]["schema"] = {
        "$ref": "#/components/schemas/Input",
        "description": "merged",
    }
    with pytest.raises(ValueError, match="reference siblings"):
        parse_capability_openapi(source)


def test_reference_chain_depth_is_bounded() -> None:
    source = document()
    schemas = source["components"]["schemas"]
    schemas["n0"] = {"type": "string"}
    for level in range(1, 34):
        schemas[f"n{level}"] = {"$ref": f"#/components/schemas/n{level - 1}"}
    schemas["Input"] = {"$ref": "#/components/schemas/n33"}
    with pytest.raises(ValueError, match="supported depth"):
        parse_capability_openapi(source)


def test_duplicate_operation_identity_is_rejected() -> None:
    source = document()
    source["paths"]["/other"] = copy.deepcopy(source["paths"][_OPERATION])
    with pytest.raises(ValueError, match="Duplicate or invalid"):
        parse_capability_openapi(source)


def test_parent_path_segment_is_rejected() -> None:
    source = document()
    _post(source)["x-livepeer"]["path"] = "/v1/../generations"
    with pytest.raises(ValidationError, match="path"):
        parse_capability_openapi(source)


def test_load_capability_openapi_reads_a_bounded_catalog(tmp_path: Path) -> None:
    assert load_capability_openapi(None) == ()
    path = tmp_path / "capabilities.json"
    path.write_text(json.dumps(document()), encoding="utf-8")
    assert load_capability_openapi(str(path))[0].id == "image"
    path.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="exceeds 4 MiB"):
        load_capability_openapi(str(path))


def test_reference_expansion_is_bounded() -> None:
    source = document()
    schemas = source["components"]["schemas"]
    schemas["n0"] = {"type": "string"}
    for level in range(1, 17):
        reference = {"$ref": f"#/components/schemas/n{level - 1}"}
        schemas[f"n{level}"] = {"type": "object", "properties": {"left": reference, "right": reference}}
    schemas["Input"] = {"$ref": "#/components/schemas/n16"}
    with pytest.raises(ValueError, match="Expanded schema exceeds"):
        parse_capability_openapi(source)


async def test_capability_endpoint_authenticates_and_matches_discovered_deployment(
    make_engine: EngineFactory,
) -> None:
    metadata = json.dumps(
        {
            "model_name": "image-model",
            "deployment_revision": "sha256:" + "a" * 64,
            "endpoints": {"images_generations": {"path": "/v1/images/generations"}},
        }
    )
    engine = await make_engine(runners=[runner("example/echo", metadata=metadata)])
    admin = await engine.keys.bootstrap_admin()
    app = create_app(engine, manage_lifecycle=False, capability_profiles=parse_capability_openapi(document()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        assert (await client.get("/v1/capabilities")).status_code == 401
        response = await client.get("/v1/capabilities", headers={"Authorization": "Bearer " + admin.token})
        assert response.status_code == 200
        assert response.json()[0]["defaults"]["model"] == "image-model"
        assert "private-prefix" not in response.text
        assert (
            await client.get("/v1/capabilities?app=other", headers={"Authorization": "Bearer " + admin.token})
        ).json() == []
    changed = document()
    changed["paths"]["/private-prefix/v1/images/generations"]["post"]["x-livepeer"]["deployment_revision"] = (
        "sha256:" + "b" * 64
    )
    app = create_app(engine, manage_lifecycle=False, capability_profiles=parse_capability_openapi(changed))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        assert (
            await client.get("/v1/capabilities", headers={"Authorization": "Bearer " + admin.token})
        ).json() == []


async def test_unreadable_runner_metadata_does_not_match_or_stop_the_scan(
    make_engine: EngineFactory,
) -> None:
    metadata = json.dumps(
        {
            "model_name": "image-model",
            "deployment_revision": "sha256:" + "a" * 64,
            "endpoints": {"images_generations": {"path": "/v1/images/generations"}},
        }
    )
    engine = await make_engine(
        runners=[
            runner("example/echo", url="https://runner-bad.example", metadata="{"),
            runner("example/echo", url="https://runner-ok.example", metadata=metadata),
        ]
    )
    admin = await engine.keys.bootstrap_admin()
    app = create_app(engine, manage_lifecycle=False, capability_profiles=parse_capability_openapi(document()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        response = await client.get("/v1/capabilities", headers={"Authorization": "Bearer " + admin.token})
    assert response.status_code == 200
    assert [profile["id"] for profile in response.json()] == ["image"]
