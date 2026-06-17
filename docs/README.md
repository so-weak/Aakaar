# Aakaar Documentation Suite

All Aakaar documentation is published as PDFs in [`pdf/`](pdf/). Each PDF is a
standalone, branded document with a cover page, diagrams, and examples — written
for a specific audience and readable on its own.

> **New here?** Start with **00 — Executive & Product Brief** for the big picture,
> then **01 — Solution Architecture Overview** for how the system fits together.

## The suite

### For leadership & business
| # | Document | What it covers |
|---|----------|----------------|
| 00 | [Executive & Product Brief](pdf/00-EXEC-executive-brief.pdf) | What Aakaar is, the problems it solves, the business value |
| 32 | [Banking Solution Playbooks](pdf/32-PLAYBOOKS-solution-playbooks.pdf) | Reconciliation, disputes, loan processing, KYC as worked stories |
| 33 | [Glossary & Concepts](pdf/33-GLOSSARY-glossary.pdf) | Plain-language definitions of every term |

### Architecture & design
| # | Document | What it covers |
|---|----------|----------------|
| 01 | [Solution Architecture Overview](pdf/01-ARCH-architecture-overview.pdf) | The whole system on one map |
| 02 | [High-Level Design (HLD)](pdf/02-HLD-high-level-design.pdf) | Subsystems, responsibilities, major flows |
| 03 | [Low-Level Design (LLD)](pdf/03-LLD-low-level-design.pdf) | Module-level design of the engine and compliance services |
| 04 | [Database Design](pdf/04-DB-database-design.pdf) | Schema, entities, relationships, tenancy |
| 05 | [Data Flow & Sequence Catalog](pdf/05-FLOW-data-flow-catalog.pdf) | Run lifecycle, intent-to-workflow, remote execution, approval gate |

### Security, compliance & governance
| # | Document | What it covers |
|---|----------|----------------|
| 06 | [Security Overview](pdf/06-SEC-security-overview.pdf) | Trust model, identity, isolation, secrets, agent surface |
| 07 | [Compliance & Governance Guide](pdf/07-COMP-compliance-governance.pdf) | Maker-checker, tamper-evident audit, retention, legal hold, erasure |

### Components
| # | Document | What it covers |
|---|----------|----------------|
| 10 | [Backend & API Service](pdf/10-API-component-backend-api.pdf) | The orchestration core |
| 11 | [Rendezvous Broker](pdf/11-BROKER-component-broker.pdf) | Pairing agents and the platform without fixed addresses |
| 12 | [Web Console](pdf/12-WEB-component-web-console.pdf) | The operator UI |
| 13 | [Remote Agent](pdf/13-AGENT-component-agent.pdf) | The desktop/RPA worker |
| 14 | [Capabilities & Catalog](pdf/14-CAPS-component-capabilities.pdf) | The library of automation building blocks |
| 15 | [MCP Server](pdf/15-MCP-component-mcp.pdf) | Exposing capabilities to AI assistants |

### Getting started & operations
| # | Document | What it covers |
|---|----------|----------------|
| 20 | [Quickstart: Server, Broker & Web](pdf/20-QSCORE-quickstart-server-broker-web.pdf) | Stand up the platform and run your first workflow |
| 21 | [Quickstart: Remote Agent](pdf/21-QSAGENT-quickstart-agent.pdf) | Enroll and connect a desktop agent |
| 22 | [Operations & Runbooks](pdf/22-OPS-operations-runbooks.pdf) | Day-2 operations and incident response |
| 23 | [Disaster Recovery & Business Continuity](pdf/23-DR-disaster-recovery.pdf) | RTO/RPO, backup, tested recovery |

### Reference
| # | Document | What it covers |
|---|----------|----------------|
| 30 | [API Reference](pdf/30-APIREF-api-reference.pdf) | REST endpoints |
| 31 | [Workflow Authoring Guide](pdf/31-AUTHOR-workflow-authoring.pdf) | Designing a workflow from intent to a governed automation |

## Rebuilding the PDFs

The PDFs are generated from the Markdown sources in [`src/`](src/) using a small
Node + Puppeteer + Mermaid pipeline (no LaTeX or external services).

```bash
cd docs/_build
npm install          # first time only (marked, mermaid, puppeteer)
node build.mjs       # rebuild every PDF into ../pdf/
node build.mjs 04-DB # or rebuild a single doc by its source slug
```

- Edit content in `src/<slug>.md` — plain Markdown with ```mermaid fenced diagrams.
- Document titles, codes, and audiences live in `_build/manifest.json`.
- The renderer (`_build/render.mjs`) adds the cover page, styling, and diagram rendering.
