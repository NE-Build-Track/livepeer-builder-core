# Core beliefs

These are the operating principles for humans and agents working in this repository. Where a
belief can be checked mechanically, the enforcing test is named.

## Product

1. **Network cost, not retail price.** The engine reports wholesale network cost: the expected value
   of tickets at signing. Retail prices, customers, subscriptions and invoices belong to the
   enterprise application.
2. **Missing evidence is never zero.** A cost that has not arrived is `pending`. A paid call with an
   unknown outcome is `uncertain`, never `failed`.
3. **Classify every charge.** Each charge is `delivered`, `failed_after_payment`, `uncertain` or
   `pending`. Charges with no matching attempt are `unattributed`. Reconciliation must balance
   against the provider's own figure.
4. **No money moves implicitly.** Allotment and top-ups are explicit admin commands, and every
   money-moving call carries an idempotency key.
5. **Customize without forking.** Enterprises plug in authentication, policy, secret storage,
   discovery and transport through protocols. Core is never patched.

## Engineering

6. **Enforce invariants, not implementations.** Layering, third-party confinement, structured logging,
   file size and the money types are tests (`tests/structure/`). Their failure messages say how to fix
   the problem.
7. **Parse at the boundary.** Wire formats are converted into frozen contract models in adapters. No
   guessed shapes inside services.
8. **Secrets never ride on records.** Records hold a `SecretRef`. Plaintext keys appear in no database
   column, event or log, and in one response only: the answer in which a standalone clearinghouse hands
   an allocation's key to the enterprise's engine, once (`tests/unit/test_payments.py`,
   `tests/unit/test_allocation_delivery.py`).
9. **Reserved means loud.** Contracts defined ahead of their implementation raise
   `NotImplementedInMilestone` (HTTP 501).
10. **The repository is the system of record.** Decisions, plans and evidence live in `docs/`. If it
    isn't in the repository, an agent can't know it.
11. **Boring, inspectable dependencies.** SQLAlchemy Core rather than a heavy ORM, stdlib `sqlite3`
    for backups, argparse for the CLI. Prefer code an agent can read end to end.
12. **One release, two modes.** Imported and service mode run the same code. HTTP is a thin adapter.
