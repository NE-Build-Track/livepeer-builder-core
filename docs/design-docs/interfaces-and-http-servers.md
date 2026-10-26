# Interfaces and the HTTP servers

Class and sequence diagrams of the engine's interfaces and of the two HTTP servers built on them:
the REST service (`lpb serve`, `http/`) and the discovery service (`lpb discovery`, `discovery/`).
Every box and message is a real class, method, route or setting, so each diagram leads to the code.
The diagrams describe the code with the discovery service (#45, D43) in place.

This document is for reviewing the shapes. Other documents cover the rest:
- the flows between components (authentication, provisioning, a paid job with failover, cost sync,
  reconciliation, recovery): [architecture-diagrams.md](architecture-diagrams.md);
- every interface's full method list, generated from the code: [generated/interfaces.md](../generated/interfaces.md);
- how to replace a default, with runnable examples: [extending-the-engine.md](extending-the-engine.md).

**Notation.** `<<interface>>` is a `typing.Protocol`. Implementations match it structurally and never
inherit from it, so `..|>` means "satisfies", checked by mypy and `tests/structure/test_conformance.py`.
`-->` means "holds", `..>` means "calls", `X?` is optional and `X[]` a sequence.

## The two kinds of interface

The engine has two sets of interfaces, and they point in opposite directions:

| Set | Module | Who implements it | Who calls it |
| --- | --- | --- | --- |
| What the engine **offers** | `api.py` (`ActorApi`, `AdminApi`, `EngineApi`) | `BuilderEngine` (and `FakeEngine` for `ActorApi`) | applications in-process, and `http/` |
| What the engine **needs** | `protocols/` (ports) | adapters, the store, or the enterprise | services only |

The discovery service has a third, one-method interface: `OrchestratorDirectory`. It lives in its own
process and shares no code with the engine ([discovery contract](../contracts/discovery.md)).

```mermaid
classDiagram
    direction LR
    class EngineApi {
        <<interface>>
        api.py
    }
    class BuilderEngine {
        runtime/engine.py
    }
    class Services {
        DiscoveryService
        JobService
        PaymentsService
        CostService
        KeyService
    }
    class Ports {
        <<interface>>
        protocols/
    }
    class Adapters {
        adapters/ and store/
    }
    class RestService {
        http/create_app
    }
    class Application {
        imported mode
    }
    Application ..> EngineApi : calls
    RestService ..> EngineApi : calls
    BuilderEngine ..|> EngineApi
    BuilderEngine --> Services : holds
    Services ..> Ports : call only
    Adapters ..|> Ports
```

## What the engine offers: the facade

`api.py` splits the engine by role. An application that only runs jobs types against `ActorApi`;
`http/` types against `EngineApi`. Optional capabilities are `X?` members decided once at construction:
`jobs.streaming` (the transport can stream), `keys` (engine API keys are on) and `payments.grants`
(the grant desk is on). Callers check for `None`; nothing probes with `hasattr`.

```mermaid
classDiagram
    direction TB
    class ActorApi {
        <<interface>>
        +jobs JobsApi
        +costs CostsApi
        +discovery DiscoveryApi
        +payments AllowanceApi
        +health() EngineHealth
        +async start() None
        +async stop() None
    }
    class AdminApi {
        <<interface>>
        +payments PaymentsAdminApi
        +keys KeysApi?
        +costs CostsAdminApi
    }
    class EngineApi {
        <<interface>>
        +payments PaymentsApi
        +costs EngineCostsApi
        +authenticator Authenticator
        +events EventFeed
        +sessions SessionManager
    }
    class JobsApi {
        <<interface>>
        +async run(actor, request) JobResult
        +async get(actor, job_id) Job
        +async list_jobs(actor, limit, before) Job[]
        +async result(actor, job_id) JobResult
        +async report_usage(actor, job_id, quantities) Job
        +streaming StreamingApi?
    }
    class StreamingApi {
        <<interface>>
        +async stream(actor, request) JobStreamApi
    }
    class JobStreamApi {
        <<interface>>
        +job Job
        +status_code int
        +content_type str
        +finished bool
        +async for chunk in it
        +async complete() None
        +async aclose() None
        +async disconnected() None
    }
    class DiscoveryApi {
        <<interface>>
        +offerings(actor, app) Offering[]
        +runners(actor, app) Runner[]
        +candidates(app) Runner[]
    }
    class CostsApi {
        <<interface>>
        +async for_job(actor, job_id) JobCost
        +async summary(actor, start, end, actor_id) CostSummary
    }
    class CostsAdminApi {
        <<interface>>
        +async recent(actor, limit) JobCost[]
        +async reconcile(admin, application_id, actor_id) Reconciliation
        +async sync_now(admin) SyncReport
    }
    class AllowanceApi {
        <<interface>>
        +async allowance(actor, actor_id) Allowance
    }
    class PaymentsAdminApi {
        <<interface>>
        +async provision(admin, request) ProvisionedActor
        +async fund(admin, application_id, actor_id, request) ProvisionedActor
        +async set_status(admin, application_id, actor_id, status) ProvisionedActor
        +async rotate_key(admin, application_id, actor_id, operation_ref) ProvisionedActor
        +async revoke(admin, application_id, actor_id) ProvisionedActor
        +async fund_grant(admin, request) None
        +grants GrantsApi?
    }
    class GrantsApi {
        <<interface>>
        +async list_grants(admin) GrantRecord[]
        +async create_grant(admin, name, amount) GrantRecord
    }
    class KeysApi {
        <<interface>>
        +async issue(admin, kind, actor_id, application_id, permissions, label) IssuedKey
        +async list_keys(admin, application_id) AccessKey[]
        +async revoke(admin, key_id) None
        +async bootstrap_admin(label) IssuedKey
    }
    class BuilderEngine
    class FakeEngine
    ActorApi <|-- EngineApi
    AdminApi <|-- EngineApi
    ActorApi --> JobsApi
    ActorApi --> CostsApi
    ActorApi --> DiscoveryApi
    ActorApi --> AllowanceApi
    JobsApi --> StreamingApi
    StreamingApi ..> JobStreamApi : returns
    AdminApi --> PaymentsAdminApi
    AdminApi --> KeysApi
    AdminApi --> CostsAdminApi
    PaymentsAdminApi --> GrantsApi
    BuilderEngine ..|> EngineApi
    FakeEngine ..|> ActorApi
```

Behind the facade, each member is one concrete service: `jobs` is `JobService` (with `Streaming` and
`JobStream`), `discovery` is `DiscoveryService`, `payments` is `PaymentsService` (with `GrantDesk`),
`costs` is `CostService`, `keys` is `KeyService`, and `authenticator` is `AuthenticatorChain`.

## What the engine needs: the ports

Services receive ports and call nothing else: no database, HTTP client or SDK. Each diagram below shows
the services of one concern, the ports they hold, and the default implementations. A service depends
on the narrowest port it needs; `JobService` holds a `JobRepo`, not the whole `EngineStore`.

### Access

```mermaid
classDiagram
    direction LR
    class Authenticator {
        <<interface>>
        +async authenticate(request) ActorContext?
        +challenge() str?
    }
    class AccessPolicy {
        <<interface>>
        +check(actor, operation, resource) None
    }
    class KeyRepo {
        <<interface>>
        +async insert_access_key(key, token_hash) None
        +async access_key_by_hash(token_hash) AccessKey?
        +async revoke_access_key(key_id, revoked_at) bool
        +async list_access_keys(application_id) AccessKey[]
        +async count_active_admin_keys() int
    }
    class AuthenticatorChain {
        -authenticators Authenticator[]
        first ActorContext wins
        NotAuthenticated stops the chain
    }
    class ApiKeyAuthenticator {
        Bearer lpk_ only
        -store KeyRepo
        -realm str
    }
    class DefaultPolicy {
        admin kind passes
        else permission and ownership
    }
    class KeyService {
        +async issue(admin, ...) IssuedKey
        +async list_keys(admin, application_id) AccessKey[]
        +async revoke(admin, key_id) None
        +async bootstrap_admin(label) IssuedKey
    }
    class EnterpriseAuthenticator {
        yours, tried before API keys
    }
    Authenticator <|.. AuthenticatorChain
    Authenticator <|.. ApiKeyAuthenticator
    Authenticator <|.. EnterpriseAuthenticator
    AuthenticatorChain o-- Authenticator : in order
    ApiKeyAuthenticator --> KeyRepo
    KeyService --> KeyRepo
    KeyService --> AccessPolicy
    AccessPolicy <|.. DefaultPolicy
```

Every service holds the same `AccessPolicy` and calls `check` before it reads or writes. A policy
refusal on another actor's record is reported as `NotFound`, so existence is not revealed.

### Discovery and runner transport

```mermaid
classDiagram
    direction LR
    class DiscoverySource {
        <<interface>>
        +async fetch() Runner[]
    }
    class SelectionPolicy {
        <<interface>>
        +order(candidates, request) Runner[]
    }
    class RunnerTransport {
        <<interface>>
        +async call(runner, request, credential, on_signed) RunnerReply
    }
    class StreamingRunnerTransport {
        <<interface>>
        +async open(runner, request, credential, on_signed) RunnerStream
    }
    class RunnerStream {
        <<interface>>
        +status_code int
        +content_type str
        +manifest_ids str[]
        +payment_sent bool
        +async for chunk in it
        +async aclose() None
    }
    class DiscoveryService {
        -source DiscoverySource
        +snapshot DiscoverySnapshot
        +async refresh() DiscoverySnapshot
        +async run(stop) None
        +candidates(app) Runner[]
        +runners(actor, app) Runner[]
        +offerings(actor, app) Offering[]
    }
    class JobService {
        -discovery DiscoveryService
        -transport RunnerTransport
        -selection SelectionPolicy
        -credentials CredentialResolver
        -store JobRepo
        -admission AdmissionLimiter
        +streaming Streaming?
        +async run(actor, request) JobResult
        +async recover_interrupted() int
        +async close_streams() None
    }
    class Streaming {
        +async stream(actor, request) JobStream
    }
    class JobStream {
        owns RunnerStream, slot, attempt
    }
    class HttpDiscoverySource {
        GET LPB_DISCOVERY_URL
    }
    class StaticDiscoverySource {
        no runners
    }
    class MostCapacityFirst
    class SdkRunnerTransport {
        SDK call_runner, signer bearer lpg_
    }
    class SdkRunnerStream
    RunnerTransport <|-- StreamingRunnerTransport
    DiscoverySource <|.. HttpDiscoverySource
    DiscoverySource <|.. StaticDiscoverySource
    SelectionPolicy <|.. MostCapacityFirst
    StreamingRunnerTransport <|.. SdkRunnerTransport
    RunnerStream <|.. SdkRunnerStream
    DiscoveryService --> DiscoverySource
    JobService --> DiscoveryService
    JobService --> SelectionPolicy
    JobService --> RunnerTransport
    JobService --> Streaming : only if the transport has open
    Streaming ..> JobStream : returns
    JobStream --> RunnerStream
```

`JobService.streaming` exists only when the transport is a `StreamingRunnerTransport`; that is decided
once, in the constructor (D33). `on_signed` is the callback a transport calls each time the signer signs
a session, before the paid request goes out, so the evidence is stored first (D42).

### Payments and costs

```mermaid
classDiagram
    direction LR
    class CredentialResolver {
        <<interface>>
        +async credential(actor) PaymentCredential
    }
    class AllowanceReader {
        <<interface>>
        +async allocation_for(application_id, actor_id) str?
        +async read_allowance(allocation_id) Allowance
    }
    class AllocationManager {
        <<interface>>
        +async create_key_for_grant(grant_id, name, amount, idempotency_key) NewAllocationKey
        +async create_key(allocation_id, name, idempotency_key) NewAllocationKey
        +async fund_allocation(allocation_id, amount, idempotency_key) None
        +async fund_grant(grant_id, amount, idempotency_key) None
        +async set_allocation_status(allocation_id, status) None
        +async revoke_allocation(allocation_id) None
        +async revoke_key(api_key_id) None
        +async read_allocation(allocation_id) Allowance
        +async list_grants() GrantRecord[]
        +async create_grant(name, amount) GrantRecord
    }
    class UsageSource {
        <<interface>>
        +async page(cursor, limit) UsagePage
    }
    class SecretStore {
        <<interface>>
        +async put(name, secret) SecretRef
        +async get(ref) str
        +async delete(ref) None
    }
    class PaymentsService {
        +mode per_actor or per_application
        -manager AllocationManager?
        -secrets SecretStore
        -store ActorRepo
        +grants GrantDesk?
    }
    class GrantDesk {
        -manager AllocationManager
    }
    class CostService {
        -store CostStore
        -worker CostSyncWorker?
        -allowances AllowanceReader
        +async for_job(actor, job_id) JobCost
        +async reconcile(admin, application_id, actor_id) Reconciliation
        +async sync_now(admin) SyncReport
        +async run(stop) None
    }
    class CostSyncWorker {
        -source UsageSource
        -store CostRepo
        +async sync_once() SyncReport
    }
    class BatteriesClient {
        engine token, or operator token for the grant desk
    }
    class BatteriesUsageSource {
        GET /v1/usage
    }
    class RoutingSecretStore
    class SettingsSecretStore
    class EncryptedDbSecretStore {
        Fernet, lpb_secrets
    }
    class JobService
    PaymentsService ..|> CredentialResolver
    PaymentsService ..|> AllowanceReader
    PaymentsService --> AllocationManager
    PaymentsService --> SecretStore
    PaymentsService --> GrantDesk
    GrantDesk --> AllocationManager
    CostService --> CostSyncWorker
    CostService --> AllowanceReader
    CostSyncWorker --> UsageSource
    JobService --> CredentialResolver
    AllocationManager <|.. BatteriesClient
    UsageSource <|.. BatteriesUsageSource
    SecretStore <|.. RoutingSecretStore
    RoutingSecretStore o-- SettingsSecretStore
    RoutingSecretStore o-- EncryptedDbSecretStore
```

`CredentialResolver` and `AllowanceReader` are seams between services: `PaymentsService` implements
them, and `JobService` and `CostService` hold only the port. A test, or an enterprise, can then give
the job service credentials without the payments service.

### Persistence

```mermaid
classDiagram
    direction TB
    class EngineStore {
        <<interface>>
        +maintenance StoreMaintenance?
        +async close() None
    }
    class ActorRepo {
        <<interface>>
        get_actor, save_actor, list_actors
    }
    class KeyRepo {
        <<interface>>
        access keys by hash
    }
    class JobRepo {
        <<interface>>
        create_job, start_attempt, record_payment_evidence
        finish_attempt, finish_job, jobs_in_state, ...
    }
    class CostRepo {
        <<interface>>
        cost_checkpoint, apply_usage, match_sessions
        save_projection, cost_snapshot, ...
    }
    class SecretRepo {
        <<interface>>
        put_secret, get_secret, delete_secret
    }
    class CostSnapshot {
        <<interface>>
        one read transaction for cost reads
    }
    class StoreMaintenance {
        <<interface>>
        +async purge(now, jobs_days, usage_days) PurgeReport
        +async schema_revision() str?
    }
    class SqlStore {
        SQLite or PostgreSQL, Alembic
    }
    class SqlCostSnapshot
    class SqlMaintenance
    ActorRepo <|-- EngineStore
    KeyRepo <|-- EngineStore
    JobRepo <|-- EngineStore
    CostRepo <|-- EngineStore
    SecretRepo <|-- EngineStore
    EngineStore --> StoreMaintenance
    CostRepo ..> CostSnapshot : cost_snapshot()
    EngineStore <|.. SqlStore
    CostSnapshot <|.. SqlCostSnapshot
    StoreMaintenance <|.. SqlMaintenance
```

Who holds which repository: `JobService` a `JobRepo`; `PaymentsService` an `ActorRepo`; `KeyService`
and `ApiKeyAuthenticator` a `KeyRepo`; `CostService` a `CostStore` (`JobRepo` and `CostRepo`);
`EncryptedDbSecretStore` a `SecretRepo`. The full method lists are in
[generated/interfaces.md](../generated/interfaces.md#persistence-sub-interfaces).

## Composition: supply, decorate, assemble

`BuilderEngine.create` builds the engine in three stages (`runtime/assembly.py`). There is no DI
container: construction is data (`EngineSupply`, `EngineParts`) and two functions.

```mermaid
classDiagram
    direction LR
    class EngineSupply {
        every field UNSET by default
        None means off
        +store, clock, policy, authenticators
        +api_keys, discovery_source, transport, selection
        +allocation_manager, grants_manager, usage_source
        +secret_store, events, sessions
    }
    class EngineParts {
        frozen, every port resolved
        change one with dataclasses.replace
    }
    class Decorate {
        <<type alias>>
        EngineParts to EngineParts
    }
    class EngineServices {
        store, clock, policy, authenticator
        discovery, jobs, payments, costs, keys
        admission, events, sessions
    }
    class BuilderEngine {
        +async create(settings, supply, decorate)$ BuilderEngine
        +async start() None
        +async stop() None
        +health() EngineHealth
        -resources AsyncExitStack
    }
    class EngineSettings {
        LPB_ settings, tuning values only
    }
    EngineSupply ..> EngineParts : default_parts fills the rest
    Decorate ..> EngineParts : wraps
    EngineParts ..> EngineServices : assemble, no I/O
    BuilderEngine --> EngineServices : holds
    BuilderEngine --> EngineSettings
```

```mermaid
sequenceDiagram
    autonumber
    actor App as Application or lpb serve
    participant BE as BuilderEngine
    participant B as assembly.build
    participant DP as default_parts
    participant Stack as AsyncExitStack
    participant D as decorate (yours)
    participant A as assemble
    App->>BE: create(settings, supply, decorate)
    BE->>B: build(settings, supply, decorate)
    B->>DP: default_parts(settings, stack, supply)
    Note over DP: builds only what was not supplied, in dependency order
    DP->>Stack: own(SqlStore), own(BatteriesClient), own(BatteriesUsageSource)
    DP-->>B: EngineParts (HttpDiscoverySource if LPB_DISCOVERY_URL, else StaticDiscoverySource)
    opt decorate given
        B->>D: decorate(parts)
        D-->>B: EngineParts (e.g. replace(parts, policy=Audited(parts.policy)))
    end
    B->>A: assemble(settings, parts)
    A-->>B: EngineServices (ApiKeyAuthenticator appended after your authenticators)
    B-->>BE: services, resources
    BE-->>App: BuilderEngine
    App->>BE: start()
    BE->>BE: jobs.recover_interrupted()
    BE->>BE: discovery.refresh()
    BE->>BE: tasks lpb-discovery, lpb-cost-sync, lpb-retention
    App->>BE: stop()
    BE->>BE: jobs.close_streams() (drain, then abandon)
    BE->>BE: cancel the tasks
    BE->>Stack: aclose() (clients before the store, failures logged)
```

A part you supply is borrowed: the engine never closes it. A failure part-way through `build` closes
whatever was already opened; a failure in `start` stops the engine.

## A paid job through the ports

The same path as [architecture-diagrams.md](architecture-diagrams.md#6-a-paid-immediate-job-with-failover),
drawn at the level of the ports, with the evidence write at signing (D42).

```mermaid
sequenceDiagram
    autonumber
    actor Caller as Application or http
    participant J as JobService
    participant P as AccessPolicy
    participant R as JobRepo
    participant Adm as AdmissionLimiter
    participant D as DiscoveryService
    participant C as CredentialResolver
    participant S as SelectionPolicy
    participant T as RunnerTransport
    participant Rec as SignedSessionRecorder
    Caller->>J: run(actor, JobRequest)
    J->>P: check(actor, JOBS_RUN)
    J->>R: job_by_operation_ref(...) (OperationExists or OperationConflict if used)
    J->>Adm: admit(app) (NoCapacity before anything is written)
    J->>R: create_job(job, fingerprint)
    J->>D: candidates(app) (none: job failed no_offering)
    J->>C: credential(actor) (NotProvisioned: job failed not_provisioned)
    J->>S: order(candidates, request), cut to max_attempts
    loop each runner, while nothing is paid
        J->>R: start_attempt(in_flight)
        J->>T: call(runner, request, credential.token, on_signed=Rec)
        opt the signer signs a session
            T->>Rec: on_signed(manifest_id)
            Rec->>R: record_payment_evidence(attempt) (shielded)
        end
        alt reply
            T-->>J: RunnerReply(manifest_ids, payment_sent)
            J->>R: finish_attempt(succeeded), finish_job(succeeded)
            J-->>Caller: JobResult
        else RunnerCallError, unpaid and refused or unreachable
            J->>R: finish_attempt(outcome)
            Note over J: fail over to the next runner
        else RunnerCallError, paid, or not a failover outcome
            J->>R: finish_attempt, finish_job(uncertain or failed)
            J-->>Caller: JobFailed(job)
        end
    end
```

A streamed job follows the same steps up to the runner's answer, through `StreamingRunnerTransport.open`,
and then hands the open `RunnerStream` to a `JobStream` ([the streamed request](#a-streamed-request)).

## Discovery: the engine and the discovery service

The engine reads runners through one port, `DiscoverySource`. By default that is
`HttpDiscoverySource`, which reads livepeer-node's `/discovery` format from `LPB_DISCOVERY_URL`. The
discovery service is what usually answers there, and its own port, `OrchestratorDirectory`, decides
which orchestrators it asks.

```mermaid
classDiagram
    direction LR
    class OrchestratorDirectory {
        <<interface>>
        +async orchestrators() Orchestrator[]
    }
    class Orchestrator {
        +url str
        +address str?
    }
    class StaticDirectory {
        +parse(value)$ StaticDirectory
        list or file of base URLs
    }
    class OnChainDirectory {
        -chain EthCall
        -bonding_manager str
        -registry str
        pool walk, isActiveTranscoder, getServiceURI
    }
    class EthCall {
        <<interface>>
        +async call(to, data) str
    }
    class JsonRpcChain {
        eth_call over LPD_ARBITRUM_RPC_URL
    }
    class ApprovedOnly {
        example: wraps any directory
    }
    class Probes {
        <<interface>>
        +async probe_all(orchestrators) Probe[]
    }
    class Prober {
        16 at a time, 5 s, 1 MiB
        deployment trust store
    }
    class Probe {
        +orchestrator Orchestrator
        +entry map?
        +failure untrusted_certificate or failed
    }
    class Catalog {
        +async load_directory() None
        +async probe() None
        +inventory() map[]
        +status() CatalogStatus
        +async run(stop, directory_interval_s, probe_interval_s) None
    }
    OrchestratorDirectory <|.. StaticDirectory
    OrchestratorDirectory <|.. OnChainDirectory
    OrchestratorDirectory <|.. ApprovedOnly
    OrchestratorDirectory ..> Orchestrator : returns
    OnChainDirectory --> EthCall
    EthCall <|.. JsonRpcChain
    Probes <|.. Prober
    Prober ..> Probe : returns
    Catalog --> OrchestratorDirectory
    Catalog --> Probes
```

```mermaid
sequenceDiagram
    autonumber
    participant Cat as Catalog.run
    participant Dir as OnChainDirectory
    participant BM as BondingManager (eth_call)
    participant Reg as AIServiceRegistry (eth_call)
    participant Pr as Prober
    participant O as Orchestrator GET /discovery
    participant DS as DiscoveryService (engine)
    participant H as HttpDiscoverySource
    participant Svc as GET /discovery (this service)
    Cat->>Dir: orchestrators() (every LPD_DIRECTORY_INTERVAL_S)
    Dir->>BM: getTranscoderPoolSize, getFirstTranscoderInPool
    loop at most pool size
        Dir->>BM: getNextTranscoderInPool(address)
    end
    par each pool member
        Dir->>BM: isActiveTranscoder(address)
        Dir->>Reg: getServiceURI(address) (empty, invalid or a manifest: skipped)
    end
    Dir-->>Cat: Orchestrator[] (a failed load keeps the previous directory)
    loop every LPD_PROBE_INTERVAL_S
        Cat->>Pr: probe_all(orchestrators)
        Pr->>O: GET base URL /discovery (verified TLS, no redirects)
        O-->>Pr: runners, or an error
        Pr-->>Cat: Probe[] (entry, or failure)
        Note over Cat: keep the last inventory until LPD_MAX_FAILED_REFRESHES failures in a row
    end
    loop every LPB_DISCOVERY_INTERVAL_S, in the engine
        DS->>H: fetch()
        H->>Svc: GET (no credentials)
        Svc-->>H: [{address, runners}] from Catalog.inventory()
        H-->>DS: Runner[] (a failure keeps the last snapshot)
    end
```

## The REST service (`http/`)

`http/` is the only package that imports FastAPI, and `http/server.py` the only one that imports
uvicorn. It depends on `EngineApi`, never on `BuilderEngine`, so any object that satisfies the facade can
be served.

### Classes

```mermaid
classDiagram
    direction LR
    class server_py {
        <<module>>
        +async serve(settings, supply, decorate, extend) None
        +async build_app(settings, supply, decorate, extend) FastAPI
        -_app(engine, extend) FastAPI
    }
    class Extend {
        <<type alias>>
        FastAPI and EngineApi to None
    }
    class app_py {
        <<module>>
        +create_app(engine, manage_lifecycle) FastAPI
        +actor_dependency(engine) Dependency
        -_authenticated(engine, request) ActorContext
        -_install_error_handlers(app, engine) None
        +JOB_ID_HEADER Livepeer-Job-Id
    }
    class streaming_py {
        <<module>>
        +async stream_response(stream, headers) Response
    }
    class AbortableStreamingResponse {
        -job_stream JobStreamApi
        +async stream_response(send) None
    }
    class StreamingResponse {
        starlette
    }
    class RequestBodies {
        IssueKeyBody, ProvisionBody, GrantBody
        StatusBody, RotateBody, UsageBody
    }
    class FastAPI
    class EngineApi {
        <<interface>>
    }
    class BuilderEngine
    class HTTPBearer {
        declares the lpk_ scheme in OpenAPI only
    }
    server_py ..> BuilderEngine : create
    server_py ..> app_py : create_app
    server_py ..> Extend : calls extend(app, engine)
    app_py ..> FastAPI : builds
    app_py ..> EngineApi : one call per route
    app_py ..> HTTPBearer : when engine.keys is set
    app_py ..> RequestBodies
    app_py ..> streaming_py : streamed jobs
    streaming_py ..> AbortableStreamingResponse
    StreamingResponse <|-- AbortableStreamingResponse
    BuilderEngine ..|> EngineApi
```

`create_app` registers every route as a closure over `engine`: nothing is looked up from a global or
from `app.state`. `actor_dependency(engine)` is the authentication dependency, and routes you add with
`extend` use the same one.

### Routes

Each route authenticates, then makes one engine call. Authorization happens in the services.

| Route | Engine call | Answer |
| --- | --- | --- |
| `GET /livez` | none | `{"status": "ok"}` |
| `GET /readyz` | `health()` | `EngineHealth`, `503` until the first discovery attempt |
| `GET /v1/offerings?app=` | `discovery.offerings` | `Offering[]` |
| `GET /v1/runners?app=` | `discovery.runners` | `Runner[]` |
| `POST /v1/jobs` | `jobs.run` | the runner's status, type and body, with `Livepeer-Job-Id` |
| `POST /v1/jobs?stream=true`, or `Accept: text/event-stream` | `jobs.streaming.stream` | the body streamed as it arrives; `400` if the transport cannot stream |
| `GET /v1/jobs`, `GET /v1/jobs/{id}` | `jobs.list_jobs`, `jobs.get` | `Job[]`, `Job` |
| `GET /v1/jobs/{id}/result` | `jobs.result` | `501` until M4 |
| `POST /v1/jobs/{id}/usage` | `jobs.report_usage` | `Job` |
| `GET /v1/jobs/{id}/cost` | `costs.for_job` | `JobCost` |
| `GET /v1/costs/projections`, `GET /v1/costs/summary` | `costs.recent`, `costs.summary` | `JobCost[]`, `CostSummary` |
| `GET /v1/allowance` | `payments.allowance` | `Allowance` |
| `GET /v1/events`, `POST /v1/sessions` | `events.after`, `sessions.open` | `501` until M4 |
| `POST, GET /v1/admin/keys`, `POST /v1/admin/keys/{id}/revoke` | `keys.*` | only when `engine.keys` is set |
| `POST /v1/admin/applications/{app}/actors/{actor}/allocation` (and `/fund`, `/status`, `/revoke`, `/rotate-key`) | `payments.provision`, `fund`, `set_status`, `revoke`, `rotate_key` | `ProvisionedActor` |
| `GET, POST /v1/admin/grants` | `payments.grants.*` | only when `payments.grants` is set |
| `POST /v1/admin/grant/fund` | `payments.fund_grant` | `204` |
| `GET /v1/admin/reconciliation` | `costs.reconcile` | `Reconciliation` |
| `POST /v1/admin/costs/sync` | `costs.sync_now` | `SyncReport` |

The generated schema is [generated/openapi.json](../generated/openapi.json); CI fails when it drifts.

### Errors and their statuses

Services raise `EngineError`s. `_install_error_handlers` maps each one to a status, so no route catches
anything.

```mermaid
classDiagram
    direction TB
    class EngineError
    class NotAuthenticated {
        401 + WWW-Authenticate from the chain
    }
    class AccessDenied {
        403
    }
    class NotFound {
        404
    }
    class InvalidRequest {
        400
    }
    class NotProvisioned {
        402
    }
    class OperationExists {
        409 + Livepeer-Job-Id + job
    }
    class OperationConflict {
        409 + Livepeer-Job-Id
    }
    class NoCapacity {
        503 + Retry-After
    }
    class ProviderUnavailable {
        503
    }
    class ProviderError {
        provider 400, 404, 409, 422 pass through
        else 502
    }
    class NotImplementedInMilestone {
        501 + milestone + ticket
    }
    class JobFailed {
        status by failure.kind
        + Livepeer-Job-Id + job
        + Retry-After when worth retrying
    }
    EngineError <|-- NotAuthenticated
    EngineError <|-- AccessDenied
    EngineError <|-- NotFound
    EngineError <|-- InvalidRequest
    EngineError <|-- NotProvisioned
    EngineError <|-- OperationExists
    EngineError <|-- OperationConflict
    EngineError <|-- NoCapacity
    EngineError <|-- ProviderUnavailable
    EngineError <|-- ProviderError
    EngineError <|-- NotImplementedInMilestone
    EngineError <|-- JobFailed
```

| `JobFailed` kind | Status | `Retry-After` |
| --- | --- | --- |
| `no_offering` | 404 | |
| `not_provisioned` | 402 | |
| `no_capacity`, `refused`, `unreachable` | 503 | 5 |
| `payment` | 503 | 10 |
| `timeout` | 504 | |
| `runner_rejected` | the runner's own status, else 422 | |
| `runner_error`, `interrupted`, anything else | 502 | |

A request body that fails validation never reaches the engine: FastAPI answers `422`.

### Startup and shutdown

```mermaid
sequenceDiagram
    autonumber
    actor Op as Operator
    participant CLI as cli._serve
    participant Srv as http.server.serve
    participant BE as BuilderEngine
    participant App as create_app
    participant U as uvicorn.Server
    participant LS as lifespan
    Op->>CLI: lpb serve
    CLI->>Srv: serve(EngineSettings.from_env())
    Srv->>Srv: configure_logging(json_lines)
    Srv->>BE: create(settings, supply, decorate)
    Srv->>App: create_app(engine)
    App-->>Srv: FastAPI (routes, error handlers, lifespan)
    opt extend given
        Srv->>Srv: extend(app, engine)
    end
    Srv->>U: serve() on LPB_HTTP_HOST, LPB_HTTP_PORT
    U->>LS: startup
    LS->>BE: start() (recover, first discovery, background loops)
    Note over U: serving requests
    Op->>U: SIGTERM
    U->>LS: shutdown
    LS->>BE: stop()
    U-->>Srv: returns
    Srv->>BE: stop() again in finally (idempotent, covers a server that never started)
```

`build_app` does the same without uvicorn and without logging, for your own ASGI stack; the app's
lifespan still starts and stops the engine. To hold the engine yourself, call `create_app(engine,
manage_lifecycle=False)`.

### A buffered request

```mermaid
sequenceDiagram
    autonumber
    actor C as Client
    participant F as FastAPI
    participant Dep as actor_dependency
    participant Ch as engine.authenticator (AuthenticatorChain)
    participant R as route run_job
    participant J as engine.jobs (JobService)
    participant EH as error handlers
    C->>F: POST /v1/jobs (Bearer lpk_..., JobRequest JSON)
    F->>F: validate body as JobRequest (422 if invalid)
    F->>Dep: resolve Actor
    Dep->>Ch: authenticate(AuthRequest(method, path, headers, client_address))
    alt no authenticator recognizes it
        Ch-->>Dep: None
        Dep->>EH: NotAuthenticated
        EH-->>C: 401 + WWW-Authenticate (challenge from the chain)
    else recognized
        Ch-->>Dep: ActorContext
        Dep-->>R: actor
        R->>J: run(actor, body)
        alt result
            J-->>R: JobResult
            R-->>C: runner status, content type and body + Livepeer-Job-Id
        else JobFailed, NoCapacity, OperationExists, AccessDenied, ...
            J-->>EH: raises
            EH-->>C: mapped status + JSON error (+ job, Livepeer-Job-Id, Retry-After)
        end
    end
```

### A streamed request

The response headers wait for the runner's first chunk. Until then, a failure is an ordinary error
response, exactly like a buffered job. After it, a failure cuts the connection, because the engine never
writes into a stream whose format it does not know (D29, D37).

```mermaid
sequenceDiagram
    autonumber
    actor C as Client
    participant R as route run_job
    participant St as engine.jobs.streaming (Streaming)
    participant JS as JobStream
    participant SR as stream_response
    participant AR as AbortableStreamingResponse
    participant EH as error handlers
    C->>R: POST /v1/jobs?stream=true
    alt engine.jobs.streaming is None
        R-->>C: 400 InvalidRequest
    else the transport can stream
    R->>St: stream(actor, body)
    Note over St: run's steps up to the runner's answer, then open()
    St-->>R: JobStream (job bound to one runner)
    R->>SR: stream_response(stream, Livepeer-Job-Id, no-cache)
    SR->>JS: first chunk
    alt JobFailed before the first chunk
        JS-->>EH: raises
        EH-->>C: 502, 504, ... + job (an ordinary error)
    else empty body
        SR-->>C: runner status, empty body
    else first chunk
        SR-->>AR: response
        AR->>C: http.response.start (runner status and type)
        loop each chunk
            AR->>C: body chunk
        end
        alt runner ends the stream
            JS->>JS: job succeeded
            AR->>C: final empty body
        else JobFailed after the first chunk
            AR--xC: connection closed without the final message
        else client disconnects (send fails)
            AR->>JS: disconnected() (runner gets LPB_STREAM_SETTLE_S, else abandoned)
        end
        AR->>JS: aclose the iterator (finally)
    end
    end
```

### Customizing the server

The REST service is customized like the embedded engine, plus `extend` for routes. `extend` receives
the engine, so a new route needs no lookup:

```python
def extend(app: FastAPI, engine: EngineApi) -> None:
    Actor = Annotated[ActorContext, Depends(actor_dependency(engine))]

    @app.get("/v1/me")
    async def me(actor: Actor) -> dict[str, str]:
        return {"actor_id": actor.actor_id}


asyncio.run(serve(EngineSettings.from_env(), supply=..., decorate=..., extend=extend))
```

A runnable version is in [extending-the-engine.md](extending-the-engine.md#service-mode).

## The discovery service's HTTP server (`discovery/server.py`)

A separate process with its own settings (`LPD_*`). It shares nothing with the engine, imports only
`utils` from the package, and uses Starlette directly.

```mermaid
classDiagram
    direction LR
    class server_py {
        <<module>>
        +async serve(settings, directory, prober) None
        +create_app(catalog) Starlette
        +directory_from(settings) OrchestratorDirectory
    }
    class DiscoverySettings {
        +directory static or onchain
        +orchestrators str?
        +arbitrum_rpc_url SecretStr?
        +bonding_manager, ai_service_registry
        +directory_interval_s, probe_interval_s, probe_timeout_s
        +max_failed_refreshes
        +http_host, http_port
        +from_env(env)$ DiscoverySettings
    }
    class Starlette {
        GET /discovery
        GET /healthz
        GET /readyz
    }
    class Catalog
    class CatalogStatus {
        +ready bool
        +orchestrators int
        +serving int
        +directory_loaded_at datetime?
        +probed_at datetime?
        +directory_error str?
    }
    class OrchestratorDirectory {
        <<interface>>
    }
    class Prober
    server_py ..> DiscoverySettings
    server_py ..> Starlette : builds
    server_py ..> OrchestratorDirectory : directory_from, unless given
    server_py --> Catalog : creates
    Catalog --> OrchestratorDirectory
    Catalog --> Prober
    Catalog ..> CatalogStatus : status()
    Starlette ..> Catalog : inventory(), status()
```

```mermaid
sequenceDiagram
    autonumber
    actor Op as Operator
    participant CLI as cli._discovery
    participant S as discovery.server.serve
    participant Cat as Catalog
    participant U as uvicorn.Server
    actor E as Engine (HttpDiscoverySource)
    Op->>CLI: lpb discovery
    CLI->>CLI: DiscoverySettings.from_env(), configure_logging
    CLI->>S: serve(settings)
    S->>Cat: Catalog(directory_from(settings), Prober(timeout_s))
    S->>Cat: task lpd-refresh = run(stop, intervals)
    S->>U: serve(create_app(catalog)) on LPD_HTTP_HOST, LPD_HTTP_PORT
    E->>U: GET /discovery
    U-->>E: 200 Catalog.inventory() (empty until the first probe pass)
    Op->>U: GET /readyz
    U-->>Op: 200 once a directory loaded and a probe pass finished, else 503
    Op->>U: SIGTERM
    U-->>S: returns
    S->>Cat: stop.set(), cancel lpd-refresh, wait for it
```

To change who is discovered, call `serve(settings, directory=YourDirectory())`. To replace the service
entirely, serve the same `GET /discovery` format from anything and point `LPB_DISCOVERY_URL` at it.

## Questions for the review

Points worth deciding, noticed while drawing these:

1. **`DiscoveryApi.candidates` checks no permission.** `runners` and `offerings` check `DISCOVER`;
   `candidates` does not, because the job service calls it after its own check. It is still on the
   public `ActorApi`, and ChatLPT and inference-gateway call it for readiness. Keep it public and
   document that, or move readiness to a policy-checked call?
2. **`ProviderError` passes some provider statuses through.** A Batteries `400`, `404`, `409` or `422`
   becomes the engine's own status, so a client cannot tell a provider rejection from an engine one.
3. **Route naming.** Grants are `/v1/admin/grants` but funding one is `/v1/admin/grant/fund`, with no
   grant id in the path.
4. **Reserved members.** `EngineApi.events`, `sessions` and `jobs.result` are on the facade and answer
   `501` until M4. Keep them visible so the contract is fixed early, or add them when they work?
5. **Ports that services implement.** `CredentialResolver` and `AllowanceReader` are implemented by
   `PaymentsService`, not by an adapter. That keeps `JobService` and `CostService` testable alone, but
   an enterprise cannot supply them through `EngineSupply`.
