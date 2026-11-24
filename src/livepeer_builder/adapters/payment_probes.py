"""The reads behind ``lpb payments check`` (D87, #168): what the engine's payments need, asked without
writing anything.

**Every request here is a GET.** They all go through ``_get``, which is the only code in this module that
touches the HTTP client, sends no body and follows no redirect, so a credential goes to the address its
setting names and to no other. ``tests/unit/test_payments_check.py`` records every request and reads
this file to hold that.

The engine's role says what is probed (``LPB_ROLE``, #169), and the report names it. Every engine that
pays signs at its signer itself, so every role probes:

- **the signer**, at the root of ``LPB_SIGNER_URL`` as the SDK reads it (scheme, host and port). The
  signer's identity call is not a GET, so it cannot be made here. ``GET /sign-orchestrator-info`` is
  asked: livepeer/node (``eee09c4``, ``signer/service.go``) registers that path for one other method
  only, so a signer answers 405 and names the method it takes in ``Allow``. That says an HTTP server
  is at the address and has the signer's identity route. It does not say the signer signs, that its
  wallet is funded, or that it is ready: only a payment, or the signer's own ``/readyz`` on its metrics
  listener, says those. No credential is sent. An engine in its ``clearinghouse`` role signs nothing,
  and is asked all the same: it runs the signer its tenants' engines sign at, and this is the address
  its wallet report reads. The probe does not say the signer is reachable at its published address.

An engine that reaches Batteries itself, in its ``both`` or its ``clearinghouse`` role, also probes:

- **Batteries** with the engine's credential: ``GET /v1/usage?limit=1``, the read cost sync makes;
- **Batteries** with the grants credential, when one is set: ``GET /v1/grants?limit=1``. An engine in
  its ``clearinghouse`` role cannot onboard a tenant without that credential, so there the probe is
  always made, and says so when the credential is not set.

An engine in its ``work`` role probes the payment clearinghouse it uses instead, with its tenant admin
key: its grant (who it is there), one page of its usage, and the runner list when that is its discovery
address. The grant and the usage are the tenant's own routes, which name no tenant: the clearinghouse
answers for the key's tenant, and the grant's id says whether that is ``LPB_CLEARINGHOUSE_TENANT_ID``.
A key that belongs to another tenant fails the grant probe, plainly, and the usage probe with it: the
page it read is that tenant's. An engine with such a key refuses every request to the clearinghouse
(``adapters/clearinghouse_tenant.py``).
Every one is a clearinghouse route: no work route is called. Its signer is the clearinghouse's
published one, at ``LPB_SIGNER_URL``, and is asked there, never at the clearinghouse's address: a
clearinghouse's engine serves no signer path (D85 point 1, as amended). With ``LPB_SIGNER_URL`` unset
the probe is ``not_configured`` and the check fails: such an engine runs its jobs unpaid.

Nothing the other side says is repeated: a probe reports a status and the engine's own words.
"""

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from livepeer_builder.adapters.batteries import TOKEN_HEADER
from livepeer_builder.adapters.clearinghouse_tenant import GRANT_PATH, USAGE_PATH
from livepeer_builder.config import CLEARINGHOUSE_RUNNER_LIST, EngineSettings
from livepeer_builder.contracts import (
    PaymentCheck,
    PaymentConfiguration,
    PaymentProbe,
    ProbeOutcome,
    Role,
    TenantGrant,
)

TIMEOUT_S = 5.0
SIGNER_IDENTITY = "/sign-orchestrator-info"
_NOT_SET = "{setting} is not set"
# No role is refused at startup for a missing signer address, so this is where each is told what follows.
_UNPAID = "LPB_SIGNER_URL is not set: this engine runs its jobs unpaid"
_NO_SIGNER: dict[Role, str] = {
    "both": _UNPAID,
    "work": f"{_UNPAID}. Set it to the address of the signer its payment clearinghouse publishes",
    "clearinghouse": "LPB_SIGNER_URL is not set: the wallet report cannot say whether the signer answers. "
    "Set it to the address this engine reaches the signer at",
}
# The grant the clearinghouse answers with is the key's tenant's. The other tenant's id is not repeated.
_ANOTHER = (
    "the tenant admin key in LPB_CLEARINGHOUSE_TOKEN belongs to another tenant than "
    "LPB_CLEARINGHOUSE_TENANT_ID: the clearinghouse answered with that tenant's grant. An engine with "
    "these settings refuses every request to the clearinghouse. Set the key and the id of one tenant"
)
Answer = httpx.Response | None  # None: no answer came


