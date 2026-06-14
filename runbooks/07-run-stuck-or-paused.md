# 07 — Run stuck or paused

A run that won't reach a terminal status (`succeeded` / `failed` /
`cancelled`) is in one of four distinguishable states. Identify which one
**before** acting — the fixes are different and one of them (resume) is
deliberately refused in the most common case.

```bash
TOKEN=...
RUN=http://localhost:8000/runs/<run_id>
curl -s -H "Authorization: Bearer $TOKEN" $RUN | python3 -m json.tool
```

Read three things from the response:

- `run.status` — `queued` / `running` / `paused`
- `pending_prompts` — non-empty means a `human.prompt` node is waiting
- `events[]` — the tail tells the story; the relevant kinds:
  - `run_paused` with `payload.reason: "human_prompt"` (and a `node_id`) —
    a prompt opened
  - `run_paused` with `payload.reason: "operator"` — someone called
    `POST /runs/{id}/pause`
  - `run_resumed` with the matching `reason` when either cause cleared
  - `run_cancelled` — a cancel took effect

## Case 1 — waiting on a human prompt (most common)

`status` is `running` (or `paused` if *also* operator-paused),
`pending_prompts` is non-empty:

```json
"pending_prompts": [{"node_id": "ask_otp", "message": "Enter the OTP", "expects": "otp"}]
```

This is not stuck — it's a deliberate human-in-the-loop wait (OTP, captcha,
confirmation). **Answer it**; do not try to resume:

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"node_id": "ask_otp", "response": "123456"}' $RUN/respond        # 204
```

`POST /runs/{id}/resume` against a prompt-blocked run returns
`409 {"detail": "run is waiting on a human prompt; respond to it instead of
resuming"}` — by design. Resume releases only *operator* pauses.

Note: `human.prompt` has a `timeout_seconds` (default 300, max 3600) — an
unanswered prompt eventually fails the node, so a "stuck" prompt usually
self-resolves into a failed run you can re-run.

## Case 2 — operator-paused

`status: "paused"`, last pause event has `payload.reason: "operator"`.
Someone called pause (audited as `run.pause` — `GET /audit` shows who).
In-flight nodes finished; no new DAG layer starts until resumed.

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" $RUN/resume    # 200, status -> running
```

Only the run's **starter or a tenant admin** may pause/resume/cancel
(else 403). A run can be both operator-paused *and* prompt-waiting; resume
then succeeds but the run still waits for the prompt response — answer it
via `/respond` as in case 1.

## Case 3 — genuinely stuck mid-node (`running`, no prompts, no progress)

The interpreter executes nodes in-process; a node inside a long external
wait (slow site, hung SFTP host, a remote agent that went offline mid-task,
a long `control.wait`) keeps the run `running`. Checks:

- last event timestamp vs. now — how long has the current node been running?
- node targeted at an agent? → check the agent is online
  ([04-agent-fleet-degradation](04-agent-fleet-degradation.md)); a remote
  task is bounded by `AAKAAR_REMOTE_TASK_TIMEOUT_SECONDS` (default 300s) and
  will fail the node when it expires.
- browser-capability node? → bounded by Playwright timeouts; give it a
  minute or two.

If you don't want to wait, **cancel** — it's cooperative and safe:

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" $RUN/cancel    # 200
```

Cancel interrupts `control.wait` sleeps and pending prompts immediately;
other in-flight nodes finish first, then the run lands on `cancelled` with
`ended_at` set (the 200 response may still show the old status — poll
`GET /runs/{id}` until `status: "cancelled"`; usually milliseconds, but an
in-flight node delays it until that node returns). A second cancel while
unwinding is a 200 no-op; after terminal it's `409 "run already finished"`.

Then relaunch with the same pinned version and inputs:

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" $RUN/rerun     # 201, NEW run id
```

## Case 4 — zombie from before a restart

`status` `paused`/`running`/`queued` but the API has restarted since the run
began. Lifecycle endpoints return
`409 {"detail": "run is not active on this server"}` — the in-process
execution state didn't survive the restart.

Normally startup recovery handles this automatically: on boot the
orchestrator marks all QUEUED/RUNNING/PAUSED runs FAILED with
`"Run interrupted by a server restart and could not be resumed."` (log:
`recovered N interrupted run(s) -> FAILED on startup`). If you can still see
a live zombie, recovery itself failed at boot — check the startup log for
`startup: interrupted-run recovery failed`, fix the underlying DB issue, and
restart again. Re-run recovered runs via `/rerun` (allowed for any terminal
run; pins the original workflow version and inputs).

## Decision table

| Observation | It is | Do |
|-------------|-------|----|
| `pending_prompts` non-empty | human-in-the-loop wait | `POST /runs/{id}/respond` |
| `paused` + last `run_paused.reason = "operator"` | operator pause | `POST /runs/{id}/resume` |
| `running`, no prompts, node clearly hung | stuck external wait | wait for node/remote timeout, or `POST /runs/{id}/cancel` then `/rerun` |
| lifecycle calls give 409 "not active on this server" | pre-restart zombie | restart-time recovery marks it FAILED; then `/rerun` |
| `queued` and never starts | orchestrator never picked it up (restart between create and schedule) | same as zombie — recovery on next boot, then `/rerun` |
