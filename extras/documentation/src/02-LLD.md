# AAKAAR — Low-Level Design (v1)

> Module-by-module reference for the planner, the executor, the capability catalog, and the signal hub. Code identifiers, file paths, and wire formats are all English; this document follows the same convention.

---

## 1. Codebase layout

```
Aakaar/
  aakar/                       # backend service (FastAPI)
    aakar/
      api/                     # routers, schemas, deps, app factory
        routers/               # auth, admin, chat, chat_sessions, runs, vault, objects
        repositories/          # SQLAlchemy session-scoped data access
        schemas.py             # Pydantic request and response models
        app.py                 # FastAPI app factory
        deps.py                # auth + db dependencies
      planner/
        agentic/               # multi-step agent loop
        openai_impl.py         # one-shot planner using strict JSON
        prompt.py              # prompt templates and registry rendering
        llm.py                 # OpenAI client wrapper
        capability_index.py    # FAISS-backed semantic index
      interpreter/
        executor.py            # protocol + LocalExecutor implementation
        activities/            # primitive activities the executor dispatches
      capabilities/
        web_login/
        file_download/
        file_upload/
        registry.py            # capability + action + control catalog
      workers/
        browser/               # playwright-backed browser worker
        http/                  # httpx-backed http worker
      models/                  # SQLAlchemy ORM models
      migrations/              # alembic
      tests/
  aakar-web/                   # tenant frontend (Vite + React + TS)
  admin-app/                   # third-party bank-ops mock (demo fixture, not the superuser surface)
```

The backend is a single deployable. The planner, executor, capabilities, and workers all live in-process behind clean module boundaries.

## 2. API router map

| Router | Mount point | Notes |
| --- | --- | --- |
| `auth` | `/auth` | login, refresh, logout, /me |
| `admin` | `/admin` | tenant-scoped admin (user CRUD, grants) |
| `chat` | `/chat` | turn-based chat that drives the planner |
| `chat_sessions` | `/chat/sessions` | persisted chat threads |
| `runs` | `/runs` | run get, cancel, events SSE |
| `workflows` | `/workflows` | saved workflow CRUD + version listing + start a run |
| `vault` | `/vault` | per-tenant credential entries |
| `objects` | `/objects` | artifact upload / download |
| `superuser` | `/superuser` | cross-tenant admin surfaces |

All routers depend on `deps.get_db` and `deps.require_user`. Tenant scoping is enforced inside repositories, not by the router, so a misuse at the router layer cannot leak rows from another tenant.

## 3. Planner

The planner has two strategies, picked per turn:

- **One-shot.** The whole prompt is rendered into a single chat completion with strict JSON mode. The model returns a complete DAG. This path is fast and used for prompts that fit one of the well-known templates.
- **Agentic.** The model is allowed to issue tool calls in a loop: `inspect_page`, `navigate`, `login_with_grant`, `done`. The loop ends when the model emits a final DAG or asks a clarifying question.

Both strategies share the registry rendering in `prompt.py` so they always see the same allowed verbs.

### 3.1 One-shot planner sequence

```mermaid
sequenceDiagram
  autonumber
  participant U as User
  participant API as Chat router
  participant P as Planner
  participant R as Registry
  participant L as OpenAI
  participant V as DAG validator

  U->>API: POST /chat with prompt
  API->>P: plan(prompt, tenant, grants)
  P->>R: render_catalog(grants)
  R-->>P: capability list
  P->>L: chat.completions strict JSON
  L-->>P: candidate DAG
  P->>V: validate(dag)
  V-->>P: ok or errors
  P-->>API: DAG or clarification
  API-->>U: planner reply
```

### 3.2 Agentic planner sequence

```mermaid
sequenceDiagram
  autonumber
  participant U as User
  participant API as Chat router
  participant A as Agent loop
  participant R as Registry
  participant L as OpenAI

  U->>API: POST /chat with prompt
  API->>A: start session
  A->>L: turn 1 with tools
  L-->>A: tool_call inspect_page
  A->>R: live page snapshot
  R-->>A: interactive elements
  A->>L: tool_result
  L-->>A: tool_call navigate
  A->>R: page nav
  R-->>A: post-nav snapshot
  A->>L: tool_result
  L-->>A: done(kind="dag")
  A->>A: validate
  A-->>API: DAG or ask_user
  API-->>U: planner reply
```

### 3.3 Validation pipeline

The planner never trusts the model. Every candidate DAG passes through a fixed pipeline:

```mermaid
flowchart TD
  IN["Candidate DAG JSON"] --> P1["Pydantic schema parse"]
  P1 --> P2["Capability allowlist filter (grants)"]
  P2 --> P3["Input typing and required field check"]
  P3 --> P4["Reference resolution (node outputs)"]
  P4 --> P5["Auto-edge completion"]
  P5 --> P6["Cycle detection"]
  P6 --> OK["Accepted DAG"]
  P1 -- fail --> ERR["Reject and return errors"]
  P2 -- fail --> ERR
  P3 -- fail --> ERR
  P4 -- fail --> ERR
  P6 -- fail --> ERR
```

