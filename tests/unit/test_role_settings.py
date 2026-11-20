"""#169: ``LPB_ROLE`` and the settings each role needs (D84).

One program covers three deployments, and the role says which one this is:

- ``both``, the default: an enterprise with self-hosted payments. Its payment ports reach Batteries and
  the signer, so it holds no clearinghouse setting;
- ``work``: an enterprise that uses a payment clearinghouse. Its payment ports reach the clearinghouse,
  so it holds the three clearinghouse settings and nothing of Batteries or a signer;
- ``clearinghouse``: a payment clearinghouse. It is nobody's tenant, so it holds no clearinghouse
  setting, and what only its admin does is served on a private listener, which it must have.

A combination that contradicts the role is refused when the settings load, before anything is built. A
refusal names settings and echoes no value: several of them are secrets.

What a ``clearinghouse`` engine needs of its parts (a signer, Batteries, the tenant desk) is judged on
the parts it will run with, when it is built (tests/unit/test_roles.py).
"""

from typing import Any

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
SELF_HOSTED = {
    "LPB_BATTERIES_URL": "http://batteries-not-to-be-shown:8081",
    "LPB_BATTERIES_TOKEN": "bt_secret-not-to-be-shown",
    "LPB_BATTERIES_GRANTS_TOKEN": "gt_secret-not-to-be-shown",
    "LPB_SIGNER_URL": "http://signer-not-to-be-shown:8937",
}
PRIVATE = {"LPB_HTTP_PRIVATE_PORT": "8081"}
SECRETS = (TOKEN, "not-to-be-shown", "clearinghouse.example", "grant_7")


def refused(env: dict[str, str], **defaults: Any) -> str:
    with pytest.raises(ValidationError) as error:
        EngineSettings.from_env(env, defaults=defaults or None)
    assert carried(error.value, *SECRETS) == [], "a refusal names settings and echoes no value, in any form"
    return str(error.value)


# -- the three roles ----------------------------------------------------------------------------------


def test_the_role_is_both_unless_it_is_set() -> None:
    """A deployment that sets nothing new is what it was: self-hosted payments, both halves."""
    settings = EngineSettings.from_env(DISCOVERY | SELF_HOSTED)
    assert settings.role == "both"
    assert settings.uses_clearinghouse is False
    assert EngineSettings.model_fields["role"].default == "both"


@pytest.mark.parametrize("role", ["both", "work", "clearinghouse"])
def test_lpb_role_names_one_of_three_roles(role: str) -> None:
    extra = {"both": SELF_HOSTED, "work": THREE, "clearinghouse": SELF_HOSTED | PRIVATE}[role]
    settings = EngineSettings.from_env({"LPB_ROLE": role} | DISCOVERY | extra)
    assert settings.role == role


@pytest.mark.parametrize("role", ["Both", "WORK", "enterprise", "work,clearinghouse", "self_hosted"])
def test_any_other_role_is_refused(role: str) -> None:
    message = refused({"LPB_ROLE": role} | DISCOVERY)
    assert "role" in message
    assert "both" in message and "work" in message and "clearinghouse" in message  # what it may be


def test_an_empty_lpb_role_is_the_default_as_any_unset_variable_is() -> None:
    assert EngineSettings.from_env({"LPB_ROLE": ""} | DISCOVERY).role == "both"


# -- work: its payment ports point at a clearinghouse ---------------------------------------------------


def test_the_work_role_uses_a_clearinghouse() -> None:
    settings = EngineSettings.from_env({"LPB_ROLE": "work"} | THREE)
    assert settings.role == "work" and settings.uses_clearinghouse is True
    assert settings.discovery_url == URL + "/v1/clearinghouse/discovery"  # the default (D87)
    assert settings.clearinghouse_runner_list_token == TOKEN
    assert settings.http_private_port is None  # one listener, unless the deployment asks for two


def test_the_work_role_without_a_clearinghouse_is_refused_and_names_the_three_settings() -> None:
    message = refused({"LPB_ROLE": "work"} | DISCOVERY)
    assert "LPB_ROLE" in message
    assert all(name in message for name in THREE)


@pytest.mark.parametrize("missing", sorted(THREE))
def test_the_work_role_with_half_a_clearinghouse_is_refused_and_names_what_is_missing(missing: str) -> None:
    env = {name: value for name, value in THREE.items() if name != missing}
    message = refused({"LPB_ROLE": "work"} | DISCOVERY | env)
    assert missing in message


