# Aakar — Demo Brief

**Aakar** (आकार — Sanskrit for *form / shape*) — *AI Assist:
**K**now-it, **A**utomate-it, **A**udit-it, **R**un-it.*

**Demo framing.** **Brahma** — the super admin, the creator — wields
**AAKAAR** to give form to **AARYA**, a new tenant on the platform.
AARYA is the PayOps use-case: the tenant that actually runs payment
operations workflows. Inside AARYA, an admin loads credentials into
the vault and an operator describes the work in chat. AAKAAR turns
the request into a validated DAG, runs it on a real browser, and
streams the result back so anyone watching can see exactly what
AARYA did.

A multi-tenant workflow platform: NL prompt → validated DAG → live
browser execution → screenshots streamed into the UI.

---

## Tech stack

**Backend** — Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy +
Alembic · SQLite (dev) → Yugabyte/Postgres (prod) · OpenAI SDK
(tool-calling + JSON mode) · BGE BAAI sentence-transformers + FAISS ·
Playwright Chromium · bcrypt · JWT (HS256, tenant-id claim).

**Frontend** — Vite + React 18 + TypeScript · TanStack Query · React
Router 6 · Tailwind · `@xyflow/react` + dagre · Recharts ·
sessionStorage auth (per-tab sessions).

**Toggles** — `AAKAR_BROWSER_HEADLESS` · `AAKAR_LIVE_SCREENSHOTS`.
All UI timestamps in IST.

---

## Architecture

`Chat (NL) → Planner (LLM) → DAG → Executor → Activities → Browser
pool → SignalHub → Live screen stream`

- **DAG-only LLM** — planner returns DAG / clarify /
  missing-capability; never asks for credentials, restricted to
  granted capabilities.
- **Executor Protocol** — v1 `LocalExecutor` (asyncio);
  `TemporalExecutor` drops in later, same Protocol.
- **Registry split** — `capability` (staff-authored, grantable),
  `action` (primitives), `control` (flow ops).
- **Per-tenant vault** — secrets keyed by
  `(tenant, capability, alias)`, never leave the server.
- **HITL via SignalHub** — captchas, pickers, confirms all use one
  pause/resume primitive.
- **Three personas** — Brahma (super admin, cross-tenant) · AARYA
  admin (vault + live view) · AARYA operator (chat + workflows).

---

## NotebookLM prompt

```text
Script a 4–5 minute Aakar demo from the uploaded screenshots. Use
ONLY what's visible — no invented features, copy, or metrics.

Framing: a multi-tenant workflow platform called AAKAAR (Sanskrit
for "form" — and a backronym: AI Assist · Know-it · Automate-it ·
Audit-it · Run-it). The demo personas are mythologically named so
the narrative is easy to follow:

  • Brahma — the super admin, the creator. Wields AAKAAR to give
    form to new tenants. Cross-tenant operator console + analytics.
  • AARYA admin — admin of the AARYA tenant (the PayOps use case).
    Manages users, grants vault credentials, watches live runs.
  • AARYA operator — tenant user inside AARYA. Describes work in
    chat, runs the resulting workflow.

Architecture spine: NL prompt → planner DAG → browser execution →
live screenshots streamed back.

Output format per scene:
  SCENE [n] · [seconds] · [persona]
  ON SCREEN: …
  ACTION: …
  NARRATION: "…"

Persona-driven five-act arc, 10s hook + 10s close:
  Act 1 — Brahma creates AARYA (the PayOps tenant) and seeds
          its first admin
  Act 2 — AARYA admin loads PayOps credentials into the vault
  Act 3 — AARYA operator describes a PayOps workflow in chat;
          planner returns a validated DAG
  Act 4 — Run executes inside AARYA; live tiles, live browser
          screen, HITL captcha pause/resume
  Act 5 — Dashboards close the loop. AARYA admin sees the
          tenant view; Brahma sees AARYA across the platform.

Tone: confident, plain English. The Brahma/AAKAAR/AARYA framing
is established once at the top of the script and then used as
proper nouns — don't over-narrate the mythology. Every claim must
be backed by a screenshot. Use the actual PayOps workflow shown,
not generic examples. Call out: planner refusing to ask for
credentials, live screenshot panel updating mid-run, HITL captcha
pause, Brahma's cross-tenant view that AARYA admin doesn't have.
Mention IST timestamps once, casually.

If a screenshot shows something not described here, include it. If
folder labels are missing, infer scene by URL/header (`/superuser/*`
→ Act 1, `/admin/grants` → Act 2, `/chat` → Act 3, `/live` or
`/runs/<id>` → Act 4, `/dashboard` → Act 5).
```

---

## Screenshot checklist

| Act | Persona | Files |
|---|---|---|
| 1 | Brahma | `dashboard_super`, `tenants_list`, `aarya_create`, `aarya_detail`, `live_super` |
| 2 | AARYA admin | `admin_users`, `grants_list_masked`, `grant_create_payops`, `grant_edit_rotate` |
| 3 | AARYA operator | `chat_empty`, `chat_dag_payops`, `chat_clarify`, `workflow_save` |
| 4 | All | `run_graph_running`, `run_live_screen`, `run_paused_captcha`, `run_timeline`, `live_tiles_aarya` |
| 5 | Insights | `dashboard_aarya`, `capability_usage_zoom`, `per_tenant_bar_brahma` |

---

## Recording tips

- 1440p+ · sidebar collapsed during run/live shots, expanded for nav.
- Two tabs (Brahma + AARYA admin) — sessionStorage keeps them
  independent so you can cut between them without re-login.
- For Act 4, set `AAKAR_BROWSER_HEADLESS=false` and capture the
  Chromium window as a PiP overlay — the "AARYA, made visible" beat.
- Pre-stage the captcha; don't rely on a live one during recording.
