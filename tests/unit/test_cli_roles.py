"""#169: ``lpb payments check`` and ``lpb smoke`` for each role (D84, D87).

``lpb payments check`` prints the role and probes what that role needs, with GET requests only:

- ``both``: the signer and Batteries, and Batteries with the grants credential when one is set;
- ``work``: the payment clearinghouse (its tenant's grant and usage, and the runner list when that is
  its discovery address), and the clearinghouse's published signer at ``LPB_SIGNER_URL``;
- ``clearinghouse``: the signer and Batteries, and Batteries with the grants credential always: a
  clearinghouse cannot onboard a tenant without it, so a missing one is a failed probe, not a skipped one.

Settings that contradict the role are refused before anything is sent (exit 2).

``lpb smoke`` builds, starts and stops an engine in the role the environment names, on a throwaway
database, and says which role it was.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from livepeer_builder import cli, generate_secret_key
from tests.unit.test_payments_check import (
    BATTERIES_TOKEN,
    GRANTS_TOKEN,
    TENANT_KEY,
    Recorder,
    clearinghouse,
    probes,
    run,
    self_hosted,
    server,  # noqa: F401  # the fixture
)

A_CLEARINGHOUSE = {"LPB_ROLE": "clearinghouse", "LPB_HTTP_PRIVATE_PORT": "8081"}

# -- lpb payments check ---------------------------------------------------------------------------------


def test_the_check_says_the_both_role_and_probes_the_signer_and_batteries(
    server: tuple[str, Recorder],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url, recorder = server
    self_hosted(monkeypatch, url, recorder)
    code, report, _ = run(capsys)
    assert code == 0 and (report["role"], report["configuration"]) == ("both", "self_hosted")
    assert list(probes(report)) == ["signer", "batteries", "batteries_grants"]


def test_the_check_says_the_work_role_and_probes_the_clearinghouse(
    server: tuple[str, Recorder],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url, recorder = server
    clearinghouse(monkeypatch, url, recorder)
    code, report, _ = run(capsys)
    assert code == 0 and (report["role"], report["configuration"]) == ("work", "clearinghouse")
    assert list(probes(report)) == [
        "clearinghouse_grant",
        "clearinghouse_usage",
        "signer",
        "clearinghouse_runner_list",
    ]
    assert recorder.methods == {"GET"}
    # nothing of Batteries' own is asked: the engine holds no address of it
    assert not [target for target in recorder.targets if target.startswith(("/v1/usage", "/v1/grants"))]


def test_the_check_says_the_clearinghouse_role_and_probes_its_signer_and_batteries(
    server: tuple[str, Recorder],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url, recorder = server
    self_hosted(monkeypatch, url, recorder, **A_CLEARINGHOUSE)
    code, report, _ = run(capsys)
    assert code == 0 and report["ok"] is True
    assert (report["role"], report["configuration"]) == ("clearinghouse", "self_hosted")
    assert list(probes(report)) == ["signer", "batteries", "batteries_grants"]
    assert recorder.methods == {"GET"}
    assert recorder.targets == ["/sign-orchestrator-info", "/v1/usage?limit=1", "/v1/grants?limit=1"]
    _, engine, grants = (headers for _, _, headers in recorder.seen)
    assert engine["livepeer-clearinghouse-token"] == BATTERIES_TOKEN
    assert grants["livepeer-clearinghouse-token"] == GRANTS_TOKEN


def test_a_clearinghouse_without_the_grants_credential_fails_the_check(
    server: tuple[str, Recorder],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """In the ``both`` role the grants credential is optional and its probe is left out. A clearinghouse
    needs it to onboard a tenant, so there the probe is made and says what is not set. Nothing is sent
    for it."""
    url, recorder = server
    self_hosted(monkeypatch, url, recorder, **A_CLEARINGHOUSE)
    monkeypatch.delenv("LPB_BATTERIES_GRANTS_TOKEN")
    code, report, _ = run(capsys)
    grants = probes(report)["batteries_grants"]
    assert code == 1 and report["ok"] is False and report["role"] == "clearinghouse"
    assert (grants["outcome"], grants["setting"], grants["request"]) == (
        "not_configured",
        "LPB_BATTERIES_GRANTS_TOKEN",
        None,
    )
    assert recorder.methods == {"GET"}
    assert recorder.targets == ["/sign-orchestrator-info", "/v1/usage?limit=1"]


def test_a_work_engine_with_no_signer_address_is_told_its_jobs_run_unpaid(
    server: tuple[str, Recorder],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No start-up rule refuses an engine in its ``work`` role without ``LPB_SIGNER_URL``: it runs its
    jobs unpaid, as any engine without a signer. The check is where that is said plainly: the probe
    fails, names the setting and says what to set it to. The clearinghouse is still probed, and the
    signer is asked nowhere, least of all at the clearinghouse's address."""
    url, recorder = server
    clearinghouse(monkeypatch, url, recorder)
    monkeypatch.delenv("LPB_SIGNER_URL")
    code, report, _ = run(capsys)
    signer = probes(report)["signer"]
    assert code == 1 and report["ok"] is False and report["role"] == "work"
    assert (signer["outcome"], signer["setting"], signer["request"]) == (
        "not_configured",
        "LPB_SIGNER_URL",
        None,
    )
    assert signer["detail"] == (
        "LPB_SIGNER_URL is not set: this engine runs its jobs unpaid. Set it to the address of the "
        "signer its payment clearinghouse publishes"
    )
    assert [probes(report)[name]["outcome"] for name in ("clearinghouse_grant", "clearinghouse_usage")] == [
        "ok",
        "ok",
    ]
    assert recorder.methods == {"GET"} and "/sign-orchestrator-info" not in recorder.targets


