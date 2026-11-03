# Dependency injection: the Build Track plan and two consumers, 23 October 2026

- **Inputs:**
  - Cloud-SPE project 13, "NE-SPE Build Track" (51 items, read 2026-10-23);
  - `chatlpt-sample-enterprise-app` at `1d6fc18`, the embedded consumer;
  - `builder-core-mgmt-ui` at `6cf0c5c`, the "builder console", which calls itself the M3 reference app and uses the stock server.
- **builder-core version:** both apps pin `ad6bba9` on `releases/m3`, an ancestor of the `6c5c689` evaluated in step 1. Everything below holds at the pinned commit.
- **Method:** two facts-only inventories (`file:line` throughout), then spot checks of the claims below against builder-core's source. Nothing was modified or run.
- **Companion:** [di-review-2026-10-23.md](di-review-2026-10-23.md), the step 1 review.

---

## 1. Conclusions

1. **The consumers confirm the step 1 container verdict.** Both apps wire by hand. The console runs `lpb serve` unchanged, and chatlpt builds the engine with one call. Neither needs, or would benefit from, a DI container in builder-core.
2. **The real cost is the facade, and it is now visible.** To customize one default, an embedder has to re-create that default and its configuration. builder-core's own extension guide does this and drops the signer's bearer header. chatlpt did it the same way and drops the same header. It also gave up decorating, re-implemented discovery against `livepeer_gateway`, and imports an internal helper. "Defaults as data" (step 1, Major 2) fixes this directly.
3. **The "reference server" finding is reframed.** The console proves the stock server is enough for a consumer with its own server-side API layer: no CORS, middleware or extra routes needed. What is missing is a documented service-mode composition root for customizing the server, plus a Swagger auth scheme in the stock server. Both belong to ISS-32.
4. **A new gap: there is no consumer-facing port or fake for the engine itself.** builder-core ships fakes for its ports but nothing an embedder can use in place of `BuilderEngine`. chatlpt types the engine as `object`, hand-rolls a duck-typed `FakeEngine` with `# type: ignore[arg-type]`, and detects streaming with `hasattr`.
5. **The plan has a natural home for all of this: ISS-32 (M4, starting 2026-11-23).** Its acceptance criteria require the very things the DI changes enable. The DI refactor should land before ISS-32 begins, so the examples are built on the new API rather than the current one.

---

## 2. Evidence from the consumers, mapped to the step 1 findings

