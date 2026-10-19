# Architecture diagrams

Mermaid diagrams of the components and the key flows. Every box and message uses a real class,
route or table name, so each diagram leads straight to the code. The database ER diagram is
**generated** from `store/schema.py` into [generated/db-schema.md](../generated/db-schema.md#entity-relationship-diagram),
so it can't drift. Layer rules are in [ARCHITECTURE.md](../../ARCHITECTURE.md#layers).

**Keep these current:** when a change alters a flow drawn here, update the diagram in the same change.
CI renders nothing, but reviewers and agents read these as text.

## 1. System context

Who talks to whom. The engine sits between enterprise applications and Livepeer's network and
payment components. Orchestrators are assumed to exist on the network.

```mermaid
flowchart LR
    subgraph Enterprise["Enterprise (customer-facing)"]
        User(["Customer / agent"])
        App["Enterprise app<br/>accounts, retail pricing, billing"]
        IdP["Enterprise identity provider<br/>(JWT bearer, M4)"]
    end
    subgraph Engine["livepeer-builder (this repo)"]
        API["REST service<br/>lpb serve"]
        Core["BuilderEngine<br/>discovery, jobs, payments, costs, access"]
        DB[("SQLite / PostgreSQL<br/>lpb_ tables")]
    end
    subgraph Payments["Payment operator (Mode A: self-operated)"]
        Signer["livepeer/node<br/>remote signer"]
        Bat["Clearinghouse Batteries<br/>webhook + management API + ledger"]
        Kafka[["Kafka<br/>create_signed_ticket"]]
    end
    subgraph Network["Livepeer network"]
        Orch["Orchestrators"]
        Runner["Live Runners<br/>(runner apps)"]
        Chain[("Arbitrum One<br/>TicketBroker, BondingManager")]
    end
    User --> App
    App -->|"imported (in-process)"| Core
    App -->|"or REST + engine key / JWT"| API --> Core
    IdP -.->|"tokens (M4)"| App
    Core --- DB
    Core -->|"discover, sign (SDK)"| Signer
    Core -->|"provision, fund, /v1/usage"| Bat
    Core -->|"paid call (SDK call_runner)"| Orch --> Runner
    Signer -->|"authorize webhook"| Bat
    Signer -->|"events (durable outbox)"| Kafka --> Bat
    Signer -.->|"deposit / reserve checks"| Chain
    Orch -.->|"redeem winning tickets"| Chain
```

## 2. Engine components

The layers, and the protocol seams through which enterprises extend the engine. Services call
**only** protocols. Adapters and the store implement them, and the runtime wires one to the other.
Every layer uses the frozen models in `contracts/`.

```mermaid
flowchart TB
    subgraph UI["http/ — REST service ([server] extra)"]
        Routes["create_app: /v1/jobs, /v1/offerings, /v1/costs, /v1/admin/*"]
    end
    subgraph Wiring["runtime/ — app wiring"]
        BE["BuilderEngine.from_settings · start · stop · health"]
    end
    subgraph Services["services/ — business rules"]
        direction LR
        Policy["DefaultPolicy"]
        Keys["KeyService"]
        Disc["DiscoveryService"]
        Jobs["JobService<br/>AdmissionLimiter"]
        Pay["PaymentsService"]
        Costs["CostService<br/>CostSyncWorker"]
        Res["Reserved: events,<br/>sessions (501)"]
    end
    subgraph Protocols["protocols/ — extension seams (replace without patching core)"]
        direction LR
        PAuth["Authenticator<br/>AccessPolicy"]
        PSec["SecretStore"]
        PNet["DiscoverySource<br/>SelectionPolicy<br/>RunnerTransport"]
        PPay["AllocationManager<br/>UsageSource"]
        PStore["EngineStore"]
    end
    subgraph Impl["adapters/ and store/ — implementations"]
        direction LR
        AAuth["ApiKeyAuthenticator<br/>AuthenticatorChain"]
        ASec["RoutingSecretStore<br/>(settings + Fernet DB)"]
        ASdk["SignerDiscoverySource<br/>SdkRunnerTransport"]
        ABat["BatteriesClient<br/>BatteriesUsageSource"]
        SQL["SqlStore + Alembic<br/>backup · retention"]
    end
    UI --> Wiring
    Wiring -->|"constructs"| Services
    Wiring -->|"injects"| Impl
    Services -->|"call only"| Protocols
    Impl -.->|"implement"| Protocols
```

## 3. Integration modes

The same release runs in both modes. In imported mode the application builds the `ActorContext`
itself; in service mode the authenticator chain builds it.

```mermaid
flowchart LR
    subgraph A["Variant A: imported"]
        AppA["Enterprise backend"] -->|"ActorContext (built after its own login)"| EngA["BuilderEngine"]
    end
    subgraph B["Variant B: service"]
        AppB["Enterprise backend or client"] -->|"Authorization: Bearer lpk_… (JWT in M4)"| Http["REST service"]
        Http -->|"AuthenticatorChain → ActorContext"| EngB["BuilderEngine"]
    end
    EngA --> Pol["AccessPolicy.check on every operation"]
    EngB --> Pol
```

## 4. Request authentication and authorization

```mermaid
sequenceDiagram
    autonumber
    actor C as Client
    participant H as http/app.py
    participant Chain as AuthenticatorChain
    participant K as ApiKeyAuthenticator
    participant S as SqlStore (lpb_access_keys)
    participant Svc as Service (e.g. JobService)
    participant P as DefaultPolicy
    C->>H: GET /v1/jobs/{id} (Authorization: Bearer lpk_…)
    H->>Chain: authenticate(AuthRequest)
    Chain->>K: authenticate(request)
    K->>S: access_key_by_hash(sha256(token))
    alt unknown or revoked key
        K-->>H: NotAuthenticated → 401 + WWW-Authenticate
    else not an lpk_ token
        K-->>Chain: None (try the next authenticator)
    else valid
        K-->>Chain: ActorContext(actor, application, permissions, kind)
    end
    H->>Svc: get(actor, job_id)
    Svc->>P: check(actor, JOBS_READ, ResourceRef(owner))
    alt another actor's job
        P-->>Svc: AccessDenied → reported as 404 (existence not revealed)
    else allowed
        Svc-->>C: Job
    end
```

## 5. Explicit provisioning (per_actor)

Money moves only on an admin command, with an idempotency key. The key goes straight into the
`SecretStore`; the database stores only a `SecretRef`.

```mermaid
sequenceDiagram
    autonumber
    actor Op as Operator (admin key)
    participant H as REST /v1/admin/.../allocation
    participant Pay as PaymentsService
    participant B as BatteriesClient
    participant Bat as Batteries management API
    participant Sec as EncryptedDbSecretStore
    participant DB as SqlStore (lpb_actors)
    Op->>H: POST {amount: {amount: "1", currency: "USD"}}
    H->>Pay: provision(admin, ProvisionRequest)
    Pay->>Pay: policy.check(ADMIN_ALLOCATIONS)
    Pay->>DB: get_actor(app, actor)
    alt already provisioned
        DB-->>Op: existing ProvisionedActor (no money moves)
    else new
        Pay->>B: create_key_for_grant(grant, name, Money, Idempotency-Key)
        B->>Bat: POST /v1/api-keys {grant_id, amount_usd}
        Bat-->>B: {allocation_id, id, api_key=lpg_…}
        B-->>Pay: NewAllocationKey (secret is a SecretStr)
        Pay->>Sec: put(name, lpg_…) → Fernet ciphertext in lpb_secrets
        Sec-->>Pay: SecretRef("db:…")
        Pay->>DB: save_actor(allocation_id, api_key_id, secret_ref)
        Pay-->>Op: ProvisionedActor (never the key)
    end
```

## 6. A paid immediate job, with failover

The core execution path. The attempt row is written **before** the call. Failover happens only
while nothing has been paid.

```mermaid
sequenceDiagram
    autonumber
    actor A as Actor
    participant J as JobService
    participant Adm as AdmissionLimiter
    participant DB as SqlStore
    participant Pay as PaymentsService
    participant T as SdkRunnerTransport (SDK call_runner)
    participant O as Orchestrator / runner
    participant Sg as node signer
    participant Bat as Batteries webhook
    A->>J: run(actor, JobRequest(app, path, payload, operation_ref))
    J->>DB: job_by_operation_ref → 409 if already used
    J->>Adm: admit(app) → NoCapacity 503 if full (before payment)
    J->>DB: create_job(state=running, fingerprint)
    J->>Pay: credential(actor) → PaymentCredential(token, allocation_id), or NotProvisioned
    loop each candidate (SelectionPolicy order, max_attempts)
        J->>DB: start_attempt(outcome=in_flight)
        J->>T: call(runner, request, credential)
        T->>O: request (no payment)
        O-->>T: 402 challenge (OrchestratorInfo payment params)
        T->>Sg: POST /generate-live-payment (Bearer lpg_…)
        alt signer refuses this orchestrator (400 / 481)
            Sg-->>T: refusal, payment_sent=false
            T-->>J: RunnerCallError(payment_refused)
            J->>DB: finish_attempt → try the next candidate
        else signer authorizes
            Sg->>Bat: authorize webhook (nested Authorization)
            Bat-->>Sg: status 200 + auth_id (or 402 if exhausted → no failover)
            Sg->>Sg: persist create_signed_ticket event (outbox) before replying
            Sg-->>T: payment
            T->>O: request + Livepeer-Payment
            alt runner answers 2xx
                O-->>T: result
                T-->>J: RunnerReply(manifest_ids, payment_sent=true)
                J->>DB: finish_attempt(succeeded, allocation_id, sessions) + finish_job(succeeded, ResultRef)
                J-->>A: JobResult (+ Livepeer-Job-Id)
            else timeout or unreachable after payment
                J->>DB: finish_job(uncertain) — never retried
            end
        end
    end
```

## 7. Cost sync, classification and correction

```mermaid
sequenceDiagram
    autonumber
    participant W as CostSyncWorker (every 30 s)
    participant U as BatteriesUsageSource
    participant Bat as Batteries GET /v1/usage
    participant DB as SqlStore
    participant C as CostService
    W->>DB: cost_checkpoint() → resume cursor
    loop pages
        W->>U: page(cursor)
        U->>Bat: GET /v1/usage?cursor=…
        alt cursor rejected (400)
            W->>DB: reset the cursor, resync from the start (known rows skipped)
        else page
            Bat-->>U: items, next_cursor ("" on the last page)
            W->>DB: apply_usage(new rows, checkpoint) in one transaction
        end
    end
    C->>DB: match_sessions(new rows) on (allocation_id, manifest_id)
    loop each affected job
        C->>DB: usage_for_payment_sessions(matched payment_session_id)
        C->>C: classify → delivered / failed_after_payment / uncertain / pending
        C->>DB: save_projection(observed, or corrected if the total changed)
    end
```

## 8. Reconciliation

Batteries is the authority. The engine shows how the provider's figure splits across cost classes.

```mermaid
sequenceDiagram
    autonumber
    actor Op as Operator (admin key)
    participant C as CostService.reconcile
    participant Pay as PaymentsService (AllowanceReader)
    participant DB as SqlStore
    participant Bat as Batteries
    Op->>C: GET /v1/admin/reconciliation?actor_id=alice
    C->>Pay: allocation_for(app, actor)
    C->>DB: jobs_between(app, actor) → classify each attempt
    C->>DB: unattributed_usage(allocation) (rows with no matched payment session)
    C->>Pay: read_allowance(allocation)
    Pay->>Bat: GET /v1/allocations/{id} → spent_usd
    C-->>Op: by_class + unattributed + balanced (spent == sum)
```

## 9. Restart recovery

```mermaid
sequenceDiagram
    autonumber
    participant BE as BuilderEngine.start
    participant J as JobService.recover_interrupted
    participant DB as SqlStore
    BE->>J: on start, before serving
    J->>DB: jobs_in_state("running")
    loop each interrupted job
        alt any attempt in_flight or payment_sent
            J->>DB: finish_job(uncertain, Failure(interrupted))
        else nothing could have paid
            J->>DB: finish_job(failed, Failure(interrupted))
        end
    end
    BE->>BE: discovery refresh, then the background loops (discovery, cost sync, retention)
```

## 10. Persistent session (planned, M4 · ISS-27)

Defined in the contract and not implemented yet; `POST /v1/sessions` answers 501 today. **The engine
proxies all session traffic** (D9).

```mermaid
sequenceDiagram
    autonumber
    actor C as Client
    participant E as Engine (SessionManager, planned)
    participant SDK as SDK reserve_session
    participant O as Orchestrator /apps/{runner}/session
    participant Sg as Signer
    C->>E: POST /v1/sessions {app}
    E->>SDK: reserve_session(signer, actor's lpg_ key, app)
    SDK->>O: reserve → app_url, control_url
    SDK->>Sg: time-based payments (funded in the engine process)
    E-->>C: Session(proxy_path) — never the app_url
    C->>E: HTTP / SSE / WebSocket on proxy_path (authenticated every request)
    E->>O: forward to app_url
    C->>E: close → stop funding, stop the session
```

## 11. State machines

### Job

```mermaid
stateDiagram-v2
    [*] --> queued: submit (async, M4)
    [*] --> running: run (immediate)
    queued --> running: worker picks it up (M4)
    running --> succeeded: runner 2xx
    running --> failed: refused everywhere, not provisioned, runner 4xx/5xx, signer refusal
    running --> uncertain: paid, then timeout or unreachable
    running --> uncertain: restart with an in_flight or paid attempt
    running --> failed: restart, nothing could have paid
    succeeded --> [*]
    failed --> [*]
    uncertain --> [*]: reconciled by an operator
```

### Job cost

```mermaid
stateDiagram-v2
    [*] --> none: no attempt opened a payment session
    [*] --> pending: paid, no applied usage row yet
    pending --> observed: every paid attempt has rows
    observed --> corrected: the total changed after first observed
    corrected --> corrected: changes again
```
