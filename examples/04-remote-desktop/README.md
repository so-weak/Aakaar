# 04 — Remote desktop automation via an agent

Drives a GUI application on a remote workstation: focus a window by title,
click a screen coordinate, type a value. Every node carries
`"target": "workstation-1"`, so the dispatcher ships each one over the
agent's WebSocket; outputs flow back through the normal run env.

```
focus (cap.window_manage) → click_field (cap.desktop_click) → type_value (cap.desktop_type)
        all three: target = "workstation-1"
```

## Prerequisites

1. **An enrolled, online agent.** A tenant admin enrolls it
   (`POST /agents/enroll {"alias": "workstation-1", "pools": []}` — the
   one-time key is shown once), then on the workstation:

   ```bash
   pip install -e aakaar-agent[gui] -e aakaar-capabilities
   aakaar-agent --server wss://your-api:8000 --key "<agent_id>.<secret>"
   ```

   The `gui` extra (pyautogui/pygetwindow) is required for these three
   capabilities, and the agent must run inside a logged-in desktop session —
   GUI automation cannot run headless.

2. `AAKAAR_REMOTE_EXEC_ENABLED` not `false` on the API (default: enabled).

3. **Grants** (tenant admin, once — all secret-less):

   ```json
   {"capability_ref": "cap.window_manage", "account_alias": "default", "secrets": {}, "input_defaults": {}}
   {"capability_ref": "cap.desktop_click", "account_alias": "default", "secrets": {}, "input_defaults": {}}
   {"capability_ref": "cap.desktop_type",  "account_alias": "default", "secrets": {}, "input_defaults": {}}
   ```

## Targeting

- `"target": "<alias>"` pins a node to one agent. `"pool:<name>"` selects any
  online agent enrolled into that pool; `"os:windows"` selects by OS. Control
  nodes (`human.prompt`, `control.wait`) must stay on the server — the
  validator rejects a `target` on them.
- Pre-flight before running: `POST /placement/check` with the DAG body
  returns per-node placement issues and the online-agent count.
- If no matching agent is online at execution time the node fails with
  `no online agent matches target 'workstation-1' for this tenant` — see
  runbook [04-agent-fleet-degradation](../../runbooks/04-agent-fleet-degradation.md).

## Node notes

- **focus** — `cap.window_manage` actions: `list`, `focus`, `minimize`,
  `maximize`, `close`. `title` is a substring match against open windows.
  Use `action: "list"` (outputs `windows`) to discover titles first.
- **click_field** — coordinate clicks are brittle across screen
  resolutions; prefer recording the flow once with the Activity Recording
  API (`POST /recordings`, tenant admin) and editing the compiled draft,
  which flags coordinate clicks in its warnings.
- **type_value** — types literal text into the focused control;
  `interval_ms` paces keystrokes for apps that drop fast input. Never put
  secrets in `text` — the DAG is stored in plaintext. Remote tasks are
  bounded by `AAKAAR_REMOTE_TASK_TIMEOUT_SECONDS` (default 300s).

## Before importing

- Change `target` to your agent's alias (must match
  `^[a-z][a-z0-9_:\-]{0,63}$`).
- Change the window `title` and click coordinates for your application.

Import + run: see [../README.md](../README.md).
