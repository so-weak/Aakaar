<!-- Generated 2026-06-22 by a 17-agent design workflow (map subsystems -> design -> adversarial critique -> finalize). See task w9m8as8t7. -->

# Browser-on-Agent: Authoritative Implementation Plan

## 1. TL;DR

Run the full stateful browser/cap stack on the agent by **relocating the Playwright runtime and `browser.*`/`cap.web_*` handlers into the shared `aakaar_caps` package** (write-once, run-either), widening `CapabilityContext` into the single context surface both hosts satisfy. The server wires that surface to its real `ActivityContext` (byte-identical to today); the agent backs it with a **run-scoped Playwright pool plus a full bidirectional request/response multiplexer** over the existing WS/broker channel — object bytes, HITL signals, and LLM planner calls are **WS-RPC proxies to the server** (chunked, since broker agents have no HTTP path), so the canonical object store, `SignalHub`, vault, and OpenAI key never leave the server. **The single biggest risk is lifecycle/security divergence of stateful sessions across a lossy wire**: a lost reply or agent drop mid-session can orphan a live Chromium holding plaintext banking cookies, or mis-route/duplicate a side-effecting login — failure modes that are structurally impossible in today's single-process server. The plan makes session-bearing refs non-retryable/idempotent-by-node, fails-fast on agent loss exactly as the server fails on restart, reaps sessions on disconnect (not just TTL), and ships the entire path dark behind a dedicated `remote_browser_enabled` flag with sealed-secret transport before any credential-bearing cap is reachable remotely.

## 2. Key design decisions

