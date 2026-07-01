// Human-readable rendering of workflow capability refs.
//
// The planner returns DAG nodes whose `ref` is a dotted capability id
// (e.g. "cap.web_form_fill", "host.desktop_click"). Operators reviewing a
// bank-touching plan need to read those as plain-language steps — and to spot
// the ones that move money or are otherwise irreversible before they run.
//
// These are hand-maintained lookup maps; the goal is a *graceful* fallback so a
// newly-shipped cap never renders as a blank or crashes — it degrades to a
// title-cased version of its own ref. Ideally the labels move to capability
// metadata from the API over time; until then this is the single source.

/**
 * Plain-English label for a capability ref. Unknown refs fall back to a
 * title-cased, de-punctuated form of the ref's tail (never empty, never the
 * raw dotted string when it can be helped).
 */
export function friendlyCapabilityName(ref: string): string {
  const known = FRIENDLY[ref];
  if (known) return known;
  const tail = ref
    .replace(/^(cap|action|control|host|browser)\./, "")
    .replace(/[._-]+/g, " ")
    .trim();
  if (!tail) return ref;
  return tail.charAt(0).toUpperCase() + tail.slice(1);
}

const FRIENDLY: Record<string, string> = {
  "cap.api_call": "Get structured data from an API",
  "cap.web_form_fill": "Fill a web form",
  "cap.web_click": "Click something on the page",
  "cap.form_autofill": "Fill a desktop form",
  "cap.screen_form_understand": "Read the visible form",
  "cap.browser_dom_query_multiple_elements": "Read repeated page items",
  "cap.browser_image_ocr": "Read text from a page image",
  "cap.browser_navigate": "Open a page",
  "cap.desktop_type": "Type into a field",
  "cap.desktop_click": "Click a field",
  "cap.file_download": "Download a file",
  "cap.file_upload": "Upload a file",
  "cap.notify": "Send a notification",
  "cap.wait": "Wait",
  "cap.screenshot": "Take a screenshot",
  "cap.transfer_funds": "Transfer funds",
  "cap.payment_submit": "Submit a payment",
  "host.desktop_click": "Click a field",
  "host.desktop_type": "Type into a field",
  "host.key_send": "Press a key or shortcut",
};

// Refs (or ref substrings) whose effect is side-effecting / irreversible —
// money-moving, submitting, deleting, sending. Surfaced as a "live action"
// pill so a reviewer notices them in the transcript, and (later) mapped to the
// dry-run/maker-checker gates. Substring match keeps new variants covered.
const SIDE_EFFECT_HINTS = [
  "transfer",
  "payment",
  "pay_",
  "submit",
  "approve",
  "delete",
  "remove",
  "send_funds",
  "wire",
  "settle",
  "disburse",
  "post_txn",
  "upload",
  "file_upload",
];

/** True when a node's ref looks side-effecting (money-moving / irreversible). */
export function isSideEffectingRef(ref: string): boolean {
  const r = ref.toLowerCase();
  return SIDE_EFFECT_HINTS.some((hint) => r.includes(hint));
}

/**
 * The remote-agent alias a node targets, or null when it runs on the server.
 * "server"/null both mean "the API host" — everything else is an agent/pool.
 */
export function nodeAgentTarget(target: string | null | undefined): string | null {
  if (!target || target === "server") return null;
  return target;
}