| Step 1 finding | What the consumers show | Updated view |
|---|---|---|
| **Major: the facade is the only full wiring path** | See [2.1](#21-decorating-a-default-means-re-creating-it). Also, chatlpt must set `LPB_BATTERIES_OPERATOR_TOKEN` "even if we never create grants" (`chatlpt docs/references/builder-engine.md:32`), because the grants client is hard-wired to settings | **Confirmed and raised.** It causes a real misconfiguration in two places |
| **Major: the reference server shows no customization** | The console runs stock `lpb serve`, configured only through env vars, on purpose (D2: "whatever it shows is true of every stock deployment"). It needs no server hooks, because its own Next.js server holds the keys and proxies streams | **Reframed.** The stock server is right for this consumer. The gap is service-mode customization (enterprise auth, extra endpoints), which ISS-32 must demonstrate. Today that root exists only outside the package, in the Railway entrypoint |
| Minor: the facade reads env implicitly | chatlpt's `build_engine` takes no settings and calls `EngineSettings.from_env()` (`runtime/engine.py:17`). Its tests have to `monkeypatch.setenv` the `LPB_*` variables to build a real engine (`test_runtime.py:18-40`) | Same pattern, copied one layer up. Make `settings` a parameter in both places |
| Minor: partial construction leaks | chatlpt opens its own store, then builds the engine with no cleanup if that fails (`runtime/app.py:140-144`). `Application.stop()` runs five shutdown steps with no try/finally (`:196-207`) | **The pattern was copied by a consumer.** The exit-stack shape should be what builder-core teaches |
| Minor: third-party functions are patched, not injected | chatlpt's discovery source takes `discover=` as a parameter and its tests inject it, with no patching of `livepeer_gateway` (`tests/unit/test_discovery_filter.py:32-48`) | The consumer already does what the skill recommends. builder-core's own SDK adapter does not |
| Minor: the package root imports every default adapter | chatlpt imports `livepeer_gateway` directly but doesn't declare it (`adapters/discovery.py:18`), the same undeclared-dependency pattern as builder-core's `aiohttp` | Same fix, in both repos |
| Minor: wiring tested through private attributes | chatlpt reaches into `application.chat._images` and the adapter's `_engine` in tests | App-local; not builder-core's problem |

### 2.1 Decorating a default means re-creating it

builder-core documents two extension patterns, Decorate and Replace (`docs/design-docs/extending-the-engine.md:124-128`). Its example wiring decorates discovery like this:

```python
discovery_source = ApprovedOrchestratorsOnly(
    SignerDiscoverySource(signer_url=settings.signer_url), approved={...}
)
```

To decorate the default, the consumer re-builds it. That means:

- importing `SignerDiscoverySource` from a submodule outside the package root's `__all__`;
- repeating its configuration by hand.

The example passes only `signer_url`. It drops `discovery_url` and the `Authorization: Bearer <shared key>` signer header that the default wiring adds (`runtime/engine.py:152-158`).

chatlpt followed the same road:

1. It started as a decorator over `SignerDiscoverySource` (`c5a40e6`).
2. It was then rewritten as a full replacement that calls `livepeer_gateway.discover_runners` itself (`12f35e4`).
3. It imports the internal helper `livepeer_builder.adapters.sdk.runners_from_entries`.
4. It also sends no signer header (`adapters/discovery.py:47-49`).

The original reason for the rewrite has since gone away. The stated reason was that `Runner` dropped the `metadata` field, but at the pinned `ad6bba9`, `Runner.metadata` exists (`contracts/discovery.py:36`) and the SDK adapter fills it (`adapters/sdk.py:311`). The replacement and its explanatory comment remain.

**Why defaults as data fixes this:** `default_parts(settings, stack)` hands the consumer the *configured* default, header included. The consumer writes `replace(parts, discovery_source=Filtered(parts.discovery_source, ...))`, with no internal imports and no repeated configuration. The silent-drop failure becomes impossible rather than merely documented.

**The dropped header is latent today, not live.** The signer here is livepeer-node's (`NE-Build-Track/node`). The console stack pins `406b248`, which shares the clearinghouse container's network namespace on `:8937`.

- **Discovery ignores the header.** At `406b248` and at `b887ad0` (1.0.3), the signer registers `GET /discover-orchestrators` with no authentication in `ServeHTTP` (`signer/service.go`), and the `discover` handler never reads request headers.
- **Payment generation uses it.** `POST /generate-live-payment` forwards the caller's request headers to the auth webhook (`signer/auth.go`, `authorizePayment`), so there the bearer header matters.
- **Consequence:** the omission in discovery bites only a deployment that puts an auth proxy in front of the signer. The copy-the-wiring problem remains either way.

### 2.2 No consumer-facing port or fake for the engine

- **Wiring side:** chatlpt's `Application` stores the engine as `engine: object | None` (`runtime/app.py:64`). It calls `engine.start()` / `stop()` with `# type: ignore[attr-defined]` (`:186,205`).
- **Feature detection:** it picks its streaming adapter with `hasattr(engine.jobs, "stream")` (`:156`).
- **Tests:** they pass a hand-written duck-typed `FakeEngine` (jobs, costs, discovery, health) into adapters annotated `engine: BuilderEngine`, with `# type: ignore[arg-type]` (`tests/unit/test_engine_backend.py:64-152`).
- **Imports:** its fakes need contract values that are not exported from the package root: `Attempt`, `Fees`, `AttemptCost` and `errors.NotFound`. It imports them from submodules.

Every embedder will rebuild this fake, and nothing keeps it in step with the real engine. The fix, in the skill's terms: a `Protocol` (or several) for the surface embedders call, such as `engine.jobs.run/stream`, `engine.costs.for_job/summary`, `engine.discovery.candidates`, `health()`, `start()` and `stop()`. Alongside it, a shipped `livepeer_builder.testing.FakeEngine` pinned by a contract test, and the missing contract types exported from the root.

### 2.3 The stock server's Swagger cannot send a key (inference)

The console's docs present the engine's Swagger UI as the operator's interactive write path (`README.md:44`, `deploy/README.md:71`, compose comment "Swagger and curl: the stack's write path"). The stock OpenAPI spec has no `securitySchemes` at either `ad6bba9` or `6c5c689` (checked). The only code that adds one is the Railway entrypoint's `install_swagger_bearer`, which sits outside the package. So Swagger on stock `lpb serve` most likely has no Authorize button. This was inferred from the spec, not tried in a browser.

**Fix:** declare the bearer scheme inside `create_app`, for example through a FastAPI `HTTPBearer(auto_error=False)` dependency in the actor resolver. Then delete the Railway patch. It is a small change that removes one piece of server customization living outside the package.

---

## 3. The plan

### 3.1 Where the DI work fits

| Item | Acceptance (abridged) | Relation to the DI changes |
|---|---|---|
| **ISS-32** (M4, ENGINE) | "One versioned core candidate in embedded and service examples. Replace standalone authentication with an enterprise adapter … Demonstrate an additional endpoint/tool … through public interfaces … without core patches" | It needs: a service-mode composition root (enterprise auth in service mode requires `authenticators=`, which `lpb serve` cannot pass); defaults as data (examples that decorate defaults without internal imports); a public engine port and fake (embedded examples and their tests); and an extension point for extra routes. `create_app` returns the FastAPI app, so routes can be added after the fact today, but that is undocumented |
| **ISS-33** (M4, APP) | Replace the reference app's access flow "without changing shared core source or requiring its default enrollment flow" | Depends on ISS-32's auth wiring in service mode; the console would talk to a customized server |
| **ISS-30** (M4) | The same journey through MCP | A second transport adapter over the same `BuilderEngine`, like `http/`. It needs one composition root shared by REST and MCP, not a container |
| **ISS-37** (M5) | Version-matched extension guidance | Extension examples should be *imported from tested code*. The current guide's example drops the signer header, a mistake a test would have caught |
| **ISS-38** (M5) | Versioned packages; "public publication … separate authorized actions" | When PyPI publication is authorized, the `livepeer-gateway` git pin blocks the upload: PyPI rejects direct-reference dependencies (verify against current PyPI rules) |

**Correction to my step 2 status note:** I said ISS-32's "avoid an extra user-facing engine key" bumps into the always-appended `ApiKeyAuthenticator`. It does not block it. An enterprise authenticator placed first in the chain authenticates users without any engine key; the API-key authenticator just stays enabled behind it. That leaves a policy question, not a blocker: can a deployment turn engine keys off entirely?

### 3.2 Timing

- M3 runs to 2026-11-20; M4 (ISS-32) starts 2026-11-23.
- The DI changes are internal to builder-core, plus small public additions: `EngineParts`, `default_parts`, `assemble`, a server-side `build_engine`, an engine port and fake, and the Swagger scheme.
- **Recommendation:** land them on the current `dependency-injection-refactor` branch before ISS-32 starts. ISS-32's examples then exercise the new API, and the two existing consumers can migrate off internal imports early. (Adopted; see section 5.)
- The plan's README says items do not commit scope by themselves, so adding this as an explicit task (or folding it into ISS-32's acceptance criteria) is your call.

