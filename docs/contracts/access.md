# Access contract

**Tickets:** ISS-03, ISS-08, #49 (ISS-33), #165 (ISS-31) · **Design accepted:** 12 October 2026 (decisions D12–D16);
ISS-08 on 26 October (D44); JWT bearer on 26 October (D45); the tenant admin on 18 November (D83) · **Code:** `contracts/access.py`, `protocols/`,
`services/policy.py`, `services/ownership.py`, `services/keys.py`, `adapters/auth.py`, `adapters/jwt.py`,
`adapters/secrets.py`

The core never authenticates anyone. It consumes a validated `ActorContext` and asks an
`AccessPolicy` whether an operation is allowed. Everything about *how* identity is established lives
in adapters.

## Actor context

| Field | Meaning |
| --- | --- |
| `actor_id` | Opaque to the engine; the owner of jobs, costs and allocations |
| `tenant_id` | Tenant isolation; defaults to `default`. A tenant's id is its grant's id ([payments.md](payments.md#tenants)) |
| `permissions` | `Operation` values (see below) |
| `credential_kind` | `actor`, `clearinghouse_admin` (an admin of every tenant), `tenant_admin` (an admin of one tenant, its own: D83), `auditor` (a read-only admin of one tenant, D46) or `service` |
| `attributes` | Non-secret labels copied onto records. **Never secrets** |
| `auth` | Provenance (method, issuer, credential id), for audit only |

**Operations:** `discover`, `jobs.run`, `jobs.read`, `costs.read`, `events.read`, `admin.keys`,
`admin.allocations`, `admin.costs`, `admin.read`, `admin.tenants`, `actors.assert`. `admin.read` is every
admin read of one tenant: the key list and key inspection, the actor list and one actor's allocation,
the tenant's grant, and reconciliation. The other `admin.*` operations are the writes.
`admin.tenants` (#165) is what only a clearinghouse admin does to a tenant: onboard it, list every
tenant, top up its grant, pause or resume it. The tenant list is every tenant's grant, so no tenant
admin and no auditor reads it.

**Where the admin routes are.** The routes are two sets (D84, #164). The clearinghouse set, under
`/v1/clearinghouse/tenants`, has tenants and their grants, a tenant's actors and their allocations, and a
tenant's access keys ([payments.md](payments.md#tenants)). The work set has
three groups of admin routes beside the routes every actor calls: an engine's own access keys
(`/v1/keys`), reconciliation (`GET /v1/costs/reconciliation`) and cost sync (`POST /v1/costs/sync`). The
set a route is in decides nothing about access: each route needs the operation it needed before.

## Protocols

```python
class Authenticator(Protocol):
    async def authenticate(self, request: AuthRequest) -> ActorContext | None: ...
    def challenge(self) -> str | None: ...


class AccessPolicy(Protocol):
    def check(
        self, actor: ActorContext, operation: Operation, resource: ResourceRef | None = None
    ) -> None: ...
```

- An `AuthRequest` carries the method, path, lower-cased headers and client address. That is enough
  for any scheme (bearer, cookies, Basic, mTLS) without changing core.
- `authenticate` has three outcomes:
  - `None`: "not my credential"; the chain tries the next authenticator.
  - An `ActorContext`: a valid credential.
  - `NotAuthenticated`: "my credential, but invalid"; the chain **stops** with 401 and never falls
    through.
- `AuthenticatorChain` runs the deployment's own authenticators (`EngineSupply.authenticators`) in
  order, then `ApiKeyAuthenticator` when engine API keys are on, and combines their challenges into one
  `WWW-Authenticate` header.
- **A bearer longer than 16 KiB is refused first (#113, C22).** The chain answers `NotAuthenticated`
  (401) before any authenticator sees an `Authorization: Bearer` credential longer than 16,384
  characters, so it also protects the API-key path and a deployment's own authenticators. It is counted
  on the whole header (16,384 characters and `Bearer `), whatever the scheme and however it is spelled,
  because a supplied authenticator may read as a bearer what the engine does not (a tab after `Bearer`). The cap is a
  fixed constant (`adapters/auth.py`, `MAX_BEARER_LENGTH`), not a setting.
- **Imported mode needs no authenticator.** The application builds `ActorContext` itself. The policy
  still applies.

## DefaultPolicy

- **Admin operations** (`admin.*`) need an **admin credential**: one of kind `clearinghouse_admin` or
  `tenant_admin`. A permission string on an actor key is not enough.
- **A clearinghouse admin credential** may act on any tenant's records.
- **A tenant admin credential** (D83, #165) is an admin of its own tenant and of no other. It divides
  its tenant's grant and cannot enlarge it:
  - **In its own tenant** it does what an admin does: allot, top up, pause and revoke allocations,
    rotate payment keys, issue and manage keys that are not admin keys, read its grant, its actors and
    their allocations, its usage and the clearinghouse's runner list (#166), and, on the work set, read
    every actor's jobs and costs and reconcile.
  - **Another tenant** is `AccessDenied`, whatever the operation: 403 for a tenant a route or a body
    names, before any lookup or provider call. Another tenant's record found by its id alone (a job, a
    key under `/v1/keys`) reads as missing, as for anyone ([Answers](#answers)).
  - **Never, in any tenant:** `admin.tenants` (onboard a tenant, list tenants, top up a grant, set a
    tenant's status); cost sync (`admin.costs`), which reads every tenant's usage; `actors.assert`; and
    issuing, rotating or revoking a `tenant_admin` or `clearinghouse_admin` key, its own included.
  - **It is confined by the policy, not by each route.** Every write passes the tenant it acts on to the
    policy as the resource (`ResourceRef.tenant_id`), and a key command passes the key's kind
    (`ResourceRef.credential_kind`). A write that names no tenant is refused to a tenant admin, so a
    command that forgot to name one fails closed.
  - In this release a tenant admin credential is an engine key of kind `tenant_admin`. Mapping an
    identity provider's role to it is ISS-33.
- **An auditor credential** (D46) only reads: `admin.read`, `discover`, `jobs.read`, `costs.read` and
  `events.read`, across every actor of its own tenant. Its tenant's usage and the clearinghouse's runner
  list are `admin.read` (#166). Every write is `AccessDenied`, and so is any
  record of another tenant. It is for a dashboard or an audit reader that must not move money. An
  engine key can be issued as `auditor`, and a JWT is one with `LPB_JWT_ADMIN_READ_SCOPE`.
- **Everyone else** needs the operation's permission, and may touch only resources owned by their own
  `(tenant_id, actor_id)`.
- **Another actor's record** reads as missing (404), never 403, so its existence isn't revealed. A
  missing permission is 403. The order of the two checks is in [Answers](#answers).

**Public reads (D51, proposed).** Health (`/livez`, `/readyz`) and the offerings catalog
(`GET /v1/offerings`, `discovery.offerings`) take no actor and check no permission. The handler
returns the full `Offering`: modes, the rate range, `runner_count` and `ready_count`. It has no
runner URL, starts no work and spends nothing from an allocation. Pending Mike's agreement
([#5](https://github.com/NE-Build-Track/livepeer-builder-core/issues/5)). `discover` gates only
`GET /v1/runners` (`discovery.runners`), which returns runner URLs.

## Answers

Every route on a job or its cost answers the same way (D44, `services/ownership.py`):

1. **The permission first, before any lookup.** A caller without the operation's permission gets
   `AccessDenied` (403), whether or not the job exists, so the answer says nothing about any job.
2. **Then ownership.** Another actor's or tenant's job, and a job that does not exist, are both
   `NotFound` (404).

**An id no record can hold is a 422 (#113, C26).** Every id a route reads from its path or query string
(a tenant, actor or key id, `app`, a filter such as `actor_id`) is stored text: at most 200
characters and no NUL, as every record keeps it. A longer one, or one holding a NUL, is a 422 when the
request is parsed, after authentication (401) and before the permission check and any query, as a job id
that is not a UUID already was. Such a lookup used to answer 404 (an empty 200 for a list filter or the
catalog, 400 for a key's rotate or revoke), and a NUL was a 500 on PostgreSQL. `tests/structure/test_route_params.py` fails when a
route declares a plain string path or query parameter.

A time in a query string (`before` on `GET /v1/jobs`, `start` and `end` on `GET /v1/costs/summary`) is
converted to UTC when the request is parsed. One that UTC cannot hold, because its offset puts it before
year 1 or after year 9999 (`0001-01-01T00:00:00+14:00`), is a 422 at the same point; it was a 500. The
same test fails when a route declares a plain `datetime` query parameter.

A summary with no `start` starts 30 days before its `end`. An `end` before `0001-01-31T00:00:00Z` has no
such start, so with no `start` it is a 400 that names `end` (`InvalidRequest` in process), after the
permission check; it was a 500 (#158). The same `end` with its own `start` is read as before.

**A 422 repeats the input it refused, as JSON every client reads.** Its body is FastAPI's: `detail` lists
each refused field with its `loc`, `msg`, `type` and `input`. Two inputs cannot be repeated as they came.
A lone surrogate is written as the escape it came in as (`"\ud800"`; #113, C26). A `NaN` or an infinity,
which the engine's JSON reader takes as a number and JSON has no number for, is written as a string of its
token: `"NaN"`, `"Infinity"` or `"-Infinity"` (#158). `POST /v1/jobs` with
`{"app":"example/echo","timeout_s":NaN}` answers 422 with `"input":"NaN"`; it was a 500, because the 422
could not be written. Every other 422 is byte for byte what FastAPI sends.

| Route | Permission |
| --- | --- |
| `GET /v1/jobs/{id}`, `GET /v1/jobs/{id}/result` | `jobs.read` |
| `GET /v1/jobs` | `jobs.read`. An actor lists its own jobs; an admin or an auditor lists every actor's jobs in its tenant. Newest first, paged with `before` ([job history](engine.md#job-history)) |
| `POST /v1/jobs/{id}/usage` | `jobs.run` (not `jobs.read`) |
| `POST /v1/jobs` (including `mode=async`, which then answers 501) | `jobs.run` |
| `GET /v1/jobs/{id}/cost` | `costs.read` |
| `GET /v1/costs/projections`, `GET /v1/costs/summary`, `GET /v1/allowance` | `costs.read`. An admin's or an auditor's projections and summary cover every actor in its tenant; summary and allowance take `?actor_id=` to name one |
| `GET /v1/clearinghouse/tenants/{tenant}/actors`, `GET /v1/clearinghouse/tenants/{tenant}/actors/{actor}/allocation` | `admin.read`. Every known actor of the tenant (`KnownActor`): the allotted ones and the people an identity provider signed in (D48), each with `allotted`, its `allocation` when there is one, `label`, the provider's `name` and when it was first and last seen. Filter by `status` (allocations only), `allotted` and `limit`. A tenant admin or an auditor lists its own tenant; a clearinghouse admin lists any. The single read returns the allocation with its amounts (`AllocationRead`: the record, and `allowance` as the provider reports it, #168). In embedded mode `payments.list_actors` still takes `tenant_id=None`, which is every tenant for a clearinghouse admin and its own for anyone else |
| `GET /v1/clearinghouse/tenants/{tenant}/grant` | `admin.read` ([payments.md](payments.md#tenants)) |
| `POST /v1/clearinghouse/tenants`, `GET /v1/clearinghouse/tenants`, `POST .../tenants/{tenant}/grant/top-up`, `POST .../tenants/{tenant}/status`, `GET /v1/clearinghouse/wallet` | `admin.tenants`: a clearinghouse admin. With two [listeners](#listeners), on the private one only |

**Every route that names a tenant answers the same way (#165):**

| The request | Answer |
| --- | --- |
| No credential, or a bad one | 401 |
| A kind that may not call the route (an actor on an admin route, an auditor on a write, a tenant admin on `admin.tenants`) | 403 |
| Another tenant's id in the path | 403, before any store lookup or provider call |
| A record that does not exist (no such actor, key or grant) | 404 (400 for a key's rotate or revoke, as before) |
| No allocation, or an empty one, when a job needs it | 402 |

`tests/unit/test_admin_route_sweep.py` reads the routes from the app, so a route added later that names a
tenant is held to the third row.

`GET /v1/jobs/{id}/result` answers 501 until M4 (ISS-26), but only to the job's owner: the two checks
come first.

Enterprises replace the policy for teams, organization admins or RBAC by implementing `AccessPolicy`.

## Listeners

A clearinghouse admin onboards tenants and tops up grants, so a leaked clearinghouse admin key could
spend the wallet. A deployment can keep what only a clearinghouse admin does off the address it
publishes (D84, #167).

**One listener is the default.** Without `LPB_HTTP_PRIVATE_PORT` the engine serves one listener, on
`LPB_HTTP_HOST` and `LPB_HTTP_PORT`, with every route for every kind of credential, as before #167.
Self-hosted payments runs this way, and its clearinghouse admin key does everything it did.

**With `LPB_HTTP_PRIVATE_PORT` set, the engine serves two**, each with an app of its own over one engine
in one process:

| | Public listener | Private listener |
| --- | --- | --- |
| Address | `LPB_HTTP_HOST`, `LPB_HTTP_PORT` | `LPB_HTTP_PRIVATE_HOST` (`127.0.0.1` unless set), `LPB_HTTP_PRIVATE_PORT` |
| Routes | Every route but the private group | Every route |
| The private group: onboarding, the tenant list, a grant's top-up, a tenant's status and the wallet report | 404, as any path the engine does not know, with a credential or without one | Served |
| A clearinghouse admin credential | 403 on every route that takes a credential, the work routes included | Accepted |
| A tenant admin, an auditor, an actor or a service credential | Accepted, as with one listener | 403 on every route, its own tenant's included |
| No credential, or a bad one | 401 | 401 |
| `/livez`, `/readyz`, `GET /v1/offerings` | Served, with no credential | Served, with no credential |
| `/openapi.json`, `/docs` | The public listener's routes only | Every route |

- **The private listener is for a clearinghouse admin only.** It is where a clearinghouse admin does
  everything, for any tenant. A tenant's admin and its auditor use the public listener, and a tenant
  credential that reaches the private address is refused there.
- **A tenant admin key of kind `tenant_admin` is issued on the private listener:**
  `POST .../tenants/{tenant_id}/keys` is served on both, and only a clearinghouse admin may issue that
  kind ([a tenant's access keys](#a-tenants-access-keys)).
- **An allocation's key stays with the engine, on one listener or two.** Every app `http/listeners.py`
  builds mounts the tenant routes with `deliver_allocation_keys=False`: the engine behind a listener
  runs jobs, so allotment and rotation store the key in its `SecretStore` and answer the allocation
  alone, on the public listener and on the private one ([payments.md](payments.md#payment-keys)).
  Handing each key to the caller once is a standalone clearinghouse's, which the `clearinghouse` role
  (#169) mounts. No listener serves a signer path: an engine signs at the signer itself.
- **The credential rule is a property of the listener, not of each route.** It is applied where every
  route authenticates (`http/auth.py`), by kind, after the credential is verified. A route added later
  is covered with no check of its own, and so is a route an extension adds with `actor_dependency`
  ([extending the engine](../design-docs/extending-the-engine.md#service-mode)). A JWT with the admin
  scope is a clearinghouse admin credential and is treated as one.
- **Which group is on which listener is one table,** in `http/listeners.py`. A group of routes that is
  in neither of its two tuples fails a test (`tests/unit/test_listeners.py`), which also reads the
  routes from each app: every route of the private group is 404 on the public listener, and every route
  that takes a credential refuses a clearinghouse admin key on the public listener before the provider
  or the store is asked. The routes that take no credential are named there, so a new one is a decision
  somebody made.
- **The published schema.** `docs/generated/openapi.json` is generated from a deployment with one
  listener, so it has every route; the routes of the private group carry the tag `private listener`.
- **Publish the public listener only.** The private one listens on loopback unless
  `LPB_HTTP_PRIVATE_HOST` says otherwise. Whoever can reach it still needs a clearinghouse admin
  credential.

The `clearinghouse` role of ISS-31 (#169) will require the private listener; until then it is a
deployment's choice. In embedded mode there is no listener: the enterprise app builds the `ActorContext`
and the policy applies as before.

## Modes

The engine is a **resource server**: it verifies credentials. How a client *obtains* a token is the
enterprise identity provider's concern.

| Mode | Where it lives | Status |
| --- | --- | --- |
| Engine API key (`lpk_…`, hashed at rest, scoped, revocable) | `ApiKeyAuthenticator` | **Implemented**, on by default. `EngineSupply(api_keys=None)` turns keys off: `engine.keys` is `None`, the `/v1/keys` routes are absent and OpenAPI declares no bearer scheme |
| JWT bearer: `iss`, `aud`, `exp`, resource (RFC 8707) via JWKS; claim mapping in config | `JwtBearerAuthenticator` | **Implemented** (D45): `LPB_AUTH=jwt` or `jwt,api_keys`. See [JWT bearer](#jwt-bearer) |
| MCP OAuth (PKCE public clients) | Enterprise authorization server issues; engine verifies with JWT bearer and publishes RFC 9728 protected-resource metadata | Defined, with MCP (M3/M4) |
| Trusted backend (see [service mode](#service-mode)) | `ServiceCredentialAuthenticator` | Defined, M4 |
| CIMD and client catalog, device flow, token exchange | Enterprise authorization server or client (appendix) | Not engine work |
| HTTP Basic for end users | An enterprise-supplied `Authenticator` | Example only |
| Local gateway (payment key leaves the server) | Per-device allocations | Out of scope |

## JWT bearer

The REST service accepts OAuth 2.0 access tokens from the enterprise's identity provider (D45). The
engine is the **resource server**: it verifies tokens and never runs a login. Configuration chooses the
mode, with no code:

| `LPB_AUTH` | Accepted | Engine API keys |
| --- | --- | --- |
| `api_keys` (default) | `lpk_` keys | On |
| `jwt` | The provider's tokens | **Off**: `engine.keys` is `None`, so the key routes and `lpb bootstrap-admin` disappear |
| `jwt,api_keys` | Both. The order does not matter, because each authenticator recognizes only its own credential | On. The migration mode, for example while the console still signs in with an `lpk_` admin key |

**Setup:**
- set `LPB_JWT_ISSUER` (matched exactly against `iss`) and `LPB_JWT_AUDIENCE` (must be in `aud`);
- the keys come from the issuer's metadata (OpenID Discovery, then RFC 8414), or from `LPB_JWT_JWKS_URL`;
- the issuer and the key URL must be `https`. `LPB_JWT_ALLOW_HTTP=true` allows `http`, for local
  development only (a Keycloak in a compose stack);
- the other `LPB_JWT_*` settings are in [RELIABILITY.md](../RELIABILITY.md#configuration).
- A supplied `EngineSupply.authenticators` still replaces the configured ones.

**Startup refuses risky settings (D47):**
- an `http` issuer or key URL without the development override;
- an `LPB_JWT_ADMIN_SCOPE` that ordinary tokens carry: an operation name (`jobs.run`, ...) or a standard
  OpenID scope (`openid`, `profile`, `email`, `address`, `phone`, `offline_access`), whether written bare or
  with `LPB_JWT_SCOPE_PREFIX` (`api:jobs.run` with the prefix `api:`, D49). The admin scope makes an
  engine-wide admin that skips permission and ownership checks in every tenant, so it must be a scope
  the provider issues to operators alone;
  - **This check is a denylist.** It catches the scopes every provider puts on ordinary tokens, not ones
    a provider invents. Choosing a scope that only operators are ever issued stays the operator's
    responsibility. A dedicated namespace or a separate admin claim would remove that reliance (a
    follow-up, not built: [#58](https://github.com/NE-Build-Track/livepeer-builder-core/issues/58), T19);
- an `LPB_JWT_ADMIN_READ_SCOPE` that ordinary tokens carry (bare or prefixed, D49), or that equals the admin scope (D46): the read
  scope marks a read-only admin across the tenant, so it too must be issued to operators and dashboards
  alone;
- an empty `LPB_JWT_TOKEN_TYPES`.

**Verification** (`JwtBearerAuthenticator`, `adapters/jwt.py`):
- **Algorithms:** only `LPB_JWT_ALGORITHMS`, which takes asymmetric algorithms only (`RS256,ES256` by
  default). `none` and `HS*` are refused, so the published key can never be used as a shared secret.
- **Token type (RFC 9068, D47):** the header's `typ` must be in `LPB_JWT_TOKEN_TYPES`, compared without
  case (`at+jwt,application/at+jwt` by default). An ID token (`typ: JWT`) or a logout token with the right
  audience is refused, as is a token with no `typ`.
  - **The opt-out:** add `jwt` for a provider that cannot send `at+jwt`. It accepts `typ: JWT` and a missing
    `typ`, and drops the `client_id` and `jti` requirements. It is weaker: only the audience then tells an
    access token from an ID token, so `LPB_JWT_AUDIENCE` must be this engine's resource identifier, never a
    client ID.
- **Signature:** checked against the issuer's JWKS.
  - **Which keys:** only asymmetric keys for signatures count (`kty` `RSA`, `EC` or `OKP`). A shared-secret
    key (`kty: oct`), a key with `use: enc`, or one with `key_ops` that lack `verify`, is skipped; a key
    that omits `use` counts. A key published with an `alg` verifies only that algorithm (RFC 8725).
  - **The algorithm must fit the key (D50):** `RS*` and `PS*` need an `RSA` key, `ES*` an `EC` key on the
    algorithm's curve, and `EdDSA` an `OKP` key. This matters for a key published without `alg`, which
    RFC 7517 allows: the token's header then names the algorithm, and a mismatch is a 401.
  - **Key length (D50):** an RSA key shorter than 2048 bits never verifies (NIST SP 800-131A).
  - **Caching (D47):** keys older than 10 minutes are refreshed in the background while requests keep
    using them, so a slow provider never stalls a request, and a key the provider withdrew stops verifying
    within about 10 minutes. Keys older than an hour are not served: the request waits for a fetch. A token
    that names an unknown `kid` causes a fetch at most once a minute; only a refetch that worked uses up
    that minute.
  - **One fetch at a time (D49):** a refresh, a cold start and an unknown-`kid` refetch all share one fetch,
    and only that fetch updates the cache, so an older response can never put back a key set the provider
    has since changed. An unknown `kid` first waits for a fetch already in flight (it may predate the
    rotation that minted the `kid`), then asks again. No lock is held while the provider answers.
  - **No `kid`:** a token without `kid` is accepted only when the provider publishes exactly one signing key.
- **Claims:** `iss`, `aud`, `exp` and `iat` are required, plus `client_id` and `jti` unless the opt-out is
  on (RFC 9068 2.2). `exp`, `nbf` and `iat` are checked with `LPB_JWT_LEEWAY_S` (30 s). `aud` may be a
  list that contains the engine.
- **Lifetime (D47):** a token whose `exp - iat` exceeds `LPB_JWT_MAX_LIFETIME_S` (an hour) is refused,
  however valid its signature. This is what makes the short-lifetime revocation guidance enforceable.
  `exp` and `iat` must be numbers: a numeric string, which PyJWT accepts, is a 401 (D49).
- **Sender-constrained tokens are refused.** A token with `cnf` is bound to a client certificate
  (RFC 8705) or a DPoP key (RFC 9449). This engine verifies neither, so it refuses the token rather than
  accept it as a plain bearer token.
- **Reading the keys:** the keys URL comes from the issuer's metadata, which must name the configured issuer
  and an `https` `jwks_uri` (`http` too with the development override).
  - **Two metadata URLs (D47):** OpenID Discovery first (`<issuer>/.well-known/openid-configuration`); if
    that request fails, RFC 8414 (`https://host/.well-known/oauth-authorization-server/<issuer path>`). A
    document that answers but is wrong is not retried at the other URL.
  - **Bounded:** each document must arrive whole within 3 s (one deadline, not one per read, so a provider
    that trickles bytes is cut off, D49), is capped at 1 MiB, and redirects are not followed.
    The token's own `jku` and `x5u` headers are ignored, so a token cannot choose where its keys come from.
  - **No host block (a decision, D47):** the metadata comes from the operator's configured issuer over
    `https`, and a provider in the same cluster (a Keycloak) legitimately sits on a private address, so
    private, link-local and loopback hosts are allowed. Agreed in the #50 review.
  - **A bad URL is retried:** a discovered URL is kept only once it has served a usable key set, so a bad
    one, or one the issuer has moved, is discovered again.
  - **After a failed fetch,** the provider is left alone for 5 s, counted from when the fetch failed (D49),
    and token requests answer 503 meanwhile, unknown `kid`s included. Any failure counts, including a key
    set the parser rejects (a non-text `kty` or `alg` is skipped): it is a 503, never a 500.
  - **Logging:** the reason, the URL and the client's error are logged as `auth.jwks_unavailable`. The 503
    body is a fixed message and never carries them.
- **Answers:**

| The credential | Answer |
| --- | --- |
| Not a JWT (an `lpk_` key, Basic, garbage), or a JWT from another issuer | `None`: the next authenticator gets a turn. A payload that cannot be read, a deeply nested one that makes JSON parsing raise `RecursionError` included, names no issuer (#113, C22) |
| From this issuer, failing a check | `NotAuthenticated` (401), with the reason: expired, not yet valid, audience, signature, algorithm, token type, lifetime, unknown key, a key the algorithm cannot use or one that is too short, missing claim, or a header PyJWT refuses (an unknown `crit`, a `kid` that is not text) or that cannot be read, a deeply nested one included (`RecursionError`, #113, C22). The issuer is read from the payload before the header, so a bad header cannot make this issuer's token look like another's |
| The provider's keys cannot be read, or its discovery document names another issuer | `ProviderUnavailable` (503) with a fixed message, because no token can be checked. The details are logged |

**Claims become an `ActorContext`:**

| Field | From |
| --- | --- |
| `actor_id` | `LPB_JWT_ACTOR_CLAIM` (`sub`). Keep the default: `sub` is the provider's immutable subject, while a username can be renamed or reused, and the engine keys money and history on `actor_id`. A readable name is the actor's `label`, given at allotment (D46). At most 200 characters, as every record stores it: a longer claim is a 401, never cut |
| `tenant_id` | `LPB_JWT_TENANT_CLAIM`, the claim that names it, or `LPB_JWT_TENANT_ID`, the one tenant every token is in (at most 200 characters: a longer one fails at startup). Exactly one is set: neither has a default, and with neither or both the engine refuses to start (#163 review). A claim over 200 characters is a 401 |
| `permissions` | The scopes in `LPB_JWT_SCOPE_CLAIM` (`scope`, a space-separated string, or a list such as `scp`) that name an operation after `LPB_JWT_SCOPE_PREFIX` is removed: `discover`, `jobs.run`, `jobs.read`, `costs.read`, `events.read`. Never `actors.assert` (the trusted backend's) and never `admin.*` |
| `credential_kind` | `clearinghouse_admin` when the token carries `LPB_JWT_ADMIN_SCOPE`; otherwise `auditor` when it carries `LPB_JWT_ADMIN_READ_SCOPE` (a dashboard's service account); otherwise `actor` |
| `auth` | `AuthProvenance(method="jwt", issuer, credential_id=jti)`. The token itself is never kept or logged |
| `display_name` | `LPB_JWT_NAME_CLAIM` (`preferred_username`), when it is text. Only for the operator's actor list (D48): never an identity, never copied onto records |

**Registration on first sign-in (D48).** An actor is the provider's `sub`, which an operator cannot
guess. So the first time a builder's token reaches the engine, the engine records that actor in
`lpb_actor_sightings`, with the provider's display name. The operator's actor list then shows them
before any allocation (`allotted: false`), and allotting to them takes that name as the label
unless the operator gives one. The allocation still belongs to the `sub`: a renamed person keeps
their actor, and a reused name never inherits one.
- **Who:** actor credentials only. Operators (admin), auditors and engine-key actors are not recorded;
  the key list already shows key holders.
- **Cost:** one write per person per process, then one an hour to move `last_seen_at`, and one at once
  when the name changes. A failed write is logged (`actors.sighting_failed`) and never fails the request.
- **Retention:** sightings are kept until removed by hand, like actors.

**Revocation.** A JWT cannot be revoked before it expires; an `lpk_` key can. Issue short-lived access
tokens (5 to 15 minutes) and let the provider refresh them. The engine enforces the ceiling: no token
lives longer than `LPB_JWT_MAX_LIFETIME_S`, and a withdrawn signing key stops verifying within about
10 minutes (D47).

**OpenAPI.** With keys off, the service declares a JWT bearer scheme, so Swagger UI's Authorize sends a
token.

**Not here:**
- opaque tokens (RFC 7662 introspection);
- RFC 9728 protected-resource metadata for MCP clients (phase 2);
- the trusted backend, below.

## Service mode

An enterprise backend calls the deployed engine on behalf of many users in one of two generic ways:

- **(A) JWT bearer.** Use this for direct clients (browser, MCP). The enterprise identity provider
  issues tokens whose audience is the engine. The engine is configured with issuer(s), audience and a
  claim mapping (`sub` → `actor_id`, a claim → `tenant_id`, the scope claim → permissions).
- **(B) Trusted backend.** Use this for backend-to-engine calls. The backend holds a **service
  credential** with `actors.assert`, **bound to one `tenant_id`**, and names the actor in a
  `Livepeer-Actor` header. The header is honored **only** for that credential, so it is never an
  unchecked request field. Service credentials are never issued to browsers.

Both are generic: no enterprise logic lives in the engine.

## Boundaries

- **End users:** an end user never needs an engine key. Enterprises enroll and authenticate their
  users with their own identity provider (imported mode, JWT bearer or a trusted backend); `lpk_`
  keys are for standalone callers and operators.
- **Browser:** browsers never hold admin, management or payment credentials. A browser reaches the
  engine through the enterprise backend, or directly with a JWT whose audience is the engine. CORS is
  off by default.
- **Admin:** `clearinghouse_admin` and `tenant_admin` are distinct credential kinds. The first
  clearinghouse admin key comes from `lpb bootstrap-admin`, which works only while no active
  clearinghouse admin key exists, and only when engine API keys are on. There is no network
  bootstrap route and no static admin token. A tenant's first admin key comes from onboarding it, and
  only a clearinghouse admin issues another. A deployment with a private listener accepts a
  clearinghouse admin credential there only ([listeners](#listeners)).
- **Payment keys (#166):** an allocation's key (`lpg_`) is no access key. It is a credential at the
  signer and nowhere else: as a bearer on any engine route it gets what no credential gets, 401. The
  engine that runs an actor's jobs holds that actor's key. With self-hosted payments that is the one
  engine, which keeps it in its `SecretStore` and returns it in no answer. At a standalone clearinghouse
  it is the enterprise's engine: allotment and rotation hand it the key once, in an answer only that
  tenant's admin or a clearinghouse admin can get, and the clearinghouse keeps none
  ([payments.md](payments.md#payment-keys)).
- **Request fields:** no request field establishes identity. The body's `actor_id` on admin routes
  names a *target* and needs an admin credential.

## Credential lifecycle

| Credential | Issued by | Held by | Rotated / revoked by |
| --- | --- | --- | --- |
| Caller API key (`lpk_`) | Engine admin (`POST /v1/keys`) | Client or enterprise backend | Engine admin: `rotate` (instant) / `revoke`, see the [example flow](#example-standalone-flow) |
| Caller JWT | Enterprise identity provider | Client | Identity provider (expiry) |
| Service credential (`actors.assert`) | Engine admin | Enterprise backend | Engine admin |
| Clearinghouse admin credential | `lpb bootstrap-admin`, then a clearinghouse admin | Operator | Clearinghouse admin |
| Tenant admin key | Onboarding the tenant (`POST /v1/clearinghouse/tenants`), then a clearinghouse admin (`POST .../tenants/{tenant_id}/keys`, kind `tenant_admin`) | The enterprise app | Clearinghouse admin; never a tenant admin |
| A tenant's auditor key | That tenant's admin (`POST .../tenants/{tenant_id}/keys`) | A dashboard or audit reader | That tenant's admin, or a clearinghouse admin |
| Batteries management credential | Batteries operator | Engine settings (`LPB_BATTERIES_TOKEN`) | Batteries operator |
| Batteries grants credential (tenant desk) | Batteries operator | Engine settings (`LPB_BATTERIES_GRANTS_TOKEN`), optional; without it the tenant desk is off | Batteries operator |
| Allocation key (`lpg_`) | Engine, through the Batteries management API | Self-hosted payments: the engine's `SecretStore` (a reference in the DB). At a standalone clearinghouse: the enterprise's engine, handed the key once by allotment or rotation; the clearinghouse keeps a receipt and no key (`credential_custody`, [payments.md](payments.md#payment-keys)) | Engine: `rotate-payment-key` / `revoke`, on the tenant admin's command |
| Signer webhook token; Kafka credentials | Batteries operator | Signer | Batteries operator |

## Example standalone flow

A deployment with no identity provider of its own: engine API keys, an operator, and one end user. The
same steps run on the dev stack.

| Step | Call | Answer |
| --- | --- | --- |
| 1. First admin key | `lpb bootstrap-admin` (CLI only; refused once an active admin key exists) | `{"key_id", "token": "lpk_...", "note": "shown once; store it now"}` |
| 2. A key for the user | `POST /v1/keys` `{"kind": "actor", "actor_id": "alice", "tenant_id": "<tenant id>", "label": "alice laptop", "operation_ref": "key-alice-1"}` | `201` `IssuedKey`: the key and its token, **shown once**. `tenant_id` is required, and a field the body does not know is a 422 (#163 review). Permissions default to `discover`, `jobs.run`, `jobs.read`, `costs.read`. A repeat of the reference is answered without the token ([below](#repeated-key-commands)) |
| 3. Allot to the user | `POST /v1/clearinghouse/tenants/{tenant_id}/actors/alice/allocation` `{"amount": {"amount": "5", "currency": "USD"}, "operation_ref": "allot-alice"}`, where the tenant's id is its grant's id (onboard the tenant first, and issue the user's key with that `tenant_id`) | `201` `ActorAllocation`; the payment key (`lpg_`) goes straight into the `SecretStore` and is never returned |
| 4. The user works | `POST /v1/jobs` with `Authorization: Bearer lpk_...` | the runner's answer, with `Livepeer-Job-Id` |
| 5. Inspect the key, and who is allotted | `GET /v1/keys/{key_id}` (or `GET /v1/keys` for all); `GET /v1/clearinghouse/tenants/{tenant_id}/actors` for the allotted actors | `AccessKey`: kind, actor, tenant, permissions, label, created and revoked times. Never the token, which the engine does not keep; never its hash |
| 6. Rotate it | `POST /v1/keys/{key_id}/rotate` `{"operation_ref": "rotate-alice-1"}`, optionally with `"label"` | `201` `IssuedKey` with a new key ID and token, shown once. The old token answers 401 at once: there is no overlap window. Kind, actor, tenant and permissions carry over. A revoked or unknown key answers 400 |
| 7. Revoke it | `POST /v1/keys/{key_id}/revoke` | `204`; the token answers 401 from then on |

### A tenant's access keys

A tenant's keys are also under its own path, in the clearinghouse set (D87, #165):
`GET`, `POST /v1/clearinghouse/tenants/{tenant_id}/keys`, `GET .../keys/{key_id}`,
`POST .../keys/{key_id}/rotate` and `POST .../keys/{key_id}/revoke`. They are the commands above with the
tenant taken from the path:

- **The body names no tenant** (a `tenant_id` there is a 422) **and takes two kinds:** `auditor` (the
  default) and `tenant_admin`. An actor's or a service's key belongs to an engine that runs jobs, and
  is issued under `/v1/keys`.
- **A tenant admin** issues, lists, inspects, rotates and revokes its tenant's keys that are not admin
  keys. Issuing, rotating or revoking a `tenant_admin` key is 403 for it, its own key included.
- **A clearinghouse admin** issues another tenant admin key with kind `tenant_admin`, and revokes one.
- **A key of another tenant** named under this tenant's path is missing: 404 for the read, 400 for a
  rotate or revoke, as for a key that never existed.

Under `/v1/keys`, where no path names a tenant, a tenant admin is confined the same way: it lists its
own tenant's keys, a body that names another tenant (or leaves the default, `default`) is 403, another
tenant's key is missing, and an admin key is 403. A tenant admin key is bound to its tenant when it is
issued, and rotation carries the tenant over: nothing moves a key to another tenant.

### Repeated key commands

Issuing and rotating a key take a required `operation_ref` (D57, Q4; 422 when it is missing), scoped
to the calling admin. The new key and its operation-ledger record commit in one transaction, so the
outcome is never unknown. A repeat of the reference with the same request answers `201` with the key
as it was issued, **`"token": null`** and **`"replayed": true`** (`false` on the first answer), and
`Idempotent-Replayed: true`:
the engine keeps only the token's hash, so it cannot show the token again. An admin who lost it
rotates the key (under a new reference). The same reference with another request, or on another
command, is 409 `code: operation_conflict` ([payments.md](payments.md#repeat-safe-commands)). A
repeated rotation is answered from its record only to a caller its first run would have answered
(#165 review): repeated under another tenant's path it is `400`, as for a key that tenant never had, and
repeated by a caller who may not change a key of that kind it is `403`. Neither shows the replacement
key. Revoking
a key takes no reference: a repeat cannot revoke twice, and a repeat of one that finished answers as
the first did (`204`), so a retry after a lost answer is not an error (D61); a key that never existed is
`400`. `lpb bootstrap-admin` takes none either.

**Audit log.** Each change is one INFO line: `keys.bootstrapped`, `keys.issued`, `keys.rotated` (with
`replaced_key_id`) and `keys.revoked`, carrying `key_id`, `kind`, `actor_id`, `tenant_id` and
`by` (the acting admin's `actor_id`, or `cli`). No log line carries a token, a token hash or a payment
key (`tests/unit/test_access.py`). A database driver's own DEBUG output echoes the parameters it
stores, token hashes among them; the engine's logging never turns that on, and a hash is not a
credential.

**Replacing enrollment.** Nothing above is required:
- **Your own identity:** an enterprise supplies its own authenticators
  (`EngineSupply(authenticators=(...))`, tried before API keys) or turns engine keys off
  (`EngineSupply(api_keys=None)`: the key routes and `lpb bootstrap-admin` disappear).
- **Your own rules:** it can wrap the policy with `decorate`.
- **Imported mode:** the application builds `ActorContext` itself.

The answer rule and the policy apply the same way in every case
([extending-the-engine.md](../design-docs/extending-the-engine.md)).

## Appendix: authorization-server guidance (non-normative)

Harvested from John's drafts (`enterprise-authorization-server.md`, `enterprise-auth-provider-modes.md`,
fork branch `jm/builder-integration-plans` `300c127`):

- **MCP public clients:** PKCE `S256`; RFC 9728 protected-resource metadata on the engine; RFC 8414
  authorization-server metadata on the enterprise issuer; RFC 8707 resource indicator equal to the
  engine's MCP or API resource.
- **Audience:** access tokens must name the engine as their resource. Tokens for another audience are
  exchanged at a trusted issuer (RFC 8693) before they reach the engine.
- **Client registration:** clients register by CIMD URL or a default catalog ID. Dynamic client
  registration is optional.
- **Device flow** (RFC 8628) belongs to CLIs and local tools. Each deployment configures its own
  client ID and scope.
- **The caller's token stops at the engine.** It is never forwarded to the signer or Batteries.
