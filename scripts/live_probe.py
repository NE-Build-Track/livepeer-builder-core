"""Live probe (ISS-23, D61): access, revocation and one repeated fund, against a running engine.

A client of the engine's public REST API only. Not part of CI or pytest: an operator runs it against a
stack, and its output is the ``live`` evidence in docs/product-specs/m3-traceability.md.

    LPB_PROBE_URL=https://engine.example LPB_PROBE_ADMIN_KEY=lpk_... \\
    LPB_PROBE_FUND_ACTOR=alice uv run --frozen python scripts/live_probe.py --yes

``LPB_PROBE_URL`` and ``LPB_PROBE_ADMIN_KEY`` (an admin ``lpk_`` key; the engine must accept API keys)
name the stack; without them the probe prints why and exits 0. It issues two actor keys and an auditor
key for fresh probe actors, checks 401 with no credential, 403 on admin routes for the actor and auditor
keys, 404 across actors (on jobs for the run's own app, ``lpb-probe/<run>``, that fail ``no_offering``
before any payment; none is sent if the catalog lists that app), then revokes a key and
expects 401.

It spends only with ``--yes`` and ``LPB_PROBE_FUND_ACTOR`` (an actor already provisioned in application
``default``, the admin key's): one fund of ``LPB_PROBE_FUND_AMOUNT`` (default 0.01 USD or 0.000005 ETH,
in the allocation's currency; at most 0.05 USD or 0.00002 ETH) sent twice under one ``operation_ref``,
expecting the repeat to be a replay (D57) and the allowance to grow once. It never provisions and runs no
paid job (ISS-25 does). It prints PASS, FAIL or SKIP per check and a summary, revokes the keys it issued,
and exits 1 when a check failed.
"""

import argparse
import asyncio
import os
import secrets
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

APPLICATION = "default"  # the fund actor's application; the allowance is read in the admin key's
SYNC = "/v1/admin/costs/sync"  # a harmless admin write: if a key were wrongly allowed, nothing moves


def probe_app(run: str) -> str:
    """The app the probe's jobs name: this run's own, so no runner on the network advertises it and the job
    fails ``no_offering`` before any payment. The catalog is checked first all the same (#83 review)."""
    return f"lpb-probe/{run}"


DEFAULT_FUND = {"USD": Decimal("0.01"), "ETH": Decimal("0.000005")}
FUND_CAP = {"USD": Decimal("0.05"), "ETH": Decimal("0.00002")}  # "a few cents", per allocation currency


@dataclass(frozen=True)
class ProbeSettings:
    url: str
    admin_key: str
    fund_actor: str | None = None
    fund_amount: str | None = None


def settings_from_env(env: Mapping[str, str]) -> ProbeSettings | None:
    """The stack to probe, or ``None`` when ``LPB_PROBE_URL`` or ``LPB_PROBE_ADMIN_KEY`` is unset."""
    url, key = env.get("LPB_PROBE_URL", "").strip(), env.get("LPB_PROBE_ADMIN_KEY", "").strip()
    if not url or not key:
        return None
    return ProbeSettings(
        url=url.rstrip("/"),
        admin_key=key,
        fund_actor=env.get("LPB_PROBE_FUND_ACTOR", "").strip() or None,
        fund_amount=env.get("LPB_PROBE_FUND_AMOUNT", "").strip() or None,
    )


@dataclass
class Probe:
    client: httpx.AsyncClient
    admin_key: str
    run: str = field(default_factory=lambda: f"lpb-probe-{int(time.time())}-{secrets.token_hex(3)}")
    results: list[tuple[str, str, str]] = field(default_factory=list)  # (PASS|FAIL|SKIP, check, detail)
    issued: list[str] = field(default_factory=list)  # key ids to revoke at the end

    def record(self, verdict: str, name: str, detail: str = "") -> bool:
        self.results.append((verdict, name, detail))
        sys.stdout.write(f"{verdict:<4}  {name}{': ' + detail if detail else ''}\n")
        return verdict == "PASS"

    def expect(self, name: str, answer: httpx.Response, status: int) -> bool:
        verdict = "PASS" if answer.status_code == status else "FAIL"
        return self.record(verdict, name, f"{answer.status_code}, expected {status}")

    async def call(self, method: str, path: str, token: str | None, **kwargs: Any) -> httpx.Response:
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        answer = await self.client.request(method, path, headers=headers, **kwargs)
        key_id = _issued_key_id(answer)
        if key_id is not None:
            self.issued.append(key_id)  # a key issued by mistake is revoked too
        return answer

    async def issue(self, kind: str, actor_id: str) -> tuple[str, str] | None:
        """A new key's (token, key id), or ``None`` when the engine did not issue one."""
        body = {"kind": kind, "actor_id": actor_id, "operation_ref": f"{actor_id}-key"}
        answer = await self.call("POST", "/v1/admin/keys", self.admin_key, json=body)
        token = answer.json().get("token") if answer.status_code == 201 else None
        key_id = _issued_key_id(answer)  # never the previous key's id: a 201 without one is a failure
        if isinstance(token, str) and key_id is not None:
            self.record("PASS", f"issue a key ({kind})", f"{answer.status_code}")
            return token, key_id
        missing = ", no key id" if answer.status_code == 201 and key_id is None else ""
        self.record("FAIL", f"issue a key ({kind})", f"{answer.status_code}{missing}")
        return None