| Decision | Choice | Why | Rejected alternatives |
|---|---|---|---|
| Shared-code strategy | **Move browser runtime + caps into `aakaar_caps`; both hosts import the same modules** | Eliminates server/agent drift — the explicit `_SharedCap`/`register_shared` invariant (capabilities/__init__.py:55, _shared.py:59); the 3 stateless caps already prove it. We extend the *context surface*, not the number of copies. | (B) duplicate handlers in agent → guaranteed drift; (C) contract-on-server/handler-on-agent (desktop-cap split) → inverse of the goal. |
| Context surface | **Widen `CapabilityContext` with `node_id`, `run_id`, `tenant_id`, `browser_pool`, `session_state`, `signals`, `object_reader/writer`, `text_completer`, AND a new `planner_completer`** | Server fills from `ActivityContext`; agent fills from proxies. Same dataclass → identical handler code path on both hosts. | Keeping `text_completer` only — **rejected, see LLM seam below**: it cannot represent web_login's call. |
| LLM seam for `web_login` | **Add `planner_completer: Callable[[list[LLMMessage]], PlannerCompletion] \| None` to the context + an `llm_plan` back-channel frame; move `LLMMessage`/`PlannerCompletion` into a portable shared module.** | VERIFIED: web_login:425 calls `ctx.llm.complete_planner(messages)->PlannerCompletion` (llm.py:152), a multi-turn structured call. `complete_text(system,user)->str` (llm.py:160) **cannot represent it** — "refactor to complete_text" would silently change disambiguation behavior. This is **not** a deferred open question; it gates Stage 1. | (a) collapse to `complete_text` — rejected (semantic change); leaving it as Open Question — rejected (it's load-bearing). |
| Object-store transport | **WS-RPC `obj_get`/`obj_put` over the existing channel as the PRIMARY path, with chunked multi-frame reassembly under the 16 MiB agent / 32 MiB broker frame caps; HTTP `POST /objects` only as a direct-mode optimization.** | VERIFIED: broker agents have **no inbound HTTP address** (broker_link.py:1-27) — both sides dial out. The draft's "HTTP primary, WS fallback" is backwards. Server injects `tenant_id` from authenticated identity (never the request body), keeping the tenant-scoped store the single canonical home. | Inline base64 in `RemoteResult` → breaks 16 MiB `max_size` (client.py:134) + bloats timeline; agent-local store → dangling `aakaar://` URIs (object_store.py:113); HTTP-primary → impossible on broker. |
| Back-channel mechanism | **A dedicated bidirectional request/response multiplexer (pending-futures keyed by `request_id`) on BOTH agent and server, layered above the one-shot task-result model — its own stage (Stage 3) ahead of object/signal/llm wiring.** | VERIFIED: `connection.py:34-42` parks on a single `task_id` future resolved only by the terminal `result`; agent only ever calls `_send_reply` (client.py:209). There is no mid-task request primitive in either direction. This is the spine, not a footnote. | Reusing the task future — impossible (resolves once, terminally). |
| Session affinity & resume | **Affinity is a property of the SESSION; bind `run_id→conn` and persist it to the run/checkpoint row; on resume, refuse to place a session-bearing node on a different agent and fail-fast with the SAME "session not resumable" error the server raises.** | VERIFIED: orchestrator.py:232-233 — `run_target` is intentionally **NOT** persisted across resume. The draft's claim that run-level pin "already pins a whole run" is **WRONG for resume**: post-restart, `navigate`/`fill_secret` fall back to `node.target` and can mis-route to agent B (or duplicate a login). Mirror broker_link.py:27 "sessions are re-established, not resumed." | Run-level `ctx.run_target` pin alone — rejected (not durable across resume; data-integrity hazard). |
| Stateful retry/redelivery | **Session-bearing refs are non-retryable-with-fresh-session; `open_session` is idempotent per `(run_id,node_id)`; its cached reply is invalidated on `run_end`. Classify by an explicit `stateful_session` flag on `CapabilitySpec`, NOT by presence of a `session` input.** | VERIFIED: retries mint a fresh `task_id` (no dedup), and the dedup cache (client.py:176-205) re-delivers stale `{session: id}` for a torn-down context. `open_session` has no `session` input, so the draft's "has a session input" predicate is both under- and over-inclusive. | "Bypass cache for refs with a `session` input" — rejected (misclassifies open_session). |
| Trust transport for secrets/blobs | **Application-layer sealed-box encryption of the `secrets` envelope (and obj_put bodies) to the agent's enrolled public key; browser/credential caps refuse the broker path unless sealed transport is active; enforce `wss://`.** | VERIFIED: broker_link.py:9-12 relays frame bodies **verbatim** — a hostile broker operator would otherwise see every banking credential + statement in cleartext. Today the blast radius is shell output; after this change it is the bank. | "Same exposure as today, bounded by TLS" — rejected (TLS isn't enforced; broker sees cleartext). |
| Browser-capability eligibility | **Admin-set enrollment attribute (like pools), gated by a real startup launch-probe; the hello `browser_capable` flag is only a liveness signal, never authoritative for routing credentials.** | VERIFIED: hello fields are agent-claimed/untrusted (protocol.py:105). A malicious agent could claim `browser_capable` + advertise `cap.web_login` to attract credential envelopes. `load_capabilities` swallows import errors (capabilities/__init__.py:48), so module-import is not proof Chromium launches. | Trusting agent-claimed `browser_capable` for placement — rejected (attracts credentials to hostile agents). |
| Kill-switch | **Dedicated `remote_browser_enabled` config flag (default False), independent of `remote_exec_enabled`, gating browser advertisement + back-channel frames + credential-bearing placement.** | `remote_exec_enabled=False` disables ALL remote exec (too coarse); operators need to roll back just the risky new path under incident. | Relying on `remote_exec_enabled` alone — rejected (no granular rollback). |

## 3. Staged implementation plan

The whole feature ships **dark** behind `remote_browser_enabled=False` until Stage 8. "Shippable on its own" below means "merges green and changes nothing observable while the flag is off."

