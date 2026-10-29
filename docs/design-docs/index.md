# Design docs

| Document | Status | Verified against code |
| --- | --- | --- |
| [core-beliefs.md](core-beliefs.md) | Accepted | Partly enforced by `tests/structure/` |
| [m3-decisions.md](m3-decisions.md) | Decisions after the M2 close-out: streaming, abandoned streams, runner metadata, the stream lifecycle fixes (D34–D39) | yes |
| [m3-guides/](m3-guides/index.md) | One implementation guide per remaining M3 engine change (architecture, code areas, test plan, open questions) | Proposals, verified against `releases/m3` `d3df806` |
| [m4-guides/](m4-guides/index.md) | One implementation guide per M4 tracker (ISS-26 to ISS-36) | Proposals, verified against `releases/m3` `d3df806` |
| [m5-guides/](m5-guides/index.md) | One implementation guide per M5 tracker (ISS-37 to ISS-39) | Proposals, verified against `releases/m3` `d3df806` |
| [2026-10-29-media-work-plan.md](2026-10-29-media-work-plan.md) | Map of the media and session requests (#73, #68, Mux, media runners) onto the M3/M4 guides; what still needs a ticket | Yes, against `d3df806`, node `406b248`, Batteries `ca09e47` |
| [m2-decisions.md](m2-decisions.md) | Accepted 12 October 2026 | Yes. Each decision names where it lives |
| [m2-review-2026-10-12.md](m2-review-2026-10-12.md) | Record of the 12 October review: findings, prototype defects and their guards | Yes |
| [extending-the-engine.md](extending-the-engine.md) | How enterprises replace the defaults through the interfaces: class diagrams, wiring, rules, runnable examples | Yes. Examples run in `tests/unit/test_extension_examples.py` and `test_service_mode_example.py` |
| [architecture-diagrams.md](architecture-diagrams.md) | Context, components, integration modes, key sequences, state machines (Mermaid) | Yes. Names match the code; the ER diagram is generated |
| [interfaces-and-http-servers.md](interfaces-and-http-servers.md) | Class and sequence diagrams of the interfaces (the facade, the ports, composition, the discovery directory) and of the REST and discovery HTTP servers, with questions for review | Yes, against the discovery-service branch (`dfdd647`) |
| [builder-layer-proposal.md](builder-layer-proposal.md) | The 1 October proposal: each idea and its disposition here | Yes |
| [di-review-2026-10-23.md](di-review-2026-10-23.md) | Dependency-injection review of the package and reference server: container verdict, findings, fixes | Yes, at `6c5c689`; two read-only probes |
| [di-consumers-and-plan-2026-10-23.md](di-consumers-and-plan-2026-10-23.md) | The DI findings checked against ChatLPT, the builder console and the Build Track plan; proposed change order | Yes, at the consumers' pin `ad6bba9` |

These documents feed the design (they are not in this repository):

- the Build Track baseline and architecture companion (Cloud-SPE/Network-Engineering-SPE,
  `docs/design-docs/self-sovereign-open-builder-stack-draft.md`, `open-builder-architecture-and-sequences.md`);
- the 1 October builder-layer proposal (`docs/references/analysis/2026-10-01-Builder-Layer-Abstraction-and-Two-Application-Review.md`);
- John's integration drafts on `eliteprox/Network-Engineering-SPE` branch `jm/builder-integration-plans`
  (`300c127`). They were harvested into `docs/contracts/`.