async def run_checks(probe: Probe, *, spend: bool, fund_actor: str | None, fund_amount: str | None) -> None:
    admin = probe.admin_key
    if not probe.expect("the admin key is accepted", await probe.call("GET", "/v1/admin/keys", admin), 200):
        return
    first, second = await probe.issue("actor", f"{probe.run}-a"), await probe.issue("actor", f"{probe.run}-b")
    third = await probe.issue("auditor", f"{probe.run}-auditor")
    if not (first and second and third):
        return
    (a, a_key), (b, _), (auditor, _) = first, second, third
    for method, path in (("GET", "/v1/jobs"), ("GET", "/v1/admin/keys"), ("POST", SYNC)):
        probe.expect(f"no credential: {method} {path}", await probe.call(method, path, None), 401)
    for method, path in (("GET", "/v1/admin/keys"), ("GET", "/v1/admin/actors"), ("POST", SYNC)):
        probe.expect(f"actor key: {method} {path}", await probe.call(method, path, a), 403)
    reads = await probe.call("GET", "/v1/admin/keys", auditor)
    probe.expect("auditor key reads: GET /v1/admin/keys", reads, 200)
    probe.expect(f"auditor key: POST {SYNC}", await probe.call("POST", SYNC, auditor), 403)
    issue = {"actor_id": f"{probe.run}-x", "operation_ref": f"{probe.run}-auditor-issue"}
    issued = await probe.call("POST", "/v1/admin/keys", auditor, json=issue)
    probe.expect("auditor key: POST /v1/admin/keys", issued, 403)
    await cross_actor(probe, a, b)
    await repeated_fund(probe, spend=spend, actor=fund_actor, amount=fund_amount)
    await revoked(probe, a, a_key)


async def cross_actor(probe: Probe, a: str, b: str) -> None:
    app = probe_app(probe.run)
    # A POST /v1/jobs returns only after the runner call, and in per_application mode the shared key pays
    # for any actor: a job for an advertised app is paid on the POST itself. So none is sent then (#83).
    offered = await probe.call("GET", "/v1/offerings", None, params={"app": app})
    listing = offered.json() if offered.status_code == 200 else None
    clear = isinstance(listing, list) and not any(item.get("app") == app for item in listing)
    if not probe.record("PASS" if clear else "FAIL", "no runner advertises the probe's app", app):
        return
    job = {"app": app, "operation_ref": f"{probe.run}-job"}
    first, read = await _unpaid_job(probe, a, job)
    if not probe.record("PASS" if first else "FAIL", "a job is recorded without payment", str(first)):
        return
    second, _ = await _unpaid_job(probe, b, job)
    reused = "PASS" if second and second != first else "FAIL"
    probe.record(reused, "another actor's operation_ref is a new job", str(second))
    assert read is not None
    probe.expect("the owner reads the job", read, 200)
    probe.expect("another actor: GET the job", await probe.call("GET", f"/v1/jobs/{first}", b), 404)
    probe.expect("another actor: GET its cost", await probe.call("GET", f"/v1/jobs/{first}/cost", b), 404)
    listed = await probe.call("GET", "/v1/jobs", b)
    ids = [item.get("id") for item in listed.json()] if listed.status_code == 200 else None
    left_out = "PASS" if ids is not None and first not in ids else "FAIL"
    probe.record(left_out, "another actor's job list leaves it out", f"{listed.status_code}")


async def _unpaid_job(
    probe: Probe, token: str, job: dict[str, str]
) -> tuple[str | None, httpx.Response | None]:
    """Send one probe job and read it back: its id only when it failed ``no_offering`` with nothing paid."""
    job_id = (await probe.call("POST", "/v1/jobs", token, json=job)).headers.get("livepeer-job-id")
    read = await probe.call("GET", f"/v1/jobs/{job_id}", token) if job_id else None
    record = read.json() if read is not None and read.status_code == 200 else {}
    failure, attempts = record.get("failure") or {}, record.get("attempts") or []
    unpaid = record.get("state") == "failed" and failure.get("kind") == "no_offering"
    unpaid = (
        unpaid and not failure.get("payment_sent") and not any(item.get("payment_sent") for item in attempts)
    )
    return (job_id if unpaid else None), read