### Stage 0 — Shared context surface + portable LLM types (no behavior change)
- **Goal:** Widen the context and make planner types portable; nothing wires them yet.
- **Files:**
  - `aakaar-capabilities/aakaar_caps/context.py` — add `node_id:str=""`, `run_id:str=""`, `tenant_id:str=""`, `browser_pool:Any=None`, `session_state:dict=field(default_factory=dict)`, `signals:Any=None`, `planner_completer:Callable[[list[LLMMessage]],PlannerCompletion]|None=None`. Add `read_object/write_object/complete_plan` accessors that raise `CapabilityError` when `None`.
  - New `aakaar-capabilities/aakaar_caps/llm_types.py` — portable `LLMMessage`/`PlannerCompletion` mirrors (or relocate the minimal dataclasses from `aakaar/planner/llm.py`); server re-exports from old path.
  - `aakaar_caps/spec.py` — add `side_effecting: bool|None=None` and `stateful_session: bool=False` to `CapabilitySpec`.
  - `aakaar_caps/loader.py` — `iter_modules`→`walk_packages` (mirror _base.py:67); skip `_`-prefixed/`remote_only`.
- **Tests:** construct `CapabilityContext()` with new defaults; `load_specs()` still returns the 3 existing caps; nested-fixture cap discovered; `side_effecting` default is `None` (fail-safe = simulate).
- **Risk:** Low. `session_state` must use `default_factory`.
- **Shippable on its own?** Yes.

### Stage 1 — Relocate browser runtime + handlers into `aakaar_caps` (server still runs everything locally)
- **Goal:** Same Playwright runtime + handlers importable/runnable by both hosts against the widened context.
- **Files:**
  - Move `aakaar/workers/browser/{session,playwright,fake}.py` → `aakaar_caps/browser/`; re-export from old paths for back-compat. (`PlaywrightBrowserPool.checkout` keeps `profile` ignored, playwright.py:370 — see edge cases.)
  - Port `aakaar/interpreter/activities/browser.py:51-270` (`_SessionHolder`, `_stash_key`, `_get_session`, the 14 `browser.*` primitives) → `aakaar_caps/caps/browser_*.py`; session-stash helpers → `aakaar_caps/browser/state.py`. Old `browser.py` becomes a thin shim re-exporting `_SessionHolder`/`_stash_key` so `executor._maybe_emit_live_screen` (executor.py:437) and `orchestrator` cleanup (orchestrator.py:460) keep working.
  - Port `aakaar/capabilities/{open_url,screenshot,web_login,file_download,web/*}` → `aakaar_caps/caps/` as `SPEC + async run(ctx,inputs)`. **web_login reads credentials from `ctx.secrets`**; **`_llm_disambiguate` uses `ctx.complete_plan` (the new `planner_completer`)**, not `ctx.llm.complete_planner`. Mark read-only caps `side_effecting=False` explicitly; `web_login`/`file_download`/`fill_secret` carry `stateful_session=True` and/or `side_effecting=True`.
  - `aakaar/capabilities/_shared.py:30` (`_server_context`) — wire `browser_pool/session_state/signals/node_id/run_id/tenant_id` from `ActivityContext`; `object_reader/writer = asyncio.to_thread(object_store.get/put, ...)`; `text_completer=llm.complete_text`; `planner_completer=llm.complete_planner`; `secrets=fetch_credentials(...)` as today. Flow `side_effecting`+`stateful_session` from `CapabilitySpec` into `CapabilityDefinition` (_shared.py:66).
  - Delete the now-duplicated server-only browser cap modules (`_base.py:52`, registry.py:46).
- **Tests:** **Full existing server browser/web_login/screenshot suite green, unchanged** (FakeBrowserPool). `aakaar_caps` imports without the server package. **web_login byte-identical vs server FakeLLM through `planner_completer`.** **Per-cap assertion: `/capabilities` `CapabilityDefinitionResponse` (ref, inputs w/ type_label+required+description, outputs, secret_names, tags) byte-identical before vs after relocation** (catches lossy spec round-trip). **Per-cap `side_effecting` classification matches pre-relocation dry-run behavior.**
- **Risk:** High-surface refactor; the credential-source + LLM-seam refactors are the subtle bits. Server-only execution is the only path exercised, so regressions are caught here.
- **Shippable on its own?** Yes (server-only path).

