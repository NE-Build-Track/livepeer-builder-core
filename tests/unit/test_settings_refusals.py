"""A refused setting's value is in no form of the refusal (#162 review).

``hide_input_in_errors`` hides what was given in ``str(error)`` only: ``errors()`` and ``json()`` kept it.
An embedding application that logs either when its settings are refused would log a mistyped or retired
setting's value, a credential of the wrong type, and, for a whole-settings rule or a missing setting,
every setting given. Each case below gives credentials beside the setting that is refused, checks every
form a refusal can be logged in (``tests/unit/refusal_forms.py``), and checks that the refusal still names
the setting and keeps its type and its words.
"""

import json
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError, model_validator

from livepeer_builder.config import EngineSettings
from tests.unit.refusal_forms import carried

CREDENTIAL = "throwaway-credential"  # in every credential below, so one search finds any of them
CREDENTIALS: dict[str, Any] = {
    "batteries_token": f"tok-{CREDENTIAL}",
    "secret_key": f"key-{CREDENTIAL}",
    "database_url": f"postgresql+asyncpg://engine:{CREDENTIAL}@db.invalid/engine",
}
DISCOVERY = "http://127.0.0.1:9/discovery"
ABSENT = object()  # a setting the case leaves out
EXTRA = "Extra inputs are not permitted"
SIGNER_URL_REFUSED = "Value error, signer_url must not carry userinfo, a query or a fragment"
STALE_WINDOW_REFUSED = (
    "Value error, LPB_OPERATION_STALE_S (1) must be at least four times LPB_BATTERIES_TIMEOUT_S (30)"
)
CLEARINGHOUSE = {  # #168: the three settings of an engine that uses a payment clearinghouse
    "clearinghouse_url": "https://clearinghouse.invalid",
    "clearinghouse_token": f"lpk-{CREDENTIAL}",
    "clearinghouse_tenant_id": "grant_7",
}
# The model validator that hides what was given (config.py). Every other must run inside it.
REDACTION = "_a_refusal_holds_no_value"

