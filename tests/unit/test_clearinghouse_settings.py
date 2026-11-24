"""#168: the settings of an engine that uses a payment clearinghouse (D84).

An enterprise's engine holds the clearinghouse's address, its tenant admin key and its tenant's id, and
nothing of Batteries. The three are set together or not at all, and a half setting, or one mixed with a
Batteries setting, is refused when the engine starts: an engine that allotted at a clearinghouse and
read its usage from a Batteries of its own would account for nothing it paid.

``LPB_SIGNER_URL`` is set beside them, as on any engine that pays: it names the clearinghouse's signer,
where the engine signs itself with each actor's own key (D85 point 1, as amended). Without it the engine
does not pay, as today.
"""

import pytest
from pydantic import ValidationError

from livepeer_builder import EngineSettings
from tests.unit.refusal_forms import carried

URL = "https://clearinghouse.example"
TOKEN = "lpk_tenant-admin-key-not-to-be-shown"
THREE = {
    "LPB_CLEARINGHOUSE_URL": URL,
    "LPB_CLEARINGHOUSE_TOKEN": TOKEN,
    "LPB_CLEARINGHOUSE_TENANT_ID": "grant_7",
}
DISCOVERY = {"LPB_DISCOVERY_URL": "http://discovery:8939/discovery"}


def refused(env: dict[str, str]) -> str:
    with pytest.raises(ValidationError) as error:
        EngineSettings.from_env(env)
    assert carried(error.value, TOKEN) == [], "the tenant admin key is in no form of a refusal"
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
    assert settings.clearinghouse_timeout_s == 20.0


@pytest.mark.parametrize("missing", sorted(THREE))
def test_a_half_setting_is_refused_and_names_what_is_missing(missing: str) -> None:
    env = {name: value for name, value in THREE.items() if name != missing} | DISCOVERY
    message = refused(env)
    assert missing in message and "set together" in message
    assert TOKEN not in message


@pytest.mark.parametrize(
    "setting", ["LPB_BATTERIES_URL", "LPB_BATTERIES_TOKEN", "LPB_BATTERIES_GRANTS_TOKEN"]
)
def test_a_clearinghouse_mixed_with_a_batteries_setting_is_refused(setting: str) -> None:
    """An engine that uses a clearinghouse knows nothing of Batteries: only the clearinghouse's own
    engine reaches it (D85 point 1). With both, an actor would be allotted at one and accounted for at
    the other."""
    value = "http://batteries:8080" if setting.endswith("_URL") else "bt_secret-not-to-be-shown"
    message = refused(THREE | DISCOVERY | {setting: value})
    assert setting in message and "LPB_CLEARINGHOUSE_URL" in message
    assert value not in message and TOKEN not in message


def test_the_signer_address_is_set_beside_a_clearinghouse() -> None:
    """The engine signs at the clearinghouse's published signer itself, so it is told that signer's
    address as any paying engine is told its own (D85 point 1, as amended on 19 November 2026)."""
    settings = EngineSettings.from_env(THREE | {"LPB_SIGNER_URL": "https://signer.clearinghouse.example"})
    assert settings.uses_clearinghouse is True
    assert settings.signer_url == "https://signer.clearinghouse.example"
    assert settings.clearinghouse_url == URL  # neither address stands in for the other


def test_a_clearinghouse_without_a_signer_address_is_an_engine_that_does_not_pay() -> None:
    """No new rule: ``LPB_SIGNER_URL`` unset is an unpaid engine, with a clearinghouse or without one.
    The clearinghouse's address never becomes the signer's."""
    settings = EngineSettings.from_env(THREE)
    assert settings.uses_clearinghouse is True and settings.signer_url is None


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
        "/pay",  # a path: the engine calls the clearinghouse's routes at the root of its address
        "/v1/clearinghouse",
        "//",
        "/?",
    ],
)
def test_an_address_with_anything_after_the_host_and_port_is_refused(more: str) -> None:
    """The address names an origin and nothing more: the engine calls ``/v1/clearinghouse/...`` at its
    root, and the default discovery address is built from it."""
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