### Stage 2 — Back-channel multiplexer (the spine), both directions
- **Goal:** A request/response correlation layer independent of the task-result future, on agent AND server.
- **Files:**
  - `aakaar-agent/aakaar_agent/client.py` — add `_pending: dict[str,Future]`, `_send_request(frame)->Future` keyed by a new `request_id`; extend the read loop (client.py:149, today only `type=="task"`) to resolve server→agent replies and dispatch server→agent control frames.
  - `aakaar/workers/remote/connection.py` — add a server→agent `request(frame)->reply` primitive + a sibling `_pending` keyed by `request_id`, independent of the task `_pending` (connection.py:28).
  - `aakaar/workers/remote/protocol.py` — frame types: agent→server `obj_get/obj_put/signal_open/llm_complete/llm_plan`; server→agent `run_end/cancel/signal_resolved`. All correlated by `request_id`. `RemoteResult`/`RemoteTask` task framing unchanged.
  - `aakaar/api/routers/agents.py:210` **and** `broker_link.py:373` — a **shared `_demux_agent_frame` helper imported by both** read-loops (the "must never drift" warning) that routes the new frames.
- **Tests:** round-trip each frame; agent issues `obj_put` mid-task and awaits the reply while its task future is still parked; both read-loops demux via the shared helper.
- **Risk:** Two transports drifting → mitigated by the shared helper. This must land before any proxy.
- **Shippable on its own?** Yes (frames are defined but unused while flag off).

### Stage 3 — Agent runtime: pool, run-scoped sessions, proxies, secure lifecycle
- **Goal:** Stateful agent runtime + the security teardown that MUST accompany it (folded in from the critiques — not deferred).
- **Files:**
  - `aakaar-agent/pyproject.toml` — `playwright` under a `browser` extra. **Chromium binaries are provisioned out-of-band** (`playwright install chromium` in the service installer with pinned `PLAYWRIGHT_BROWSERS_PATH`, or an air-gapped mirror) — PyInstaller cannot freeze them.
  - `aakaar-agent/aakaar_agent/client.py` — `self._browser_pool` (lazy, headless, launch-probed at startup); `self._run_sessions: dict[(tenant_id,run_id), dict]` (tenant in the key, threaded from the task); `shutdown` hook → `browser_pool.shutdown()`.
  - `client.py` `_run_and_reply` — widen to thread `run_id/node_id/tenant_id` and build the context. **Enforce `asyncio.wait_for(timeout_s)`** (today none — client.py:191) so agent and server share the deadline. Cancel/`run_end` frames **cancel the in-flight task (`_inflight`) AND close+release the pool checkout**, not just at run-end.
  - `capabilities/__init__.py` `_SharedCap.run`/`dispatch` — widen signature; build context with proxies: `object_reader/writer`→chunked `obj_get/obj_put` (Stage 2), `signals`→`signal_open`, `text_completer`→`llm_complete`, `planner_completer`→`llm_plan`.
  - **Result-cache hardening (client.py:60,176-205):** session-bearing/side-effecting/screenshot/download/web refs are **excluded from `_results` entirely** (re-delivery is moot for them); `_results` stores only `{ok,task_id,error}` + opaque ref for stateless caps; entries cleared on `run_end`. **No screenshot/statement bytes in the LRU.**
  - **`open_session` idempotency:** keyed by `(run_id,node_id)` — a retried `open_session` returns the existing session, never a second Chromium context.
  - **Disconnect-triggered sweep + idle-TTL sweep** of `_run_sessions`: on socket drop, evict/close sessions whose only channel was that socket so a reconnected agent never claims a session it can't prove live.
  - Download teardown: per-session throwaway temp dir (0700); delete download files immediately after reading bytes and on close; disable Chromium persistent download history.
- **Tests:** open→navigate reuse same fake session via `_run_sessions`; retried `open_session` (same node_id) returns existing session, no 2nd context; `wait_for` fires and cancel tears down the checkout; socket-drop sweep closes the session; cache test asserts **no image/byte payloads cached**; download file gone after close_session.
- **Risk:** Sync-over-async object store (use `to_thread`); concurrency (add max-concurrent-context cap on the pool).
- **Shippable on its own?** Yes (runtime exists, advertised dark).