async def repeated_fund(probe: Probe, *, spend: bool, actor: str | None, amount: str | None) -> None:
    name = "a fund repeated under one operation_ref applies once"
    if not spend:
        probe.record("SKIP", name, "pass --yes to spend one fund of a few cents")
        return
    if actor is None:
        probe.record("SKIP", name, "set LPB_PROBE_FUND_ACTOR to an actor already provisioned")
        return
    before = await _granted(probe, actor)
    if before is None:
        probe.record("FAIL", name, f"could not read {actor!r}'s allowance")
        return
    chosen = fund_amount(before[1], amount)
    if chosen is None:
        cap = FUND_CAP.get(before[1], 0)
        probe.record("FAIL", name, f"refused to spend: not a number above 0 and at most {cap} {before[1]}")
        return
    path = f"/v1/admin/applications/{APPLICATION}/actors/{actor}/allocation/fund"
    body = {"amount": {"amount": str(chosen), "currency": before[1]}, "operation_ref": f"{probe.run}-fund"}
    first = await probe.call("POST", path, probe.admin_key, json=body)
    again = await probe.call("POST", path, probe.admin_key, json=body)
    probe.expect("fund", first, 200)
    probe.record(
        "PASS" if again.status_code == 200 and again.headers.get("idempotent-replayed") == "true" else "FAIL",
        "the repeat is a replay (Idempotent-Replayed: true)",
        f"{again.status_code}",
    )
    after = await _granted(probe, actor)
    grew = None if after is None else after[0] - before[0]
    probe.record("PASS" if grew == chosen else "FAIL", name, f"granted grew by {grew}, expected {chosen}")


def fund_amount(currency: str, configured: str | None) -> Decimal | None:
    """The amount to fund: the configured one if it is a number within the cap, else ``None``."""
    if currency not in FUND_CAP:
        return None
    try:
        amount = Decimal(configured) if configured else DEFAULT_FUND[currency]
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and 0 < amount <= FUND_CAP[currency] else None


async def revoked(probe: Probe, token: str, key_id: str) -> None:
    answer = await probe.call("POST", f"/v1/admin/keys/{key_id}/revoke", probe.admin_key)
    if probe.expect("revoke the actor key", answer, 204):
        probe.issued.remove(key_id)
    probe.expect("a revoked key: GET /v1/jobs", await probe.call("GET", "/v1/jobs", token), 401)


async def cleanup(probe: Probe) -> None:
    """Revoke every key the probe still holds; one that fails does not stop the next."""
    left = []
    for key in list(probe.issued):
        try:
            answer = await probe.call("POST", f"/v1/admin/keys/{key}/revoke", probe.admin_key)
        except httpx.HTTPError:
            answer = None
        if answer is None or answer.status_code != 204:
            left.append(key)
    if probe.issued:
        probe.record("FAIL" if left else "PASS", "revoke the probe's other keys", ", ".join(left))


async def probe(
    settings: ProbeSettings, *, spend: bool, client: httpx.AsyncClient | None = None, run: str | None = None
) -> int:
    """Run every check against ``settings.url`` (or ``client``); 1 when any check failed."""
    own = client is None
    http = client or httpx.AsyncClient(base_url=settings.url, timeout=30.0)
    checks = Probe(http, settings.admin_key) if run is None else Probe(http, settings.admin_key, run=run)
    try:
        try:
            await run_checks(
                checks, spend=spend, fund_actor=settings.fund_actor, fund_amount=settings.fund_amount
            )
        except httpx.HTTPError as exc:
            checks.record("FAIL", "the engine answered", type(exc).__name__)
        except ValueError as exc:  # a body that is not what the contract says (JSON included)
            checks.record("FAIL", "the engine answered as the contract says", type(exc).__name__)
        finally:
            await cleanup(checks)  # whatever broke, the probe's keys are revoked (#83)
    finally:
        if own:
            await http.aclose()
    counts = {
        verdict: sum(result[0] == verdict for result in checks.results)
        for verdict in ("PASS", "FAIL", "SKIP")
    }
    sys.stdout.write(
        f"live probe {checks.run} against {settings.url}: "
        f"{counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped\n"
    )
    return 1 if counts["FAIL"] else 0


def _issued_key_id(answer: httpx.Response) -> str | None:
    issued = answer.request.method == "POST" and answer.url.path.endswith("/v1/admin/keys")
    if not issued or answer.status_code != 201:
        return None
    key = answer.json().get("key")
    return key.get("key_id") if isinstance(key, dict) else None


async def _granted(probe: Probe, actor: str) -> tuple[Decimal, str] | None:
    answer = await probe.call("GET", "/v1/allowance", probe.admin_key, params={"actor_id": actor})
    granted = answer.json().get("granted") if answer.status_code == 200 else None
    if not isinstance(granted, dict):
        return None
    return Decimal(str(granted["amount"])), str(granted["currency"])


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    parser.add_argument("--yes", action="store_true", help="spend: one fund of a few cents, sent twice")
    args = parser.parse_args(argv)
    settings = settings_from_env(os.environ if env is None else env)
    if settings is None:
        sys.stdout.write("live probe skipped: set LPB_PROBE_URL and LPB_PROBE_ADMIN_KEY to name a stack\n")
        return 0
    return asyncio.run(probe(settings, spend=args.yes))


if __name__ == "__main__":
    raise SystemExit(main())