BUILT_IN_CODE: dict[str, Callable[[dict[str, Any]], EngineSettings]] = {
    "EngineSettings(...)": lambda given: EngineSettings(**given),
    "model_validate": EngineSettings.model_validate,
    "model_validate_json": lambda given: EngineSettings.model_validate_json(json.dumps(given)),
}
# What each case changes in valid settings that hold credentials, and the refusal's (type, loc, msg).
REFUSED_IN_CODE = [
    pytest.param(
        {"shared_payment_key": f"lpg-{CREDENTIAL}"},
        ("extra_forbidden", ("shared_payment_key",), EXTRA),
        id="a retired setting",
    ),
    pytest.param(
        {"batteries_tokn": f"tok-{CREDENTIAL}"},
        ("extra_forbidden", ("batteries_tokn",), EXTRA),
        id="a credential setting's name mistyped",
    ),
    pytest.param(  # the grants token's name before #163; no setting keeps an old name
        {"batteries_operator_token": f"tok-{CREDENTIAL}"},
        ("extra_forbidden", ("batteries_operator_token",), EXTRA),
        id="a renamed credential setting",
    ),
    pytest.param(
        {"batteries_token": {"token": f"tok-{CREDENTIAL}"}},
        ("string_type", ("batteries_token",), "Input should be a valid string"),
        id="a credential of the wrong type",
    ),
    pytest.param(
        {"signer_url": f"https://signer:{CREDENTIAL}@signer.invalid"},
        ("value_error", ("signer_url",), SIGNER_URL_REFUSED),
        id="a field validator",
    ),
    pytest.param(
        {"operation_stale_s": 1},
        ("value_error", (), STALE_WINDOW_REFUSED),
        id="the rule on the stale window",
    ),
    pytest.param(
        {"auth": "jwt"},
        ("value_error", (), "Value error, LPB_JWT_ISSUER is required when LPB_AUTH includes jwt"),
        id="the rule on JWT settings",
    ),
    pytest.param(  # #167
        {"http_private_port": 8080},
        (
            "value_error",
            (),
            "Value error, LPB_HTTP_PRIVATE_PORT (8080) is the public listener's port on the same host: "
            "the private listener needs an address of its own",
        ),
        id="the rule on the two listeners",
    ),
    pytest.param(  # #168, with the tenant admin key given
        CLEARINGHOUSE | {"clearinghouse_tenant_id": ABSENT},
        (
            "value_error",
            (),
            "Value error, LPB_CLEARINGHOUSE_URL, LPB_CLEARINGHOUSE_TOKEN, LPB_CLEARINGHOUSE_TENANT_ID are "
            "set together or not at all; missing: LPB_CLEARINGHOUSE_TENANT_ID",
        ),
        id="the rule on a half clearinghouse",
    ),
    pytest.param(  # #168: the Batteries token is one of the credentials every case gives
        CLEARINGHOUSE,
        (
            "value_error",
            (),
            "Value error, LPB_BATTERIES_TOKEN cannot be set with LPB_CLEARINGHOUSE_URL: an engine that uses "
            "a payment clearinghouse reaches Batteries and the signer through it, and holds no address or "
            "credential of theirs",
        ),
        id="the rule on a clearinghouse beside self-hosted payments",
    ),
    pytest.param(  # #168
        CLEARINGHOUSE | {"batteries_token": ABSENT, "batteries_timeout_s": 1, "operation_stale_s": 100},
        (
            "value_error",
            (),
            "Value error, LPB_OPERATION_STALE_S (100) must be at least four times "
            "LPB_CLEARINGHOUSE_TIMEOUT_S (30)",
        ),
        id="the rule on the stale window with a clearinghouse",
    ),
    pytest.param(  # #168
        CLEARINGHOUSE | {"clearinghouse_url": f"https://tenant:{CREDENTIAL}@clearinghouse.invalid"},
        (
            "value_error",
            ("clearinghouse_url",),
            "Value error, LPB_CLEARINGHOUSE_URL must be http:// or https://, a host and an optional port, "
            "with no path, query, fragment or userinfo, such as https://clearinghouse.example: the "
            "signer's two paths are called at its root",
        ),
        id="the clearinghouse address's field validator",
    ),
    pytest.param(  # #169
        {"role": f"tok-{CREDENTIAL}"},
        ("literal_error", ("role",), "Input should be 'both', 'work' or 'clearinghouse'"),
        id="a credential given as the role",
    ),
    pytest.param(  # #169: the Batteries token is one of the credentials every case gives
        {"role": "work"},
        (
            "value_error",
            (),
            "Value error, LPB_ROLE=work needs LPB_CLEARINGHOUSE_URL, LPB_CLEARINGHOUSE_TOKEN, "
            "LPB_CLEARINGHOUSE_TENANT_ID: an engine in its work role pays through a payment clearinghouse, "
            "and cannot hold LPB_BATTERIES_TOKEN",
        ),
        id="the role rule: work without a clearinghouse",
    ),
    pytest.param(  # #169, with the tenant admin key given
        CLEARINGHOUSE | {"batteries_token": ABSENT, "role": "both"},
        (
            "value_error",
            (),
            "Value error, LPB_CLEARINGHOUSE_URL, LPB_CLEARINGHOUSE_TOKEN, LPB_CLEARINGHOUSE_TENANT_ID cannot "
            "be set with LPB_ROLE=both: an engine pays through a payment clearinghouse in its work role "
            "only. Set LPB_ROLE=work to use a payment clearinghouse, or remove them",
        ),
        id="the role rule: a clearinghouse beside both halves",
    ),
    pytest.param(  # #169, with the tenant admin key given
        CLEARINGHOUSE | {"batteries_token": ABSENT, "role": "clearinghouse", "http_private_port": 8081},
        (
            "value_error",
            (),
            "Value error, LPB_CLEARINGHOUSE_URL, LPB_CLEARINGHOUSE_TOKEN, LPB_CLEARINGHOUSE_TENANT_ID cannot "
            "be set with LPB_ROLE=clearinghouse: a payment clearinghouse is not a tenant of another one. "
            "Set LPB_ROLE=work to use a payment clearinghouse, or remove them",
        ),
        id="the role rule: a clearinghouse that is a tenant",
    ),
    pytest.param(  # #169
        {"role": "clearinghouse"},
        (
            "value_error",
            (),
            "Value error, LPB_ROLE=clearinghouse needs LPB_HTTP_PRIVATE_PORT: what only a clearinghouse "
            "admin does (onboarding, grant top-ups, tenant status, the wallet) is served on the private "
            "listener and nowhere else",
        ),
        id="the role rule: a clearinghouse without its private listener",
    ),
    pytest.param(
        {"discovery_url": ABSENT},
        ("missing", ("discovery_url",), "Field required"),
        id="a required setting missing",
    ),
]
ENVIRONMENT = {"LPB_DISCOVERY_URL": DISCOVERY} | {
    f"LPB_{name.upper()}": value for name, value in CREDENTIALS.items()
}
REFUSED_FROM_ENV = [
    pytest.param(
        {"LPB_SIGNER_URL": f"https://signer:{CREDENTIAL}@signer.invalid"},
        ("value_error", ("signer_url",), SIGNER_URL_REFUSED),
        id="a field validator",
    ),
    pytest.param(
        {"LPB_BATTERIES_TIMEOUT_S": f"tok-{CREDENTIAL}"},
        (
            "float_parsing",
            ("batteries_timeout_s",),
            "Input should be a valid number, unable to parse string as a number",
        ),
        id="a credential in a number's variable",
    ),
    pytest.param(
        {"LPB_OPERATION_STALE_S": "1"},
        ("value_error", (), STALE_WINDOW_REFUSED),
        id="a whole-settings rule",
    ),
    pytest.param(
        {"LPB_DISCOVERY_URL": ""},  # an empty variable is unset
        ("missing", ("discovery_url",), "Field required"),
        id="a required setting missing",
    ),
]


