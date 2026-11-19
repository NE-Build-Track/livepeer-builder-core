"""#168: the settings of an engine that uses a payment clearinghouse (D84).

An enterprise's engine holds the clearinghouse's address, its tenant admin key and its tenant's id, and
nothing of Batteries or the signer. The three are set together or not at all, and a half setting, or one
mixed with the settings of self-hosted payments, is refused when the engine starts: an engine that
allotted at a clearinghouse and paid from a signer of its own would pay for nothing it accounts for.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from livepeer_builder import EngineSettings

URL = "https://clearinghouse.example"
TOKEN = "lpk_tenant-admin-key-not-to-be-shown"
THREE = {
    "LPB_CLEARINGHOUSE_URL": URL,
    "LPB_CLEARINGHOUSE_TOKEN": TOKEN,
    "LPB_CLEARINGHOUSE_TENANT_ID": "grant_7",
}
DISCOVERY = {"LPB_DISCOVERY_URL": "http://discovery:8939/discovery"}


def refused(env: dict[str, str], **defaults: Any) -> str:
    with pytest.raises(ValidationError) as error:
        EngineSettings.from_env(env, defaults=defaults or None)
    return str(error.value)


def test_no_clearinghouse_setting_is_self_hosted_payments() -> None:
    settings = EngineSettings.from_env(DISCOVERY)
    assert settings.clearinghouse_url is None and settings.clearinghouse_token is None
    assert settings.clearinghouse_tenant_id is None
    assert settings.uses_clearinghouse is False
    assert settings.clearinghouse_runner_list_token is None


def test_the_three_settings_select_a_clearinghouse() -> None:
    settings = EngineSettings.from_env(THREE | DISCOVERY)
    assert settings.uses_clearinghouse is True
    assert settings.clearinghouse_url == URL
    assert settings.clearinghouse_tenant_id == "grant_7"
    assert settings.clearinghouse_token is not None
    assert settings.clearinghouse_token.get_secret_value() == TOKEN
    assert settings.clearinghouse_timeout_s == 30.0


@pytest.mark.parametrize("missing", sorted(THREE))
def test_a_half_setting_is_refused_and_names_what_is_missing(missing: str) -> None:
    env = {name: value for name, value in THREE.items() if name != missing} | DISCOVERY
    message = refused(env)
    assert missing in message and "set together" in message
    assert TOKEN not in message


@pytest.mark.parametrize(
    "setting",
    ["LPB_BATTERIES_URL", "LPB_BATTERIES_TOKEN", "LPB_BATTERIES_GRANTS_TOKEN", "LPB_SIGNER_URL"],
)
def test_a_clearinghouse_mixed_with_self_hosted_payments_is_refused(setting: str) -> None:
    """An engine that uses a clearinghouse knows nothing of Batteries or the signer. With both, an actor
    would be allotted at one and paid for from the other."""
    value = "http://signer:8935" if setting.endswith("_URL") else "bt_secret-not-to-be-shown"
    message = refused(THREE | DISCOVERY | {setting: value})
    assert setting in message and "LPB_CLEARINGHOUSE_URL" in message
    assert value not in message and TOKEN not in message


def test_the_token_is_in_no_repr_and_no_dump() -> None:
    settings = EngineSettings.from_env(THREE | DISCOVERY)
    assert TOKEN not in repr(settings) and TOKEN not in str(settings)
    assert TOKEN not in settings.model_dump_json()


@pytest.mark.parametrize(
    "url",
    [
        "clearinghouse.example",  # no scheme
        "ftp://clearinghouse.example",
        "https://user:secret@clearinghouse.example",
        "https://clearinghouse.example?token=secret",
        "https://clearinghouse.example#secret",
        "https:///v1",
        "https://[::1",  # an address that cannot be read at all
        "https://clearing house.example",
    ],
)
def test_an_address_that_is_no_plain_http_address_is_refused_and_not_echoed(url: str) -> None:
    """The address is quoted in error text and logs, so it carries no userinfo, query or fragment: the
    credential is the tenant admin key, sent as a header."""
    message = refused(THREE | DISCOVERY | {"LPB_CLEARINGHOUSE_URL": url})
    assert "LPB_CLEARINGHOUSE_URL" in message
    assert "secret" not in message


def test_the_three_settings_may_be_stated_as_unset() -> None:
    settings = EngineSettings.model_validate(
        {
            "discovery_url": DISCOVERY["LPB_DISCOVERY_URL"],
            "clearinghouse_url": None,
            "clearinghouse_token": None,
        }
    )
    assert settings.uses_clearinghouse is False


def test_a_tenant_id_that_is_no_path_segment_is_refused() -> None:
    message = refused(THREE | DISCOVERY | {"LPB_CLEARINGHOUSE_TENANT_ID": "one/two"})
    assert "clearinghouse_tenant_id" in message


# -- the runner list (D87) ------------------------------------------------------------------------------


def test_the_clearinghouse_runner_list_is_the_default_discovery_address() -> None:
    """D87: the clearinghouse's runner list is the default for an enterprise's engine that uses one."""
    settings = EngineSettings.from_env(THREE)
    assert settings.discovery_url == URL + "/v1/clearinghouse/discovery"
    assert settings.clearinghouse_runner_list_token == TOKEN


@pytest.mark.parametrize(
    "more",
    [
        "/pay",  # a path: the gateway SDK drops it, so signing would go to the root while the rest did not
        "/v1/clearinghouse",
        "//",
        "/?",
    ],
)
def test_an_address_with_anything_after_the_host_and_port_is_refused(more: str) -> None:
    """The gateway SDK keeps the scheme, the host and the port of a signer's address and drops the rest
    (``livepeer_gateway.http._http_origin``). An engine signs at the clearinghouse through that SDK, so
    the address names an origin and nothing more: with a path, the allocation routes would be called
    under it and the two signer paths at the root."""
    given = "https://ch.internal.test:8443"
    message = refused(THREE | DISCOVERY | {"LPB_CLEARINGHOUSE_URL": given + more})
    assert "LPB_CLEARINGHOUSE_URL" in message and "no path" in message
    assert "ch.internal.test" not in message  # the value is not echoed


def test_a_trailing_slash_alone_names_no_path_and_is_taken_off() -> None:
    slashed = EngineSettings.from_env(THREE | {"LPB_CLEARINGHOUSE_URL": URL + "/"})
    assert slashed.clearinghouse_url == URL
    assert slashed.discovery_url == URL + "/v1/clearinghouse/discovery"
    assert slashed.clearinghouse_runner_list_token == TOKEN
    with_port = EngineSettings.from_env(THREE | {"LPB_CLEARINGHOUSE_URL": "http://10.0.0.5:8080/"})
    assert with_port.clearinghouse_url == "http://10.0.0.5:8080"


def test_an_enterprise_may_use_its_own_discovery_and_the_key_is_not_sent_there() -> None:
    settings = EngineSettings.from_env(THREE | DISCOVERY)
    assert settings.discovery_url == DISCOVERY["LPB_DISCOVERY_URL"]
    assert settings.clearinghouse_runner_list_token is None


@pytest.mark.parametrize(
    "elsewhere",
    [
        "https://clearinghouse.example.evil.test/v1/clearinghouse/discovery",  # another host
        "http://clearinghouse.example/v1/clearinghouse/discovery",  # the same host, without TLS
        "https://clearinghouse.example:8443/v1/clearinghouse/discovery",  # another port
        "https://clearinghouse.example/v1/clearinghouse/discovery/",  # another path
        "https://clearinghouse.example/discovery",
    ],
)
def test_the_key_goes_to_the_clearinghouse_runner_list_and_to_no_other_address(elsewhere: str) -> None:
    settings = EngineSettings.from_env(THREE | {"LPB_DISCOVERY_URL": elsewhere})
    assert settings.clearinghouse_runner_list_token is None


def test_an_engine_without_a_clearinghouse_still_needs_a_discovery_address() -> None:
    assert "discovery_url" in refused({})


def test_a_command_that_serves_nothing_still_reads_the_clearinghouse_runner_list_by_default() -> None:
    """``lpb`` commands that serve nothing give the discovery address a default of their own (#112, C23).
    For an engine that uses a clearinghouse, the clearinghouse's list comes first."""
    settings = EngineSettings.from_env(THREE, defaults={"discovery_url": "http://127.0.0.1:9/discovery"})
    assert settings.discovery_url == URL + "/v1/clearinghouse/discovery"
    alone = EngineSettings.from_env({}, defaults={"discovery_url": "http://127.0.0.1:9/discovery"})
    assert alone.discovery_url == "http://127.0.0.1:9/discovery"


# -- the operation ledger's stale window ----------------------------------------------------------------


def test_a_resume_never_starts_while_a_clearinghouse_call_may_be_in_flight() -> None:
    """As for Batteries (D57): a command is resumed only after a window four times one call's deadline."""
    message = refused(THREE | {"LPB_CLEARINGHOUSE_TIMEOUT_S": "40"})
    assert "LPB_OPERATION_STALE_S" in message and "LPB_CLEARINGHOUSE_TIMEOUT_S" in message
    slow = EngineSettings.from_env(
        THREE | {"LPB_CLEARINGHOUSE_TIMEOUT_S": "40", "LPB_OPERATION_STALE_S": "160"}
    )
    assert slow.clearinghouse_timeout_s == 40.0
