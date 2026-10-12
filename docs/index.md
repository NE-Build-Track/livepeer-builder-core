# Documentation

This directory is the repository's system of record. Agents and humans read the same files.

| Area | What it holds |
| --- | --- |
| [design-docs/](design-docs/index.md) | Core beliefs, and the decisions behind the design |
| [contracts/](contracts/index.md) | What every public interface promises, and what is reserved for later |
| [exec-plans/](../docs/PLANS.md) | Active and completed execution plans, plus the tech-debt tracker |
| [product-specs/](product-specs/index.md) | The builder outcomes the engine serves |
| [references/](references/index.md) | Upstream pins, wire formats, and runtime evidence |
| [generated/](generated/db-schema.md) | Generated database and REST schemas (`lpb gen-docs`) |
| [PLANS.md](PLANS.md) | How plans work, and the M2 plan index |
| [QUALITY_SCORE.md](QUALITY_SCORE.md) | Quality grade for each domain and layer |
| [RELIABILITY.md](RELIABILITY.md) | Configuration, operations, backup/restore, failure behavior |
| [SECURITY.md](SECURITY.md) | Credentials, secrets, and trust boundaries |

`scripts/check_docs.py` checks that every file here is indexed, every link and anchor resolves, and
every exec plan has the required sections. CI runs it.