Auto-edge completion is the one piece of leniency: if a node references `${node_a.output}` but the model forgot to declare an edge from `node_a`, the validator inserts the edge.

## 4. DAG types

```mermaid
classDiagram
  class Dag {
    +string version
    +string tenant_id
    +list~Node~ nodes
    +list~Edge~ edges
    +Inputs inputs
  }
  class Node {
    +string id
    +string capability_ref
    +map inputs
    +list~string~ depends_on
  }
  class Edge {
    +string from_node
    +string to_node
    +string label
  }
  class Inputs {
    +map defaults
    +map required
  }
  Dag --> Node
  Dag --> Edge
  Dag --> Inputs
```

`Node.inputs` accepts both literal values and reference expressions of the form `${node_id.output_field}`. References are resolved at run-time by the executor.

## 5. Capability registry

Capabilities, actions, and controls live in three tables.

```mermaid
classDiagram
  class Capability {
    +string id
    +string name
    +string description
    +list~InputSpec~ inputs
    +list~OutputSpec~ outputs
    +list~SignalSpec~ signals
    +list~string~ action_ids
  }
  class Action {
    +string id
    +string name
    +string handler
    +list~InputSpec~ inputs
    +list~OutputSpec~ outputs
  }
  class Control {
    +string id
    +string capability_id
    +string selector
    +string wait_condition
  }
  Capability --> Action
  Capability --> Control
```

Each capability has a Python module under `aakar/capabilities/<id>/` that exports:

- `INPUTS` — Pydantic input schema.
- `OUTPUTS` — Pydantic output schema.
- `SIGNALS` — list of signal specs published.
- `run(inputs, ctx) -> outputs` — async entrypoint.

The registry is loaded at process start. The planner sees only what is registered for the tenant's grants.

## 6. Capability deep dive: `cap.web_login`

```mermaid
sequenceDiagram
  autonumber
  participant E as Executor
  participant W as web_login
  participant V as Vault
  participant B as Browser worker
  participant H as Signal hub
  participant U as User

  E->>W: run(site_id, user_handle)
  W->>V: read(site_id, user_handle)
  V-->>W: credentials
  W->>B: open page
  B-->>W: page loaded
  W->>B: set_field("username", value)
  W->>B: set_field("password", value)
  W->>B: click_by_text("Sign in")
  alt captcha shown
    W->>H: publish captcha signal with screenshot
    H-->>U: render captcha
    U-->>H: resolve with answer
    H-->>W: answer
    W->>B: set_field("captcha", answer)
    W->>B: click_by_text("Verify")
  end
  B-->>W: post-login screenshot
  W-->>E: session_id, cookies
```

## 7. Capability deep dive: `cap.file_download`

```mermaid
sequenceDiagram
  autonumber
  participant E as Executor
  participant D as file_download
  participant B as Browser worker
  participant O as Object store

  E->>D: run(target_hint, session)
  D->>B: navigate if needed
  loop nav recovery
    D->>B: click_by_text(target_hint)
    alt download dialog opened
      B-->>D: dialog detected
    else page redirected
      D->>B: go back
      D->>B: re-resolve target
    end
  end
  B-->>D: downloaded file path
  D->>O: persist(file_path, run_id)
  O-->>D: object_uri
  D-->>E: object_uri, filename, size
```

## 8. Capability deep dive: `cap.file_upload`

```mermaid
sequenceDiagram
  autonumber
  participant E as Executor
  participant U as file_upload
  participant FS as Local FS
  participant B as Browser worker
  participant API as Third-party

  E->>U: run(file_uri, switch_type, cycle, date)
  U->>FS: stage file with user-facing basename (no uuid prefix)
  FS-->>U: temp path
  U->>B: navigate to upload page
  U->>B: set_field("Switch Type", switch_type)
  U->>B: set_field("Cycle", cycle)
  U->>B: set_field("Date", date)
  U->>B: attach file to input[type=file]
  U->>B: click submit (prefer button[type=submit], skip role=tab)
  B->>API: POST multipart upload
  API-->>B: response
  B-->>U: result row
  U-->>E: upload_id, status
```

Three specific lessons baked into v1:

