# JWT bearer: OAuth access tokens in the REST service, chosen by configuration

## Status

- **Owner:** Mike.
- **Ticket:** [#49](https://github.com/NE-Build-Track/livepeer-builder-core/issues/49), part of ISS-33 (M4). The mode
  was defined in M2 (D13, D14; [access.md#modes](../../contracts/access.md#modes)).
- **Branch:** `tasks/mz/jwt-bearer`, on top of the ISS-08 branch (`tasks/mz/iss-08-credentials`, #48) at `a2d115d`.
- **Progress:** decisions locked 26 October (D45); built, shown live, and in review.
- **Source:** Mike's recommendation of 26 October (`2026-10-26-builder-core-oauth-recommendation.md`).
- **Decision:** D45.

## Goal

An enterprise points the REST service at its identity provider and the engine accepts that provider's
access tokens, with no code. Configuration chooses one of three modes: API keys only (today), JWT only
(replacing keys), or both (for a migration). The engine stays a resource server: it verifies tokens and
never runs a login.

## Acceptance

- [x] `JwtBearerAuthenticator` (`adapters/jwt.py`):
  - verifies the signature against the issuer's JWKS, cached, and fetched again (rate-limited) for an
    unknown `kid`;
  - allows only the configured asymmetric algorithms; never `none` or `HS*`;
  - checks `iss` exactly, `aud` (the engine's audience), and `exp` and `nbf` with a leeway; requires
    the actor claim;
  - follows the chain rule: `None` for a token that is not a JWT or comes from another issuer;
    `NotAuthenticated` for an invalid token from this issuer; the provider's keys unreachable is
    `ProviderUnavailable` (503).
- [x] Claims map to `ActorContext`:
  - `actor_id` from the actor claim (`sub`);
  - `application_id` from a claim or a fixed value;
  - permissions from scopes named after operations (with an optional prefix), never `actors.assert`;
  - `credential_kind="admin"` only with the configured admin scope;
  - `auth` set to `AuthProvenance(method="jwt", issuer, credential_id=jti)`;
  - the token itself is never kept or logged.
- [x] Settings: `LPB_AUTH` (`api_keys`, `jwt`, or `jwt,api_keys`) and `LPB_JWT_*`, validated at
  startup. `default_parts` builds the authenticator and owns its HTTP client. `jwt` alone sets
  `api_keys=None`, so the key routes and `lpb bootstrap-admin` disappear. A supplied
  `EngineSupply.authenticators` still wins.
- [x] OpenAPI declares a bearer scheme when JWT is on without API keys, so Swagger can send a token.
- [x] Tests, written first: tokens signed in the test with a local RSA key, the JWKS served by an
  injected HTTP transport, and no network.
- [x] Docs:
  - `access.md`: the mode is implemented; settings, claim mapping, revocation guidance;
  - `RELIABILITY.md` settings;
  - the extension guide;
  - D45;
  - generated docs.
- [x] Live: `lpb serve` with `LPB_AUTH=jwt`, and with `jwt,api_keys`, against a local stand-in identity
  provider (a JWKS and discovery document served from the test machine).

## Design

### Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `LPB_AUTH` | `api_keys` | `api_keys`, `jwt`, or `jwt,api_keys`. Order does not matter: each authenticator recognizes only its own credential |
| `LPB_JWT_ISSUER` | unset | Required for `jwt`. Matched exactly against `iss`; its `/.well-known/openid-configuration` gives the JWKS URL |
| `LPB_JWT_JWKS_URL` | unset | Overrides discovery |
| `LPB_JWT_AUDIENCE` | unset | Required for `jwt`. Must be in `aud` (RFC 8707 resource) |
| `LPB_JWT_ALGORITHMS` | `RS256,ES256` | Allowed signature algorithms; only asymmetric ones are accepted |
| `LPB_JWT_ACTOR_CLAIM` | `sub` | Becomes `actor_id` |
| `LPB_JWT_APPLICATION_CLAIM` | unset | Becomes `application_id` when set; otherwise `LPB_JWT_APPLICATION_ID` |
| `LPB_JWT_APPLICATION_ID` | `default` | The application of every token without an application claim |
| `LPB_JWT_SCOPE_CLAIM` | `scope` | A space-separated string (RFC 9068) or a list (`scp`) |
| `LPB_JWT_SCOPE_PREFIX` | empty | Stripped before matching scopes to operations, e.g. `api://engine/` |
| `LPB_JWT_ADMIN_SCOPE` | unset | The scope that makes an admin credential; unset, no token is admin |
| `LPB_JWT_LEEWAY_S` | `30` | Clock skew allowed on `exp` and `nbf` |

### The authenticator

- **Recognizing a token:** `Authorization: Bearer <three base64url parts>` whose unverified `iss` is the
  configured issuer. Anything else returns `None`, so `lpk_` keys and other authenticators still work.
- **Keys:** `JwksCache` fetches the JWKS through `httpx`, finding it from the issuer's discovery
  document and checking that document's `issuer`. It keeps the keys for an hour and fetches again, at
  most once a minute, for an unknown `kid`. A token without `kid` is accepted only when the set has
  exactly one key.
- **Claims:** a token without `exp`, `iss`, `aud` or the actor claim is refused. Permissions are the
  scopes that name an operation, after the prefix. `actors.assert` is never granted: it belongs to the
  trusted-backend credential (D14).

### REST

`actor_dependency` declares a bearer scheme in OpenAPI when engine keys are on (as today, `lpk_`) or,
with keys off, when the authenticator chain announces a `Bearer` challenge. That covers JWT, and stays
silent for an authenticator with another scheme. `AuthenticatorChain.challenge` drops repeated values.

## Out of scope

- Opaque tokens through RFC 7662 introspection: a second implementation of the same interface, when an
  enterprise needs it.
- RFC 9728 protected-resource metadata and its `WWW-Authenticate` pointer (MCP clients): phase 2.
- The trusted-backend authenticator (`ServiceCredentialAuthenticator`, D14).
- Moving the console from an `lpk_` admin key to a client-credentials token.

## Progress

- 26 October: issue #49 opened; plan written; decisions locked (D45); branch started.
- 26 October: built test-first.
  - **Tests:** `tests/unit/test_jwt.py` (the authenticator, 33) and `test_jwt_service.py` (settings,
    wiring and REST against a local aiohttp identity provider, 19).
  - **Mutations,** each caught: no algorithm allow-list, no issuer check, no refetch rate limit, and
    granting every operation.
  - **Local CI:** 789 passed; coverage floors, ruff, mypy, `check_docs`, `gen-docs --check` and
    `lpb smoke` are clean.
  - **Live, 14/14:** `lpb serve` against a stand-in identity provider on the test machine.
    - With `LPB_AUTH=jwt`: tokens accepted; expired and wrong-audience tokens 401; a missing scope 403;
      the admin scope reaches admin routes; the key routes gone; OpenAPI declares a JWT bearer scheme;
      `lpb bootstrap-admin` refused.
    - With `jwt,api_keys`: both credentials work, and the challenge is announced once.
    - No token in the engine log.

- 26 October: PR #50 review (Cursor), each item fixed test-first:
  - **Refused:** a sender-constrained token (`cnf`).
  - **Key URLs:** a discovered key URL is kept only after it served usable keys, so it is discovered again
    when it fails or moves. A `jwks_uri` must be http(s).
  - **Refetches:** only a refetch that worked uses up the minute. A failed fetch pauses the provider for
    5 s, answering 503.
  - **The 503 body** is a fixed message; the URL and client error are logged (`auth.jwks_unavailable`).
  - **Signing keys only:** `use: enc`, or `key_ops` without `verify`, is skipped.
  - **Algorithms** (found while checking): a key's published `alg` must match the token's.
  - **Mutations,** each caught: cnf accepted, a bad discovered URL kept, no refetch limit, encryption keys
    allowed, a key's `alg` ignored, no pause after a failure. The first version of the URL test missed one,
    so a test for a URL that worked and then moved was added.

## Decision log

- **26 October (Mike, D45, from his recommendation):**
  1. **Implementation:** build the defined `JwtBearerAuthenticator` and choose it with `LPB_AUTH`.
  2. **Admin:** comes from a configured scope (`LPB_JWT_ADMIN_SCOPE`); with keys off there is no
     bootstrap command.
  3. **Migration:** run `jwt,api_keys` until the console moves to a client-credentials token.
  4. **Tokens:** JWT only; no introspection.
  5. **Revocation:** a JWT cannot be revoked before it expires, so the guidance is short-lived access
     tokens (5 to 15 minutes).
  6. **Library:** `PyJWT[crypto]`, confined to `adapters/jwt.py`, which also joins the `httpx` row.
