"""Schema reads spend nothing and remain pinned to current model/deployment identity."""

import copy
import json

import httpx
import pytest

from livepeer_builder.http import create_app
from livepeer_builder.runtime.capability_schemas import parse_capability_openapi
from livepeer_builder.testing import runner
from tests.conftest import EngineFactory


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
