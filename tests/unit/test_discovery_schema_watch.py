"""The weekly schema watcher: a missing file is drift, a network failure is not."""

import importlib.util
import json
import urllib.error
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[2] / "scripts" / "check_discovery_schema.py"


def _watcher():
    spec = importlib.util.spec_from_file_location("check_discovery_schema", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _contents(digest_of: bytes) -> bytes:
    import base64

    return json.dumps({"content": base64.b64encode(digest_of).decode()}).encode()


def test_a_release_without_the_schema_is_drift(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _watcher()
    schema = (module._ROOT / "src/livepeer_builder/contracts/discovery.schema.json").read_bytes()

    def get(url: str) -> bytes:
        if url.endswith("/releases/latest"):
            return b'{"tag_name": "v1.0.6"}'
        if url.endswith("/commits/main"):
            return b'{"sha": "abc"}'
        if "ref=abc" in url:
            return _contents(schema)
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    monkeypatch.setattr(module, "_get", get)
    assert module.main() == 2
    report = capsys.readouterr().out
    assert "latest tag v1.0.6: HTTP 404" in report
    assert "main abc" not in report


def test_a_network_failure_is_not_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _watcher()

    def get(url: str) -> bytes:
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(module, "_get", get)
    with pytest.raises(urllib.error.URLError):
        module.main()
