"""#168: ``lpb payments check``, a read-only check of what the engine's payments need (D87).

A self-hosted engine probes the signer and Batteries. An engine that uses a payment clearinghouse probes
the clearinghouse (who it is there, its grant, its usage and the runner list) and the clearinghouse's
signer, at ``LPB_SIGNER_URL``, exactly as a self-hosted engine probes its own: the engine signs there
itself, and the clearinghouse's engine serves no signer path. **It never sends a request that is not a
GET.** Three things hold that here: every request the command makes is recorded, by the servers it
reaches and by its own HTTP client; the module that makes the requests is read, and has no other call in
it; and the same is checked when each probe fails.

Under that rule the signer cannot be asked who it is, since its identity call is a ``POST``. The command
asks ``GET /sign-orchestrator-info``, which a signer answers 405 with ``Allow: POST``: an HTTP server is
at the signer's address and has the signer's identity route. It signs nothing and proves no more.
"""

import ast
import asyncio
import json
import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from livepeer_builder import EngineSettings, cli
from livepeer_builder.adapters import payment_probes
from livepeer_builder.adapters.payment_probes import probe_payments
from livepeer_builder.contracts import PaymentCheck
from livepeer_builder.runtime import check_payments

BATTERIES_TOKEN = "bt_engine-credential-not-to-be-shown"
GRANTS_TOKEN = "bt_grants-credential-not-to-be-shown"
TENANT_KEY = "lpk_tenant-admin-key-not-to-be-shown"
SECRETS = (BATTERIES_TOKEN, GRANTS_TOKEN, TENANT_KEY)
GRANT = {
    "tenant_id": "grant_7",
    "name": "ChatLPT",
    "status": "active",
    "total": {"amount": "100", "currency": "USD"},
    "unallocated": {"amount": "60", "currency": "USD"},
    "spent": {"amount": "12.5", "currency": "USD"},
    "allocated_unspent": {"amount": "27.5", "currency": "USD"},
}
USAGE = {"items": [], "set_aside": [], "next_cursor": ""}
Answer = tuple[int, Any]