def refusal(build: Callable[[], object]) -> ValidationError:
    with pytest.raises(ValidationError) as refused:
        build()
    return refused.value


def described(error: ValidationError) -> list[tuple[str, tuple[int | str, ...], str]]:
    return [(detail["type"], detail["loc"], detail["msg"]) for detail in error.errors()]


@pytest.mark.parametrize("build", BUILT_IN_CODE.values(), ids=BUILT_IN_CODE.keys())
@pytest.mark.parametrize(("change", "expected"), REFUSED_IN_CODE)
def test_settings_refused_in_code_carry_no_value_given(
    build: Callable[[dict[str, Any]], EngineSettings], change: dict[str, Any], expected: tuple[Any, ...]
) -> None:
    given = {"discovery_url": DISCOVERY, **CREDENTIALS, **change}
    given = {name: value for name, value in given.items() if value is not ABSENT}
    error = refusal(lambda: build(given))
    assert carried(error, CREDENTIAL) == []
    assert described(error) == [expected]


@pytest.mark.parametrize(("change", "expected"), REFUSED_FROM_ENV)
def test_settings_refused_from_the_environment_carry_no_value_given(
    change: dict[str, str], expected: tuple[Any, ...]
) -> None:
    environment = ENVIRONMENT | change
    error = refusal(lambda: EngineSettings.from_env(environment))
    assert carried(error, CREDENTIAL) == []
    assert described(error) == [expected]


def test_every_refusal_of_a_build_is_in_one_error_and_each_names_its_setting() -> None:
    """Pydantic reports every field it refuses at once; hiding the values keeps each of them."""
    given = {"discovery_url": DISCOVERY, **CREDENTIALS, "batteries_tokn": CREDENTIAL, "http_port": CREDENTIAL}
    error = refusal(lambda: EngineSettings.model_validate(given))
    assert carried(error, CREDENTIAL) == []
    assert [(detail["type"], detail["loc"]) for detail in error.errors()] == [
        ("int_parsing", ("http_port",)),
        ("extra_forbidden", ("batteries_tokn",)),
    ]


def outermost_rule(model: type[BaseModel]) -> str:
    """The model validator that runs around every other. Pydantic nests ``wrap`` and ``after`` validators
    in the order the class defines them, the last outermost, and runs ``before`` validators and field
    validators inside them all (the test below pins that)."""
    decorators = model.__pydantic_decorators__.model_validators.values()
    return [decorator.cls_var_name for decorator in decorators if decorator.info.mode != "before"][-1]


def test_every_rule_of_the_settings_runs_inside_the_redaction() -> None:
    """The guard for rules added later: a rule outside the redaction would refuse with every setting
    given, credentials too, in its ``errors()``."""
    assert outermost_rule(EngineSettings) == REDACTION, (
        f"EngineSettings.{outermost_rule(EngineSettings)} is defined after {REDACTION} in config.py, so its "
        f"refusal carries every setting given. Define it above {REDACTION}, which must stay the last model "
        "validator of the class."
    )


def test_a_rule_defined_after_the_redaction_would_carry_the_settings() -> None:
    """What the guard above relies on: the last model validator defined runs outside the others."""

    class Later(EngineSettings):
        @model_validator(mode="after")
        def _later_rule(self) -> "Later":
            raise ValueError("a rule defined after the redaction")

    given = {"discovery_url": DISCOVERY, **CREDENTIALS}
    error = refusal(lambda: Later.model_validate(given))
    assert outermost_rule(Later) == "_later_rule"
    assert carried(error, CREDENTIAL)
