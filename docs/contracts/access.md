# Access contract

**Ticket:** ISS-03 · **Design accepted:** 12 October 2026 (decisions D12–D16) · **Code:**
`contracts/access.py`, `protocols/`, `services/policy.py`, `services/keys.py`, `adapters/auth.py`,
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
| `credential_kind` | `actor`, `admin` or `service` |
| `attributes` | Non-secret labels copied onto records. **Never secrets** |
| `auth` | Provenance (method, issuer, credential id), for audit only |

**Operations:** `discover`, `jobs.run`, `jobs.read`, `costs.read`, `events.read`, `admin.keys`,
`admin.allocations`, `admin.costs`, `actors.assert`.

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
- `AuthenticatorChain` runs authenticators in configuration order and combines their challenges into
  one `WWW-Authenticate` header.
- **Imported mode needs no authenticator.** The application builds `ActorContext` itself. The policy
  still applies.

## DefaultPolicy

- **Admin operations** (`admin.*`) need an **admin credential kind**. A permission string on an actor
  key is not enough.
- **An admin credential** may act on any application's records.
- **Everyone else** needs the operation's permission, and may touch only resources owned by their own
  `(application_id, actor_id)`.
- **Reads of other actors' records** return 404, never 403, so their existence isn't revealed.

Enterprises replace the policy for teams, organization admins or RBAC by implementing `AccessPolicy`.

## Modes

The engine is a **resource server**: it verifies credentials. How a client *obtains* a token is the
enterprise identity provider's concern.

| Mode | Where it lives | Status |
| --- | --- | --- |
| Engine API key (`lpk_…`, hashed at rest, scoped, revocable) | `ApiKeyAuthenticator` | **Implemented** (standalone baseline) |
| JWT bearer: `iss`, `aud`, `exp`, resource (RFC 8707) via JWKS; claim mapping in config | `JwtBearerAuthenticator` | Defined, M4 |
| MCP OAuth (PKCE public clients) | Enterprise authorization server issues; engine verifies with JWT bearer and publishes RFC 9728 protected-resource metadata | Defined, with MCP (M3/M4) |
| Trusted backend (see [service mode](#service-mode)) | `ServiceCredentialAuthenticator` | Defined, M4 |
| CIMD and client catalog, device flow, token exchange | Enterprise authorization server or client (appendix) | Not engine work |
| HTTP Basic for end users | An enterprise-supplied `Authenticator` | Example only |
| Local gateway (payment key leaves the server) | Per-device allocations | Out of scope |

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

- **Browser:** browsers never hold admin, management or payment credentials. A browser reaches the
  engine through the enterprise backend, or directly with a JWT whose audience is the engine. CORS is
  off by default.
- **Admin:** admin is a distinct credential kind. The first admin key comes from
  `lpb bootstrap-admin`, which works only once (when no admin key exists). There is no network
  bootstrap route and no static admin token.
- **Request fields:** no request field establishes identity. The body's `actor_id` on admin routes
  names a *target* and needs an admin credential.

## Credential lifecycle

| Credential | Issued by | Held by | Rotated / revoked by |
| --- | --- | --- | --- |
| Caller API key (`lpk_`) | Engine admin (`POST /v1/admin/keys`) | Client or enterprise backend | Engine admin |
| Caller JWT | Enterprise identity provider | Client | Identity provider (expiry) |
| Service credential (`actors.assert`) | Engine admin | Enterprise backend | Engine admin |
| Engine admin credential | `lpb bootstrap-admin`, then an admin | Operator | Engine admin |
| Batteries management credential | Batteries operator | Engine settings (`LPB_BATTERIES_TOKEN`) | Batteries operator |
| Allocation key (`lpg_`) | Engine, through the Batteries management API | `SecretStore` (a reference in the DB) | Engine: `rotate-key` / `revoke` |
| Signer webhook token; Kafka credentials | Batteries operator | Signer | Batteries operator |

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
