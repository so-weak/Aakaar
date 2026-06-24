# ViewChequeCTSOut — planner prompt

Paste the prompt below into the **chanakya** chat/planner to generate the
ViewChequeCTSOut DAG. It assumes these capabilities are granted to chanakya
(they are): `cap.web_login` (alias `ctsoutward`), `cap.web_tree_select`,
`cap.web_select`, `cap.web_click`, `cap.screenshot`, plus the `browser.*`
actions and `control.wait`.

The canonical, node-for-node DAG this is meant to produce is
`docs/design/viewchequectsout-dag.json` (and is already saved in the DB as the
ViewChequeCTSOut workflow). An LLM planner can vary node *ids* or collapse the
waits; if you need the exact graph, import the JSON. The prompt is written to
minimise that variance.

---

## Prompt

> Create and save a new workflow named **ViewChequeCTSOut** for the CTS Outward
> portal. Run every step on the connected agent as **one single linear browser
> session** — thread the session returned by the login node through every later
> step, and order the nodes as a strict chain (each step depends on the previous
> one). Put a **5-second wait (`control.wait`, seconds=5) before every step
> except the first login and the final close**. Never ask me for credentials.
>
> Steps, in this exact order:
>
> 1. **Log in** to the CTS Outward portal with `cap.web_login` using account
>    alias **`ctsoutward`**.
> 2. **Dismiss the post-login dialog** by clicking its **OK** button —
>    `cap.web_click` with `text: "OK"`.
> 3. **Open the left menu** to *E-Callback Processing → Ecall Back Processing*
>    with `cap.web_tree_select`, `path: ["E-Callback Processing", "Ecall Back
>    Processing"]` (it expands the parent node's caret, then clicks the child).
> 4. **Fill the "Selection Criterion" form.** These are ZK comboboxes, so set
>    each one by its field label with `cap.web_select` (label → value), in this
>    order:
>    - `Processsing Date` → `19-JUN-2026`   *(note the label's triple-s spelling)*
>    - `Record Type` → `TXN`
>    - `Core System` → `FLEX`
>    - `Cycle No` → `06`
>    - `Core Batch Number` → `0000000144`   *(set this one last — it depends on
>      the others)*
> 5. **Take a screenshot** with `cap.screenshot`.
> 6. **Click the Fetch button** — `cap.web_click` with `text: "Fetch"`.
> 7. **Take another screenshot** with `cap.screenshot`.
> 8. **Log out** by clicking the logout control — it is an **icon image with no
>    text** — using `cap.web_click` with `image: "logout"`.
> 9. **Close** the browser session with `browser.close_session`.

---

## Notes that keep the output faithful

- "ZK comboboxes set by label" steers the planner to `cap.web_select` (not
  `browser.set_field`/`cap.web_form_fill`, which can't drive a readonly ZK
  combobox).
- "icon image with no text" + `image: "logout"` steers logout to `cap.web_click`
  (image mode) instead of `browser.click_by_text`, which fails on the icon.
- The date / cycle / batch values are **per-run data** captured on 19-JUN-2026.
  On another clearing day they may not be offered and `cap.web_select` will fail
  with "no option matching" — change them, or ask for a "pick latest option"
  mode.
