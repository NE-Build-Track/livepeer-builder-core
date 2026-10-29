# ISS-33: Demonstrate replacement of the reference application's access flow

**Status:** proposal for discussion, 29 October 2026. Nothing here is decided until it is locked; the
console's locked rules go to its `docs/design-docs/decisions.md`, engine rules to builder-core's decisions.\
**Tracker:** ISS-33 ([Network-Engineering-SPE#47](https://github.com/Cloud-SPE/Network-Engineering-SPE/issues/47)),
home **APP** (the builder console), board status *In progress*.\
**Owner:** Mike. **Reviewers:** Mike, John.\
**Verified against:** builder console `main` at `938db3e` (the merge of PR #7, `tasks/mz/keycloak-oidc`);
builder-core `releases/m3` at `d3df806`.

## Done when

- The reference app connects to the **alternative enterprise-auth example** through the supported access
  contract, **without changing shared core source** and **without requiring its default enrollment flow**.
- **Catalog and rate access** and **isolation** are verified under that configuration. One reference
  login stays an example, not an enterprise policy or a promise to support every identity provider.
- **Depends on** ISS-21 (the console's access and live catalog, M3) and ISS-32 (the enterprise-adapter
  proofs).

## Summary

The replacement is **built and was shown live on 28 October**; what is missing is **repeatable,
committed verification** of catalog access and isolation, an **example-not-policy** write-up, and three
engine follow-ups that are risks rather than blockers. No engine code change is required.

## Current state (verified)

### The default flow being replaced

Keys mode (`CONSOLE_AUTH=keys`, the console's default, decision D16): an operator issues `lpk_` keys
with `POST /v1/admin/keys` (enrollment), people paste them, and the background reads hold an engine
admin key (console gap G8).

### What is done

| Side | What | Evidence |
| --- | --- | --- |
| Engine | JWT bearer resource server chosen by `LPB_AUTH` (D45; #49, PR #50 merged 27 October): JWKS, asymmetric algorithms, `iss`/`aud`/`exp`/`nbf`, RFC 9068 `at+jwt` by default, scopes → operations, admin and read-only admin by scope | `adapters/jwt.py`, `config.py:75-92`; [access.md](../../contracts/access.md#jwt-bearer) |
| Engine | Hardening D47, D49, D50 (PR #57 merged 27 October) | `m3-decisions.md` |
| Engine | Actor list and auditor (D46), people registered on first token (D48), merged as #69 into `d3df806` | `services/sightings.py`, `GET /v1/admin/actors` |
| Engine | `LPB_AUTH=jwt` removes the key routes and `bootstrap-admin` | `http/app.py:299` (`if engine.keys is not None`) |
| Console | **D21**, `CONSOLE_AUTH=oidc`: a confidential OpenID Connect client (`openid-client`, confined to `web/src/adapters/oidc/provider.ts`), authorization code + PKCE on the server, tokens sealed in the area's cookie, refreshed by `web/src/proxy.ts`, RP-initiated logout with `id_token_hint` | `web/src/ports/identity.ts`, `web/src/services/signin.ts` (`classifyPerson`) |
| Console | Operator area needs `livepeer:admin`; Builder needs `jobs.run`; the builder is the actor its `sub` names; background reads use `console-reader` (client credentials, an engine auditor), so no admin key sits in the console | D21; `deploy/keycloak/compose.yaml` |
| Stack | `deploy/keycloak/`: Keycloak 26.8.1, realm `livepeer` (users `operator`, `alice`, `bob`, `carol`; roles `operator`, `builder`, `reader`; role-gated scopes; `at+jwt`; audience `https://engine.builder.local`; pinned user ids); the **stock engine image `d3df806`** with `LPB_AUTH=jwt`, `LPB_JWT_ADMIN_SCOPE=livepeer:admin`, `LPB_JWT_ADMIN_READ_SCOPE=livepeer:read` | `deploy/keycloak/README.md`, `realm-livepeer.json` |
| CI | Both compose files parse; `pins.test.ts` checks the Keycloak stack builds every component from its pin and from no fork | console `.github/workflows/ci.yml`, `web/tests/structure/pins.test.ts` |

**No core source changed:** the stack runs the stock engine image configured by environment only (no
`supply`, `decorate` or `extend`), which is the strongest form of the first criterion.

### Gaps

| # | Gap | Evidence |
| --- | --- | --- |
| A1 | **The live verification is not repeatable.** PR #7 reports an engine probe (14/14), browser checks (17/17), refresh (4/4), a paid run ($0.000111) and first-sign-in registration (2/2), but no probe script or evidence file is committed: `docs/references/evidence/` holds only `2026-10-20-demo-traffic.md`, `scripts/` only `build-images.sh` and `demo-traffic.py` | console tree at `938db3e` |
| A2 | **One application only.** The stack sets no `LPB_JWT_APPLICATION_CLAIM`, so every token lands in `default` (`config.py:80-81`); isolation between applications is not exercised | `deploy/keycloak/compose.yaml` |
| A3 | **Catalog and rates under JWT are not recorded.** `GET /v1/offerings` is public (`app.py:206-209`), `GET /v1/runners` needs `discover`, which the builder scope set includes; rates are wei only (console gap G14; the ISS-10 guide adds USD) | console `deploy/keycloak/README.md` |
| A4 | **"An example, not a policy" is not written down** for enterprises: which engine and console settings replace the flow, and what any other OIDC provider must issue | not found |
| A5 | **Admin by scope relies on a denylist.** #58 (dedicated namespace or admin claim) was closed on 28 October with no comment and no code; `config.py:145-153` still checks a denylist, and `access.md` still calls it "a follow-up, not built" (T19). The realm mitigates it by granting `livepeer:admin` only to the `operator` role | `gh issue view 58`; `access.md` |
| A6 | **Client guidance is open:** #51 (public and confidential OIDC clients around the engine) is an open docs suggestion | `gh issue view 51` |
| A7 | **Small doc drift:** console D21 still says #69 is "not merged"; builder-core `docs/contracts/index.md` still lists JWT as "defined (M4)" | both files |

## Engine versus app

| Concern | Owner | Where |
| --- | --- | --- |
| People, roles, scopes, audience, token type and lifetimes | Identity provider (Keycloak here, any OIDC provider in general) | `deploy/keycloak/realm-livepeer.json` |
| Login, PKCE, state and nonce, cookies, refresh, logout, area by scope | **App** (console as a confidential client and backend-for-frontend) | `web/src/adapters/oidc/`, `proxy.ts`, `services/signin.ts` |
| Token verification, claims → `ActorContext`, admin and auditor scopes | **Engine** (resource server) | `adapters/jwt.py` |
| Permissions, ownership, the 404 answer rule | **Engine** | `services/policy.py`, `services/ownership.py` |
| Registering people as actors; provisioning an allocation for them | **Engine** (D48), driven by the operator in the app | `services/sightings.py`, `services/payments.py` |
| Retail identity, invitations, sign-up | **App**; the engine never enrolls end users ([access.md](../../contracts/access.md#boundaries)) | console, enterprise apps |

Identity is a generic subdomain: the enterprise buys it and the engine **conforms** to standard access
tokens (no identity logic of its own beyond claim mapping). The console relays the person's token to
the engine (access.md service mode **A**), so it needs no trusted-backend credential (mode **B**, ISS-32).

## Decision proposed

1. **Close ISS-33 on the JWT configuration that exists**, with the evidence below. Treat ISS-32's
   dependency as met by D45 (the "enterprise adapter" chosen by configuration); ISS-32's supplied
   authenticator and trusted backend are separate proofs that ISS-33 does not wait for (Q1).
2. **A repeatable access probe in the console repository**, `scripts/verify-access.py` (Python, like
   `demo-traffic.py`). Against the running Keycloak stack it signs in as each realm user through the real
   authorization-code + PKCE flow (posting Keycloak's login form with `DEMO_PASSWORD`; no direct-access
   grants are enabled in the realm, and none are added), gets `console-reader`'s token by client
   credentials, then calls the **engine directly** with each token and writes a pass/fail matrix to
   `docs/references/evidence/<date>-oidc-access.md`. It spends nothing: an unprovisioned builder's job is
   recorded as `failed`/`not_provisioned`, which is enough for the cross-actor checks.
3. **A second application in the realm** (Q2): a user attribute mapped to a token claim (for example
   `application`), `LPB_JWT_APPLICATION_CLAIM` set on the engine, and one user (`dave`) in another
   application, so cross-application isolation is shown, not assumed.
4. **Write the replacement up as an example**: a console guide `docs/guides/enterprise-sign-in.md` with
   the two settings blocks (engine `LPB_AUTH`, `LPB_JWT_*`; console `CONSOLE_AUTH`, `CONSOLE_OIDC_*`,
   `CONSOLE_READER_*`) and what any provider must issue (an `https` issuer, `aud` = the engine,
   `at+jwt` with `client_id`, `jti`, `iat`, or the `LPB_JWT_TOKEN_TYPES` opt-out; role-gated admin
   scopes; a lifetime under `LPB_JWT_MAX_LIFETIME_S`). builder-core lands #51 as the client-shapes part
   of `access.md`'s appendix.
5. **Engine follow-ups, not blockers:** decide T19 explicitly (reopen #58 or record why the denylist plus
   role-gated scopes is accepted); fix the stale `contracts/index.md` row; `GET /v1/me` (ISS-29) lets the
   console show the engine's view of the person instead of the ID token's.

## Code areas

| Repository · file | Change |
| --- | --- |
| console `scripts/verify-access.py` (new) | the probe and its evidence writer |
| console `deploy/keycloak/realm-livepeer.json`, `compose.yaml`, `README.md` | `dave`, the application claim mapper, `LPB_JWT_APPLICATION_CLAIM` |
| console `docs/guides/enterprise-sign-in.md` (new), `docs/design-docs/decisions.md` (D21 text), `docs/product-specs/m3-journey.md` (the M4 access row) | the example write-up |
| console `docs/references/evidence/<date>-oidc-access.md` (new) | the recorded run |
| builder-core `docs/contracts/access.md`, `docs/contracts/index.md` | #51 client shapes; JWT row; T19 outcome |

## Test plan (test-driven)

**Levels.** The probe is an acceptance test at the HTTP level against the live stack (the predictive
layer: real Keycloak, real engine image, real tokens). The console's own sign-in logic keeps its unit
tests (`web/src/services/signin.test.ts`, `web/src/domain/session.test.ts`). **Seams:** none mocked; the
probe asserts on engine answers only. Write each check first against the expectation, run it, and see it
fail for the right reason by pointing it at a deliberately wrong token or user before trusting a pass.

**Starter test:** with no token, `GET /v1/offerings` is 200 and `GET /v1/runners?app=…` is 401 with a
`Bearer` challenge.

```text
Catalog and rates
[ ] no token: offerings 200 with rates; runners 401 with WWW-Authenticate: Bearer
[ ] alice's token: runners 200 for a catalog app, each with its rate
Replacement of enrollment
[ ] operator's token: /v1/admin/keys answers 404 (routes absent under LPB_AUTH=jwt)
[ ] an lpk_ key answers 401
[ ] an ID token used as a bearer answers 401 (typ JWT, no engine audience)
[ ] carol's first token lists her in /v1/admin/actors, provisioned=false (D48)
Isolation
[ ] alice runs a job while unprovisioned: 402 not_provisioned, the job is recorded
[ ] alice reads her own job: 200
[ ] bob reads alice's job: 404, never 403
[ ] alice calls GET /v1/admin/actors: 403
[ ] alice asking Keycloak for livepeer:admin does not receive it
[ ] console-reader reads /v1/admin/actors (200) and is refused provisioning (403)
[?] dave in another application reads alice's job: 404           (Q2)
[?] console-reader lists only its own application's actors      (Q2)
Paid path (optional, spends money)
[ ] operator provisions carol; carol's chat job succeeds; its cost is observed
Documentation
[ ] the guide names every setting the stack uses (a console docs test compares it with compose.yaml)
```

**Definition of done.** Every item `[x]` or `[-]` with a reason and every `[?]` answered; the evidence
file committed with the pins it ran against; console `make check` green; builder-core `check_docs.py`
green after the `access.md` and `index.md` edits.

## Upstream dependencies

| Upstream | Needed? | Why |
| --- | --- | --- |
| livepeer/node | No | The signer never sees the person's token; it signs for the engine's allocation key |
| livepeer-python-gateway | No | Not on the access path |
| clearinghouse-batteries | No | Only the optional paid run touches it, unchanged |

Keycloak is a third-party product pinned at 26.8.1, used as an example only.

## Consumer impact

| Consumer | Change |
| --- | --- |
| Builder console | The probe, the realm change, the guide (this ticket) |
| Enterprise desk | None here; its user keys are ISS-32's trusted-backend case |
| ChatLPT, inference-gateway | None: embedded, they build `ActorContext` themselves |

## Alternatives considered

- **Enable direct-access (password) grants for a test client.** Simpler probe, but it adds a grant the
  example should not teach; posting the login form exercises the real flow.
- **A browser test (Playwright) in CI.** Closer to the user, but it needs the whole paid stack in CI;
  the HTTP probe covers the engine's answers, and the console's flow has unit tests.
- **A second identity provider.** Not required ("not a mandate to implement every identity provider");
  the guide states the provider requirements instead.

## Open questions to lock

- **Q1.** Close ISS-33 on the JWT configuration now (recommended), or wait for ISS-32's examples?
- **Q2.** Add a second application to the realm (recommended, small), or record single-application as
  the reference configuration?
- **Q3.** Run the probe in console CI with Keycloak and an engine but no payment stack, or by hand per
  release with the evidence committed (recommended for M4)?
- **Q4.** T19: reopen #58 before M4 ends, or accept the denylist plus role-gated scopes with a decision?

## Delivery slices

1. The probe and the first evidence file on the current realm (A1, A3).
2. The second application and its isolation checks (A2).
3. The guide, D21 text fix, `m3-journey.md` row; builder-core `access.md` (#51) and `index.md` (A4, A6, A7).
4. The T19 decision (A5).