async def probe_payments(
    settings: EngineSettings, *, client: httpx.AsyncClient | None = None
) -> PaymentCheck:
    """Probe what this engine's payments need, by its settings. ``client`` is borrowed when given."""
    owned = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT_S)
    try:
        if settings.uses_clearinghouse:
            return await _clearinghouse(settings, client)
        return await _self_hosted(settings, client)
    finally:
        if owned:
            await client.aclose()


async def _get(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
) -> Answer:
    """The one request this module makes. A redirect is an answer, never followed. An address that
    cannot be called at all is no answer either: ``LPB_BATTERIES_URL`` is not checked when settings load."""
    try:
        return await client.get(
            url, params=params, headers=dict(headers or {}), follow_redirects=False, timeout=TIMEOUT_S
        )
    except (httpx.HTTPError, httpx.InvalidURL):
        return None


# -- a self-hosted engine ---------------------------------------------------------------------------


async def _self_hosted(settings: EngineSettings, client: httpx.AsyncClient) -> PaymentCheck:
    probes = [await _signer(settings, client), await _batteries(settings, client)]
    # optional for self-hosted payments; a clearinghouse onboards its tenants with it
    if settings.batteries_grants_token is not None or not settings.runs_jobs:
        probes.append(await _batteries_grants(settings, client))
    return _checked(settings, "self_hosted", probes)


async def _signer(settings: EngineSettings, client: httpx.AsyncClient) -> PaymentProbe:
    name, setting = "signer", "LPB_SIGNER_URL"
    if settings.signer_url is None:
        return _probe(name, setting, None, "not_configured", _NO_SIGNER[settings.role])
    url = settings.signer_url if "://" in settings.signer_url else f"https://{settings.signer_url}"
    parts = urlsplit(url)
    request = f"GET {SIGNER_IDENTITY}"
    answer = await _get(client, f"{parts.scheme}://{parts.netloc}{SIGNER_IDENTITY}")
    if answer is None:
        return _probe(name, setting, request, "unavailable", "the signer did not answer")
    status = answer.status_code
    if status == 405:
        return _probe(
            name,
            setting,
            request,
            "ok",
            "an HTTP server is at the signer's address and has its identity route. Whether it signs, "
            "and whether its wallet is funded, only a payment shows",
            status,
        )
    if status >= 500:
        return _probe(name, setting, request, "unavailable", f"the signer answered HTTP {status}", status)
    return _probe(
        name,
        setting,
        request,
        "unexpected",
        f"HTTP {status}: what answers at this address does not have the signer's identity route",
        status,
    )


async def _batteries(settings: EngineSettings, client: httpx.AsyncClient) -> PaymentProbe:
    name = "batteries"
    for setting, value in (
        ("LPB_BATTERIES_URL", settings.batteries_url),
        ("LPB_BATTERIES_TOKEN", settings.batteries_token),
    ):
        if value is None:
            return _probe(name, setting, None, "not_configured", _NOT_SET.format(setting=setting))
    token = settings.batteries_token.get_secret_value() if settings.batteries_token else ""
    return await _batteries_list(settings, client, name, "LPB_BATTERIES_TOKEN", "/v1/usage", token)


async def _batteries_grants(settings: EngineSettings, client: httpx.AsyncClient) -> PaymentProbe:
    name, setting = "batteries_grants", "LPB_BATTERIES_GRANTS_TOKEN"
    if settings.batteries_url is None:
        return _probe(
            name, "LPB_BATTERIES_URL", None, "not_configured", _NOT_SET.format(setting="LPB_BATTERIES_URL")
        )
    if settings.batteries_grants_token is None:
        return _probe(name, setting, None, "not_configured", _NOT_SET.format(setting=setting))
    token = settings.batteries_grants_token.get_secret_value()
    return await _batteries_list(settings, client, name, setting, "/v1/grants", token)


