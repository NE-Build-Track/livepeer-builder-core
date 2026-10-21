# livepeer-builder

The shared builder engine for Livepeer. It discovers capabilities, runs paid jobs through the
Python gateway SDK, controls access, and reports network cost. Use it in two ways:

- **Embed it** in your Python backend.
- **Run it as a REST service** with `lpb serve`.

Retail pricing, customer accounts and billing stay in your application. The engine reports
wholesale **network** cost and classifies every charge, so you can reconcile what you bill.

> Status: **M2 scaffold** (Cloud SPE Build Track). The contracts are defined; the core paths work.
> Async jobs, persistent sessions and the events feed are defined but not yet implemented: they
> answer `501`. See [docs/PLANS.md](docs/PLANS.md).

## Install

```sh
uv sync --all-extras          # development
pip install "livepeer-builder[server]"            # REST service
pip install "livepeer-builder[server,postgres]"   # optional PostgreSQL store
```

M2 pins the SDK to the NE-Build-Track fork at the head of its `dev/bytestreaming` branch (see
[docs/references/upstream-baseline.md](docs/references/upstream-baseline.md)).

## Embed it

```python
from livepeer_builder import ActorContext, BuilderEngine, EngineSettings, JobRequest

engine = await BuilderEngine.from_settings(EngineSettings.from_env())
await engine.start()
actor = ActorContext(actor_id="customer-42", permissions=frozenset({"discover", "jobs.run", "jobs.read"}))
result = await engine.jobs.run(actor, JobRequest(app="livepeer-example/echo", path="/echo", payload={"x": 1}))
cost = await engine.costs.for_job(actor, result.job.id)  # "pending" until Batteries reports it
```

## Run the service

```sh
export LPB_DATABASE_URL=sqlite+aiosqlite:///./livepeer-builder.db
export LPB_SIGNER_URL=https://signer.example LPB_BATTERIES_URL=https://batteries.example
export LPB_BATTERIES_TOKEN=... LPB_BATTERIES_OPERATOR_TOKEN=... LPB_SECRET_KEY="$(uv run lpb gen-secret-key)"
uv run lpb migrate
uv run lpb bootstrap-admin   # prints the first admin key once
uv run lpb serve
```

Configuration, backup/restore and retention are covered in
[docs/RELIABILITY.md](docs/RELIABILITY.md). Credentials and trust boundaries are covered in
[docs/SECURITY.md](docs/SECURITY.md).

## Develop

See [AGENTS.md](AGENTS.md) for the repository map and commands, and
[ARCHITECTURE.md](ARCHITECTURE.md) for the layers. To run a real node stack locally, see
[deploy/dev-stack](deploy/dev-stack/README.md). To deploy Batteries, the signer, and the engine on Railway, see
[deploy/railway](deploy/railway/README.md).

## License

MIT. See [LICENSE](LICENSE).