def test_an_engine_without_a_clearinghouse_still_needs_a_discovery_address_to_start() -> None:
    """Its settings are read without one (#154): only an engine that starts needs the address."""
    settings = EngineSettings.from_env({})
    assert settings.discovery_url is None and settings.clearinghouse_runner_list_token is None
    with pytest.raises(ValueError, match="LPB_DISCOVERY_URL is required to start the engine"):
        settings.require_discovery_url()


def test_a_command_that_serves_nothing_still_reads_the_clearinghouse_runner_list_by_default() -> None:
    """``lpb`` commands that serve nothing give no discovery address (#154). For an engine that uses a
    clearinghouse the address is still the clearinghouse's list, as for one that starts; an address the
    caller puts under the environment's values is a discovery of its own, and stays."""
    assert EngineSettings.from_env(THREE).discovery_url == URL + "/v1/clearinghouse/discovery"
    own = {"LPB_DISCOVERY_URL": "http://127.0.0.1:9/discovery"}
    assert EngineSettings.from_env(own | THREE).discovery_url == "http://127.0.0.1:9/discovery"


# -- the operation ledger's stale window ----------------------------------------------------------------


def test_the_longest_command_at_a_clearinghouse_fits_in_half_the_stale_window() -> None:
    """The ledger cancels a run at half ``LPB_OPERATION_STALE_S`` (D57). The longest command through the
    remote adapters makes three calls, each within ``LPB_CLEARINGHOUSE_TIMEOUT_S``: a rotation that
    another one overtook reads the allocation, rotates, and reads which key the allocation takes
    (``tests/unit/test_clearinghouse_handed_keys.py`` counts them). Three calls in half the window is six
    timeouts in the whole of it. Batteries' rule is four, for two calls."""
    message = refused(THREE | {"LPB_CLEARINGHOUSE_TIMEOUT_S": "40"})  # 6 x 40 = 240 > 120
    assert "LPB_OPERATION_STALE_S must be at least six times LPB_CLEARINGHOUSE_TIMEOUT_S" in message
    assert "half the stale window" in message and "three calls" in message
    assert "40" not in message and "120" not in message  # the rule's arithmetic, never the values given

    # at the boundary: three whole calls fit in exactly half the window, and not in less
    exact = EngineSettings.from_env(
        THREE | {"LPB_CLEARINGHOUSE_TIMEOUT_S": "40", "LPB_OPERATION_STALE_S": "240"}
    )
    assert exact.operation_stale_s / 2 == 3 * exact.clearinghouse_timeout_s
    refused(THREE | {"LPB_CLEARINGHOUSE_TIMEOUT_S": "40", "LPB_OPERATION_STALE_S": "239.9"})
    refused(
        THREE | {"LPB_CLEARINGHOUSE_TIMEOUT_S": "40", "LPB_OPERATION_STALE_S": "160"}
    )  # four was not enough
    refused(THREE | {"LPB_CLEARINGHOUSE_TIMEOUT_S": "20.1"})


def test_the_default_timeout_and_the_default_stale_window_fit_the_rule_exactly() -> None:
    """An engine that sets the three settings and nothing else starts: 6 x 20 s is the 120 s window."""
    settings = EngineSettings.from_env(THREE)
    assert (settings.clearinghouse_timeout_s, settings.operation_stale_s) == (20.0, 120.0)
    assert settings.operation_stale_s == 6 * settings.clearinghouse_timeout_s


def test_the_stale_window_rule_of_a_clearinghouse_binds_no_engine_without_one() -> None:
    settings = EngineSettings.from_env(DISCOVERY | {"LPB_CLEARINGHOUSE_TIMEOUT_S": "100"})
    assert settings.uses_clearinghouse is False and settings.operation_stale_s == 120.0