async def _batteries_list(
    settings: EngineSettings, client: httpx.AsyncClient, name: str, setting: str, path: str, token: str
) -> PaymentProbe:
    request = f"GET {path}"
    base = (settings.batteries_url or "").rstrip("/")
    answer = await _get(client, base + path, params={"limit": "1"}, headers={TOKEN_HEADER: token})
    if answer is None:
        return _probe(name, "LPB_BATTERIES_URL", request, "unavailable", "Batteries did not answer")
    status = answer.status_code
    if status in (401, 403):
        detail = f"Batteries refused the credential in {setting} (HTTP {status})"
        return _probe(name, setting, request, "refused", detail, status)
    if status == 404:
        detail = "HTTP 404: what answers at this address is not Batteries' management API"
        return _probe(name, "LPB_BATTERIES_URL", request, "refused", detail, status)
    if not answer.is_success:
        outcome: ProbeOutcome = "unavailable" if status >= 500 else "unexpected"
        return _probe(
            name, "LPB_BATTERIES_URL", request, outcome, f"Batteries answered HTTP {status}", status
        )
    body = _json(answer)
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        return _probe(
            name, "LPB_BATTERIES_URL", request, "unexpected", "the answer is not a Batteries list", status
        )
    return _probe(name, setting, request, "ok", "Batteries answered the read with this credential", status)


# -- an engine that uses a payment clearinghouse ------------------------------------------------------


async def _clearinghouse(settings: EngineSettings, client: httpx.AsyncClient) -> PaymentCheck:
    base = settings.clearinghouse_url or ""
    tenant_id = settings.clearinghouse_tenant_id or ""
    token = settings.clearinghouse_token.get_secret_value() if settings.clearinghouse_token else ""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    grant_probe, grant = await _tenant_grant(client, base, tenant_id, headers)
    own = grant is not None and grant_probe.outcome == "ok"  # the key's tenant is the engine's
    probes = [
        grant_probe,
        await _tenant_usage(client, base, headers, of_another_tenant=grant is not None and not own),
        await _signer(settings, client),  # the clearinghouse's signer, asked as an engine asks its own
    ]
    # the list is this engine's discovery address: the key goes to that address and no other
    if settings.discovery_url is not None and settings.clearinghouse_runner_list_token is not None:
        probes.append(await _runner_list(client, settings.discovery_url, headers))
    return _checked(settings, "clearinghouse", probes, grant if own else None)  # no other tenant's grant


async def _tenant_grant(
    client: httpx.AsyncClient, base: str, tenant_id: str, headers: Mapping[str, str]
) -> tuple[PaymentProbe, TenantGrant | None]:
    """Who the engine is at the clearinghouse: the grant of the key's tenant, when the read answered with
    it. The probe passes only when that tenant is the one ``LPB_CLEARINGHOUSE_TENANT_ID`` names."""
    name, request = "clearinghouse_grant", "GET " + GRANT_PATH
    answer = _answered(name, request, await _get(client, base + GRANT_PATH, headers=headers))
    if isinstance(answer, PaymentProbe):
        return answer, None
    try:
        grant = TenantGrant.model_validate_json(answer.content)
    except ValidationError:
        return _unexpected(name, request, "the grant could not be read", answer), None
    if grant.tenant_id != tenant_id:
        return _probe(name, "LPB_CLEARINGHOUSE_TENANT_ID", request, "unexpected", _ANOTHER, 200), grant
    detail = "the clearinghouse answered with this tenant's grant"
    return _probe(name, "LPB_CLEARINGHOUSE_TENANT_ID", request, "ok", detail, 200), grant


