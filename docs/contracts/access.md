# Access contract

**Tickets:** ISS-03, ISS-08, #49 (ISS-33) · **Design accepted:** 12 October 2026 (decisions D12–D16);
ISS-08 on 26 October (D44); JWT bearer on 26 October (D45) · **Code:** `contracts/access.py`, `protocols/`,
`services/policy.py`, `services/ownership.py`, `services/keys.py`, `adapters/auth.py`, `adapters/jwt.py`,
`adapters/secrets.py`

The core never authenticates anyone. It consumes a validated `ActorContext` and asks an
`AccessPolicy` whether an operation is allowed. Everything about *how* identity is established lives
in adapters.

## Actor context

| Field | Meaning |
| --- | --- |
| `actor_id` | Opaque to the engine; the owner of jobs, costs and allocations |
| `application_id` | Tenant isolation; defaults to `default` |
| `permissions` | `Operation` values (see below) |
| `credential_kind` | `actor`, `admin`, `auditor` (a read-only admin, D46) or `service` |
| `attributes` | Non-secret labels copied onto records. **Never secrets** |
| `auth` | Provenance (method, issuer, credential id), for audit only |

**Operations:** `discover`, `jobs.run`, `jobs.read`, `costs.read`, `events.read`, `admin.keys`,
`admin.allocations`, `admin.costs`, `admin.read`, `actors.assert`. `admin.read` is every admin read: the
key list and key inspection, the grant list, the actor list and one actor's allocation, and
reconciliation. The other `admin.*` operations are the writes.

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
- **Imported mode needs no authenticator.** The application builds `ActorContext` itself. The policy
  still applies.

## DefaultPolicy

- **Admin operations** (`admin.*`) need an **admin credential kind**. A permission string on an actor
  key is not enough.
- **An admin credential** may act on any application's records.
- **An auditor credential** (D46) only reads: `admin.read`, `discover`, `jobs.read`, `costs.read` and
  `events.read`, across every actor of its own application. Every write is `AccessDenied`, and so is any
  record of another application. It is for a dashboard or an audit reader that must not move money. An
  engine key can be issued as `auditor`, and a JWT is one with `LPB_JWT_ADMIN_READ_SCOPE`.
- **Everyone else** needs the operation's permission, and may touch only resources owned by their own
  `(application_id, actor_id)`.