def test_a_work_engine_whose_signer_does_not_answer_fails_the_check(
    server: tuple[str, Recorder],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url, recorder = server
    clearinghouse(monkeypatch, url, recorder, LPB_SIGNER_URL="http://127.0.0.1:9")
    code, report, _ = run(capsys)
    signer = probes(report)["signer"]
    assert code == 1 and report["role"] == "work"
    assert (signer["outcome"], signer["detail"]) == ("unavailable", "the signer did not answer")
    assert recorder.methods == {"GET"} and "/sign-orchestrator-info" not in recorder.targets


def test_a_clearinghouse_with_no_signer_address_fails_the_check_though_it_signs_nothing(
    server: tuple[str, Recorder],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """It starts without one: it signs nothing. It runs the signer its tenants' engines sign at, and its
    wallet report reads the signer's readiness at this address, so the check says what is missing."""
    url, recorder = server
    self_hosted(monkeypatch, url, recorder, **A_CLEARINGHOUSE)
    monkeypatch.delenv("LPB_SIGNER_URL")
    code, report, _ = run(capsys)
    signer = probes(report)["signer"]
    assert code == 1 and report["role"] == "clearinghouse"
    assert (signer["outcome"], signer["setting"], signer["request"]) == (
        "not_configured",
        "LPB_SIGNER_URL",
        None,
    )
    assert signer["detail"] == (
        "LPB_SIGNER_URL is not set: the wallet report cannot say whether the signer answers. Set it to "
        "the address this engine reaches the signer at"
    )
    assert recorder.methods == {"GET"}
    assert recorder.targets == ["/v1/usage?limit=1", "/v1/grants?limit=1"]


def test_a_both_engine_with_no_signer_address_is_told_what_is_not_set(
    server: tuple[str, Recorder],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url, recorder = server
    self_hosted(monkeypatch, url, recorder)
    monkeypatch.delenv("LPB_SIGNER_URL")
    code, report, _ = run(capsys)
    signer = probes(report)["signer"]
    assert code == 1 and report["role"] == "both"
    assert (signer["outcome"], signer["detail"]) == (
        "not_configured",
        "LPB_SIGNER_URL is not set: this engine runs its jobs unpaid",
    )


@pytest.mark.parametrize(
    ("env", "named"),
    [
        ({"LPB_ROLE": "clearinghouse"}, ("LPB_ROLE", "LPB_HTTP_PRIVATE_PORT")),
        ({"LPB_ROLE": "work"}, ("LPB_ROLE", "LPB_CLEARINGHOUSE_URL")),
        ({"LPB_ROLE": "enterprise"}, ("role",)),
    ],
)
def test_settings_that_contradict_the_role_are_refused_and_nothing_is_sent(
    server: tuple[str, Recorder],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    env: dict[str, str],
    named: tuple[str, ...],
) -> None:
    url, recorder = server
    self_hosted(monkeypatch, url, recorder, **env)
    code, report, printed = run(capsys)
    assert code == 2 and report == {} and recorder.seen == []
    assert all(name in printed for name in named)
    assert url not in printed  # no value is echoed


def test_a_clearinghouse_named_in_the_both_role_is_refused_and_nothing_is_sent(
    server: tuple[str, Recorder],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url, recorder = server
    clearinghouse(monkeypatch, url, recorder, LPB_ROLE="both")
    code, _, printed = run(capsys)
    assert code == 2 and recorder.seen == []
    assert "LPB_ROLE" in printed and "LPB_CLEARINGHOUSE_URL" in printed
    assert url not in printed and TENANT_KEY not in printed


# -- lpb smoke --------------------------------------------------------------------------------------------


@pytest.fixture
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Nothing here is called: every address is a loopback port that refuses a connection."""
    monkeypatch.setenv("LPB_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'never-opened.db'}")
    monkeypatch.setenv("LPB_LOG_JSON", "false")
    monkeypatch.setenv("LPB_SECRET_KEY", generate_secret_key())
    monkeypatch.chdir(tmp_path)
    return monkeypatch


def smoke(capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, object]]:
    code = cli.main(["smoke"])
    return code, dict(json.loads(capsys.readouterr().out))


def test_smoke_in_the_both_role_is_what_it_was(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, report = smoke(capsys)
    assert code == 0 and report["ok"] is True
    assert report["role"] == "both" and report["health"]["role"] == "both"  # type: ignore[index]


def test_smoke_builds_and_starts_an_engine_in_its_work_role(
    environment: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environment.setenv("LPB_ROLE", "work")
    environment.setenv("LPB_CLEARINGHOUSE_URL", "http://127.0.0.1:9")
    environment.setenv("LPB_CLEARINGHOUSE_TOKEN", TENANT_KEY)
    environment.setenv("LPB_CLEARINGHOUSE_TENANT_ID", "grant_7")
    code, report = smoke(capsys)
    assert code == 0 and report["ok"] is True and report["role"] == "work"
    assert TENANT_KEY not in json.dumps(report)
    assert not (tmp_path / "never-opened.db").exists()


def test_smoke_builds_and_starts_an_engine_in_its_clearinghouse_role(
    environment: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in (A_CLEARINGHOUSE | _SELF_HOSTED).items():
        environment.setenv(name, value)
    code, report = smoke(capsys)
    assert code == 0 and report["ok"] is True and report["role"] == "clearinghouse"
    health = report["health"]
    assert health["cost_sync_at"] is None and health["running_jobs"] == 0  # type: ignore[index]
    assert not (tmp_path / "never-opened.db").exists()


def test_smoke_in_a_role_its_settings_contradict_does_not_start(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    environment.setenv("LPB_ROLE", "clearinghouse")  # no private listener
    with pytest.raises(ValidationError, match="LPB_HTTP_PRIVATE_PORT"):
        cli.main(["smoke"])
    environment.setenv("LPB_HTTP_PRIVATE_PORT", "8081")  # and no Batteries
    with pytest.raises(ValueError, match="LPB_BATTERIES_TOKEN"):
        cli.main(["smoke"])
    capsys.readouterr()


_SELF_HOSTED = {
    "LPB_SIGNER_URL": "http://127.0.0.1:9",
    "LPB_BATTERIES_URL": "http://127.0.0.1:9",
    "LPB_BATTERIES_TOKEN": BATTERIES_TOKEN,
    "LPB_BATTERIES_GRANTS_TOKEN": GRANTS_TOKEN,
}