### Stage 4 — Wire payload: tenant, run-lifecycle, object/signal/llm authz
- **Goal:** Carry run lifecycle and enforce server-side authz on every new agent→server frame.
- **Files:**
  - `aakaar/workers/remote/protocol.py` `RemoteTask.to_wire` + `dispatcher.py:50` — add `tenant_id=str(ctx.tenant_id)`, `run_id`, `node_id`; add `dispatcher.signal_run_end(conn,run_id)`.
  - `aakaar/storage/object_store.py` + `objects.py` + the `_demux_agent_frame`: `obj_put`/`obj_get` inject `tenant_id` from authenticated `AgentIdentity` (broker_link.py:68), **never the body**. **`obj_get` re-applies the objects.py:45 tenant-prefix guard AND is constrained to URIs the current run legitimately produced** (track run-issued URIs) — not arbitrary tenant blobs.
  - **`llm_complete`/`llm_plan`: rate-limited and size-capped per agent/run** (prevent the agent becoming a free unbounded OpenAI oracle). **`signal_open`: at most one outstanding prompt per node.**
  - `orchestrator.py:459` — send `run_end` to the holding agent **inside the same finally-style block as local cleanup, on ALL THREE terminal statuses** (succeeded/failed/cancelled), since the server's synchronous `session_state` cleanup loop runs over an **empty** dict for remote runs.
  - **Reconnect reconciliation:** on agent `hello`, the server sends `run_end` for any runs that reached terminal state while the agent was gone.
- **Tests:** hostile-agent fixture: `obj_get` of a cross-tenant/foreign-run URI is rejected; `llm_complete` flood is throttled; `run_end` fires on failed+cancelled paths and closes a leaked fake session; reconnect reconciliation closes an orphan.
- **Risk:** Both read-loops drifting → shared helper. Authz gaps are blockers → tests gate.
- **Shippable on its own?** Yes (dark).