- **Another actor's record** reads as missing (404), never 403, so its existence isn't revealed. A
  missing permission is 403. The order of the two checks is in [Answers](#answers).

## Answers

Every route on a job or its cost answers the same way (D44, `services/ownership.py`):

1. **The permission first, before any lookup.** A caller without the operation's permission gets
   `AccessDenied` (403), whether or not the job exists, so the answer says nothing about any job.
2. **Then ownership.** Another actor's or application's job, and a job that does not exist, are both
   `NotFound` (404).

| Route | Permission |
| --- | --- |
| `GET /v1/jobs/{id}`, `GET /v1/jobs/{id}/result` | `jobs.read` |
| `GET /v1/jobs` | `jobs.read`. An actor lists its own jobs; an admin or an auditor lists every actor's jobs in its application |
| `POST /v1/jobs/{id}/usage` | `jobs.run` (not `jobs.read`) |
| `POST /v1/jobs` (including `mode=async`, which then answers 501) | `jobs.run` |
| `GET /v1/jobs/{id}/cost` | `costs.read` |
| `GET /v1/costs/projections`, `GET /v1/costs/summary`, `GET /v1/allowance` | `costs.read`. An admin's or an auditor's projections and summary cover every actor in its application; summary and allowance take `?actor_id=` to name one |
| `GET /v1/admin/actors`, `GET /v1/admin/applications/{app}/actors/{actor}/allocation` | `admin.read`. The provisioned actors, with status, allocation and `label`; filter by `application_id`, `status` and `limit`. An admin lists every application unless it names one; an auditor lists its own |

`GET /v1/jobs/{id}/result` answers 501 until M4 (ISS-26), but only to the job's owner: the two checks
come first.

Enterprises replace the policy for teams, organization admins or RBAC by implementing `AccessPolicy`.

## Modes

The engine is a **resource server**: it verifies credentials. How a client *obtains* a token is the
enterprise identity provider's concern.

| Mode | Where it lives | Status |
| --- | --- | --- |
| Engine API key (`lpk_…`, hashed at rest, scoped, revocable) | `ApiKeyAuthenticator` | **Implemented**, on by default. `EngineSupply(api_keys=None)` turns keys off: `engine.keys` is `None`, the `/v1/admin/keys` routes are absent and OpenAPI declares no bearer scheme |
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
  OpenID scope (`openid`, `profile`, `email`, `address`, `phone`, `offline_access`). The admin scope makes an
  engine-wide admin that skips permission and ownership checks in every application, so it must be a scope
  the provider issues to operators alone;
- an `LPB_JWT_ADMIN_READ_SCOPE` that ordinary tokens carry, or that equals the admin scope (D46): the read
  scope marks a read-only admin across the application, so it too must be issued to operators and dashboards
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
  - **Caching (D47):** keys older than 10 minutes are refreshed in the background while requests keep
    using them, so a slow provider never stalls a request, and a key the provider withdrew stops verifying
    within about 10 minutes. Keys older than an hour are not served: the request waits for a fetch. A token
    that names an unknown `kid` causes a fetch at most once a minute; only a refetch that worked uses up
    that minute.
  - **No `kid`:** a token without `kid` is accepted only when the provider publishes exactly one signing key.
- **Claims:** `iss`, `aud`, `exp` and `iat` are required, plus `client_id` and `jti` unless the opt-out is
  on (RFC 9068 2.2). `exp`, `nbf` and `iat` are checked with `LPB_JWT_LEEWAY_S` (30 s). `aud` may be a
  list that contains the engine.
- **Lifetime (D47):** a token whose `exp - iat` exceeds `LPB_JWT_MAX_LIFETIME_S` (an hour) is refused,
  however valid its signature. This is what makes the short-lifetime revocation guidance enforceable.
- **Sender-constrained tokens are refused.** A token with `cnf` is bound to a client certificate
  (RFC 8705) or a DPoP key (RFC 9449). This engine verifies neither, so it refuses the token rather than
  accept it as a plain bearer token.
- **Reading the keys:** the keys URL comes from the issuer's metadata, which must name the configured issuer
  and an `https` `jwks_uri` (`http` too with the development override).
  - **Two metadata URLs (D47):** OpenID Discovery first (`<issuer>/.well-known/openid-configuration`); if
    that request fails, RFC 8414 (`https://host/.well-known/oauth-authorization-server/<issuer path>`). A
    document that answers but is wrong is not retried at the other URL.
  - **Bounded:** each request has a 3 s timeout and a 1 MiB body cap, and redirects are not followed.
    The token's own `jku` and `x5u` headers are ignored, so a token cannot choose where its keys come from.
  - **No host block:** the metadata comes from the operator's configured issuer over `https`, and a
    provider in the same cluster (a Keycloak) legitimately sits on a private address, so private and
    loopback hosts are allowed.
  - **A bad URL is retried:** a discovered URL is kept only once it has served a usable key set, so a bad
    one, or one the issuer has moved, is discovered again.
  - **After a failed fetch,** the provider is left alone for 5 s, and token requests answer 503 meanwhile.
  - **Logging:** the reason, the URL and the client's error are logged as `auth.jwks_unavailable`. The 503
    body is a fixed message and never carries them.
- **Answers:**

| The credential | Answer |
| --- | --- |
| Not a JWT (an `lpk_` key, Basic, garbage), or a JWT from another issuer | `None`: the next authenticator gets a turn |
| From this issuer, failing a check | `NotAuthenticated` (401), with the reason: expired, not yet valid, audience, signature, algorithm, token type, lifetime, unknown key, missing claim, or a header PyJWT refuses (an unknown `crit`, a `kid` that is not text). The issuer is read from the payload before the header, so a bad header cannot make this issuer's token look like another's |
| The provider's keys cannot be read, or its discovery document names another issuer | `ProviderUnavailable` (503) with a fixed message, because no token can be checked. The details are logged |

**Claims become an `ActorContext`:**

| Field | From |
| --- | --- |
| `actor_id` | `LPB_JWT_ACTOR_CLAIM` (`sub`). Keep the default: `sub` is the provider's immutable subject, while a username can be renamed or reused, and the engine keys money and history on `actor_id`. A readable name is the actor's `label`, given at provisioning (D46) |
| `application_id` | `LPB_JWT_APPLICATION_CLAIM` when set, otherwise `LPB_JWT_APPLICATION_ID` (`default`) |
| `permissions` | The scopes in `LPB_JWT_SCOPE_CLAIM` (`scope`, a space-separated string, or a list such as `scp`) that name an operation after `LPB_JWT_SCOPE_PREFIX` is removed: `discover`, `jobs.run`, `jobs.read`, `costs.read`, `events.read`. Never `actors.assert` (the trusted backend's) and never `admin.*` |
| `credential_kind` | `admin` when the token carries `LPB_JWT_ADMIN_SCOPE`; otherwise `auditor` when it carries `LPB_JWT_ADMIN_READ_SCOPE` (a dashboard's service account); otherwise `actor` |
| `auth` | `AuthProvenance(method="jwt", issuer, credential_id=jti)`. The token itself is never kept or logged |

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
  claim mapping (`sub` → `actor_id`, a claim → `application_id`, the scope claim → permissions).
- **(B) Trusted backend.** Use this for backend-to-engine calls. The backend holds a **service
  credential** with `actors.assert`, **bound to one `application_id`**, and names the actor in a
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
- **Admin:** admin is a distinct credential kind. The first admin key comes from
  `lpb bootstrap-admin`, which works only while no active admin key exists, and only when engine API
  keys are on. There is no network
  bootstrap route and no static admin token.
- **Request fields:** no request field establishes identity. The body's `actor_id` on admin routes
  names a *target* and needs an admin credential.

## Credential lifecycle

| Credential | Issued by | Held by | Rotated / revoked by |
| --- | --- | --- | --- |
| Caller API key (`lpk_`) | Engine admin (`POST /v1/admin/keys`) | Client or enterprise backend | Engine admin: `rotate` (instant) / `revoke`, see the [example flow](#example-standalone-flow) |
| Caller JWT | Enterprise identity provider | Client | Identity provider (expiry) |
| Service credential (`actors.assert`) | Engine admin | Enterprise backend | Engine admin |
| Engine admin credential | `lpb bootstrap-admin`, then an admin | Operator | Engine admin |
| Batteries management credential | Batteries operator | Engine settings (`LPB_BATTERIES_TOKEN`) | Batteries operator |
| Batteries operator credential (grant desk) | Batteries operator | Engine settings (`LPB_BATTERIES_OPERATOR_TOKEN`), optional; without it the grant desk is off | Batteries operator |
| Allocation key (`lpg_`) | Engine, through the Batteries management API | `SecretStore` (a reference in the DB) | Engine: `rotate-key` / `revoke` |
| Signer webhook token; Kafka credentials | Batteries operator | Signer | Batteries operator |

## Example standalone flow

A deployment with no identity provider of its own: engine API keys, an operator, and one end user. The
same steps run on the dev stack.

| Step | Call | Answer |
| --- | --- | --- |
| 1. First admin key | `lpb bootstrap-admin` (CLI only; refused once an active admin key exists) | `{"key_id", "token": "lpk_...", "note": "shown once; store it now"}` |
| 2. A key for the user | `POST /v1/admin/keys` `{"kind": "actor", "actor_id": "alice", "label": "alice laptop"}` | `201` `IssuedKey`: the key and its token, **shown once**. Permissions default to `discover`, `jobs.run`, `jobs.read`, `costs.read` |
| 3. Fund the user (`per_actor` mode) | `POST /v1/admin/applications/default/actors/alice/allocation` `{"amount": {"amount": "5", "currency": "USD"}, "grant_id": "..."}` | `201` `ProvisionedActor`; the payment key (`lpg_`) goes straight into the `SecretStore` and is never returned |
| 4. The user works | `POST /v1/jobs` with `Authorization: Bearer lpk_...` | the runner's answer, with `Livepeer-Job-Id` |
| 5. Inspect the key, and who is provisioned | `GET /v1/admin/keys/{key_id}` (or `GET /v1/admin/keys` for all); `GET /v1/admin/actors` for the provisioned actors | `AccessKey`: kind, actor, application, permissions, label, created and revoked times. Never the token, which the engine does not keep; never its hash |
| 6. Rotate it | `POST /v1/admin/keys/{key_id}/rotate`, optionally `{"label": "..."}` | `201` `IssuedKey` with a new key ID and token, shown once. The old token answers 401 at once: there is no overlap window. Kind, actor, application and permissions carry over. A revoked or unknown key answers 400 |
| 7. Revoke it | `POST /v1/admin/keys/{key_id}/revoke` | `204`; the token answers 401 from then on |

**Audit log.** Each change is one INFO line: `keys.bootstrapped`, `keys.issued`, `keys.rotated` (with
`replaced_key_id`) and `keys.revoked`, carrying `key_id`, `kind`, `actor_id`, `application_id` and
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