- The local temp file preserves the original suffix. Some upload endpoints reject `''` as an extension and return 415.
- The staged copy uses the **user-facing basename** (recovered from the storage key's `<uuid32hex>_<name>.ext` pattern), so the third party records `biller_transactions_2026_05.csv` rather than `tmpXXXXXX.csv`.
- `click_by_text("Upload")` on a recon-upload page could match both the "Upload" tab and the "Upload" submit button. The capability resolves the click using a custom JS resolver that prefers `button[type='submit']` and skips elements with `role="tab"`.

## 9. Executor

The Executor Protocol is small and stable:

```mermaid
classDiagram
  class Executor {
    +start() RunHandle
    +cancel() void
    +events() AsyncIterator
  }
  class LocalExecutor {
    -ThreadPoolExecutor pool
    -SignalHub hub
    -EventBus bus
  }
  class TemporalExecutor {
    -Client client
    -TaskQueue queue
  }
  Executor <|.. LocalExecutor
  Executor <|.. TemporalExecutor
  note for Executor "Protocol / interface"
```

v1 ships `LocalExecutor`. It walks the DAG topologically, dispatches each ready node to its capability, persists `RunEvent` rows, and pushes them to subscribers (the SSE endpoint and the audit log). `TemporalExecutor` is a Phase 2 roadmap item — comment only today, no class.

```mermaid
flowchart TD
  S["Run start"] --> Q["Queue ready nodes"]
  Q --> N{"Any ready"}
  N -->|"yes"| D["Dispatch node"]
  D --> R["Capability run"]
  R --> SIG{"Signal raised"}
  SIG -->|"yes"| W["Persist paused state"]
  W --> H["Wait for resolution"]
  H --> R
  SIG -->|"no"| E["Persist node done event"]
  E --> Q
  N -->|"no"| F["Run complete"]
  F --> X["Persist final status"]
```

### 9.1 Grant-defaults merge (runtime input shaping)

Before dispatching a `cap.*` node, the executor merges the grant's `input_defaults` into the node's `inputs`. The merge treats `None` and `""` as "missing" so a planner-emitted `login_url: null` does not override a real default from the vault. Real non-empty values from the DAG always win.

### 9.2 Stateful chain ordering

Browser sessions are mutable: navigating, clicking, filling, and uploading each change the visible page. Nodes that operate on the same session must form a strict linear chain (not a parallel fan-out under a single login). The planner prompt enforces this; the executor honors whatever edge structure the validated DAG presents.

## 10. Signal hub (HITL)

```mermaid
classDiagram
  class SignalHub {
    +publish(run_id, signal) id
    +resolve(signal_id, payload)
    +wait(signal_id) Future
    +cancel(signal_id)
  }
  class Signal {
    +string id
    +string kind
    +string description
    +map context
    +string screenshot_uri
  }
  SignalHub --> Signal
```

Signal kinds in v1: `captcha`, `picker`, `otp`, `confirm`. Each resolution is persisted as a `RunEvent` so the audit log preserves who resolved what and when.

## 11. Run event kinds

| Kind | Emitted by | Carries |
| --- | --- | --- |
| `run.started` | Executor | dag snapshot |
| `node.started` | Executor | node id, capability id |
| `node.input` | Executor | resolved input map |
| `node.output` | Executor | output map (PII scrubbed) |
| `node.failed` | Executor | error type, message |
| `node.screenshot` | Browser worker | object_uri, mime |
| `signal.published` | Hub | id, kind, screenshot |
| `signal.resolved` | Hub | id, who, payload |
| `run.succeeded` | Executor | summary |
| `run.failed` | Executor | failed node id |
| `run.cancelled` | Executor | who, reason |

The frontend subscribes via SSE on `/runs/{id}/events`.

## 12. Input defaults merge

A capability declares default inputs. A DAG node may override any of them. Reference resolution happens after merge.

```mermaid
flowchart LR
  A["capability.defaults"] --> M["merge"]
  B["dag.node.inputs"] --> M
  G["grant.input_defaults"] --> M
  M --> R["reference resolution"]
  R --> X["typed inputs into capability.run"]
```

Order of precedence: `dag.node.inputs` (non-empty) > `grant.input_defaults` > `capability.defaults`. References are resolved last so they can target merged values.

## 13. Capability semantic index

`CapabilityIndex` embeds each granted capability's description + tags via `sentence-transformers` (`BAAI/bge-small-en-v1.5`) and writes per-tenant FAISS shards to disk. It is refreshed when an admin adds, updates, or removes a grant.

**Status note:** v1 maintains the index on every grant change but the planner does *not* consult it at plan-time today — the prompt builder still renders every granted capability for the model. Wiring `CapabilityIndex.search(prompt, k=12)` into `PromptBuilder` is a planned change (Phase 1, listed in the roadmap) that activates the read path.

## 14. Test harness

Tests live alongside the source under `aakar/tests/`. Categories:

- **Unit** — pure functions in the planner, validator, prompt rendering, discovery scoring.
- **Capability** — each capability has its own test file driving a fake browser or HTTP backend.
- **API integration** — FastAPI TestClient, real database, faked LLM and browser.
- **End-to-end** — small set of scripted runs against a local mock site.

Integration tests must hit a real database, not mocks. Mock and prod divergence has masked broken migrations in the past.

## 15. Reading guide

- For intent and boundaries, read the HLD.
- For request and run lifecycles plus the schema, read the backend doc.
- For UI shape and state, read the frontend doc.
- For what comes next, read the roadmap.