### Stage 5 — Credentials for `fill_secret` + multi-step flows + sealed transport + audit
- **Goal:** Resolve mid-flow secrets server-side, ship them sealed, audit every release.
- **Files:**
  - `aakaar/shared/registry/types.py:79` `ActionDefinition` — add optional `secrets: tuple[SecretSpec,...]` so `browser.fill_secret` can declare vault need (today actions can't → `_collect_secrets` returns `{}`, dispatcher.py:172).
  - `aakaar/workers/remote/dispatcher.py:166` `_collect_secrets` — resolve for the `fill_secret` input shape (`capability_ref`+`account_alias`+`secret_name`) via `fetch_credentials`; **explicit error if `secret_name` present but no grant resolves** (no silent `{}`).
  - **Sealed-box transport:** register the agent's public key at enrollment alongside `api_key_hash`; `_collect_secrets`/dispatch seal the `secrets` envelope (and obj_put bodies) to that key so the broker relays ciphertext. **Browser/credential caps refuse the broker path unless sealed transport is active.** Enforce `wss://` (refuse/loudly warn on `ws://` for any agent that may carry secrets, unless `AAKAAR_AGENT_INSECURE`); require it for `master_link_url` too (broker_link.py:114).
  - **Audit:** emit `remote.secret_release` (grant alias + agent + run + time, **never the value**) each time `_collect_secrets` ships a credential remotely; surface browser-session open/close/orphan-reap as audit events. **Central redaction helper** applied to every logged/recorded frame carrying `secrets` and to agent-supplied `_relay_event` payloads (agents.py:234) before recording.
  - Shared `fill_secret` handler reads `ctx.secrets[secret_name]`; returns `{}` (value never in outputs).
- **Tests:** remote `fill_secret` fills password, value absent from outputs/events/cache/logs; missing-grant raises; broker path refused without sealed transport; `ws://` refused; audit event emitted without value.
- **Risk:** Sealed-box is the gating security work — without it, browser caps stay broker-forbidden.
- **Shippable on its own?** Yes (dark).

### Stage 6 — Advertisement, placement, session affinity (durable, resume-correct)
- **Goal:** Stop "none support capability"; pin sessions correctly including across resume.
- **Files:**
  - `capabilities/__init__.py` `advertised()` — includes the new caps via Stage 1 relocation; verify hello payload carries them.
  - `protocol.py` `AgentInfo`/`parse_hello` — add `browser_capable: bool` (liveness from the **launch probe**, not just import); thread through `AgentIdentity` for both paths and `mark_connected` (agents.py:194) + the DB capabilities column for offline placement hints. **Routing eligibility for browser/credential caps is the admin enrollment attribute, NOT the agent-claimed flag.**
  - `_shared.py:72` — derive a `browser` tag from `CapabilitySpec`; `registry.py:61`/`placement.py:42` honor `require_browser` (mirror `require_gui`); update error strings (registry.py:85/88).
  - **Session affinity (durable):** bind `run_id→conn` in the registry **and persist run→agent in the run/checkpoint row.** `executor.py:552` pins the resolved agent when a session-opening node is dispatched remotely. **On resume (orchestrator.py:232): if remaining layers contain session-bearing refs whose open_session layer already completed, FAIL the run with an explicit non-resumable-session error** (mirror broker_link.py:27) — never silently re-route to a fresh agent.
- **Tests:** `check_placement` no longer flags `cap.web_login` when advertised+admin-trusted; agent advertising the ref but `browser_capable=False` rejected with a clear reason; **resume of a session-bearing DAG fails-fast with the explicit error, never mis-routes**; end-to-end open→screenshot→close on one agent, URI resolves via `GET /objects`.
- **Risk:** Run pinning forces all nodes onto the agent; acceptable, refine later if needed.
- **Shippable on its own?** Yes (still dark behind `remote_browser_enabled`).

### Stage 7 — Remote live screenshots (parity, lands WITH placement reachability)
- **Goal:** Restore live-preview for remote sessions — this is part of "exact same fashion," not a refinement.
- **Files:**
  - `executor.py:437` `_maybe_emit_live_screen` — placement-aware: for a remote session (server `session_state` empty), the **agent** emits a `kind="live_screen"` event after each browser node carrying **real `run_id` (UUID), `node_id`, `payload={"uri": <server-issued URI>}`** (screenshots its live session, uploads via the Stage-3 `object_writer`). `_relay_event` (agents.py:233) does `uuid.UUID(str(msg['run_id']))` — the agent MUST send the real UUID or the relay drops it. **Guard `_relay_event` against empty/invalid `run_id`** (the `invoke()` path uses `run_id=''`, dispatcher.py:126).
- **Tests:** live-screen events appear on the timeline with resolvable URIs for a remote run; malformed/empty run_id doesn't crash the relay.
- **Risk:** Event-timeline volume. **Must ship in the same release as Stage 6 placement** so remote runs never go dark by default (`live_screenshots` defaults True, executor.py:103).
- **Shippable on its own?** Couple to Stage 6 in one release.

### Stage 8 — Enable flag, dry-run confirmation, fleet ops
- **Goal:** Turn the feature on per-deployment after the security stages land.
- **Files:**
  - `config.py` — `remote_browser_enabled` default False gating advertisement + new frames + credential placement.
  - `executor.py:534` — confirm the dry-run gate stays server-side authoritative reading `defn.side_effecting` (now non-None from Stage 0); side-effecting browser caps simulated before dispatch — no agent involvement.
  - Fleet: launch-probe result + pinned Chromium version recorded in hello for auditing; failed probe is a **loud surfaced agent state**, not a swallowed debug log (capabilities/__init__.py:48).
- **Tests:** flag off → no browser advertisement, no new frames reachable; dry-run of a `cap.web_login` DAG never dispatches; half-installed agent (probe fails) never receives browser work.
- **Shippable on its own?** Yes — this is the go-live switch.

## 4. Edge cases & how they're handled

- **Dry-run:** Server-side authoritative (executor.py:534) reading `defn.side_effecting` (now non-None, Stage 0). Read-only caps (`screenshot`/`open_url`/`web`) run for real; side-effecting (`web_login`/`file_download`/`fill_secret`) simulated **before** dispatch — agent never touches a dry-run.
- **Retries:** Session-bearing/side-effecting refs are **non-retryable-with-fresh-session**. `open_session` is idempotent per `(run_id,node_id)` so a retry returns the existing session, never a 2nd Chromium. Stateless caps keep `task_id` dedup. Documented: browser.* retry semantics differ from stateless caps.
- **Cancel:** Agent enforces `asyncio.wait_for(timeout_s)` (matching the server's `timeout_s+5` guard, dispatcher.py:104); a `cancel` frame cancels the in-flight `_inflight` task AND closes+releases the pool checkout immediately — not at run-end.
- **Resume:** Run→agent binding persisted to the run row; on resume, a session-bearing DAG whose open_session already completed **fails-fast with an explicit non-resumable-session error** (mirrors the server, where the in-memory Playwright handle is gone after restart). Never silently re-routes.
- **Session affinity:** `run_id→conn` binding; the live Playwright handle is non-serializable (session.py) so all session nodes co-locate. `(tenant_id,run_id)`-keyed `_run_sessions` on the agent.
- **Agent-drop mid-session:** Treated as fatal to the session, exactly as a server restart. Server: a `ConnectionError` on a session-bearing node propagates a session-lost error and aborts the chain (no transparent retry onto a fresh session). Agent: session-bearing `open_session` replies are excluded from re-delivery; socket-drop triggers an immediate sweep of sessions owned only by that socket.
- **Cleanup:** `run_end` on all three terminal statuses inside the cleanup finally-block; disconnect-triggered sweep + idle-TTL sweep; reconnect reconciliation sends `run_end` for runs that terminated while the agent was gone. Documented as best-effort-async (weaker than the server's synchronous-before-terminal cleanup), bounded by the TTL.
- **Live screenshots:** Agent emits `live_screen` events with real run UUID + node_id + server URI (Stage 7, shipped with placement). `_relay_event` guarded against empty/invalid run_id (the `invoke()` path).
- **Multi-tenant:** Agent is single-tenant at enrollment today (`AgentIdentity.tenant_id`). `_run_sessions`/pool keyed by `(tenant_id,run_id)` to be future-proof. Cross-tenant isolation on a shared agent would rest only on Playwright `BrowserContext` separation in one Chromium process — **declared insufficient for banking**; recommendation is one agent (or one browser process) per tenant. Profiles remain ignored (playwright.py:370); if persistence is ever added, `storage_state` lives in the tenant-scoped server store loaded per checkout, never on agent disk.
- **Concurrency:** Tasks run concurrently (client.py:182); one shared `_browser_pool` (one Chromium) with per-context isolation. Add a max-concurrent-context cap to bound memory. Interleaved-runs test on one agent.

## 5. Security & trust boundary

When the agent holds credentials/cookies, the boundary shifts from "agent sees stateless shell output" to "agent holds live banking sessions + plaintext secrets." Changes and mitigations:

- **Broker sees everything in cleartext (blocker):** broker_link.py:9-12 relays bodies verbatim. Mitigation: **sealed-box encryption** of the `secrets` envelope and obj_put bodies to the agent's enrolled public key; broker relays ciphertext. Browser/credential caps **refuse the broker path** unless sealed transport is active. The broker host is documented as handling banking PII and must be isolated.
- **TLS not enforced (blocker):** main.py accepts `ws://`. Mitigation: require `wss://` for secret-bearing/browser-capable agents and the master link; refuse/loud-warn on `ws://` unless `AAKAAR_AGENT_INSECURE`.
- **New agent→server oracles (blocker):** Mitigation: `obj_get` re-applies the tenant-prefix guard (objects.py:45) and is scoped to run-issued URIs; `llm_complete`/`llm_plan` rate-limited and size-capped; `signal_open` bounded to one prompt per node; tenant always from authenticated identity, never the body. Hostile-agent authz tests.
- **Result cache leaks sensitive bytes (blocker):** the 128-entry LRU (client.py:60) never expires. Mitigation: exclude browser/web/screenshot/download outputs from the cache; clear on `run_end`; never hold image/statement bytes. Test asserts no byte payloads cached.
- **Downloaded statements on untrusted disk (blocker):** Playwright writes to local temp (playwright.py:298). Mitigation: per-session 0700 temp dir, immediate deletion after reading bytes and on teardown, disabled persistent download history; browser-capable agents must run on disk-encrypted hosts.
- **Eligibility from agent-claimed flag (major):** Mitigation: admin enrollment attribute is authoritative for credential routing; the hello flag + launch probe are only liveness.
- **Audit gap (major):** Mitigation: `remote.secret_release` audit event per credential shipped (no value); session lifecycle audited; central redaction on logged/recorded frames and agent-supplied event payloads.
- **Unreapable sessions (major):** folded into Stage 3 (not deferred) — `wait_for` + cancel + disconnect sweep + TTL present the moment the agent can hold a session.

## 6. Open questions — resolved 2026-06-22 (one still open)

2. **Tenancy — RESOLVED.** One agent binds to **exactly one tenant**; a tenant may run **multiple agents**. This is today's `AgentIdentity.tenant_id` model, so cross-tenant-on-one-agent isolation is **out of scope** — there is never more than one tenant on an agent. `(tenant_id,run_id)` keying of `_run_sessions`/pool stays as defense-in-depth, and placement may pick any of the tenant's online browser-capable agents.
3. **Headless vs headed — DEFAULT (headless).** Proceed with **headless Chromium**. Headed/Xvfb (a `gui`+`browser` placement requirement + display provisioning) is an additive change deferred until a flow demonstrably needs a visible display.
4. **Chromium provisioning — RESOLVED.** Allow the agent installer to run `playwright install chromium` from Microsoft's CDN at install time, **with TLS verification disabled** (`NODE_TLS_REJECT_UNAUTHORIZED=0` / `PLAYWRIGHT_DOWNLOAD_*`) so the deployment network's TLS interception doesn't cause a connection error. ⚠️ **Supply-chain caveat:** disabling TLS verification on the binary download means the Chromium bytes could be tampered with in transit. Mitigation (do this, not optional for banking): **pin the expected Chromium revision and verify the downloaded binary's checksum** against a value baked into the installer; fail the install on mismatch. This keeps the "no third-party infra" rule (no internal mirror needed) while bounding the MITM risk.
5. **Sealed-transport scope — RESOLVED (seal everything).** Seal **both** the `secrets` envelope **and** all `obj_put` bodies (statements/screenshots) to the agent's enrolled public key. This fully closes the broker-sees-banking-data hole; the per-blob crypto cost is accepted given the compliance bar.

### Still open (does not block Stage 0–1)
1. **Planning-time browsing.** `PlannerToolRunner` checks out `browser_pool` during PLAN generation (runner.py:164) — before any run/node/agent exists. If the bank portal is only reachable from the **agent's** network, planning would browse from the **server** (wrong network) while the run browses from the agent. Options: (a) planning stays server-side and the portal must be reachable from both server and agent (document it), or (b) route planning through remote dispatch with a pre-run agent lease (much larger). Decide before any flow whose login page is unreachable from the server — irrelevant if the portal is reachable from both.

Verified file:line anchors backing the major corrections: `orchestrator.py:232-233` (run_target not persisted on resume), `orchestrator.py:459-468` (server cleanup iterates server-side session_state — empty for remote), `connection.py:34-42` (single-future one-shot dispatch), `client.py:176-205` (LRU dedup cache stores full outputs), `web_login/__init__.py:425` + `planner/llm.py:152,160` (planner seam ≠ complete_text), `broker_link.py:1-27,9-12` (no HTTP path + verbatim cleartext relay).