class Recorder:
    """What the servers were sent, and what each path answers. Every method is recorded."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, dict[str, str]]] = []
        self.answers: dict[str, Answer] = {}

    def answer(self, method: str, target: str, headers: dict[str, str]) -> Answer:
        self.seen.append((method, target, headers))
        path = target.split("?")[0]
        if path == "/sign-orchestrator-info":
            if "signer" in self.answers:
                return self.answers["signer"]
            # livepeer/node at eee09c4 registers "POST /sign-orchestrator-info": any other method is 405
            return (405, None) if method != "POST" else (200, {"address": "0x00", "signature": "0x00"})
        return self.answers.get(path, (404, {"error": "no such route"}))

    @property
    def methods(self) -> set[str]:
        return {method for method, _, _ in self.seen}

    @property
    def targets(self) -> list[str]:
        return [target for _, target, _ in self.seen]


@pytest.fixture
def server() -> Iterator[tuple[str, Recorder]]:
    recorder = Recorder()

    class Handler(BaseHTTPRequestHandler):
        def _any(self) -> None:
            headers = {name.lower(): value for name, value in self.headers.items()}
            status, body = recorder.answer(self.command, self.path, headers)
            payload = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            if status == 405:
                self.send_header("Allow", "POST")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _any

        def log_message(self, *_: object) -> None:  # quiet
            return None

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", recorder
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()


def self_hosted(monkeypatch: pytest.MonkeyPatch, url: str, recorder: Recorder, **env: str) -> None:
    recorder.answers |= {"/v1/usage": (200, {"items": []}), "/v1/grants": (200, {"items": []})}
    values = {
        "LPB_SIGNER_URL": url + "/a/path/the/sdk/drops",
        "LPB_BATTERIES_URL": url,
        "LPB_BATTERIES_TOKEN": BATTERIES_TOKEN,
        "LPB_BATTERIES_GRANTS_TOKEN": GRANTS_TOKEN,
        "LPB_DISCOVERY_URL": "http://127.0.0.1:9/discovery",
    } | env
    for name in [key for key in os.environ if key.startswith("LPB_")]:
        monkeypatch.delenv(name)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def clearinghouse(monkeypatch: pytest.MonkeyPatch, url: str, recorder: Recorder, **env: str) -> None:
    tenant = "/v1/clearinghouse/tenants/grant_7"
    recorder.answers |= {
        tenant + "/grant": (200, GRANT),
        tenant + "/usage": (200, USAGE),
        "/v1/clearinghouse/discovery": (200, []),
    }
    values = {
        "LPB_ROLE": "work",
        "LPB_CLEARINGHOUSE_URL": url,
        "LPB_CLEARINGHOUSE_TOKEN": TENANT_KEY,
        "LPB_CLEARINGHOUSE_TENANT_ID": "grant_7",
        # the clearinghouse's published signer. One server plays both here; they are two addresses
        "LPB_SIGNER_URL": url + "/a/path/the/sdk/drops",
    } | env
    for name in [key for key in os.environ if key.startswith("LPB_")]:
        monkeypatch.delenv(name)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def run(capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any], str]:
    code = cli.main(["payments", "check"])
    captured = capsys.readouterr()
    printed = captured.out + captured.err
    for secret in SECRETS:
        assert secret not in printed, "a credential was printed"
    return code, (json.loads(captured.out) if captured.out.strip() else {}), printed


def probes(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {probe["name"]: probe for probe in report["probes"]}


# -- a self-hosted engine -------------------------------------------------------------------------------


def test_a_self_hosted_engine_probes_the_signer_and_batteries_with_get_requests_only(
    server: tuple[str, Recorder], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    url, recorder = server
    self_hosted(monkeypatch, url, recorder)

    code, report, _ = run(capsys)

    assert code == 0 and report["ok"] is True and report["configuration"] == "self_hosted"
    assert recorder.methods == {"GET"}
    assert recorder.targets == ["/sign-orchestrator-info", "/v1/usage?limit=1", "/v1/grants?limit=1"]
    by_name = probes(report)
    assert list(by_name) == ["signer", "batteries", "batteries_grants"]
    assert all(probe["outcome"] == "ok" for probe in by_name.values())
    assert by_name["signer"]["status_code"] == 405 and by_name["signer"]["setting"] == "LPB_SIGNER_URL"
    assert by_name["signer"]["request"] == "GET /sign-orchestrator-info"
    # each Batteries credential goes to Batteries, under its own header, and the signer is sent none
    signer, engine, grants = (headers for _, _, headers in recorder.seen)
    assert "authorization" not in signer and "livepeer-clearinghouse-token" not in signer
    assert engine["livepeer-clearinghouse-token"] == BATTERIES_TOKEN
    assert grants["livepeer-clearinghouse-token"] == GRANTS_TOKEN


def test_without_a_grants_credential_the_grants_are_not_probed(
    server: tuple[str, Recorder], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    url, recorder = server
    self_hosted(monkeypatch, url, recorder)
    monkeypatch.delenv("LPB_BATTERIES_GRANTS_TOKEN")
    code, report, _ = run(capsys)
    assert code == 0 and list(probes(report)) == ["signer", "batteries"]


@pytest.mark.parametrize(
    ("path", "answer", "probe", "outcome"),
    [
        ("/v1/usage", (401, {"error": "bad token"}), "batteries", "refused"),
        ("/v1/usage", (403, {"error": "no usage.read"}), "batteries", "refused"),
        ("/v1/grants", (403, {"error": "no grants.read"}), "batteries_grants", "refused"),
        ("/v1/usage", (503, {"error": "starting"}), "batteries", "unavailable"),
        ("/v1/usage", (200, [1, 2]), "batteries", "unexpected"),
        ("/v1/usage", (404, {"error": "not found"}), "batteries", "refused"),
        ("signer", (404, {"error": "not found"}), "signer", "unexpected"),
        ("signer", (200, {"hello": "world"}), "signer", "unexpected"),
        ("signer", (503, {"error": "signer busy"}), "signer", "unavailable"),
    ],
)
def test_a_probe_that_is_refused_or_fails_exits_1_and_still_sends_only_get_requests(
    server: tuple[str, Recorder],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    path: str,
    answer: Answer,
    probe: str,
    outcome: str,
) -> None:
    url, recorder = server
    self_hosted(monkeypatch, url, recorder)
    recorder.answers[path] = answer

    code, report, printed = run(capsys)

    assert code == 1 and report["ok"] is False
    assert probes(report)[probe]["outcome"] == outcome
    assert recorder.methods == {"GET"} and len(recorder.seen) == 3  # every probe still ran, once
    for words in ("bad token", "no usage.read", "no grants.read", "starting", "hello", "signer busy"):
        assert words not in printed  # what the other side said is not printed


def test_a_signer_or_batteries_that_is_not_there_is_unavailable(
    server: tuple[str, Recorder], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    url, recorder = server
    self_hosted(
        monkeypatch,
        url,
        recorder,
        LPB_SIGNER_URL="http://127.0.0.1:9",
        LPB_BATTERIES_URL="http://127.0.0.1:9",
    )
    code, report, _ = run(capsys)
    assert code == 1 and recorder.seen == []
    assert {probe["outcome"] for probe in probes(report).values()} == {"unavailable"}


async def test_a_grants_credential_with_no_batteries_address_is_not_configured() -> None:
    report = await probe_payments(settings(batteries_grants_token=GRANTS_TOKEN))
    by_name = {probe.name: probe for probe in report.probes}
    assert by_name["batteries_grants"].outcome == "not_configured"
    assert by_name["batteries_grants"].setting == "LPB_BATTERIES_URL" and not report.ok


async def test_a_batteries_address_that_cannot_be_read_is_unavailable_and_no_error() -> None:
    """``LPB_BATTERIES_URL`` is not checked when the settings load, so the check meets it as it is."""
    report = await probe_payments(settings(batteries_url="http://[::1", batteries_token=BATTERIES_TOKEN))
    by_name = {probe.name: probe for probe in report.probes}
    assert by_name["batteries"].outcome == "unavailable" and not report.ok


async def test_an_answer_that_is_no_json_is_unexpected() -> None:
    def page(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>a proxy's page</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(page)) as client:
        report = await probe_payments(settings(**CLEARINGHOUSE), client=client)
    assert [probe.outcome for probe in report.probes] == ["unexpected"] * 4 and report.tenant is None
    assert "proxy" not in report.model_dump_json()


def test_an_engine_with_no_payment_settings_has_nothing_to_pay_with(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in [key for key in os.environ if key.startswith("LPB_")]:
        monkeypatch.delenv(name)
    code, report, _ = run(capsys)
    assert code == 1 and report["configuration"] == "self_hosted"
    by_name = probes(report)
    assert [by_name[name]["outcome"] for name in ("signer", "batteries")] == ["not_configured"] * 2
    assert by_name["signer"]["request"] is None and "LPB_SIGNER_URL" in by_name["signer"]["detail"]


# -- an engine that uses a clearinghouse ----------------------------------------------------------------


def test_an_engine_that_uses_a_clearinghouse_probes_it_and_its_signer_with_get_requests_only(
    server: tuple[str, Recorder], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    url, recorder = server
    clearinghouse(monkeypatch, url, recorder)

    code, report, _ = run(capsys)

    assert code == 0 and report["ok"] is True and report["configuration"] == "clearinghouse"
    assert recorder.methods == {"GET"}
    assert recorder.targets == [
        "/v1/clearinghouse/tenants/grant_7/grant",
        "/v1/clearinghouse/tenants/grant_7/usage?limit=1",
        "/sign-orchestrator-info",
        "/v1/clearinghouse/discovery",
    ]
    assert list(probes(report)) == [
        "clearinghouse_grant",
        "clearinghouse_usage",
        "signer",
        "clearinghouse_runner_list",
    ]
    # the tenant admin key goes to the tenant's routes and the runner list, and never to the signer
    for _, target, headers in recorder.seen:
        if target == "/sign-orchestrator-info":
            assert "authorization" not in headers
        else:
            assert headers["authorization"] == f"Bearer {TENANT_KEY}"
    # the signer is probed as a self-hosted engine probes its own: the same GET, at LPB_SIGNER_URL
    signer = probes(report)["signer"]
    assert (signer["outcome"], signer["status_code"], signer["request"]) == (
        "ok",
        405,
        "GET /sign-orchestrator-info",
    )
    assert signer["setting"] == "LPB_SIGNER_URL"
    # who the engine is there, and its grant
    assert report["tenant"] == GRANT


def test_a_runner_list_of_the_enterprise_own_is_not_probed_and_gets_no_key(
    server: tuple[str, Recorder], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    url, recorder = server
    clearinghouse(monkeypatch, url, recorder, LPB_DISCOVERY_URL="http://127.0.0.1:9/discovery")
    code, report, _ = run(capsys)
    assert code == 0
    assert list(probes(report)) == ["clearinghouse_grant", "clearinghouse_usage", "signer"]
    assert len(recorder.seen) == 3


def test_the_signer_of_a_clearinghouse_is_probed_at_its_own_address_and_never_at_the_clearinghouse(
    server: tuple[str, Recorder], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The clearinghouse's engine serves no signer path. With the signer's address naming a port
    nothing listens on, the signer probe fails there, and the clearinghouse is never asked for it."""
    url, recorder = server
    clearinghouse(monkeypatch, url, recorder, LPB_SIGNER_URL="http://127.0.0.1:9")
    code, report, _ = run(capsys)
    assert code == 1 and probes(report)["signer"]["outcome"] == "unavailable"
    assert "/sign-orchestrator-info" not in recorder.targets and len(recorder.seen) == 3
    assert [probes(report)[name]["outcome"] for name in ("clearinghouse_grant", "clearinghouse_usage")] == [
        "ok",
        "ok",
    ]


