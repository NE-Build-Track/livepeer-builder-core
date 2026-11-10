"""The weekly schema watcher (D63).

``main`` is always compared. The latest tag is compared once it contains the pinned commit. A different
schema, or none (HTTP 404), on a compared ref is drift (exit 2). Any other HTTP error, or a network
failure, is not drift: it raises, the job fails and no issue is opened.
"""

import base64
import importlib.util
import json
import urllib.error
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).parents[2] / "scripts" / "check_discovery_schema.py"
_MAIN = "abc"
_TAG = "v1.0.6"


def _watcher() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_discovery_schema", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _github(
    module: ModuleType,
    *,
    tag_contains_pin: bool,
    schema_at: dict[str, bytes | int],
    compare_error: int | None = None,
) -> Callable[[str], bytes]:
    """A fake GitHub API. ``schema_at`` maps a ref to the schema it serves, or to an HTTP error code."""
    pin = json.loads(module._PIN.read_text())

    def get(url: str) -> bytes:
        if url.endswith("/releases/latest"):
            return json.dumps({"tag_name": _TAG}).encode()
        if url.endswith("/commits/main"):
            return json.dumps({"sha": _MAIN}).encode()
        if f"/compare/{pin['commit']}...{_TAG}" in url:
            if compare_error is not None:
                raise urllib.error.HTTPError(url, compare_error, "error", None, None)  # type: ignore[arg-type]
            return json.dumps({"status": "ahead" if tag_contains_pin else "behind"}).encode()
        for ref, served in schema_at.items():
            if url.endswith(f"?ref={ref}"):
                if isinstance(served, int):
                    raise urllib.error.HTTPError(url, served, "error", None, None)  # type: ignore[arg-type]
                return json.dumps({"content": base64.b64encode(served).decode()}).encode()
        raise AssertionError(f"unexpected request {url}")

    return get


def _pinned(module: ModuleType) -> bytes:
    return (module._ROOT / "src/livepeer_builder/contracts/discovery.schema.json").read_bytes()


def test_a_tag_that_predates_the_pin_is_not_compared(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _watcher()
    get = _github(module, tag_contains_pin=False, schema_at={_MAIN: _pinned(module), _TAG: 404})
    monkeypatch.setattr(module, "_get", get)
    assert module.main() == 0
    report = capsys.readouterr().out
    assert f"latest tag {_TAG} predates the pin; compared main only" in report
    assert "discovery schema matches" in report


def test_a_tag_after_the_pin_with_the_same_schema_matches(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _watcher()
    get = _github(module, tag_contains_pin=True, schema_at={_MAIN: _pinned(module), _TAG: _pinned(module)})
    monkeypatch.setattr(module, "_get", get)
    assert module.main() == 0
    assert "predates" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("ref", "served", "line"),
    [
        (_TAG, b"{}", f"differs at latest tag {_TAG}"),
        (_TAG, 404, f"latest tag {_TAG}: HTTP 404, schema not on this ref"),
        (_MAIN, b"{}", f"differs at main {_MAIN}"),
        (_MAIN, 404, f"main {_MAIN}: HTTP 404, schema not on this ref"),
    ],
)
def test_a_changed_or_missing_schema_on_a_compared_ref_is_drift(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ref: str,
    served: bytes | int,
    line: str,
) -> None:
    module = _watcher()
    schema_at: dict[str, bytes | int] = {_MAIN: _pinned(module), _TAG: _pinned(module)}
    schema_at[ref] = served
    monkeypatch.setattr(module, "_get", _github(module, tag_contains_pin=True, schema_at=schema_at))
    assert module.main() == 2
    assert line in capsys.readouterr().out


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_an_http_error_on_the_schema_is_not_drift(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    module = _watcher()
    get = _github(module, tag_contains_pin=True, schema_at={_MAIN: status, _TAG: _pinned(module)})
    monkeypatch.setattr(module, "_get", get)
    with pytest.raises(urllib.error.HTTPError):
        module.main()


def test_an_http_error_on_the_tag_comparison_is_not_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _watcher()
    get = _github(
        module,
        tag_contains_pin=True,
        schema_at={_MAIN: _pinned(module), _TAG: _pinned(module)},
        compare_error=502,
    )
    monkeypatch.setattr(module, "_get", get)
    with pytest.raises(urllib.error.HTTPError):
        module.main()


def test_a_network_failure_is_not_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _watcher()

    def get(url: str) -> bytes:
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(module, "_get", get)
    with pytest.raises(urllib.error.URLError):
        module.main()