@pytest.mark.parametrize("setting", sorted(SELF_HOSTED))
def test_the_work_role_beside_batteries_or_a_signer_is_refused(setting: str) -> None:
    """An engine in its work role holds no address or credential of Batteries or a signer: it would
    allot at one place and pay from the other."""
    message = refused({"LPB_ROLE": "work", setting: SELF_HOSTED[setting]} | THREE)
    assert setting in message


@pytest.mark.parametrize("setting", sorted(SELF_HOSTED))
def test_the_work_role_with_self_hosted_settings_and_no_clearinghouse_names_both(setting: str) -> None:
    """Somebody set ``LPB_ROLE=work`` on a self-hosted deployment: the refusal says what the role needs
    and what it cannot hold, in one message."""
    message = refused({"LPB_ROLE": "work", setting: SELF_HOSTED[setting]} | DISCOVERY)
    assert all(name in message for name in THREE)
    assert setting in message


# -- both: self-hosted payments -----------------------------------------------------------------------


@pytest.mark.parametrize("role", [{}, {"LPB_ROLE": "both"}])
def test_the_both_role_with_a_clearinghouse_is_refused_and_names_the_role(role: dict[str, str]) -> None:
    """The role is the one switch. The three settings alone do not turn an engine into a clearinghouse's
    tenant: it would still serve the clearinghouse routes of an engine that has no Batteries."""
    message = refused(role | THREE)
    assert "LPB_ROLE" in message and "work" in message
    assert all(name in message for name in THREE)


def test_the_both_role_may_have_a_private_listener_or_none() -> None:
    assert EngineSettings.from_env(DISCOVERY | SELF_HOSTED).http_private_port is None
    assert EngineSettings.from_env(DISCOVERY | SELF_HOSTED | PRIVATE).http_private_port == 8081


# -- clearinghouse: payments only, with a private listener ---------------------------------------------


def test_the_clearinghouse_role_needs_the_private_listener() -> None:
    """What only a clearinghouse admin does is never served on a published listener (D84): a
    clearinghouse with one listener would serve onboarding and top-ups to whoever reaches it."""
    message = refused({"LPB_ROLE": "clearinghouse"} | DISCOVERY | SELF_HOSTED)
    assert "LPB_ROLE" in message and "LPB_HTTP_PRIVATE_PORT" in message


def test_the_clearinghouse_role_with_a_private_listener_loads() -> None:
    settings = EngineSettings.from_env({"LPB_ROLE": "clearinghouse"} | DISCOVERY | SELF_HOSTED | PRIVATE)
    assert (settings.role, settings.http_private_port) == ("clearinghouse", 8081)
    assert settings.http_private_host == "127.0.0.1"  # loopback unless the deployment says otherwise
    assert settings.uses_clearinghouse is False


def test_the_clearinghouse_role_is_no_tenant_of_another_clearinghouse() -> None:
    message = refused({"LPB_ROLE": "clearinghouse"} | THREE | PRIVATE)
    assert "LPB_ROLE" in message
    assert all(name in message for name in THREE)


def test_the_clearinghouse_role_still_refuses_one_address_for_both_listeners() -> None:
    env = {"LPB_ROLE": "clearinghouse", "LPB_HTTP_PORT": "9000", "LPB_HTTP_PRIVATE_PORT": "9000"}
    assert "LPB_HTTP_PRIVATE_PORT" in refused(env | DISCOVERY | SELF_HOSTED)


# -- the role is a setting like any other ---------------------------------------------------------------


def test_the_role_is_set_in_code_as_it_is_in_the_environment() -> None:
    """An embedding app builds ``EngineSettings`` itself (docs/guides/embedded-mode.md)."""
    embedded = EngineSettings.model_validate(
        {
            "role": "work",
            "clearinghouse_url": URL,
            "clearinghouse_token": TOKEN,
            "clearinghouse_tenant_id": "grant_7",
        }
    )
    assert embedded.role == "work" and embedded.uses_clearinghouse
    with pytest.raises(ValidationError, match="LPB_ROLE"):
        EngineSettings.model_validate(
            {"clearinghouse_url": URL, "clearinghouse_token": TOKEN, "clearinghouse_tenant_id": "grant_7"}
        )