def test_an_engine_that_uses_a_clearinghouse_and_names_no_signer_cannot_pay(
    server: tuple[str, Recorder], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """As for a self-hosted engine: ``LPB_SIGNER_URL`` unset is an engine that does not pay, and the
    check says which setting is missing. The clearinghouse's address stands in for nothing."""
    url, recorder = server
    clearinghouse(monkeypatch, url, recorder)
    monkeypatch.delenv("LPB_SIGNER_URL")
    code, report, _ = run(capsys)
    signer = probes(report)["signer"]
    assert code == 1 and report["ok"] is False
    assert (signer["outcome"], signer["request"], signer["setting"]) == (
        "not_configured",
        None,
        "LPB_SIGNER_URL",
    )
    assert "/sign-orchestrator-info" not in recorder.targets and recorder.methods == {"GET"}


@pytest.mark.parametrize(
    ("ending", "answer", "probe", "outcome", "said"),
    [
        (
            "/grant",
            (401, {"error": "unknown key"}),
            "clearinghouse_grant",
            "refused",
            "LPB_CLEARINGHOUSE_TOKEN",
        ),
        ("/grant", (403, {"error": "another tenant"}), "clearinghouse_grant", "refused", "TENANT_ID"),
        ("/grant", (404, {"error": "nope"}), "clearinghouse_grant", "refused", "LPB_CLEARINGHOUSE_URL"),
        ("/grant", (503, {"error": "down"}), "clearinghouse_grant", "unavailable", "HTTP 503"),
        ("/grant", (200, GRANT | {"tenant_id": "grant_8"}), "clearinghouse_grant", "unexpected", "another"),
        ("/grant", (200, {"not": "a grant"}), "clearinghouse_grant", "unexpected", "could not be read"),
        ("/usage", (403, {"error": "another tenant"}), "clearinghouse_usage", "refused", "TENANT_ID"),
        ("/discovery", (503, {"error": "no list"}), "clearinghouse_runner_list", "unavailable", "503"),
        ("/discovery", (307, None), "clearinghouse_runner_list", "unexpected", "redirect"),
        # the signer, at its own address: what a self-hosted engine's check says of its own
        ("signer", (404, {"error": "not found"}), "signer", "unexpected", "signer's identity route"),
        ("signer", (200, {"hello": "world"}), "signer", "unexpected", "signer's identity route"),
        ("signer", (503, {"error": "down"}), "signer", "unavailable", "HTTP 503"),
    ],
)
def test_a_clearinghouse_probe_that_is_refused_exits_1_and_says_which_setting_to_look_at(
    server: tuple[str, Recorder],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ending: str,
    answer: Answer,
    probe: str,
    outcome: str,
    said: str,
) -> None:
    url, recorder = server
    clearinghouse(monkeypatch, url, recorder)
    [path] = [known for known in recorder.answers if known.endswith(ending)] or [ending]
    recorder.answers[path] = answer

    code, report, _ = run(capsys)

    found = probes(report)[probe]
    assert code == 1 and report["ok"] is False
    assert found["outcome"] == outcome and said in found["detail"], found
    assert recorder.methods == {"GET"} and len(recorder.seen) == 4


def test_a_clearinghouse_address_with_a_path_exits_2_and_sends_nothing(
    server: tuple[str, Recorder], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The address names a scheme, a host and a port: the clearinghouse's routes are called at its root."""
    url, recorder = server
    clearinghouse(monkeypatch, url, recorder, LPB_CLEARINGHOUSE_URL=url + "/pay")
    code, report, printed = run(capsys)
    assert code == 2 and report == {} and recorder.seen == []
    assert "LPB_CLEARINGHOUSE_URL" in printed and "no path" in printed and url not in printed


def test_settings_that_do_not_fit_together_exit_2_and_send_nothing(
    server: tuple[str, Recorder], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    url, recorder = server
    clearinghouse(monkeypatch, url, recorder, LPB_BATTERIES_URL=url)
    code, report, printed = run(capsys)
    assert code == 2 and report == {} and recorder.seen == []
    assert "LPB_BATTERIES_URL" in printed and "LPB_CLEARINGHOUSE_URL" in printed


# -- the client's own record, and the module itself -----------------------------------------------------


def settings(**values: object) -> EngineSettings:
    return EngineSettings.model_validate({"discovery_url": "http://127.0.0.1:9/discovery", **values})


SELF_HOSTED = {
    "signer_url": "https://signer.test:8935",
    "batteries_url": "https://batteries.test",
    "batteries_token": BATTERIES_TOKEN,
    "batteries_grants_token": GRANTS_TOKEN,
}
CLEARINGHOUSE = {
    "role": "work",
    "clearinghouse_url": "https://clearinghouse.test",
    "clearinghouse_token": TENANT_KEY,
    "clearinghouse_tenant_id": "grant_7",
    "discovery_url": "https://clearinghouse.test/v1/clearinghouse/discovery",
    "signer_url": "https://signer.test:8935",  # the clearinghouse's published signer
}
ANSWERS = [
    lambda request: httpx.Response(200, json={}),
    lambda request: httpx.Response(401, json={"error": "no"}),
    lambda request: httpx.Response(500, text="boom"),
    lambda request: httpx.Response(302, headers={"location": "https://elsewhere.test/"}),
    lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused")),
    lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow")),
]


@pytest.mark.parametrize("configuration", [SELF_HOSTED, CLEARINGHOUSE], ids=["self_hosted", "clearinghouse"])
@pytest.mark.parametrize("answer", ANSWERS, ids=["200", "401", "500", "redirect", "refused", "timeout"])
async def test_every_request_the_check_makes_is_a_get_to_the_address_it_was_given(
    configuration: dict[str, object], answer: Any
) -> None:
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return answer(request)  # type: ignore[no-any-return]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        report = await probe_payments(settings(**configuration), client=client)

    assert isinstance(report, PaymentCheck) and len(sent) == (
        4 if "clearinghouse_url" in configuration else 3
    )
    assert {request.method for request in sent} == {"GET"}
    assert all(request.content == b"" for request in sent)  # and no request carries a body
    hosts = {request.url.host for request in sent}
    assert hosts <= {"signer.test", "batteries.test", "clearinghouse.test"}  # no redirect is followed
    for secret in SECRETS:
        assert secret not in report.model_dump_json() and secret not in repr(report)
    for request in sent:  # a credential goes only to the service it belongs to
        if request.url.host == "signer.test" or request.url.path == "/sign-orchestrator-info":
            assert "authorization" not in request.headers
            assert "livepeer-clearinghouse-token" not in request.headers
    # the signer is asked at its own address, whoever runs it, and nowhere else
    assert [r.url.host for r in sent if r.url.path == "/sign-orchestrator-info"] == ["signer.test"]


def test_the_module_that_probes_calls_nothing_but_get() -> None:
    """Read from the source: the probes go through one function, which calls ``client.get`` and no other
    method of the HTTP client, and the module names no other HTTP method."""
    source = Path(payment_probes.__file__).read_text()
    called = {
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    sending = {
        "post",
        "put",
        "patch",
        "delete",
        "request",
        "send",
        "stream",
        "build_request",
        "head",
        "options",
    }
    assert "get" in called and not called & sending, sorted(called & sending)
    words = {node.value for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Constant)}
    assert not {"POST", "PUT", "PATCH", "DELETE"} & {word for word in words if isinstance(word, str)}


async def test_the_check_is_offered_to_an_embedding_app_too() -> None:
    report = await check_payments(settings())
    assert report.ok is False and report.configuration == "self_hosted"
    assert asyncio.iscoroutinefunction(check_payments)