### 3.3 Board hygiene (observations, outside DI)

- ISS-20 to ISS-25 are "Planned", but the console's merged PR #1 (`m3/reference-app`) and its commits report work on ISS-13, ISS-19 and ISS-24, and ChatLPT reports streaming and accounts delivered. Statuses and dates appear behind the code.
- The console records 14 gaps, 11 of them owned by builder-core (G1–G4, G8–G14: no "who am I" route, no actor listing, admin reads scoped to one application, reconciliation 404s, …). The repo cites no builder-core issues for them. They are API and contract work, not DI, but they are the console's main friction.
- chatlpt's uncommitted change re-pins builder-core to `877d61d` on PR #40's branch, not a release. The pin comment still says `ad6bba9`. Between the pins, failure and cost kinds became `StrEnum`s; chatlpt compares them to strings, which should still work but is untested.

---

## 4. Proposed builder-core DI changes, in order

> **Superseded.** The agreed plan, with a revised order and design, is [dependency-injection-refactor.md](../exec-plans/active/dependency-injection-refactor.md). This section records the proposal as first written.

Each change is small on its own; together they make up the refactor.

| # | Change | Fixes | Cost |
|---|---|---|---|
| 1 | Build `from_settings` into an `AsyncExitStack`; the engine owns the stack | Partial-failure leak; the hand-rolled closers list | Small |
| 2 | `EngineParts` + `default_parts(settings, stack)` + `assemble(settings, parts)`; `from_settings` becomes the three-line facade; every part is replaceable, including the grants manager, event feed and sessions | Major 2; the forced operator token; decorate-without-re-creating | Medium; a new public type to version |
| 3 | Require `settings` (no implicit `from_env`) | Hidden env reads; consumer tests that set env vars | Small |
| 4 | Engine port(s) and `livepeer_builder.testing.FakeEngine` with a contract test; export `Attempt`, `Fees`, `AttemptCost`, `NotFound` from the root; a `supports_streaming` capability instead of `hasattr` | New finding (2.2) | Medium |
| 5 | A server composition root in the package (`build_engine(settings, **overrides)` used by `lpb serve`, Railway and the docs); a bearer scheme declared in `create_app`; a documented way to add routes | Reframed Major 1; Swagger auth; ISS-32 service mode | Small to medium |
| 6 | `SdkRunnerTransport` and `SignerDiscoverySource` take the gateway functions as parameters | Patched SDK functions in tests | Small |
| 7 | Declare `aiohttp`; import default adapters inside `default_parts` | Import weight; undeclared dependency | Small |

**Explicitly not proposed:** a DI container in builder-core or in either consumer.

---

## 5. Decisions

The questions this review raised were decided on 2026-10-23 and are recorded in the plan's decision log ([dependency-injection-refactor.md](../exec-plans/active/dependency-injection-refactor.md#decision-log)):

- API keys become configurable.
- The embedding surface is the public API.
- The refactor is tracked internally, with no board item.
- All seven changes are in scope, in order.

The console's G-series gaps are API and contract work, outside the DI refactor. They will be taken up when the console is reviewed.