async def _tenant_usage(
    client: httpx.AsyncClient, base: str, headers: Mapping[str, str], *, of_another_tenant: bool
) -> PaymentProbe:
    """One page of the key's tenant's usage. The page states no tenant: the grant probe says whose it
    is, and a page of another tenant than the engine's is no pass."""
    name, request = "clearinghouse_usage", "GET " + USAGE_PATH
    url = base + USAGE_PATH
    answer = _answered(name, request, await _get(client, url, params={"limit": "1"}, headers=headers))
    if isinstance(answer, PaymentProbe):
        return answer
    body = _json(answer)
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        return _unexpected(name, request, "the usage page could not be read", answer)
    if of_another_tenant:
        detail = "the clearinghouse answered with a page of another tenant's payment records: " + _ANOTHER
        return _probe(name, "LPB_CLEARINGHOUSE_TENANT_ID", request, "unexpected", detail, 200)
    detail = "the clearinghouse answered with a page of the key's tenant's payment records"
    return _probe(name, "LPB_CLEARINGHOUSE_TOKEN", request, "ok", detail, 200)


async def _runner_list(client: httpx.AsyncClient, url: str, headers: Mapping[str, str]) -> PaymentProbe:
    name, request = "clearinghouse_runner_list", f"GET {CLEARINGHOUSE_RUNNER_LIST}"
    answer = _answered(name, request, await _get(client, url, headers=headers))
    if isinstance(answer, PaymentProbe):
        return answer
    if not isinstance(_json(answer), list):
        return _unexpected(name, request, "the runner list could not be read", answer)
    detail = "the clearinghouse answered with its runner list"
    return _probe(name, "LPB_DISCOVERY_URL", request, "ok", detail, 200)


def _answered(name: str, request: str, answer: Answer) -> PaymentProbe | httpx.Response:
    """The answer when it is a 2xx; otherwise the probe that says what came in its place."""
    if answer is None:
        return _probe(
            name, "LPB_CLEARINGHOUSE_URL", request, "unavailable", "the clearinghouse did not answer"
        )
    return _refused(name, request, answer) or answer


def _refused(name: str, request: str, answer: httpx.Response) -> PaymentProbe | None:
    """What an answer that is no 2xx means, in the settings a person can look at; ``None`` for a 2xx."""
    status = answer.status_code
    if answer.is_success:
        return None
    if status == 401:
        detail = "the clearinghouse does not accept the tenant admin key in LPB_CLEARINGHOUSE_TOKEN"
        return _probe(name, "LPB_CLEARINGHOUSE_TOKEN", request, "refused", detail, status)
    if status == 403:
        detail = (
            "the key in LPB_CLEARINGHOUSE_TOKEN is not a tenant admin's or an auditor's: a tenant's own "
            "routes refuse any other, and the public listener refuses a clearinghouse admin's"
        )
        return _probe(name, "LPB_CLEARINGHOUSE_TOKEN", request, "refused", detail, status)
    if status == 404:
        detail = "HTTP 404: LPB_CLEARINGHOUSE_URL does not serve this route, or the key's tenant has no grant"
        return _probe(name, "LPB_CLEARINGHOUSE_URL", request, "refused", detail, status)
    if 300 <= status < 400:
        return _unexpected(
            name, request, "the clearinghouse answered with a redirect, which is not followed", answer
        )
    outcome: ProbeOutcome = "unavailable" if status >= 500 or status == 429 else "unexpected"
    detail = f"the clearinghouse answered HTTP {status}"
    return _probe(name, "LPB_CLEARINGHOUSE_URL", request, outcome, detail, status)


def _unexpected(name: str, request: str, detail: str, answer: httpx.Response) -> PaymentProbe:
    return _probe(name, "LPB_CLEARINGHOUSE_URL", request, "unexpected", detail, answer.status_code)


# -- shared -------------------------------------------------------------------------------------------


def _json(answer: httpx.Response) -> Any:
    try:
        return answer.json()
    except (ValueError, RecursionError):
        return None


def _probe(
    name: str,
    setting: str,
    request: str | None,
    outcome: ProbeOutcome,
    detail: str,
    status: int | None = None,
) -> PaymentProbe:
    return PaymentProbe(
        name=name, setting=setting, request=request, outcome=outcome, status_code=status, detail=detail
    )


def _checked(
    settings: EngineSettings,
    configuration: PaymentConfiguration,
    probes: list[PaymentProbe],
    tenant: TenantGrant | None = None,
) -> PaymentCheck:
    return PaymentCheck(
        role=settings.role,
        configuration=configuration,
        ok=all(probe.outcome == "ok" for probe in probes),
        probes=tuple(probes),
        tenant=tenant,
    )
