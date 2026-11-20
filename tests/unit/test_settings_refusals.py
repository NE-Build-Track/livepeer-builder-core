"""A refused setting's value is in no form of the refusal (#162 review).

``hide_input_in_errors`` hides what was given in ``str(error)`` only: ``errors()`` and ``json()`` kept it.
An embedding application that logs either when its settings are refused would log a mistyped or retired
setting's value, a credential of the wrong type, and, for a whole-settings rule or a missing setting,
every setting given. Each case below gives credentials beside the setting that is refused, checks every
form a refusal can be logged in (``tests/unit/refusal_forms.py``), and checks that the refusal still names
the setting and keeps its type and its words.

A rule's own words are kept, so a rule that quoted what it refused put the value back in every form
(#162 review). ``RULE_REFUSALS`` refuses each rule's own setting with a value that carries the credential,
once for each ``raise`` the rule holds, and a guard fails when a rule or a ``raise`` is added without one.
"""

import ast
import inspect
import json
import textwrap
from collections.abc import Callable
from typing import Any, NamedTuple

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
    "Value error, LPB_OPERATION_STALE_S must be at least four times LPB_BATTERIES_TIMEOUT_S"
)
PRIVATE_PORT_REFUSED = (  # #167
    "LPB_HTTP_PRIVATE_PORT is the public listener's port (LPB_HTTP_PORT) on the same host: the private "
    "listener needs an address of its own"
)
CLEARINGHOUSE = {  # #168: the three settings of an engine that uses a payment clearinghouse
    "clearinghouse_url": "https://clearinghouse.invalid",
    "clearinghouse_token": f"lpk-{CREDENTIAL}",
    "clearinghouse_tenant_id": "grant_7",
}
# The same three, each with the credential in its own value, for the rules that refuse them.
CLEARINGHOUSE_OF_ITS_OWN = {
    "clearinghouse_url": f"https://{CREDENTIAL}.invalid",
    "clearinghouse_token": f"lpk-{CREDENTIAL}",
    "clearinghouse_tenant_id": f"grant-{CREDENTIAL}",
}
CLEARINGHOUSE_STALE_WINDOW_REFUSED = (
    "LPB_OPERATION_STALE_S must be at least four times LPB_CLEARINGHOUSE_TIMEOUT_S"
)
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
        ("value_error", (), f"Value error, {PRIVATE_PORT_REFUSED}"),
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
        ("value_error", (), f"Value error, {CLEARINGHOUSE_STALE_WINDOW_REFUSED}"),
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
            "be set unless LPB_ROLE=work: an engine pays through a payment clearinghouse in its work role "
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
            "be set unless LPB_ROLE=work: a payment clearinghouse is not a tenant of another one. Set "
            "LPB_ROLE=work to use a payment clearinghouse, or remove them",
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
    pytest.param(  # #163 review: neither tenant source has a default
        {"auth": "jwt", "jwt_issuer": "https://idp.invalid/", "jwt_audience": "engine"},
        (
            "value_error",
            (),
            "Value error, LPB_AUTH includes jwt, so exactly one of LPB_JWT_TENANT_CLAIM and "
            "LPB_JWT_TENANT_ID must be set: the claim that names each token's tenant, or the one tenant "
            "every token is in; neither is set",
        ),
        id="the rule on a JWT's tenant",
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


class Refused(NamedTuple):
    """One refusal by a rule of the settings: what the case changes, and the refusal's (type, loc, msg)."""

    why: str
    change: dict[str, Any]
    expected: tuple[str, tuple[str, ...], str]
    numerals: tuple[str, ...] = ()  # a refused number as a message would print it


def value_error(setting: str | None, words: str) -> tuple[str, tuple[str, ...], str]:
    """A rule's refusal as pydantic reports it: a field rule's names its setting, a whole-settings rule's
    names none."""
    return ("value_error", (setting,) if setting else (), f"Value error, {words}")


# Each rule of the settings (a field or model validator of EngineSettings), with a refusal for each
# ``raise`` it holds. The value a case gives the refused setting carries CREDENTIAL wherever the setting
# takes free text, and is in ``numerals`` where it takes a number. A setting that takes one of a closed set
# of words, or that the rule refuses for being empty or absent, can carry neither: those cases still check
# the credentials beside it.
RULE_REFUSALS: dict[str, list[Refused]] = {
    "_signer_url_holds_no_secret": [
        Refused(
            "a signer URL with userinfo",
            {"signer_url": f"https://signer:{CREDENTIAL}@signer.invalid"},
            value_error("signer_url", "signer_url must not carry userinfo, a query or a fragment"),
        ),
        Refused(
            "a signer URL with no host",
            {"signer_url": f"https:///{CREDENTIAL}"},
            value_error("signer_url", "signer_url needs a host, as http(s)://host[:port] or host:port"),
        ),
    ],
    "_http_discovery_url": [
        Refused(
            "a discovery URL with a query",
            {"discovery_url": f"https://discovery.invalid/discovery?token={CREDENTIAL}"},
            value_error("discovery_url", "LPB_DISCOVERY_URL must not carry userinfo, a query or a fragment"),
        ),
        Refused(
            "a discovery URL that is not one",
            {"discovery_url": f"not a url {CREDENTIAL}"},
            value_error(
                "discovery_url",
                "LPB_DISCOVERY_URL must be an http:// or https:// URL with a host and no whitespace, such "
                "as http://discovery:8939/discovery",
            ),
        ),
    ],
    "_one_of_each": [
        Refused(
            "no auth mode",
            {"auth": ""},
            value_error("auth", "auth needs at least one mode: api_keys, jwt, or jwt,api_keys"),
        ),
        Refused(
            "an auth mode named twice",
            {"auth": "api_keys,api_keys"},
            value_error("auth", "auth names a mode twice"),
        ),
    ],
    "_asymmetric": [
        Refused(
            "a JWT algorithm the engine does not take",
            {"jwt_algorithms": f"RS256,{CREDENTIAL}"},
            value_error(
                "jwt_algorithms",
                "jwt_algorithms takes asymmetric algorithms only, at least one of: EdDSA, ES256, ES384, "
                "ES512, PS256, PS384, PS512, RS256, RS384, RS512",
            ),
        ),
    ],
    "_token_types": [
        Refused(
            "no JWT token type",
            {"jwt_token_types": ""},
            value_error("jwt_token_types", "jwt_token_types needs at least one token type, such as at+jwt"),
        ),
    ],
    "_stale_outlasts_batteries_calls": [
        Refused(
            "a stale window shorter than four Batteries calls",
            {"operation_stale_s": 6103.25, "batteries_timeout_s": 1717.5},
            ("value_error", (), STALE_WINDOW_REFUSED),
            numerals=("6103.25", "1717.5"),
        ),
    ],
    "_jwt_is_configured": [
        Refused(
            "JWT with no issuer",
            {"auth": "jwt"},
            value_error(None, "LPB_JWT_ISSUER is required when LPB_AUTH includes jwt"),
        ),
        Refused(
            "an issuer that is not https",
            {"jwt_issuer": f"http://idp.invalid/{CREDENTIAL}"},
            value_error(None, "LPB_JWT_ISSUER must be https (LPB_JWT_ALLOW_HTTP=true for local development)"),
        ),
        Refused(
            "an admin scope ordinary tokens carry",
            {"jwt_scope_prefix": f"{CREDENTIAL}:", "jwt_admin_scope": f"{CREDENTIAL}:openid"},
            value_error(
                None,
                "jwt_admin_scope is a scope ordinary tokens carry (an operation's name or a standard "
                "scope, as written or after LPB_JWT_SCOPE_PREFIX), so every user would be an admin",
            ),
        ),
        Refused(
            "a read-only admin scope that is the admin scope",
            {"jwt_admin_scope": f"admin-{CREDENTIAL}", "jwt_admin_read_scope": f"admin-{CREDENTIAL}"},
            value_error(
                None,
                "jwt_admin_read_scope is a scope ordinary tokens carry (an operation's name or a "
                "standard scope, as written or after LPB_JWT_SCOPE_PREFIX), or the admin scope, so it "
                "would not mark a read-only admin",
            ),
        ),
    ],
    "_jwt_names_one_tenant_source": [  # #163 review
        Refused(
            "a JWT's tenant named twice",
            {
                "auth": "jwt",
                "jwt_issuer": "https://idp.invalid/",
                "jwt_audience": "engine",
                "jwt_tenant_claim": f"claim-{CREDENTIAL}",
                "jwt_tenant_id": f"tenant-{CREDENTIAL}",
            },
            value_error(
                None,
                "LPB_AUTH includes jwt, so exactly one of LPB_JWT_TENANT_CLAIM and LPB_JWT_TENANT_ID must be "
                "set: the claim that names each token's tenant, or the one tenant every token is in; both "
                "are set",
            ),
        ),
    ],
    "_two_listeners_have_two_addresses": [  # #167
        Refused(
            "a private listener on the public listener's address",
            {"http_port": 18473, "http_private_port": 18473},
            value_error(None, PRIVATE_PORT_REFUSED),
            numerals=("18473",),
        ),
    ],
    "_clearinghouse_url_is_an_origin": [  # #168
        Refused(
            "a clearinghouse address with a path",
            CLEARINGHOUSE | {"clearinghouse_url": f"https://clearinghouse.invalid/{CREDENTIAL}"},
            value_error(
                "clearinghouse_url",
                "LPB_CLEARINGHOUSE_URL must be http:// or https://, a host and an optional port, with no "
                "path, query, fragment or userinfo, such as https://clearinghouse.example: the signer's "
                "two paths are called at its root",
            ),
        ),
    ],
    "_a_clearinghouse_is_whole_and_alone": [  # #168
        Refused(
            "half a clearinghouse",
            CLEARINGHOUSE_OF_ITS_OWN | {"clearinghouse_token": ABSENT},
            value_error(
                None,
                "LPB_CLEARINGHOUSE_URL, LPB_CLEARINGHOUSE_TOKEN, LPB_CLEARINGHOUSE_TENANT_ID are set "
                "together or not at all; missing: LPB_CLEARINGHOUSE_TOKEN",
            ),
        ),
        Refused(
            "a clearinghouse beside self-hosted payments",
            CLEARINGHOUSE_OF_ITS_OWN | {"batteries_url": f"https://batteries.invalid/{CREDENTIAL}"},
            value_error(
                None,
                "LPB_BATTERIES_URL, LPB_BATTERIES_TOKEN cannot be set with LPB_CLEARINGHOUSE_URL: an "
                "engine that uses a payment clearinghouse reaches Batteries and the signer through it, "
                "and holds no address or credential of theirs",
            ),
        ),
        Refused(
            "a stale window shorter than four calls to the clearinghouse",
            CLEARINGHOUSE_OF_ITS_OWN
            | {
                "batteries_token": ABSENT,
                "batteries_timeout_s": 1,
                "operation_stale_s": 6103.25,
                "clearinghouse_timeout_s": 1717.5,
            },
            value_error(None, CLEARINGHOUSE_STALE_WINDOW_REFUSED),
            numerals=("6103.25", "1717.5"),
        ),
    ],
    "_the_settings_agree_with_the_role": [  # #169: the role is one of three words, and carries nothing
        Refused(
            "the work role without a clearinghouse",
            {"role": "work", "batteries_url": f"https://batteries.invalid/{CREDENTIAL}"},
            value_error(
                None,
                "LPB_ROLE=work needs LPB_CLEARINGHOUSE_URL, LPB_CLEARINGHOUSE_TOKEN, "
                "LPB_CLEARINGHOUSE_TENANT_ID: an engine in its work role pays through a payment "
                "clearinghouse, and cannot hold LPB_BATTERIES_URL, LPB_BATTERIES_TOKEN",
            ),
        ),
        Refused(
            "a clearinghouse beside both halves",
            CLEARINGHOUSE_OF_ITS_OWN | {"batteries_token": ABSENT, "role": "both"},
            value_error(
                None,
                "LPB_CLEARINGHOUSE_URL, LPB_CLEARINGHOUSE_TOKEN, LPB_CLEARINGHOUSE_TENANT_ID cannot be set "
                "unless LPB_ROLE=work: an engine pays through a payment clearinghouse in its work role "
                "only. Set LPB_ROLE=work to use a payment clearinghouse, or remove them",
            ),
        ),
        Refused(
            "a clearinghouse without its private listener",
            {"role": "clearinghouse", "batteries_url": f"https://batteries.invalid/{CREDENTIAL}"},
            value_error(
                None,
                "LPB_ROLE=clearinghouse needs LPB_HTTP_PRIVATE_PORT: what only a clearinghouse admin does "
                "(onboarding, grant top-ups, tenant status, the wallet) is served on the private "
                "listener and nowhere else",
            ),
        ),
    ],
}


@pytest.mark.parametrize(
    "case",
    [pytest.param(case, id=f"{rule}: {case.why}") for rule, cases in RULE_REFUSALS.items() for case in cases],
)
def test_a_rule_does_not_repeat_the_value_it_refuses(case: Refused) -> None:
    """The rule's words say what the setting needs and name it. What was given is in no form."""
    given = {"discovery_url": DISCOVERY, **CREDENTIALS, **case.change}
    given = {name: value for name, value in given.items() if value is not ABSENT}
    error = refusal(lambda: EngineSettings.model_validate(given))
    assert carried(error, CREDENTIAL, *case.numerals) == []
    assert described(error) == [case.expected]


def rules(model: type[BaseModel]) -> list[str]:
    """The field and model validators of a model, by name."""
    decorators = model.__pydantic_decorators__
    return [*decorators.field_validators, *decorators.model_validators]


def refusals_in(model: type[BaseModel], rule: str) -> int:
    """How many ``raise`` statements a rule holds."""
    source = textwrap.dedent(inspect.getsource(getattr(model, rule)))
    return sum(isinstance(node, ast.Raise) for node in ast.walk(ast.parse(source)))


def test_every_refusal_a_rule_holds_is_tried_with_a_value_of_its_own() -> None:
    """The guard for rules and refusals added later: one that quotes what it refuses passes every test that
    only gives credentials beside it."""
    held = {rule: refusals_in(EngineSettings, rule) for rule in rules(EngineSettings) if rule != REDACTION}
    tried = {rule: len({case.expected for case in cases}) for rule, cases in RULE_REFUSALS.items()}
    untried = {
        rule: f"{tried.get(rule, 0)} of {count}" for rule, count in held.items() if tried.get(rule, 0) < count
    }
    assert untried == {}, (
        f"RULE_REFUSALS has too few refusals for these rules of EngineSettings (differently worded refusals "
        f"tried, of the raise statements the rule holds): {untried}. Add a case for each, whose refused "
        "setting carries CREDENTIAL in its own value (or numerals, for a number), and word the refusal "
        "without the value given."
    )
    unknown = sorted(set(RULE_REFUSALS) - set(held))
    assert unknown == [], f"RULE_REFUSALS names rules EngineSettings does not have: {unknown}"


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
