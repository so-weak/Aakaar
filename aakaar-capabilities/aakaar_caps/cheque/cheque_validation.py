"""Per-spec cheque validator — the operator-facing 'accept / review /
reject' surface for every cheque the CTS UAT pipeline reads.

The rules come straight from the bank's validation spec. The list
below reflects the June-2026 split of the original combined
`amount` rule into `amount_words` + `amount_figures` (so the
operator can see at-a-glance which channel — the handwritten
'Rupees ...' line or the digit box — failed, instead of reading a
single combined "Amount Verification" FAIL and drilling into a
sub-check breakdown). The authoritative ordering lives in the
`rules` tuple in `evaluate_cheque_validation`, and the live
`ChequeValidationReport.checks` reflects exactly what the backend
ran for any given cheque.

  1. Date              : cheque is dated; date is not stale (>90 d
                         per RBI's April 2012 rule) and not far
                         in the future.
  2. Payee Name        : cheque-side payee matches the system-of-
                         record beneficiary name exactly (after
                         case + whitespace + punctuation
                         normalisation — see `_normalise_text`).
  3. Amount in Words   : handwritten 'Rupees ... Only' line on
                         the cheque parses to the same numeric
                         value as the system amount.
  4. Amount in Figures : digit-box amount on the cheque equals
                         the system amount.
  5. Cheque Number     : printed cheque number matches the system
                         cheque number (digit-only compare).
  6. Account Number    : back-side handwritten / stamped account
                         number matches the system account number
                         (digit-only compare).
  7. Signature         : drawee's signature is present in the
                         bottom-right panel of the cheque face
                         (ink-density check via
                         `signature_detector`).

Contract:
  * Each rule produces a `CheckResult` with one of four statuses:
        - PASS         : rule satisfied
        - FAIL         : rule definitively violated
        - WARN         : ambiguous evidence (e.g. signature ink
                         in the maybe-band, near-miss fuzzy
                         match) — operator should eyeball
        - NOT_VERIFIED : couldn't evaluate (a dependency is
                         missing, e.g. OpenCV for the signature
                         check, or the bank's panel didn't
                         expose this field). Distinct from FAIL
                         so the operator knows the difference
                         between 'we checked and it's wrong'
                         vs. 'we couldn't check'.
  * Never raises. A bug inside one rule must not poison the
    other five — each rule wraps its own logic in a try/except
    and downgrades exceptions to NOT_VERIFIED with the
    exception message in `evidence`.
"""

from __future__ import annotations

import difflib
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from aakaar_caps.cheque.cheque_ocr import (
    ChequeFields,
    _check_presence,
)
from aakaar_caps.cheque.words_to_number import (
    _tokenise as _amount_words_tokenise,
    decimal_to_words,
    expected_token_coverage,
    figures_to_decimal,
    words_to_decimal,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    """Structured visual + plain-English context for a verification
    rule. Designed for the operator UI to render WITHOUT the
    confusing technical evidence dump (`extractor_disagreed`,
    `vlm_amount_in_figures_matches`, etc. which the existing
    `CheckResult.evidence` dict carries for engineers).

    Phase 6 — added 2026-06 after operator feedback "this
    information is confusing for normal users — I want a cropped
    image alongside this".

    Fields:
      * `plain_summary` — ONE sentence in human English explaining
        what the rule actually checked and why it passed/failed.
        e.g. "On the cheque we read 16,141 in figures and 'Sixteen
        Thousand One Hundred Forty-One' in words — these match."
      * `crop_bbox` — (x1, y1, x2, y2) normalized 0..1 of the
        cheque region the rule examined. Frontend uses canvas
        cropping on the already-loaded cheque PNG so no server-
        side image cropping endpoint is needed. None when the
        rule examined the whole image or has no spatial focus.
      * `crop_side` — "front" / "back" / None — which cheque image
        the bbox applies to.
      * `from_cheque` — the value the OCR read off the cheque
        (in display-friendly form, e.g. "16,141" not "16141").
      * `expected` — the value the rule wanted to see (typically
        from the bank's DOM panel).
      * `comparison_kind` — "match" / "mismatch" / "missing" /
        "not_applicable" — drives the visual cue (green tick /
        red cross / amber dash / grey "—").
    """

    plain_summary: str
    crop_bbox: tuple[float, float, float, float] | None = None
    crop_side: str | None = None  # "front" | "back" | None
    from_cheque: str | None = None
    expected: str | None = None
    comparison_kind: str = "not_applicable"

    def to_dict(self) -> dict[str, Any]:
        return {
            "plain_summary": self.plain_summary,
            "crop_bbox": (
                list(self.crop_bbox) if self.crop_bbox else None
            ),
            "crop_side": self.crop_side,
            "from_cheque": self.from_cheque,
            "expected": self.expected,
            "comparison_kind": self.comparison_kind,
        }


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One validation rule's outcome — surfaced 1:1 in the UI."""

    check_id: str
    label: str
    # PASS / FAIL / WARN / NOT_VERIFIED
    status: str
    summary: str
    details: tuple[str, ...] = ()
    evidence: tuple[tuple[str, Any], ...] = ()
    # Phase 6: visual + plain-English context for the operator UI.
    # Always populated by the orchestrator (`_decorate_with_evidence_payload`)
    # after the rule has built its CheckResult, so individual rule
    # implementations don't have to know about the band bboxes.
    # None ONLY when the orchestrator couldn't infer a payload
    # (e.g. unrecognised check_id) — the frontend falls back to
    # `summary` + the legacy `evidence` dict in that case.
    evidence_payload: VerificationEvidence | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "label": self.label,
            "status": self.status,
            "summary": self.summary,
            "details": list(self.details),
            "evidence": dict(self.evidence),
            "evidence_payload": (
                self.evidence_payload.to_dict()
                if self.evidence_payload is not None
                else None
            ),
        }


@dataclass(slots=True)
class ChequeValidationReport:
    """All rule outcomes for a single cheque (one `CheckResult` per
    enabled rule — see the module docstring for the canonical list
    and the `rules` tuple in `evaluate_cheque_validation` for the
    authoritative ordering)."""

    checks: list[CheckResult] = field(default_factory=list)
    overall_status: str = "REVIEW"  # ACCEPT / REVIEW / REJECT
    pass_count: int = 0
    fail_count: int = 0
    warn_count: int = 0
    not_verified_count: int = 0
    # Pipeline-wide diagnostic. When the OCR text is empty or
    # pathologically short on either side, none of the rules have
    # a chance of PASSing — so we surface a banner-level
    # signal that the OPERATOR's first action should be to verify
    # the image-acquisition path, not to read the per-rule
    # evidence. `ocr_health` is one of:
    #   "ok"         both sides have substantive raw_text
    #   "front_weak" front has <50 chars (cheque body unreadable)
    #   "back_weak"  back has <50 chars (endorsement unreadable)
    #   "both_weak"  neither side captured useful text
    #   "no_capture" no ChequeFields at all
    ocr_health: str = "ok"
    # Human-readable advice for the operator. Empty when ocr_health
    # is "ok".
    ocr_health_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "checks": [c.to_dict() for c in self.checks],
            "overall_status": self.overall_status,
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "warn_count": self.warn_count,
            "not_verified_count": self.not_verified_count,
            "ocr_health": self.ocr_health,
            "ocr_health_summary": self.ocr_health_summary,
        }


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# RBI's circular DPSS.CO.CHD.No.2030/04.07.05/2011-12 (effective
# 01-Apr-2012) reduced the cheque-validity period from 6 months
# to 3 months from the date written on the cheque. We use 90
# days as the operator-default; callers can override per-run via
# the `validity_days` kwarg.
DEFAULT_VALIDITY_DAYS = 90

# How many days into the future a cheque date is allowed to be
# before we flag it. Most banks clear post-dated cheques on the
# date written — but a cheque dated >7 days ahead is usually a
# data-entry mistake or a fraud signal worth surfacing.
DEFAULT_FUTURE_TOLERANCE_DAYS = 7

# Verification checks that are disabled in production. Listing a
# check_id here removes it from the validation report entirely —
# it won't render in the UI, won't count toward pass/fail tallies,
# and won't affect the overall verdict. To disable a check, add
# its id to this set; the rule implementation and its
# registration are otherwise untouched.
#
# History:
#   * June 2026 (original): {"amount_words"} — the handwritten
#     'Rupees ... Only' line was too OCR-noisy on the legacy
#     pipeline and produced too many false NOT_VERIFIEDs.
#   * June 2026 (revised): empty — the rule was hardened with the
#     DOM-amount-to-words fuzzy fallback (see `_rule_amount_in_words`
#     and `decimal_to_words`) which lets the verdict survive OCR
#     noise that confuses the strict numeric parser, so the
#     operator now gets a useful PASS / WARN / FAIL instead of a
#     blanket "we couldn't read it" hidden behind the disable.
_DISABLED_CHECK_IDS: frozenset[str] = frozenset()

# Similarity thresholds used by the four "X Verification" rules
# when they fall back to fuzzy-substring searching the raw OCR
# text for a DOM value. These mirror the tiers the operator
# already sees in the presence-check panel ('exact', 'near
# miss', 'not on image'):
#   ≥ _SIM_PASS  → PASS  ("we found it cleanly")
#   ≥ _SIM_WARN  → WARN  ("we found a near-miss — eyeball please")
#   < _SIM_WARN  → FAIL  ("digit/text not on the cheque image")
# Tuned to the same 0.85 / 0.5 split the existing
# _check_presence + UI grid already use, so a rule's verdict
# never contradicts the per-field badge directly below it.
_SIM_PASS: float = 0.85
_SIM_WARN: float = 0.50

# Length cap for the per-rule OCR snippet surfaced in evidence
# dicts. Keep small so the JSON payload stays cheap and the UI
# can render the snippet inline without truncating, but long
# enough to show roughly what the engine actually read off the
# cheque body.
_OCR_SNIPPET_CHARS: int = 400


def _ocr_snippet(fields: ChequeFields | None) -> str:
    """Compact summary of `fields.raw_text` for an evidence dict.
    Collapses runs of whitespace to single spaces and truncates
    to `_OCR_SNIPPET_CHARS` — the goal is 'what the human can
    skim in two seconds and decide if the OCR even saw the
    cheque'."""
    if fields is None:
        return "(no capture)"
    raw = (fields.raw_text or "").strip()
    if not raw:
        return "(empty)"
    flat = re.sub(r"\s+", " ", raw)
    if len(flat) <= _OCR_SNIPPET_CHARS:
        return flat
    return flat[:_OCR_SNIPPET_CHARS] + "…"


def _ocr_engines(fields: ChequeFields | None) -> list[str]:
    """Return a short list of engine names that produced output
    for this side (e.g. ['paddleocr', 'easyocr', 'trocr']). Used
    so the per-rule evidence dict tells the operator WHICH OCR
    backend's noise they're looking at.

    The four per-region focused Paddle passes
    (`paddle_focused_payee_line`, `paddle_focused_amount_words`,
    `paddle_focused_amount_figures`, `paddle_focused_date`) get
    collapsed into a single `paddle_focused` entry so the list
    stays readable — operators can dig into per-region scores via
    the engine-runs panel.

    Diagnostic engines (apple_vision_date, paddle_focused_back_stamp)
    are ALWAYS surfaced even when their text is empty, so the
    operator can tell at a glance whether the pass ran at all.
    Without this, a date-rule failure looks identical whether
    the apple_vision_date reader silently skipped, returned empty,
    or wasn't compiled into the binary at all (the original symptom
    that prompted lifting this filter)."""
    if fields is None or not fields.engine_runs:
        return []
    # Engines whose presence in the engines list is itself a
    # signal — even an empty read tells the operator the pipeline
    # tried to run them. Keep this list small; adding every
    # engine here defeats the purpose of the readable summary.
    # These engines surface under their FULL name (not collapsed
    # into `paddle_focused`) so they're visually distinct from
    # the per-band focused passes.
    ALWAYS_SURFACE = {"apple_vision_date", "paddle_focused_back_stamp"}
    seen: list[str] = []
    for run in fields.engine_runs:
        if not run[0]:
            continue
        name = run[0]
        has_text = bool((run[1] or "").strip()) if len(run) > 1 else False
        if name in ALWAYS_SURFACE:
            # Surfaces under its full name regardless of text —
            # the OPERATOR uses the presence to confirm the
            # rescue pass ran.
            if name not in seen:
                seen.append(name)
            continue
        if not has_text:
            continue
        if name.startswith("paddle_focused_"):
            name = "paddle_focused"
        if name not in seen:
            seen.append(name)
    return seen


# ---------------------------------------------------------------------------
# VLM cross-check helpers
# ---------------------------------------------------------------------------
#
# Local Qwen2.5-VL-3B verification results live on
# `ChequeFields.vlm_verification` as a dict (set by
# `cheque_ocr.extract_fields` when a dom is provided and the model
# is loadable). Each rule consults the VLM FIRST — when the VLM
# has a confident answer (>= _VLM_TRUST_THRESHOLD), we treat it as
# the primary signal; the OCR-based logic still runs, and we
# surface AGREE / DISAGREE / VLM_ONLY / OCR_ONLY in evidence so
# operators can see when the two signals diverge.
#
# Threshold rationale: 0.7 is the inflection point on Qwen-VL's
# self-reported confidence where false-positive rates on cheque
# verification fall below 5% in internal eval. Operators tune
# this down to 0.6 in start_background.sh if they want the VLM to
# speak more often.

# Confidence threshold at which the VLM's answer is considered
# trustworthy enough to override OCR. Tunable in tests / future
# config; not exposed as an env var yet (would change rule
# behaviour invisibly to operators).
_VLM_TRUST_THRESHOLD: float = 0.7


def _vlm_payload(front: ChequeFields | None) -> dict[str, Any]:
    """Pull the VLM verification dict off the front-side fields,
    returning an empty dict when the VLM didn't run (no dom,
    weights missing, kill-switch on, back-side rule)."""
    if front is None:
        return {}
    payload = getattr(front, "vlm_verification", None)
    if not isinstance(payload, dict):
        return {}
    return payload


def _vlm_field(
    front: ChequeFields | None,
    field_name: str,
    confidence_name: str,
) -> tuple[Any, float]:
    """Read one (value, confidence) pair off the VLM payload.
    Returns (None, 0.0) when the field is absent or the VLM
    didn't run."""
    payload = _vlm_payload(front)
    if not payload:
        return None, 0.0
    if payload.get("missing_dep"):
        return None, 0.0
    value = payload.get(field_name)
    try:
        conf = float(payload.get(confidence_name) or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    return value, conf


def _vlm_evidence_keys(
    field_name: str,
    confidence_name: str,
    value: Any,
    confidence: float,
    *,
    agree: str | None = None,
) -> list[tuple[str, Any]]:
    """Standardised evidence keys for the VLM-OCR cross-check.
    Returned as a list so callers can extend their own evidence
    tuple. The `vlm_agreement` key is one of:
      * 'agree'           — VLM and OCR returned the same verdict
      * 'disagree'        — VLM and OCR returned different verdicts
      * 'vlm_only'        — OCR had no signal, VLM did
      * 'ocr_only'        — VLM had no signal, OCR did
      * 'vlm_unavailable' — VLM didn't run at all
      * None              — caller hasn't computed agreement yet
    """
    rows: list[tuple[str, Any]] = [
        (f"vlm_{field_name}", value),
        (f"vlm_{confidence_name}", round(confidence, 3)),
    ]
    if agree is not None:
        rows.append(("vlm_agreement", agree))
    return rows


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def validate_cheque(
    *,
    front: ChequeFields | None,
    back: ChequeFields | None,
    dom: dict[str, Any] | None,
    today: date | None = None,
    validity_days: int = DEFAULT_VALIDITY_DAYS,
    future_tolerance_days: int = DEFAULT_FUTURE_TOLERANCE_DAYS,
    back_flip_status: dict[str, Any] | None = None,
) -> ChequeValidationReport:
    """Run all six validation rules and assemble the report.

    `today` is injected so test fixtures (and a future
    "as-of-date" feature) can freeze the validity clock. When
    None we use the host system date.

    `back_flip_status` is the capability's verdict on whether the
    Alt+F1 keyboard shortcut actually flipped the cheque viewer
    to the back image (see cap.cts_uat_read_cheques._capture_side).
    Shape: {"requested": "Alt+F1", "retries": int, "changed": bool}
    or None when the capability didn't (or couldn't) perform the
    check. The account-number rule consults this so a back-side
    FAIL is downgraded to NOT_VERIFIED when we know the back
    image is actually a duplicate of the front — operators
    shouldn't see "account number rejected" for a cheque whose
    back we never saw.
    """
    today = today or date.today()
    dom = dom or {}
    front_fields = front  # alias for readability
    back_fields = back

    report = ChequeValidationReport()
    # (check_id, display_label, implementation) tuples. Kept
    # explicit (rather than deriving id/label from function names
    # via `__name__` introspection) so that monkey-patching one
    # rule in a test, or renaming the implementation, never
    # silently breaks the report's canonical id/label scheme.
    rules: tuple[tuple[str, str, Any], ...] = (
        ("date",            "Date Verification",            _rule_date),
        ("payee",           "Payee Name Verification",      _rule_payee),
        # June 2026: the original `amount` rule (`_rule_amount`,
        # internal sub-checks a + b) was split per operator
        # feedback into TWO top-level rules. Both compare against
        # the system (SC) value directly, surfaced separately so
        # the operator can see at-a-glance which channel (the
        # handwritten "Rupees ..." line or the digit box) failed,
        # rather than reading a single combined "Amount Verification"
        # FAIL and drilling into a sub-check breakdown.
        ("amount_words",    "Amount in Words Verification", _rule_amount_in_words),
        ("amount_figures",  "Amount in Figures Verification", _rule_amount_in_figures),
        ("cheque_no",       "Cheque Number Verification",   _rule_cheque_no),
        ("account_no",      "Account Number Verification",  _rule_account_no),
        ("signature",       "Signature Verification",       _rule_signature),
    )

    # Temporarily disabled checks (operator request, June 2026).
    # The rules above are kept registered so re-enabling is a
    # one-line change: just remove the id from this set. Any id
    # listed here is skipped — it won't appear in the report's
    # `checks`, won't count toward pass/fail tallies, and won't
    # influence the overall verdict.
    rules = tuple(r for r in rules if r[0] not in _DISABLED_CHECK_IDS)

    # Diagnostic preamble: capture what OCR actually produced so
    # each rule's evidence carries it. When the OCR text is short
    # or empty there's no way for any rule to PASS — surfacing
    # this lets operators immediately distinguish 'OCR couldn't
    # read the cheque' from 'OCR read it, but the rule fired
    # incorrectly'.
    front_snippet = _ocr_snippet(front_fields)
    back_snippet = _ocr_snippet(back_fields)
    front_engines = _ocr_engines(front_fields)
    back_engines = _ocr_engines(back_fields)

    for check_id, label, rule in rules:
        try:
            r = rule(
                front=front_fields,
                back=back_fields,
                dom=dom,
                today=today,
                validity_days=validity_days,
                future_tolerance_days=future_tolerance_days,
                back_flip_status=back_flip_status,
            )
        except Exception as e:  # noqa: BLE001
            # Defensive depth — a rule bug must not poison the
            # other five. Downgrade to NOT_VERIFIED with the
            # exception text as evidence so the rule's failure
            # is investigable.
            logger.warning(
                "validate_cheque: rule %r raised (%s) — "
                "downgrading to NOT_VERIFIED", check_id, e,
            )
            r = CheckResult(
                check_id=check_id,
                label=label,
                status="NOT_VERIFIED",
                summary=f"Validator crashed: {e}",
                evidence=(("error", str(e)),),
            )
        # Append OCR diagnostic to every rule's evidence so the
        # operator can correlate a failure with the raw OCR
        # output the rule was working from. Front-only rules
        # (signature, payee) still benefit from seeing 'OCR text
        # was 0 chars' inline.
        diag_evidence = (
            ("ocr_front_engines", front_engines),
            ("ocr_front_raw_text_len",
             len(front_fields.raw_text or "") if front_fields is not None else 0),
            ("ocr_front_raw_text_snippet", front_snippet),
            ("ocr_back_engines", back_engines),
            ("ocr_back_raw_text_len",
             len(back_fields.raw_text or "") if back_fields is not None else 0),
            ("ocr_back_raw_text_snippet", back_snippet),
        )
        # Phase 6: compute the visual + plain-English evidence
        # payload from the per-rule context. Done AFTER each rule
        # builds its CheckResult so rule implementations stay
        # focused on the verdict logic and don't have to thread
        # bbox / from-cheque / expected through their internals.
        # Defensive try — a bug in the payload builder must NOT
        # poison the rule's verdict.
        try:
            payload = _build_evidence_payload(
                check_id=r.check_id,
                status=r.status,
                summary=r.summary,
                front=front_fields,
                back=back_fields,
                dom=dom,
                evidence=dict(r.evidence),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "validate_cheque: evidence_payload build failed "
                "for %r (%s)", r.check_id, e,
            )
            payload = None

        r = CheckResult(
            check_id=r.check_id,
            label=r.label,
            status=r.status,
            summary=r.summary,
            details=r.details,
            evidence=tuple(r.evidence) + diag_evidence,
            evidence_payload=payload,
        )
        report.checks.append(r)

    # Tally + overall verdict.
    for c in report.checks:
        if c.status == "PASS":
            report.pass_count += 1
        elif c.status == "FAIL":
            report.fail_count += 1
        elif c.status == "WARN":
            report.warn_count += 1
        else:  # NOT_VERIFIED
            report.not_verified_count += 1

    # Verdict policy — surface only; the operator decides what
    # to do per UAT mandate. ACCEPT only when ALL six are PASS;
    # REJECT when there's any FAIL; REVIEW otherwise (including
    # any WARN or NOT_VERIFIED).
    if report.fail_count > 0:
        report.overall_status = "REJECT"
    elif report.warn_count == 0 and report.not_verified_count == 0:
        report.overall_status = "ACCEPT"
    else:
        report.overall_status = "REVIEW"

    # OCR pipeline health — a banner the UI can show ABOVE the
    # per-rule list when OCR itself failed. Threshold of 50 chars
    # comes from the empirical observation that a CTS-2010 cheque
    # front with a working OCR pass always emits >300 chars of
    # text (MICR line + printed labels alone account for ~150).
    # If we get <50, the engine almost certainly couldn't see the
    # cheque (wrong image, blank capture, severe orientation).
    front_len = (
        len(front_fields.raw_text or "") if front_fields is not None else 0
    )
    back_len = (
        len(back_fields.raw_text or "") if back_fields is not None else 0
    )
    front_weak = front_fields is None or front_len < 50
    back_weak = back_fields is None or back_len < 50

    if front_fields is None and back_fields is None:
        report.ocr_health = "no_capture"
        report.ocr_health_summary = (
            "No cheque image was captured at all. Check the browser "
            "session is still on the cheque viewer page and that the "
            "row's F/B buttons are reachable."
        )
    elif front_weak and back_weak:
        report.ocr_health = "both_weak"
        report.ocr_health_summary = (
            f"OCR produced almost no text on either side "
            f"(front={front_len} chars, back={back_len} chars). The "
            f"likely cause is the wrong image being captured (e.g. "
            f"the page UI instead of the cheque bitmap) or both "
            f"images being blank/low-res. Inspect the captured PNGs "
            f"and the engine-runs panel to confirm. Until the OCR "
            f"text actually contains cheque content, NONE of the "
            f"rules can return PASS."
        )
    elif front_weak:
        report.ocr_health = "front_weak"
        report.ocr_health_summary = (
            f"Front-side OCR produced very little text "
            f"({front_len} chars). The date / payee / amount / "
            f"signature rules cannot evaluate properly. Inspect the "
            f"captured front PNG and verify the direct-image-fetch "
            f"path is working."
        )
    elif back_weak:
        report.ocr_health = "back_weak"
        report.ocr_health_summary = (
            f"Back-side OCR produced very little text "
            f"({back_len} chars). The account-number rule cannot "
            f"evaluate the endorsed depositor account. Verify the "
            f"Alt+F1 flip actually happened and the back image was "
            f"captured."
        )
    elif front_fields is not None and front_fields.handwriting_missing_dep:
        # OCR text length is fine (likely from PaddleOCR reading
        # the printed labels + MICR strip), but the dedicated
        # HANDWRITING engine (TrOCR) didn't load.
        #
        # Three tiers, weakest signal -> strongest:
        #   1. Structured extraction SUCCEEDED — the primary engine
        #      (Apple Vision / GOT-OCR2 / paddle_focused fallback)
        #      already produced usable values for the handwriting
        #      fields. The operator doesn't need a banner saying
        #      "fallback active, accuracy may be lower" because
        #      extraction WORKED. Suppress entirely. (added 2026-06)
        #   2. Structured extraction PARTIAL but focused-region
        #      passes produced text — soft "handwriting_fallback"
        #      banner. The rules CAN evaluate, just at lower
        #      confidence than with TrOCR.
        #   3. NOTHING worked — hard "handwriting_unavailable"
        #      banner. Operator must run the download script.
        focused_payload = _focused_region_text(front_fields)
        if _handwriting_extraction_succeeded(front_fields):
            # Tier 1: structured fields came out fine despite
            # TrOCR being missing. No banner needed; operator
            # shouldn't be alarmed when extraction worked. We
            # still log the TrOCR unavailability to the
            # diagnostics drawer via the existing engine_runs
            # entry — that channel surfaces "engine attempted,
            # failed because X" for operators who DO want to
            # know what's loaded.
            report.ocr_health = "ok"
            report.ocr_health_summary = ""
        elif focused_payload:
            # Tier 2: TrOCR missing AND structured fields are
            # incomplete BUT the focused-region fallback did
            # produce some text. Show the soft banner.
            report.ocr_health = "handwriting_fallback"
            report.ocr_health_summary = (
                f"TrOCR (the optional handwriting model) didn't load "
                f"({front_fields.handwriting_missing_dep[:160]}). "
                f"The pipeline is using the EasyOCR / PaddleOCR "
                f"region-focused fallback on the handwriting bands "
                f"({focused_payload}). Accuracy on cursive cheque "
                f"text will be lower than with TrOCR — surface this "
                f"to the operator as a near-miss rather than a hard "
                f"fail. To restore TrOCR: run "
                f"`./scripts/download_trocr.sh small` from "
                f"`aakaar/` on a non-corporate network (the "
                f"download is blocked by the corporate proxy)."
            )
        else:
            # Tier 3: no focused-region text either — handwriting
            # is truly invisible. Keep the original strong banner.
            report.ocr_health = "handwriting_unavailable"
            report.ocr_health_summary = (
                f"Print OCR ran fine ({front_len} chars on front) but "
                f"TrOCR (the handwriting model) didn't load: "
                f"{front_fields.handwriting_missing_dep[:300]}. "
                f"The region-focused EasyOCR fallback also returned "
                f"no text from the handwriting bands. Without either, "
                f"the Date, Payee, and Amount-in-words rules can "
                f"only see what print OCR catches (often nothing "
                f"useful from the handwritten regions). Run "
                f"`./scripts/download_trocr.sh small` from "
                f"`aakaar/` on a non-corporate network to pre-stage "
                f"the 248 MB small variant, or set "
                f"AAKAAR_TROCR_MODEL_PATH to a pre-downloaded copy."
            )

    return report


def _handwriting_extraction_succeeded(
    fields: ChequeFields | None,
) -> bool:
    """Return True when the structured handwriting-derived fields
    on `fields` came out usable enough that the operator does
    NOT need to see a "TrOCR unavailable, fallback active"
    banner. Used to suppress the soft handwriting_fallback
    banner in tier 1 of the validation OCR-health logic.

    Heuristic: at least 2 of the 3 critical handwriting-derived
    fields (`beneficiary`, `amount`, `amount_words`) have a
    non-empty extracted value. Date is a 4th band but its
    extraction outcome lives in engine_runs (the `apple_vision_date`
    entry) rather than on `ChequeFields` directly, so we don't
    count it here; missing date alone is unusual when 2+ of the
    other three came out fine.

    Why 2-of-3 rather than 3-of-3: on cheques with a printed
    account-payee stamp covering the amount-words band, that
    field can legitimately remain empty even with TrOCR
    working. Demanding all-three would re-trigger the banner
    on perfectly-valid extractions. 2-of-3 captures "main
    handwriting bands are working" without false negatives.
    """
    if fields is None:
        return False
    filled = sum(
        1
        for v in (fields.beneficiary, fields.amount, fields.amount_words)
        if (v or "").strip()
    )
    return filled >= 2


# ---------------------------------------------------------------------------
# Phase 6: VerificationEvidence payload builder
# ---------------------------------------------------------------------------
#
# Per-rule mapping of (check_id) -> (bbox, side). bbox is
# (x1, y1, x2, y2) normalised to 0..1 of the captured PNG.
# Values mirror `cheque_consensus.FIELD_BAND_BBOXES` to keep the
# operator-visible crops consistent with the bands the consensus
# engine voted from; the signature band is added here because
# it's a verification-only concept (consensus doesn't vote on it).
_RULE_BBOXES: dict[str, tuple[tuple[float, float, float, float], str]] = {
    "date":       ((0.72, 0.04, 0.99, 0.14), "front"),
    "payee":      ((0.06, 0.18, 0.86, 0.32), "front"),
    # Legacy combined-amount band — kept for backwards-compat
    # (older capability runs and unit tests still reference
    # check_id="amount").
    "amount":     ((0.06, 0.32, 0.99, 0.54), "front"),
    # Split rules (June 2026). amount_words crops the handwritten
    # 'Rupees ... Only' line band (left two-thirds of the amount
    # zone); amount_figures crops the boxed digit zone (right
    # third). amount_words bottom widened 0.46 -> 0.52 (kept in sync
    # with `cheque_ocr._AMOUNT_WORDS_BBOX`) so a wrapped two-row
    # handwritten amount is shown whole in the operator's crop.
    "amount_words":   ((0.06, 0.32, 0.66, 0.52), "front"),
    "amount_figures": ((0.66, 0.32, 0.99, 0.46), "front"),
    "cheque_no":  ((0.00, 0.82, 1.00, 1.00), "front"),
    "account_no": ((0.00, 0.00, 1.00, 0.55), "back"),
    # Signature band: bottom-right of front (drawee's signature
    # area on CTS-2010 cheques).
    "signature":  ((0.55, 0.70, 0.99, 0.95), "front"),
}


def _display_amount(value: Any) -> str:
    """Format an amount as '16,141' for display. Accepts ints as
    well as DOM strings like '21,715.00' or '21715.00' (commas and
    a trailing .00 are stripped). Falls back to str() for input
    that isn't numeric at all."""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        try:
            num = float(cleaned)
        except ValueError:
            return value
        # Drop a whole-rupee '.0' tail; keep paise when present.
        return f"{int(num):,}" if num == int(num) else f"{num:,.2f}"
    return str(value) if value is not None else ""


def _dom_pick(dom: dict, *keys: str) -> str | None:
    """First non-empty value from `dom` matching any of `keys`."""
    for k in keys:
        v = dom.get(k)
        if v:
            return str(v).strip()
    return None


def _classify_comparison(status: str) -> str:
    """Map a rule's PASS/FAIL/WARN/NOT_VERIFIED status to the
    comparison_kind enum the frontend uses for visual cues."""
    if status == "PASS":
        return "match"
    if status == "FAIL":
        return "mismatch"
    if status == "WARN":
        return "near_miss"
    return "missing"  # NOT_VERIFIED


def _rescued_by_text_search(evidence: dict | None) -> str | None:
    """When a rule PASSED via the raw-text presence search (because
    the structured extractor disagreed or returned nothing), return
    a short human label for HOW it matched ("exact", "digits",
    "fuzzy", ...). Returns None when the verdict did NOT come from
    the text-search rescue.

    This lets the evidence payload show the value that ACTUALLY
    matched the cheque rather than the stray structured read — so
    the "On cheque vs Expected" row never contradicts the green
    MATCH badge (the bug operators kept hitting: header says PASS,
    comparison row shows two different numbers)."""
    if not evidence:
        return None
    kind = evidence.get("ocr_search_kind")
    if not kind:
        return None
    pretty = {
        "exact": "exact match",
        "digits": "digits match",
        "digits_tolerant": "digits match (OCR-tolerant)",
        "fuzzy": "close match",
    }
    return str(pretty.get(str(kind), str(kind)))


def _build_evidence_payload(
    *,
    check_id: str,
    status: str,
    summary: str,
    front: ChequeFields | None,
    back: ChequeFields | None,
    dom: dict,
    evidence: dict | None = None,
) -> VerificationEvidence | None:
    """Build the structured visual + plain-English evidence
    payload for a single rule's CheckResult.

    Returns None when the rule's check_id isn't recognised
    (defensive — the frontend falls back to the textual
    `summary` field in that case).
    """
    if check_id not in _RULE_BBOXES:
        return None

    bbox, side = _RULE_BBOXES[check_id]
    comparison_kind = _classify_comparison(status)

    if check_id == "date":
        # Prefer the traced raw text (matches the actual ladder
        # step the rule used) over a generic regex sweep of
        # raw_text. This keeps the operator's "we read X from
        # the cheque" claim aligned with the rule's verdict —
        # critical when the user reports a stale-dated FAIL on
        # a cheque whose date band visibly reads a recent year.
        _, ocr_path, ocr_raw = _extract_cheque_date_traced(front, None)
        from_cheque = ocr_raw or _extract_date_from_text(
            front.raw_text if front else None,
        )
        if not from_cheque and front and front.raw_text:
            from_cheque = front.raw_text.strip().splitlines()[0]
        path_suffix = (
            f" (read by `{ocr_path}`)" if ocr_path else ""
        )
        plain = (
            f"On the cheque the date reads {from_cheque!r}"
            f"{path_suffix}. Cheques are valid for 90 days from "
            f"the written date. {summary}"
            if from_cheque
            else f"No date could be read off the cheque. {summary}"
        )
        return VerificationEvidence(
            plain_summary=plain,
            crop_bbox=bbox,
            crop_side=side,
            from_cheque=from_cheque,
            expected="Valid date within last 90 days",
            comparison_kind=comparison_kind,
        )

    if check_id == "payee":
        from_cheque = (front.beneficiary if front else None) or None
        expected = _dom_pick(dom, "Beneficiary 1", "Beneficiary")
        plain = _plain_compare(
            "payee", status, from_cheque, expected,
            cheque_label="payee on cheque",
            expected_label="payee in system",
        )
        return VerificationEvidence(
            plain_summary=plain,
            crop_bbox=bbox,
            crop_side=side,
            from_cheque=from_cheque,
            expected=expected,
            comparison_kind=comparison_kind,
        )

    if check_id == "amount":
        words_raw = (front.amount_words if front else None) or None
        figs_raw = (front.amount if front else None) or None
        # Display: prefer "words + (figures)" so the operator
        # sees both on the cheque face.
        from_parts: list[str] = []
        if figs_raw:
            from_parts.append(f"{_display_amount(figs_raw)} (figures)")
        if words_raw:
            from_parts.append(f"{words_raw!r} (words)")
        from_cheque = " / ".join(from_parts) or None
        expected_raw = _dom_pick(dom, "Amount", "Batch Amount")
        # Strip "36 / 12,05,345.00" prefix if a slash is present
        if expected_raw and "/" in expected_raw and not expected_raw.endswith("/"):
            expected_raw = expected_raw.rsplit("/", 1)[-1].strip()
        rescue = _rescued_by_text_search(evidence)
        if status == "PASS" and rescue and expected_raw:
            # PASS via raw-text search: the system amount was found
            # in the cheque's scanned text even though the structured
            # figures read disagreed. Show what truly matched.
            structured_note = (
                f" (The digit-box reader returned {from_cheque}, "
                f"which we ignored.)"
                if from_cheque
                else ""
            )
            from_cheque = (
                f"{_display_amount(expected_raw)} "
                f"(found in cheque text — {rescue})"
            )
            plain = (
                f"The system amount {_display_amount(expected_raw)} "
                f"appears in the cheque's scanned text ({rescue}), so "
                f"it's confirmed.{structured_note}"
            )
        else:
            plain = _plain_compare(
                "amount", status, from_cheque, expected_raw,
                cheque_label="amount on cheque",
                expected_label="amount in system",
            )
        return VerificationEvidence(
            plain_summary=plain,
            crop_bbox=bbox,
            crop_side=side,
            from_cheque=from_cheque,
            expected=expected_raw,
            comparison_kind=comparison_kind,
        )

    if check_id == "amount_words":
        from_cheque = (front.amount_words if front else None) or None
        expected_raw = _dom_pick(dom, "Amount", "Batch Amount")
        if expected_raw and "/" in expected_raw and not expected_raw.endswith("/"):
            expected_raw = expected_raw.rsplit("/", 1)[-1].strip()
        # Display the DOM amount in its words form alongside the
        # figures — the rule is fundamentally a words-vs-words
        # comparison from the operator's standpoint, so the
        # "expected" should read like a cheque writer's line
        # ("Rupees One Lakh Ninety Thousand Only") rather than
        # the raw figures string ("1,90,000.00"). Falls back to
        # the figures form when the DOM amount can't be parsed
        # (already shown as_raw before the conversion).
        expected_words_wrapped: str | None = (
            evidence.get("expected_amount_in_words")
            if isinstance(evidence, dict)
            else None
        )
        if expected_words_wrapped and expected_raw:
            expected_display = (
                f"{expected_words_wrapped} ({_display_amount(expected_raw)})"
            )
        else:
            expected_display = expected_raw
        plain = _plain_compare(
            "amount in words", status, from_cheque, expected_display,
            cheque_label="handwritten 'Rupees ... Only' line on the cheque",
            expected_label="amount in system (converted to words)",
        )
        return VerificationEvidence(
            plain_summary=plain,
            crop_bbox=bbox,
            crop_side=side,
            from_cheque=from_cheque,
            expected=expected_display,
            comparison_kind=comparison_kind,
        )

    if check_id == "amount_figures":
        figs_raw = (front.amount if front else None) or None
        structured = (
            _display_amount(figs_raw) if figs_raw else None
        )
        expected_raw = _dom_pick(dom, "Amount", "Batch Amount")
        if expected_raw and "/" in expected_raw and not expected_raw.endswith("/"):
            expected_raw = expected_raw.rsplit("/", 1)[-1].strip()
        rescue = _rescued_by_text_search(evidence)
        if status == "PASS" and rescue and expected_raw:
            structured_note = (
                f" (The digit-box reader returned {structured}, "
                f"which we ignored.)"
                if structured
                else ""
            )
            from_cheque = (
                f"{_display_amount(expected_raw)} "
                f"(found in cheque text — {rescue})"
            )
            plain = (
                f"The system amount {_display_amount(expected_raw)} "
                f"appears in the cheque's scanned text ({rescue}), so "
                f"it's confirmed.{structured_note}"
            )
        else:
            from_cheque = structured
            plain = _plain_compare(
                "amount in figures", status, from_cheque, expected_raw,
                cheque_label="digit-box amount on the cheque",
                expected_label="amount in system",
            )
        return VerificationEvidence(
            plain_summary=plain,
            crop_bbox=bbox,
            crop_side=side,
            from_cheque=from_cheque,
            expected=expected_raw,
            comparison_kind=comparison_kind,
        )

    if check_id == "cheque_no":
        structured = (front.cheque_no if front else None) or None
        expected = _dom_pick(dom, "Cheque No", "Cheque No.", "Cheque Number")
        rescue = _rescued_by_text_search(evidence)
        if status == "PASS" and rescue and expected and structured != expected:
            # The PASS came from finding the system cheque number in
            # the raw OCR text — NOT from the structured read (which
            # grabbed a stray digit run). Show the value that truly
            # matched so the row agrees with the green badge.
            from_cheque = f"{expected} (found in cheque text — {rescue})"
            plain = (
                f"The cheque number {expected} appears in the cheque's "
                f"scanned text ({rescue}), so it's confirmed."
            )
            if structured:
                plain += (
                    f" (The structured reader returned {structured!r}, "
                    f"a stray digit run, which we ignored.)"
                )
        else:
            from_cheque = structured
            plain = _plain_compare(
                "cheque number", status, from_cheque, expected,
                cheque_label="cheque number on cheque",
                expected_label="cheque number in system",
            )
        return VerificationEvidence(
            plain_summary=plain,
            crop_bbox=bbox,
            crop_side=side,
            from_cheque=from_cheque,
            expected=expected,
            comparison_kind=comparison_kind,
        )

    if check_id == "account_no":
        structured = (back.account_no if back else None) or None
        expected = _dom_pick(
            dom, "Account No", "Account No.", "A/C No", "A/C No.", "A/c No",
        )
        rescue = _rescued_by_text_search(evidence)
        if status == "PASS" and rescue and expected and structured != expected:
            from_cheque = f"{expected} (found in cheque text — {rescue})"
            plain = (
                f"The account number {expected} appears in the cheque's "
                f"scanned text ({rescue}), so it's confirmed."
            )
            if structured:
                plain += (
                    f" (The structured reader returned {structured!r}, "
                    f"which we ignored.)"
                )
        else:
            from_cheque = structured
            plain = _plain_compare(
                "account number", status, from_cheque, expected,
                cheque_label="account number on the back of the cheque",
                expected_label="account number in system",
            )
        return VerificationEvidence(
            plain_summary=plain,
            crop_bbox=bbox,
            crop_side=side,
            from_cheque=from_cheque,
            expected=expected,
            comparison_kind=comparison_kind,
        )

    if check_id == "signature":
        density = (
            getattr(front, "signature_density", 0.0) if front else 0.0
        )
        verdict = (
            getattr(front, "signature_verdict", None) if front else None
        )
        from_cheque = (
            f"signature {verdict}" if verdict
            else f"ink density {density:.1%}"
        )
        plain = (
            f"We looked at the signature band on the cheque's "
            f"bottom right. {summary}"
        )
        return VerificationEvidence(
            plain_summary=plain,
            crop_bbox=bbox,
            crop_side=side,
            from_cheque=from_cheque,
            expected="Signature present",
            comparison_kind=comparison_kind,
        )

    return None  # Unreachable given the _RULE_BBOXES gate above


def _plain_compare(
    field_label: str,
    status: str,
    from_cheque: str | None,
    expected: str | None,
    *,
    cheque_label: str,
    expected_label: str,
) -> str:
    """Generate a one-sentence plain-English explanation for the
    typical 'compare cheque value to system value' rule.

    The phrasing puts WHAT WE SAW first (operator orients by
    looking at the cheque) and the verdict at the end."""
    if not from_cheque and not expected:
        return (
            f"We couldn't read the {field_label} from the cheque, "
            f"and the bank's system didn't list one either."
        )
    if not from_cheque:
        return (
            f"We couldn't read the {field_label} from the cheque. "
            f"The {expected_label} is {expected!r}."
        )
    if not expected:
        return (
            f"The {cheque_label} reads {from_cheque!r}, but the "
            f"bank's system didn't list a value to compare against."
        )
    if status == "PASS":
        return (
            f"The {cheque_label} reads {from_cheque!r}, which "
            f"matches the {expected_label} ({expected!r})."
        )
    if status == "WARN":
        return (
            f"The {cheque_label} reads {from_cheque!r}, which is "
            f"close to but not an exact match for the "
            f"{expected_label} ({expected!r}). Eyeball please."
        )
    if status == "FAIL":
        return (
            f"The {cheque_label} reads {from_cheque!r}, but the "
            f"{expected_label} is {expected!r}. These don't match."
        )
    # NOT_VERIFIED
    return (
        f"The {cheque_label} reads {from_cheque!r}; the "
        f"{expected_label} is {expected!r}. The rule couldn't "
        f"reach a verdict — needs operator review."
    )


# Match the same date tokens cheque_consensus.normalize_date does
# (DDMMYYYY plus DD-MM / DD/MM / DD.MM with 4-digit year).
_DATE_TOKEN_FOR_DISPLAY_RE = re.compile(
    r"\b(\d{1,2}[-./\s]?\d{1,2}[-./\s]?\d{4})\b",
)


def _extract_date_from_text(text: str | None) -> str | None:
    """Find a date-shaped token in `text` and return it verbatim
    so the operator sees the raw cheque-text form rather than a
    canonical reformatting. Used only for the evidence payload's
    `from_cheque` display value."""
    if not text:
        return None
    m = _DATE_TOKEN_FOR_DISPLAY_RE.search(text)
    return m.group(1) if m else None


def _focused_region_text(fields: ChequeFields | None) -> str:
    """Summarise which handwriting bands the region-focused OCR
    passes (`paddle_focused_*` in `engine_runs`) actually read.
    Returns a short comma-list like "payee_line, date" when at
    least one band has non-empty text, or "" when all are empty
    (or there are no focused-pass entries at all).

    Used by the OCR health banner to decide between the soft
    "handwriting_fallback" message and the hard
    "handwriting_unavailable" one. When the fallback engine
    (EasyOCR) is producing text on the handwriting bands, the
    rules CAN evaluate them — the operator just needs to know
    accuracy is degraded vs the TrOCR-equipped path.
    """
    if fields is None or not fields.engine_runs:
        return ""
    bands_with_text: list[str] = []
    for run in fields.engine_runs:
        if not run or not run[0]:
            continue
        name = run[0]
        if not name.startswith("paddle_focused_"):
            continue
        text = (run[1] or "").strip() if len(run) > 1 else ""
        if not text:
            continue
        band = name[len("paddle_focused_"):]
        if band not in bands_with_text:
            bands_with_text.append(band)
    return ", ".join(bands_with_text)


# ---------------------------------------------------------------------------
# Shared fallback: search the raw OCR text for a DOM value
# ---------------------------------------------------------------------------


def _search_dom_in_ocr(
    dom_value: str,
    ocr_text: str,
    *,
    numeric: bool,
) -> tuple[str, float, str | None]:
    """Search `ocr_text` for `dom_value` using the same 3-tier
    strategy the presence-check panel runs (exact normalised
    substring → digit-only substring → fuzzy windowed
    SequenceMatcher). Returns `(verdict, similarity, match_kind)`
    where verdict is one of:

      * 'pass'      similarity ≥ _SIM_PASS (0.85) — clean match
      * 'warn'      similarity ≥ _SIM_WARN (0.50) — near-miss
      * 'fail'      similarity <  _SIM_WARN       — not on cheque
      * 'no_ocr'    ocr_text was empty

    Used by all four 'X Verification' rules as a fallback when
    the structured field extractors (regex / MICR / TrOCR)
    failed to isolate the value but the underlying OCR text
    DOES contain it with noise.
    """
    if not ocr_text or not ocr_text.strip():
        return "no_ocr", 0.0, None
    result = _check_presence(dom_value, ocr_text, numeric=numeric)
    sim = float(result.get("similarity") or 0.0)
    match_kind = result.get("match_kind")
    if result.get("present"):
        return "pass", sim, match_kind
    if sim >= _SIM_WARN:
        return "warn", sim, "fuzzy"
    return "fail", sim, None


# ---------------------------------------------------------------------------
# Rule 1: Date Verification
# ---------------------------------------------------------------------------

# Indian cheque date formats — operators write in DD-MM-YYYY, but
# OCR sometimes ingests them with `/`, `.`, or no separator, and
# the year is sometimes 2 digits. We also accept the bank's own
# `19-JUN-2026` MMM-YYYY presentation because OCR sometimes
# picks up the system-printed batch date adjacent to the cheque
# image.
#
# (pattern, datetime.strptime format).
_DATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(\d{1,2})[\-/\.](\d{1,2})[\-/\.](\d{4})\b"), "%d-%m-%Y"),
    (re.compile(r"\b(\d{1,2})[\-/\.](\d{1,2})[\-/\.](\d{2})\b"),  "%d-%m-%y"),
    (re.compile(r"\b(\d{2})(\d{2})(\d{4})\b"), "%d%m%Y"),  # boxed DDMMYYYY
    # Bank-printed batch-date form, e.g. '19-JUN-2026' / '19 JUN 2026'.
    (re.compile(r"\b(\d{1,2})[\-\s]([A-Z]{3})[\-\s](\d{4})\b", re.IGNORECASE),
     "%d-%b-%Y"),
)

# Special-case the BOXED DDMMYYYY date field that sits at the top
# of every CTS-2010 cheque — 8 single-digit cells. PaddleOCR
# reads them with spaces between every digit (and sometimes with
# stray glyphs in between when the box outlines are interpreted
# as separators), which means none of the patterns above match.
#
# This finds exactly 8 isolated digits within a small window
# (≤24 characters of any-noise), close enough together that they
# very plausibly came from the same boxed-date field. Reconstructs
# them as DDMMYYYY and parses with the strict pattern below.
_BOXED_DATE_RE = re.compile(
    r"(?<!\d)"            # don't start mid-number
    r"(\d)"               # D
    r"(?:[^\d]{0,3}?)"    # tolerate up to 3 non-digit chars between boxes
    r"(\d)"               # D
    r"(?:[^\d]{0,3}?)"
    r"(\d)"               # M
    r"(?:[^\d]{0,3}?)"
    r"(\d)"               # M
    r"(?:[^\d]{0,3}?)"
    r"(\d)"               # Y
    r"(?:[^\d]{0,3}?)"
    r"(\d)"               # Y
    r"(?:[^\d]{0,3}?)"
    r"(\d)"               # Y
    r"(?:[^\d]{0,3}?)"
    r"(\d)"               # Y
    r"(?!\d)"             # don't continue mid-number
)


def _extract_cheque_date_traced(
    front: ChequeFields | None,
    back: ChequeFields | None = None,
) -> tuple[date | None, str | None, str | None]:
    """Same ladder as `_extract_cheque_date` but ALSO returns a
    trace of WHICH step matched and WHAT raw text was parsed.
    Returns ``(parsed_date, source_label, raw_text_used)``.

    The trace exists so the operator can read in the date rule's
    evidence panel exactly which engine and which raw token the
    rule consumed — critical for diagnosing "stale-dated FAIL on
    a cheque whose date band visibly reads 21/06/2026" reports.
    Operator confusion (June 2026) was driven by the rule's
    summary saying e.g. "Stale-dated: 2207 days old" with no
    visible audit trail of the source date — adding the trace
    means the operator can immediately see whether the rule
    misparsed an 8-digit string OR consumed the wrong engine.
    """
    if front is not None:
        for run in front.engine_runs:
            if not run:
                continue
            engine_name = run[0]
            engine_text = run[1] if len(run) > 1 else ""
            if engine_name != "apple_vision_date" or not engine_text:
                continue
            # Apple Vision reads the cropped date band as free text,
            # so it may return "21062026", "21/06/2026", or
            # "21 06 2026" — let _try_parse_date handle every form
            # (it walks the separator patterns AND the boxed
            # 8-digit fallback).
            parsed = _try_parse_date(engine_text)
            if parsed:
                matched = _extract_date_from_text(engine_text) or engine_text
                return parsed, "apple_vision_date", matched

    if front is not None:
        for name, text, _conf in front.handwriting_regions:
            if name != "date" or not text:
                continue
            parsed = _try_parse_date(text)
            if parsed:
                return parsed, "trocr_date_region", text

    if front is not None:
        for run in front.engine_runs:
            if not run:
                continue
            engine_name = run[0]
            engine_text = run[1] if len(run) > 1 else ""
            if engine_name != "paddle_focused_date" or not engine_text:
                continue
            parsed = _try_parse_date(engine_text)
            if parsed:
                return parsed, "paddle_focused_date", engine_text

    if front and front.raw_text:
        parsed = _try_parse_date(front.raw_text)
        if parsed:
            matched = _extract_date_from_text(front.raw_text) or front.raw_text
            return parsed, "front_raw_text", matched

    if back and back.raw_text:
        parsed = _try_parse_date(back.raw_text)
        if parsed:
            matched = _extract_date_from_text(back.raw_text) or back.raw_text
            return parsed, "back_raw_text", matched

    return None, None, None


def _extract_cheque_date(
    front: ChequeFields | None,
    back: ChequeFields | None = None,
) -> date | None:
    """Find a parseable date in the OCR output. Strategy ladder
    (most-focused/clean source first → broadest fallback last):

      1. Apple Vision read of the cropped date band (engine_runs
         entry `apple_vision_date`) — Vision is already loaded as
         the primary engine and reads the boxed DDMMYYYY band in
         ~100-300ms. This replaced the old EasyOCR per-cell
         brute force, which cost 5-8s and still missed cells.
      2. TrOCR `date` region — focused handwriting OCR; byte-clean
         when TrOCR is available.
      3. PaddleOCR's focused-region pass on the same date band
         (engine_runs entry `paddle_focused_date`) — the
         upscale+CLAHE+sharpen recipe routinely reads the boxed
         DDMMYYYY digits even though they're written by hand,
         because they're spatially structured like printed digits.
         This is the fallback that rescues the date rule when
         Apple Vision's band read comes back empty.
      4. Front-side raw text regex sweep.
      5. Back-side raw text (endorsement stamps sometimes carry
         the deposit date).
    """
    # 1. Apple Vision date-band read. Stored in `engine_runs` as
    #    ("apple_vision_date", text, conf, ...). Vision returns the
    #    band as free text, so parse with the full date-pattern
    #    table (handles "21062026" / "21/06/2026" / "21 06 2026").
    if front is not None:
        for run in front.engine_runs:
            if not run:
                continue
            engine_name = run[0]
            engine_text = run[1] if len(run) > 1 else ""
            if engine_name != "apple_vision_date" or not engine_text:
                continue
            parsed = _try_parse_date(engine_text)
            if parsed:
                return parsed

    # 2. Prefer the TrOCR `date` region — when present it's a
    #    focused crop and usually byte-clean.
    if front is not None:
        for name, text, _conf in front.handwriting_regions:
            if name != "date" or not text:
                continue
            parsed = _try_parse_date(text)
            if parsed:
                return parsed

    # 3. Paddle's region-focused pass on the date box. Stored in
    #    `engine_runs` as ("paddle_focused_date", text, conf, ...).
    if front is not None:
        for run in front.engine_runs:
            if not run:
                continue
            engine_name = run[0]
            engine_text = run[1] if len(run) > 1 else ""
            if engine_name != "paddle_focused_date" or not engine_text:
                continue
            parsed = _try_parse_date(engine_text)
            if parsed:
                return parsed

    # 4. Front-side raw text.
    if front and front.raw_text:
        parsed = _try_parse_date(front.raw_text)
        if parsed:
            return parsed

    # 5. Back-side raw text (endorsement stamps sometimes carry
    #    the deposit date).
    if back and back.raw_text:
        parsed = _try_parse_date(back.raw_text)
        if parsed:
            return parsed

    return None


def _try_parse_date(text: str) -> date | None:
    """Walk the date-pattern table and return the first plausible
    parse. Plausibility filter: date must be between 2010 and 10
    years from today (cheques older than that don't exist in any
    real-world flow). Rejects month==0, day==0, etc."""
    if not text:
        return None
    today_year = date.today().year
    for pat, fmt in _DATE_PATTERNS:
        for m in pat.finditer(text):
            # Reconstruct the matched chunk in a strptime-friendly
            # form (single separator, no double spaces).
            raw = re.sub(r"[\s/\.]+", "-", m.group(0).strip())
            try:
                parsed = datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
            # Sanity-check the year — a 2-digit year that came
            # out as 19xx or 20xx is OK; anything before 2010 is
            # almost certainly an OCR misread of a recent date.
            if not (2010 <= parsed.year <= today_year + 10):
                continue
            return parsed

    # Boxed DDMMYYYY fallback — try every match because the cheque
    # body usually contains other 8-digit sequences too (cheque
    # number, account number suffix). We prefer the FIRST plausible
    # date because the boxed field sits at the top of the cheque
    # and the OCR text is normally rendered top-to-bottom.
    for m in _BOXED_DATE_RE.finditer(text):
        digits = "".join(m.groups())
        try:
            parsed = datetime.strptime(digits, "%d%m%Y").date()
        except ValueError:
            continue
        if 2010 <= parsed.year <= today_year + 10:
            return parsed
    return None


def _best_partial_date_read(
    front: ChequeFields | None,
) -> tuple[str, str] | None:
    """When the date ladder couldn't produce a CLEAN, valid date,
    dig the raw (unparsed) date-band OCR out of `engine_runs` so
    the operator can still SEE what the OCR managed to resolve.

    Returns ``(raw_text, source_engine)`` for the highest-signal
    partial read, or None when no date engine produced anything.

    Why this exists (operator question, June 2026): "you're
    confusing the date with the cheque number — so you ARE
    reading the date, why show 'not detected'?". The answer is
    that the boxed-date reader routinely resolves only 6-7 of the
    8 DDMMYYYY cells (or 8 cells at junk confidence), which the
    strict validator rejects rather than emit a WRONG date. But
    refusing to SHOW the partial read makes it look like the OCR
    saw nothing — so we surface it as a tentative, verify-me
    value. It is NEVER used for the pass/fail verdict.
    """
    if front is None:
        return None
    # Priority: the dedicated boxed-date reader first, then the
    # focused paddle pass on the same band.
    for want in ("apple_vision_date", "paddle_focused_date"):
        for run in front.engine_runs:
            if not run:
                continue
            name = run[0]
            text = run[1] if len(run) > 1 else ""
            if name == want and text and str(text).strip():
                return str(text).strip(), want
    return None


def _rule_date(
    *,
    front: ChequeFields | None,
    back: ChequeFields | None = None,
    today: date,
    validity_days: int,
    future_tolerance_days: int,
    **_kwargs: Any,
) -> CheckResult:
    ocr_date, ocr_date_path, ocr_date_raw = _extract_cheque_date_traced(
        front, back,
    )

    # VLM date — when present with high confidence, prefer it over
    # the OCR-derived date. The VLM's date answer is constrained
    # to 8 digits (DDMMYYYY) so we can parse deterministically.
    vlm_raw, vlm_conf = _vlm_field(front, "date_ddmmyyyy", "date_confidence")
    vlm_date: date | None = None
    if isinstance(vlm_raw, str) and len(vlm_raw) == 8 and vlm_raw.isdigit():
        try:
            vlm_date = datetime.strptime(vlm_raw, "%d%m%Y").date()
        except ValueError:
            vlm_date = None

    if vlm_date is not None and vlm_conf >= _VLM_TRUST_THRESHOLD:
        cheque_date = vlm_date
        date_source = "vlm"
    else:
        cheque_date = ocr_date
        date_source = "ocr"

    evidence: list[tuple[str, Any]] = [
        ("today", today.isoformat()),
        ("validity_days", validity_days),
        ("date_source", date_source),
    ]
    # Trace which OCR ladder step produced the date and which raw
    # text token it parsed. Surfaces the answer for operator-
    # reported "stale-dated mismatch even though the cheque clearly
    # says 21/06/2026" cases (June 2026) — without this trace the
    # operator can't tell whether the rule misparsed an 8-digit
    # string OR consumed the wrong engine. Only emitted when the
    # OCR path actually returned a date (the trace fields are
    # None when nothing matched).
    if ocr_date_path:
        evidence.append(("ocr_date_path", ocr_date_path))
    if ocr_date_raw:
        evidence.append(("ocr_date_raw_text", ocr_date_raw))

    if _vlm_payload(front):
        agree: str
        if vlm_date is not None and ocr_date is not None:
            agree = "agree" if vlm_date == ocr_date else "disagree"
        elif vlm_date is not None:
            agree = "vlm_only"
        elif ocr_date is not None:
            agree = "ocr_only"
        else:
            agree = "neither_signal"
        evidence.extend(
            _vlm_evidence_keys(
                "date_ddmmyyyy", "date_confidence",
                vlm_raw, vlm_conf, agree=agree,
            )
        )
    elif front is not None:
        evidence.append(("vlm_agreement", "vlm_unavailable"))

    if cheque_date is None:
        # No CLEAN date parsed — but the boxed-date reader almost
        # always resolves *something* (6-7 of 8 cells, or 8 cells
        # at low confidence). Surface that partial read so the
        # operator can see the OCR DID see the date band and can
        # eyeball it, instead of a bare "not detected". This read
        # is tentative and is NOT used for the stale/valid verdict.
        partial = _best_partial_date_read(front)
        if partial:
            partial_text, partial_src = partial
            evidence.append(("ocr_date_partial_read", partial_text))
            evidence.append(("ocr_date_partial_source", partial_src))
            summary = (
                f"No fully-legible date — the boxed-date reader "
                f"resolved '{partial_text}' (partial / low-confidence; "
                f"please verify against the cheque)."
            )
        else:
            summary = "No date could be extracted from the cheque face."
        return CheckResult(
            check_id="date",
            label="Date Verification",
            status="NOT_VERIFIED",
            summary=summary,
            details=(
                "Tried the boxed DDMMYYYY reader and the consolidated "
                "raw OCR text; no read matched a supported date format "
                "(DD-MM-YYYY / DD/MM/YYYY / DD.MM.YYYY / DDMMYYYY) at "
                "full 8-digit confidence. Any partial read shown above "
                "is for eyeballing only — it is not used for the "
                "stale/valid verdict.",
            ),
            evidence=tuple(evidence),
        )

    evidence.append(("cheque_date", cheque_date.isoformat()))
    delta = (today - cheque_date).days
    evidence.append(("age_days", delta))

    # Future-dated tolerance — most banks clear post-dated cheques
    # ON the date written, but a date >7 days in the future is
    # almost always a data-entry error or fraud signal.
    if delta < -future_tolerance_days:
        return CheckResult(
            check_id="date",
            label="Date Verification",
            status="WARN",
            summary=(
                f"Cheque dated {cheque_date.isoformat()} is "
                f"{-delta} days in the future — operator should "
                f"confirm intent."
            ),
            evidence=tuple(evidence),
        )

    # Stale-dated — RBI default 90 days.
    if delta > validity_days:
        return CheckResult(
            check_id="date",
            label="Date Verification",
            status="FAIL",
            summary=(
                f"Stale-dated: cheque is {delta} days old "
                f"(validity period {validity_days} days). Per RBI "
                f"DPSS.CO.CHD.No.2030/04.07.05/2011-12 this cheque "
                f"must not be accepted."
            ),
            evidence=tuple(evidence),
        )

    return CheckResult(
        check_id="date",
        label="Date Verification",
        status="PASS",
        summary=(
            f"Cheque dated {cheque_date.isoformat()} is within the "
            f"{validity_days}-day validity window."
        ),
        evidence=tuple(evidence),
    )


# ---------------------------------------------------------------------------
# Rule 2: Payee Name Verification (normalised-exact)
# ---------------------------------------------------------------------------


_PUNCT_RE = re.compile(r"[^a-z0-9\s]")


def _normalise_name(s: str | None) -> str:
    """Lower + strip punctuation + collapse whitespace. The
    operator-agreed 'exact' match is performed on this normalised
    form so 'JAYSHIVSAKTHI TRADERS' matches 'Jay Shivsakthi
    Traders' — which is what 'exact' means in practice on a
    handwritten cheque OCR'd through PaddleOCR + TrOCR."""
    if not s:
        return ""
    s = s.lower().replace("\u00a0", " ")
    s = _PUNCT_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _cheque_side_payee(front: ChequeFields | None) -> str | None:
    """Return the operator's best-guess payee name from the
    cheque side. Prefers the TrOCR `payee_line` region (most
    accurate on cursive handwriting); falls back to the
    consolidated `beneficiary` extractor."""
    if front is None:
        return None
    for name, text, _conf in front.handwriting_regions:
        if name == "payee_line" and text:
            return text
    return front.beneficiary


def _dom_beneficiaries(dom: dict[str, Any]) -> list[str]:
    """The bank's panel exposes the beneficiary as up to three
    rows (Beneficiary, Beneficiary 1, Beneficiary 2, Beneficiary
    3) — operators see the JOIN of these on a multi-payee
    cheque. We return every non-empty value so the rule can
    cross-check the cheque against any of them."""
    aliases = (
        "Beneficiary", "Beneficiary 1",
        "Beneficiary 2", "Beneficiary 3",
        "Payee", "Payee Name",
    )
    out: list[str] = []
    for k in aliases:
        v = dom.get(k)
        if v and str(v).strip():
            out.append(str(v).strip())
    return out


def _payee_token_score(beneficiary: str, ocr_text: str) -> float:
    """Fraction of `beneficiary`'s name-tokens that can be located
    in the `ocr_text`, using a per-token exact-or-fuzzy match.

    Designed to fix the specific failure mode the user reported:
    'HEMA RAM' (8 chars, two short tokens) accidentally fuzzy-
    matches at 50% against the OCR'd payee line of a DIFFERENT
    payee ('JAYSHIVSAKTHI TRADERS'), because SequenceMatcher
    scores a sliding window of 8 chars against any patch of OCR
    noise. Token-overlap correctly scores HEMA RAM = 0/2 there:
    neither 'hema' nor 'ram' appears anywhere in the OCR text.

    Per-token match strategy:
      1. Token appears verbatim in any OCR token → hit.
      2. Token is a substring of any OCR token (or vice versa)
         → hit. Catches the 'JAYSHIVSAKTHI' (no space) vs
         'JAY SHIVSAKTHI' (with space) case.
      3. Fuzzy-token match (SequenceMatcher ≥ 0.8) → hit.
         Catches the 'SHIVSAKTHI' vs 'SHIVSAKTI' case.

    Returns a float in [0, 1] = hits / total-significant-tokens.
    Tokens shorter than 3 chars are excluded from BOTH numerator
    and denominator (they collide too easily with random OCR
    noise — 'RAM' would otherwise match every 'RAM' substring in
    a long endorsement stamp).
    """
    bene_tokens_raw = _normalise_name(beneficiary).split()
    bene_tokens = [t for t in bene_tokens_raw if len(t) >= 3]
    if not bene_tokens:
        return 0.0
    ocr_norm = _normalise_name(ocr_text)
    ocr_tokens = ocr_norm.split()
    if not ocr_tokens:
        return 0.0

    from difflib import SequenceMatcher  # noqa: PLC0415

    hits = 0
    for bt in bene_tokens:
        if bt in ocr_tokens:
            hits += 1
            continue
        # Substring either direction — catches concatenated
        # 'JAYSHIVSAKTHI' vs 'JAY SHIVSAKTHI'.
        if any(bt in ot or ot in bt for ot in ocr_tokens if len(ot) >= 3):
            hits += 1
            continue
        # Fuzzy per-token: tolerates one or two letter swaps.
        if any(
            SequenceMatcher(None, bt, ot).ratio() >= 0.8
            for ot in ocr_tokens if len(ot) >= 3
        ):
            hits += 1
    return hits / len(bene_tokens)


def _rule_payee(
    *,
    front: ChequeFields | None,
    dom: dict[str, Any],
    **_kwargs: Any,
) -> CheckResult:
    """Rule 2 — match strategy ladder:
       1. Structured payee (TrOCR `payee_line` or `beneficiary`)
          == any DOM Beneficiary after normalisation → PASS.
       2. Token-overlap fallback: for each DOM beneficiary,
          compute the fraction of its name-tokens that appear
          (verbatim, substring, or per-token fuzzy) in
          `front.raw_text`. Highest-scoring beneficiary wins:
            ≥ 0.6 → PASS  (most tokens accounted for)
            ≥ 0.3 → WARN  (some signal — operator eyeball)
            < 0.3 → FAIL  (no real evidence on cheque)

    Token-overlap is more robust than SequenceMatcher windowing
    on the common multi-beneficiary case (system has 2-3 candidate
    payees; cheque carries exactly one of them): the WINNING
    beneficiary's tokens light up in OCR, the others score 0,
    and the ordering is unambiguous."""

    cheque_payee = _cheque_side_payee(front)
    dom_payees = _dom_beneficiaries(dom)
    raw_text = (front.raw_text if front else "") or ""

    evidence: list[tuple[str, Any]] = [
        ("cheque_payee", cheque_payee or ""),
        ("dom_payees", dom_payees),
    ]
    # Record the VLM's payee read in evidence even when its
    # confidence is below the trust threshold — operators
    # comparing the OCR fallback verdict to the VLM signal can
    # see when the VLM is hedging vs. confidently disagreeing.
    if _vlm_payload(front):
        peek_match, peek_conf = _vlm_field(
            front, "payee_match", "payee_confidence",
        )
        evidence.extend(
            _vlm_evidence_keys(
                "payee_match", "payee_confidence", peek_match, peek_conf,
            )
        )
    elif front is not None:
        evidence.append(("vlm_agreement", "vlm_unavailable"))

    if not dom_payees:
        return CheckResult(
            check_id="payee",
            label="Payee Name Verification",
            status="NOT_VERIFIED",
            summary="No payee name available in the system panel.",
            evidence=tuple(evidence),
        )

    # VLM cross-check — if the model confidently picked one of the
    # candidate payees (or said 'neither' confidently), trust it.
    # This is the rule that benefits most from the VLM because the
    # cursive-payee OCR problem is exactly what the LM context-
    # disambiguates: "H_M_ R_M" + candidate ['HEMA RAM',
    # 'JAYSHIVSAKTHI TRADERS'] resolves to HEMA RAM at high
    # confidence even when the OCR text is garbage.
    vlm_match, vlm_conf = _vlm_field(
        front, "payee_match", "payee_confidence",
    )
    if (
        isinstance(vlm_match, str)
        and vlm_conf >= _VLM_TRUST_THRESHOLD
        and vlm_match
        and vlm_match.lower() != "unreadable"
    ):
        vlm_evidence = list(evidence) + _vlm_evidence_keys(
            "payee_match", "payee_confidence", vlm_match, vlm_conf,
            agree="vlm_primary",
        )
        if vlm_match.lower() == "neither":
            return CheckResult(
                check_id="payee",
                label="Payee Name Verification",
                status="FAIL",
                summary=(
                    f"Local VLM read the cheque payee and reports it "
                    f"matches NONE of the system beneficiaries "
                    f"({', '.join(repr(p) for p in dom_payees)}) "
                    f"with {int(vlm_conf * 100)}% confidence."
                ),
                details=(
                    "VLM verdict — cross-check with the printed payee "
                    "line if this seems wrong.",
                ),
                evidence=tuple(vlm_evidence),
            )
        # Find canonical DOM match (case-insensitive).
        matched_dom = next(
            (p for p in dom_payees if p.lower() == vlm_match.lower()),
            vlm_match,
        )
        return CheckResult(
            check_id="payee",
            label="Payee Name Verification",
            status="PASS",
            summary=(
                f"Local VLM confirmed cheque payee matches system "
                f"beneficiary {matched_dom!r} ({int(vlm_conf * 100)}% "
                f"confidence)."
            ),
            evidence=tuple(vlm_evidence),
        )

    # Stage 1: structured-field exact match (after normalisation).
    if cheque_payee:
        cn = _normalise_name(cheque_payee)
        for dom_payee in dom_payees:
            if cn == _normalise_name(dom_payee):
                return CheckResult(
                    check_id="payee",
                    label="Payee Name Verification",
                    status="PASS",
                    summary=(
                        f"Cheque payee matches system beneficiary "
                        f"({dom_payee!r}) after case + whitespace + "
                        f"punctuation normalisation."
                    ),
                    evidence=tuple(evidence),
                )

    # Stage 2: token-overlap fallback. Score EACH DOM beneficiary
    # and pick the highest. We also compute a SequenceMatcher
    # similarity score per beneficiary so the evidence dict
    # carries both signals — operators looking at the report can
    # see why token-overlap accepted what windowing rejected (or
    # vice versa).
    per_payee: list[tuple[str, float, float]] = []  # (name, token_score, fuzzy_sim)
    for dom_payee in dom_payees:
        tscore = _payee_token_score(dom_payee, raw_text)
        _v, fsim, _k = _search_dom_in_ocr(dom_payee, raw_text, numeric=False)
        per_payee.append((dom_payee, tscore, fsim))

    # Ranking: prefer the highest token_score. Tie-break by fuzzy
    # similarity (gives the operator a sensible 'best match' even
    # when no beneficiary scores any tokens).
    per_payee.sort(key=lambda x: (x[1], x[2]), reverse=True)
    best_dom_payee, best_token_score, best_fuzzy = per_payee[0]

    evidence.append(("ocr_search_best_payee", best_dom_payee))
    evidence.append(("ocr_search_token_score", round(best_token_score, 3)))
    evidence.append(("ocr_search_fuzzy_similarity", round(best_fuzzy, 3)))
    # Per-payee breakdown so operators see ALL the candidates'
    # scores side by side — distinguishes 'one obviously matched'
    # from 'none matched but one was closest'.
    evidence.append((
        "per_payee_scores",
        [
            {
                "payee": n,
                "token_score": round(ts, 3),
                "fuzzy_similarity": round(fs, 3),
            }
            for n, ts, fs in per_payee
        ],
    ))

    if not raw_text and not cheque_payee:
        return CheckResult(
            check_id="payee",
            label="Payee Name Verification",
            status="NOT_VERIFIED",
            summary="OCR couldn't extract the payee name from the cheque.",
            evidence=tuple(evidence),
        )

    if best_token_score >= 0.6:
        return CheckResult(
            check_id="payee",
            label="Payee Name Verification",
            status="PASS",
            summary=(
                f"System beneficiary {best_dom_payee!r} matches the "
                f"cheque OCR ({int(best_token_score * 100)}% of its "
                f"name-tokens are present in the cheque text)."
            ),
            details=(
                "Token-overlap match — the operator should still "
                "eyeball the printed payee line if other beneficiaries "
                "scored close to this one.",
            ),
            evidence=tuple(evidence),
        )
    if best_token_score >= 0.3:
        return CheckResult(
            check_id="payee",
            label="Payee Name Verification",
            status="WARN",
            summary=(
                f"Partial match: {int(best_token_score * 100)}% of "
                f"{best_dom_payee!r}'s name-tokens are present in the "
                f"cheque OCR. Operator should eyeball the printed "
                f"payee line."
            ),
            evidence=tuple(evidence),
        )
    # Token-overlap couldn't find any name-tokens — but
    # SequenceMatcher might still have detected SOMETHING
    # (mangled chars / noise) that hints at the payee being
    # present. When the fuzzy similarity is >= 0.5 we surface
    # this as a WARN so the operator gets a "low-confidence
    # near-miss" rather than a hard FAIL. This rescues the
    # genuinely-handwritten cursive case where EasyOCR returns
    # garbled tokens but the overall character distribution
    # still resembles one of the beneficiaries.
    #
    # Threshold deliberately HIGHER than the >=0.3 token tier:
    # fuzzy is character-window based and can accidentally
    # match short names against long random OCR strings (the
    # exact false-positive that token-overlap was introduced
    # to fix). 0.5 keeps "HEMA RAM at 50% in a noisy payee
    # band" as a WARN, while still letting "JANE SMITH" vs
    # "JAYSHIVSAKTHI TRADERS" stay FAIL.
    if best_fuzzy >= 0.5:
        return CheckResult(
            check_id="payee",
            label="Payee Name Verification",
            status="WARN",
            summary=(
                f"Low-confidence near-miss: cheque OCR has "
                f"{int(best_fuzzy * 100)}% character similarity to "
                f"{best_dom_payee!r} but no name-tokens matched "
                f"cleanly. Likely an OCR misread of the cursive payee "
                f"line — operator should eyeball the printed payee "
                f"to confirm."
            ),
            evidence=tuple(evidence),
        )
    # No beneficiary's tokens overlap meaningfully → FAIL.
    return CheckResult(
        check_id="payee",
        label="Payee Name Verification",
        status="FAIL",
        summary=(
            f"Cheque payee does not match any system beneficiary "
            f"({', '.join(repr(p) for p in dom_payees)})."
            + (f" OCR extracted: {cheque_payee!r}." if cheque_payee else "")
        ),
        details=(
            "No beneficiary's name-tokens were located in the cheque "
            f"OCR (best token score: {int(best_token_score * 100)}%; "
            f"best fuzzy similarity: {int(best_fuzzy * 100)}%).",
        ),
        evidence=tuple(evidence),
    )


# ---------------------------------------------------------------------------
# Rule 3: Amount Verification (two sub-checks combined)
# ---------------------------------------------------------------------------


def _rule_amount(
    *,
    front: ChequeFields | None,
    dom: dict[str, Any],
    **_kwargs: Any,
) -> CheckResult:
    """Rule 3 — two sub-checks:
       a. amount-in-words on the cheque == amount-in-figures on
          the cheque ('internal consistency').
       b. amount-in-figures on the cheque == DOM/ST amount
          ('external match'). When the structured figures
          extractor came back empty, fall back to searching
          for the DOM amount digits in the raw OCR text.

    If either sub-check fails, the rule FAILs and the details
    pinpoint which sub-check broke. If a sub-check can't be
    evaluated either way, the rule is NOT_VERIFIED — operator
    can read the partial evidence to decide."""

    cheque_words_raw = (front.amount_words if front else None) or ""
    cheque_figures_raw = (front.amount if front else None) or ""
    dom_amount_raw = (
        dom.get("Amount") or dom.get("Batch Amount") or ""
    )
    # Batch Amount is "36 / 12,05,345.00" — take the rightmost
    # numeric token if a slash is present.
    if "/" in dom_amount_raw and not dom_amount_raw.endswith("/"):
        dom_amount_raw = dom_amount_raw.rsplit("/", 1)[-1].strip()

    words_value = words_to_decimal(cheque_words_raw)
    figures_value = figures_to_decimal(cheque_figures_raw)
    dom_value = figures_to_decimal(str(dom_amount_raw))
    raw_text = (front.raw_text if front else "") or ""

    evidence: list[tuple[str, Any]] = [
        ("cheque_amount_in_words", cheque_words_raw),
        ("cheque_amount_in_words_parsed",
         str(words_value) if words_value is not None else None),
        ("cheque_amount_in_figures", cheque_figures_raw),
        ("cheque_amount_in_figures_parsed",
         str(figures_value) if figures_value is not None else None),
        ("system_amount", dom_amount_raw),
        ("system_amount_parsed", str(dom_value) if dom_value is not None else None),
    ]

    # VLM cross-check — if both sub-answers came back high
    # confidence and BOTH say "matches", short-circuit to PASS.
    # If both came back high confidence and EITHER says "does
    # not match", short-circuit to FAIL. Otherwise fall through
    # to the OCR-driven sub-checks below.
    vlm_fig, vlm_fig_conf = _vlm_field(
        front, "amount_in_figures_matches", "amount_in_figures_confidence",
    )
    vlm_words, vlm_words_conf = _vlm_field(
        front, "amount_in_words_matches", "amount_in_words_confidence",
    )
    if _vlm_payload(front):
        evidence.extend(
            _vlm_evidence_keys(
                "amount_in_figures_matches",
                "amount_in_figures_confidence",
                vlm_fig, vlm_fig_conf,
            )
        )
        evidence.extend(
            _vlm_evidence_keys(
                "amount_in_words_matches",
                "amount_in_words_confidence",
                vlm_words, vlm_words_conf,
            )
        )
    elif front is not None:
        evidence.append(("vlm_agreement", "vlm_unavailable"))

    if (
        dom_value is not None
        and vlm_fig is not None
        and vlm_words is not None
        and vlm_fig_conf >= _VLM_TRUST_THRESHOLD
        and vlm_words_conf >= _VLM_TRUST_THRESHOLD
    ):
        if vlm_fig is True and vlm_words is True:
            return CheckResult(
                check_id="amount",
                label="Amount Verification",
                status="PASS",
                summary=(
                    f"Local VLM confirmed cheque amount matches "
                    f"system amount {dom_value} in both figures and "
                    f"words ({int(min(vlm_fig_conf, vlm_words_conf) * 100)}% "
                    f"min confidence)."
                ),
                evidence=tuple(evidence),
            )
        if vlm_fig is False or vlm_words is False:
            mismatch_parts = []
            if vlm_fig is False:
                mismatch_parts.append("figures")
            if vlm_words is False:
                mismatch_parts.append("words")
            return CheckResult(
                check_id="amount",
                label="Amount Verification",
                status="FAIL",
                summary=(
                    f"Local VLM reports cheque "
                    f"{' and '.join(mismatch_parts)} do NOT match "
                    f"the system amount {dom_value}."
                ),
                details=(
                    "VLM verdict — cross-check the amount box and "
                    "the 'Rupees ...' line if this seems wrong.",
                ),
                evidence=tuple(evidence),
            )

    # Sub-check 3a: words vs figures (internal).
    sub_a_status: str
    sub_a_summary: str
    if words_value is None or figures_value is None:
        sub_a_status = "NOT_VERIFIED"
        if words_value is None and figures_value is None:
            sub_a_summary = "OCR couldn't read either amount; can't compare."
        elif words_value is None:
            sub_a_summary = (
                "Amount-in-words couldn't be parsed; can't compare to figures."
            )
        else:
            sub_a_summary = (
                "Amount-in-figures couldn't be parsed; can't compare to words."
            )
    elif words_value == figures_value:
        sub_a_status = "PASS"
        sub_a_summary = (
            f"Amount-in-words ({words_value}) matches amount-in-figures "
            f"({figures_value})."
        )
    else:
        sub_a_status = "FAIL"
        sub_a_summary = (
            f"Amount mismatch on the cheque itself: words "
            f"say {words_value} but figures say {figures_value}."
        )

    # Sub-check 3b: cheque figures vs system amount (external).
    #
    # We DON'T short-circuit on `figures_value is not None`. The
    # structured extractor is occasionally wrong (e.g. it grabs a
    # stray '12/-' from a stamp or page label), and we want to
    # ALWAYS confirm the verdict by searching the raw OCR text
    # for the canonical DOM amount.
    #
    # Decision matrix (figures_value = F, search verdict = V):
    #   F == DOM         → PASS  (most common happy path)
    #   F != DOM, V=PASS → PASS  (extractor wrong, raw text correct)
    #   F != DOM, V=WARN → WARN  (operator should eyeball)
    #   F != DOM, V=FAIL → FAIL  (extractor confirms: not on cheque)
    #   F is None, V=*   → V-dependent (the previous behaviour)
    sub_b_status: str
    sub_b_summary: str
    if dom_value is None:
        sub_b_status = "NOT_VERIFIED"
        sub_b_summary = "No system amount available to compare against."
    elif figures_value is not None and figures_value == dom_value:
        sub_b_status = "PASS"
        sub_b_summary = (
            f"Cheque amount ({figures_value}) matches the system "
            f"amount ({dom_value})."
        )
    else:
        # Either figures_value is None OR it disagrees with DOM.
        # Run the raw-text search; if it hits, prefer it over the
        # structured value (with a note explaining the disagreement
        # so operators understand why the verdict isn't a simple
        # equality check).
        #
        # Try BOTH the DOM amount as-is AND a paise-stripped int
        # variant — operators write '51060' not '51060.00' in the
        # cheque's figures box.
        verdict, sim, kind = _search_dom_in_ocr(
            str(dom_amount_raw), raw_text, numeric=True,
        )
        int_part = int(dom_value) if dom_value == int(dom_value) else None
        if int_part is not None and verdict != "pass":
            alt_v, alt_s, alt_k = _search_dom_in_ocr(
                str(int_part), raw_text, numeric=True,
            )
            rank = {"pass": 3, "warn": 2, "fail": 1, "no_ocr": 0}
            if rank[alt_v] > rank[verdict] or (
                rank[alt_v] == rank[verdict] and alt_s > sim
            ):
                verdict, sim, kind = alt_v, alt_s, alt_k

        evidence.append(("ocr_search_similarity", round(sim, 3)))
        evidence.append(("ocr_search_kind", kind))
        if figures_value is not None:
            evidence.append((
                "extractor_disagreed",
                f"structured extractor returned {figures_value} but "
                f"raw-text search produced verdict={verdict}",
            ))

        if verdict == "pass":
            sub_b_status = "PASS"
            sub_b_summary = (
                f"System amount {dom_value} found in the OCR text "
                f"({kind} match)."
                + (
                    f" (Structured extractor returned {figures_value} —"
                    f" likely picked up a stray digit; raw-text search "
                    f"is more reliable here.)"
                    if figures_value is not None else ""
                )
            )
        elif verdict == "warn":
            sub_b_status = "WARN"
            sub_b_summary = (
                f"Near-miss: system amount {dom_value} appears in "
                f"the OCR with noise ({int(sim * 100)}% similarity)."
            )
        elif verdict == "no_ocr" and figures_value is not None:
            # No raw text to second-guess the structured value,
            # but structured DID extract something and it
            # disagrees with DOM. Trust the structured read → FAIL.
            sub_b_status = "FAIL"
            sub_b_summary = (
                f"Cheque amount ({figures_value}) does not match the "
                f"system amount ({dom_value})."
            )
        elif verdict == "no_ocr":
            # Both structured value and raw text are empty.
            sub_b_status = "NOT_VERIFIED"
            sub_b_summary = (
                "OCR text is empty; can't compare cheque amount "
                "to the system amount."
            )
        elif figures_value is not None:
            # Structured says X (≠ DOM), raw-text confirms DOM
            # isn't on the cheque either → FAIL.
            sub_b_status = "FAIL"
            sub_b_summary = (
                f"Cheque amount ({figures_value}) does not match the "
                f"system amount ({dom_value}), and the system amount "
                f"could not be located anywhere in the OCR text "
                f"(best similarity {int(sim * 100)}%)."
            )
        else:
            sub_b_status = "FAIL"
            sub_b_summary = (
                f"System amount {dom_value} not found on the cheque "
                f"image (best OCR similarity {int(sim * 100)}%)."
            )

    # Sub-check (a) defer: when sub-check (b) PASSed via raw-text
    # search (i.e. the system amount IS visibly on the cheque) AND
    # the structured words/figures extractors clearly grabbed
    # something that ISN'T an amount, FAILing the rule on the
    # words-vs-figures internal-consistency check is a false
    # negative — we're punishing the operator for our own
    # extraction quality.
    #
    # Production motivator: a cheque whose system amount
    # `346237.00` appeared verbatim in the OCR text (sub-check b
    # PASSed with similarity 1.0), but where the structured
    # extractors latched onto the drawer's printed A/C No band
    # (`NJC NO: 924030007346028` → 15-digit "amount in words")
    # and a stray date digit (`34374` → "amount in figures").
    # Sub-check (a) FAILed comparing `924030007246023` to `34374`,
    # which then FAILed the whole rule even though the cheque
    # genuinely carries the right amount.
    #
    # "Mis-targeted" heuristic: the structured value is not
    # amount-shaped — see `_amount_extractor_mis_targeted`. When
    # BOTH extractors are mis-targeted (or sub-check (a) was
    # comparing two clearly-not-amounts), we downgrade sub-check
    # (a) from FAIL to NOT_VERIFIED so it stops voting in the
    # combine step below.
    sub_a_deferred_for_mis_target = False
    if (
        sub_a_status == "FAIL"
        and sub_b_status == "PASS"
        and _amount_extractor_mis_targeted(
            cheque_words_raw, cheque_figures_raw,
            words_value, figures_value, dom_value,
        )
    ):
        original_a_summary = sub_a_summary
        sub_a_status = "NOT_VERIFIED"
        sub_a_summary = (
            "Skipped — structured words/figures extractors targeted "
            "wrong cheque region (got "
            f"words={cheque_words_raw!r}, figures={cheque_figures_raw!r}); "
            "sub-check (b) already confirmed the system amount is on "
            "the cheque."
        )
        sub_a_deferred_for_mis_target = True
        evidence.append(("sub_a_deferred_reason", original_a_summary))
        evidence.append((
            "sub_a_deferred",
            "extractors mis-targeted; sub-check (b) confirmed amount on cheque",
        ))

    # Combine. PASS only when both sub-checks pass; FAIL when
    # either fails; WARN when either warns (and neither fails);
    # NOT_VERIFIED otherwise. WARN tier is new since the user
    # has reported many near-miss cases — it's the operator-
    # eyeball signal.
    if sub_a_status == "PASS" and sub_b_status == "PASS":
        status = "PASS"
        summary = (
            f"Amount {figures_value or dom_value} verified: words / "
            f"figures / system amount all agree."
        )
    elif (
        sub_a_deferred_for_mis_target
        and sub_b_status == "PASS"
    ):
        # Sub-check (a) was deferred SPECIFICALLY because the
        # words/figures extractors mis-targeted (not because OCR
        # failed generally), AND sub-check (b) confirmed the
        # system amount IS visibly on the cheque. PASS the rule
        # with a note so operators understand why we couldn't
        # internally cross-check. We intentionally do NOT promote
        # the broader "sub_a NOT_VERIFIED + sub_b PASS" case to
        # PASS — that's the unparseable-words case where staying
        # NOT_VERIFIED is the conservative call (operator can
        # tell at a glance the rule wasn't fully exercised).
        status = "PASS"
        summary = (
            f"System amount {dom_value} found on the cheque "
            f"(structured words/figures extractors mis-targeted, so "
            f"internal words-vs-figures consistency couldn't be "
            f"verified — operator should eyeball if suspicious)."
        )
    elif sub_a_status == "FAIL" or sub_b_status == "FAIL":
        status = "FAIL"
        summary = (
            "Amount verification failed — see details for the "
            "specific sub-check that broke."
        )
    elif sub_a_status == "WARN" or sub_b_status == "WARN":
        status = "WARN"
        summary = (
            "Amount near-miss — see details. Operator should "
            "eyeball the figures box on the cheque."
        )
    else:
        status = "NOT_VERIFIED"
        summary = (
            "Amount could not be fully verified — one or more "
            "values couldn't be read or parsed."
        )

    return CheckResult(
        check_id="amount",
        label="Amount Verification",
        status=status,
        summary=summary,
        details=(
            f"(a) Words vs figures on cheque: {sub_a_status} — {sub_a_summary}",
            f"(b) Cheque vs system amount:    {sub_b_status} — {sub_b_summary}",
        ),
        evidence=tuple(evidence),
    )


# ---------------------------------------------------------------------------
# Rules 3a + 3b: Amount-in-Words + Amount-in-Figures (split, June 2026)
# ---------------------------------------------------------------------------
#
# The original `_rule_amount` rule combined two sub-checks inside one
# CheckResult — (a) words vs figures internal consistency and (b)
# cheque figures vs system amount. Operators found the merged
# "Amount Verification FAIL — see details" surface unhelpful because
# it required drilling into the rule to learn WHICH channel broke.
#
# Per the June 2026 operator request, the two channels are split
# into two top-level rules so each appears as its own row in the
# verification report:
#
#   * amount_words   — handwritten "Rupees ... Only" line vs SC value
#   * amount_figures — printed/handwritten digit box vs SC value
#
# Both rules compare DIRECTLY against the system (SC) amount; we
# DROP the internal words-vs-figures cross-check from the
# top-level rule list (the operator can read the two rule
# verdicts side-by-side and infer it). The mis-target heuristic
# from `_rule_amount` is preserved per-rule: a structured value
# that doesn't look amount-shaped at all is downgraded to
# NOT_VERIFIED so a bad extraction doesn't masquerade as a real
# mismatch.


def _amount_evidence_dom(
    front: ChequeFields | None,
    dom: dict[str, Any],
) -> tuple[str, Decimal | None]:
    """Helpers to extract the DOM/SC amount and parse it. Returns
    (raw_string, parsed_decimal_or_none). Shared by both rules so
    they read the same source-of-truth field.
    """
    dom_amount_raw = (
        dom.get("Amount") or dom.get("Batch Amount") or ""
    )
    # 'Batch Amount' often has the form '36 / 12,05,345.00' — keep
    # the rightmost numeric token after the slash.
    if "/" in dom_amount_raw and not dom_amount_raw.endswith("/"):
        dom_amount_raw = dom_amount_raw.rsplit("/", 1)[-1].strip()
    dom_value = figures_to_decimal(str(dom_amount_raw))
    return dom_amount_raw, dom_value


def _amount_words_similarity(observed: str, expected: str) -> float:
    """Token-normalized similarity between two amount-in-words
    strings on the 0..1 scale.

    Both strings are tokenised with the same rule the numeric
    parser uses (`_amount_words_tokenise`): lower-cased, split on
    whitespace / hyphens / commas, with the cheque preamble
    ('rupees') and terminators ('only', 'and') dropped so an
    OCR that loses the wrapper doesn't artificially drag the
    score down. The tokens are re-joined with a single space and
    fed to `difflib.SequenceMatcher`.

    DIAGNOSTIC ONLY — this score is surfaced in evidence so the
    operator can see how closely the cheque-side OCR resembles
    the DOM-derived expected words form (handy for spotting OCR
    drift on a single token), but it does NOT drive the verdict.
    A char-level SequenceMatcher can't tell "Twenty" vs "Ninety"
    (one critical numeral swap = real value mismatch) apart from
    "Pusnoyt" vs "Thousand" (one OCR-garbled scale word = same
    intended value): both score ~0.88. So we let the numeric
    parser remain the only verdict driver and surface this score
    purely as context.

    Returns 0.0 when either side is empty so callers can use the
    score directly without a defensive empty-check.
    """
    if not observed or not expected:
        return 0.0
    a = " ".join(_amount_words_tokenise(observed))
    b = " ".join(_amount_words_tokenise(expected))
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


# Minimum expected-token coverage for a failed-parse amount-in-words
# line to be surfaced as WARN ("likely the right amount, confirm")
# rather than a bare NOT_VERIFIED. 0.5 = at least half the expected
# value tokens are recognisable on the OCR'd line (e.g. 'Two' read but
# 'Lakh' too garbled to snap) — enough signal that the writing is on
# the cheque and matches, but not enough to auto-verify.
_AMOUNT_WORDS_COVERAGE_WARN: float = 0.5

# Minimum focused-pass OCR confidence required to let a FUZZY words read
# auto-PASS (even with figures-box corroboration). Below this we hold at
# WARN — a near-random low-confidence read that coincidentally fuzzy-
# parses to the DOM amount must not silently verify a banking amount.
_AMOUNT_WORDS_MIN_PASS_CONF: float = 0.45


def _rule_amount_in_words(
    *,
    front: ChequeFields | None,
    dom: dict[str, Any],
    **_kwargs: Any,
) -> CheckResult:
    """Compare the handwritten 'Rupees ... Only' line on the cheque
    against the system (SC) amount.

    Two pieces of evidence are surfaced for every verdict:

      1. NUMERIC COMPARISON (verdict driver) — parse the cheque
         words to a Decimal via `words_to_decimal` and compare to
         the DOM Decimal. PASS on equality, FAIL on inequality,
         NOT_VERIFIED when the OCR string can't be parsed at all.

      2. EXPECTED-WORDS DISPLAY (diagnostic, not a driver) —
         render the DOM amount via `decimal_to_words` to produce
         the canonical "Rupees ... Only" form and a similarity
         score against the cheque's OCR string. Carried in
         `evidence['expected_amount_in_words']` and
         `evidence['word_form_similarity']` so the operator can
         see at a glance what the line should have read and how
         closely the OCR'd text resembles it. Does NOT shift the
         verdict — see `_amount_words_similarity` for why a
         character-level ratio isn't a safe driver.

    Decision matrix:
      cheque_words → numeric W
      dom          → numeric D
      D is None                   → NOT_VERIFIED ('no SC amount')
      extractor mis-targeted      → NOT_VERIFIED
      W is None                   → NOT_VERIFIED
      W == D                      → PASS
      W != D                      → FAIL

    VLM short-circuit: when the VLM voted high-confidence
    (>= _VLM_TRUST_THRESHOLD) on `amount_in_words_matches`, trust
    that verdict and skip the OCR comparison entirely.
    """
    cheque_words_raw = (front.amount_words if front else None) or ""
    words_value = words_to_decimal(cheque_words_raw)
    dom_amount_raw, dom_value = _amount_evidence_dom(front, dom)

    # Fuzzy recovery: when the strict parse choked on cursive-OCR
    # garble, snap each token to its nearest closed-vocab number word
    # and re-parse (see `words_to_decimal(..., fuzzy=True)`). Only used
    # when strict returned nothing — strict equality stays the
    # canonical, no-surprises PASS path.
    fuzzy_value = (
        words_to_decimal(cheque_words_raw, fuzzy=True)
        if words_value is None
        else words_value
    )
    # Independent second read of the SAME amount: the courtesy figures
    # box. Equality with the DOM amount is what licenses a fuzzy-words
    # PASS (the corroborated policy) — two independent reads agreeing.
    cheque_figures_raw = (front.amount if front else None) or ""
    figures_value = figures_to_decimal(cheque_figures_raw)
    figures_corroborated = (
        figures_value is not None
        and dom_value is not None
        and figures_value == dom_value
    )

    # Signal: did a geometric-band focused OCR pass produce this
    # text? When yes, the line came from the amount-words band by
    # construction — the mis-target guard below (which catches the
    # full-page extractor latching onto the wrong band) is
    # unnecessary and would actively harm recall on cheques where
    # the focused pass's OCR-substituted output ('Ropeos Iwo lulch
    # Ouly') lacks the strict rupee-vocab tokens. Skip the guard
    # when the focused engine ran.
    from_focused_words_pass = bool(
        front is not None
        and any(
            run[0] == "rapidocr_focused_amount_words"
            and (run[1] or "").strip()
            for run in (front.engine_runs or ())
        )
    )
    # OCR confidence of the focused amount-words read (engine_runs index
    # 2). Used to gate the corroborated auto-PASS: a very low-confidence
    # handwriting read that happens to fuzzy-parse to the DOM amount is
    # exactly where a false-accept hides, so below the floor we hold it
    # at WARN (operator confirm) even when the figures box corroborates.
    focused_words_conf = 0.0
    if front is not None:
        for run in front.engine_runs or ():
            if run[0] == "rapidocr_focused_amount_words" and len(run) > 2:
                try:
                    focused_words_conf = float(run[2] or 0.0)
                except (TypeError, ValueError):
                    focused_words_conf = 0.0
                break

    # Convert the DOM amount to its canonical Indian English words
    # form so operators see "expected: 'Rupees One Lakh Ninety
    # Thousand Only'" alongside the cheque-side OCR. None when the
    # DOM didn't carry a parseable amount.
    expected_words_inner = decimal_to_words(dom_value)
    expected_words_wrapped = decimal_to_words(
        dom_value, with_rupees_wrapper=True,
    )
    word_similarity = (
        _amount_words_similarity(cheque_words_raw, expected_words_inner)
        if expected_words_inner is not None
        else 0.0
    )
    # Expected-guided coverage: fraction of the expected amount-words'
    # value tokens that resemble (fuzzily, in order) something on the
    # OCR'd line. High coverage with a failed numeric parse = "clearly
    # the right amount, just too garbled to auto-verify" → WARN.
    token_coverage = (
        expected_token_coverage(cheque_words_raw, expected_words_inner)
        if expected_words_inner is not None
        else 0.0
    )

    evidence: list[tuple[str, Any]] = [
        ("cheque_amount_in_words", cheque_words_raw),
        ("cheque_amount_in_words_parsed",
         str(words_value) if words_value is not None else None),
        ("cheque_amount_in_words_fuzzy_parsed",
         str(fuzzy_value) if fuzzy_value is not None else None),
        ("system_amount", dom_amount_raw),
        ("system_amount_parsed",
         str(dom_value) if dom_value is not None else None),
        ("expected_amount_in_words", expected_words_wrapped),
        ("word_form_similarity", round(word_similarity, 3)),
        ("expected_token_coverage", round(token_coverage, 3)),
        ("figures_corroborated", figures_corroborated),
        ("focused_words_confidence", round(focused_words_conf, 3)),
        # Soft internal cross-check: do the WORDS read and the FIGURES
        # read agree with EACH OTHER (independent of the DOM)? "agree" /
        # "disagree" / "unknown" (one side unreadable). Pure evidence —
        # it never changes the verdict here, but a "disagree" on a cheque
        # that still PASSed is a useful signal that one read is wrong.
        (
            "words_figures_consistency",
            (
                "unknown"
                if (fuzzy_value is None or figures_value is None)
                else ("agree" if fuzzy_value == figures_value else "disagree")
            ),
        ),
    ]

    vlm_w, vlm_w_conf = _vlm_field(
        front, "amount_in_words_matches", "amount_in_words_confidence",
    )
    if _vlm_payload(front):
        evidence.extend(
            _vlm_evidence_keys(
                "amount_in_words_matches",
                "amount_in_words_confidence",
                vlm_w, vlm_w_conf,
            )
        )
    elif front is not None:
        evidence.append(("vlm_agreement", "vlm_unavailable"))

    if (
        dom_value is not None
        and vlm_w is not None
        and vlm_w_conf >= _VLM_TRUST_THRESHOLD
    ):
        if vlm_w is True:
            return CheckResult(
                check_id="amount_words",
                label="Amount in Words Verification",
                status="PASS",
                summary=(
                    f"Local VLM confirmed the handwritten 'Rupees ... Only' "
                    f"line on the cheque matches the system amount "
                    f"{dom_value} (expected words "
                    f"{expected_words_wrapped!r}, "
                    f"{int(vlm_w_conf * 100)}% confidence)."
                ),
                evidence=tuple(evidence),
            )
        if vlm_w is False:
            return CheckResult(
                check_id="amount_words",
                label="Amount in Words Verification",
                status="FAIL",
                summary=(
                    f"Local VLM reports the handwritten amount line on the "
                    f"cheque does NOT match the system amount {dom_value} "
                    f"(expected words {expected_words_wrapped!r})."
                ),
                evidence=tuple(evidence),
            )

    if dom_value is None:
        return CheckResult(
            check_id="amount_words",
            label="Amount in Words Verification",
            status="NOT_VERIFIED",
            summary="No system amount available to compare against.",
            evidence=tuple(evidence),
        )

    # Mis-target guard — the OCR-extracted "amount in words"
    # string sometimes latches onto a non-amount band (printed
    # A/C No, MICR, branch line). When the string clearly isn't
    # amount-shaped we treat the rule as NOT_VERIFIED rather
    # than FAILing a real cheque on an extractor bug.
    #
    # Signals (any one is enough; mirrors the heuristics in
    # `_amount_extractor_mis_targeted` for consistency):
    #   * No rupee/number-word vocabulary in the raw string
    #   * Raw string contains a 10+ digit run (account-no shape)
    #   * Parsed value > 1e10 (₹10,000 crore — impossible for a
    #     real cheque; means parser fell back to digit-only mode)
    #
    # SKIP the no-rupee-vocab check when the value came from the
    # geometric-band focused pass (`rapidocr_focused_amount_words`):
    # by construction it cannot mis-target, and its OCR
    # substitutions ('Ropeos' for 'Rupees', 'Ouly' for 'Only')
    # routinely fail strict-vocab matching despite being clearly
    # the correct band. The digit-run and too-large heuristics
    # still apply since a focused-pass crop CAN over-extend if
    # the band geometry is mis-calibrated.
    if cheque_words_raw:
        tokens = re.findall(r"[a-zA-Z]+", cheque_words_raw.lower())
        # Fuzzy match (not strict membership) so cursive-OCR
        # substitutions of preprinted/handwritten amount-vocab
        # words still count — see `_has_amount_vocab` for the
        # threshold and the worked examples.
        has_amount_vocab = _has_amount_vocab(tokens)
        long_digit_run = bool(re.search(r"\d{10,}", cheque_words_raw))
        too_large = (
            words_value is not None and words_value > Decimal("1e10")
        )
        vocab_guard_fires = (
            not has_amount_vocab and not from_focused_words_pass
        )
        if vocab_guard_fires or long_digit_run or too_large:
            return CheckResult(
                check_id="amount_words",
                label="Amount in Words Verification",
                status="NOT_VERIFIED",
                summary=(
                    f"Amount-in-words extractor mis-targeted: the OCR "
                    f"string {cheque_words_raw!r} contains no "
                    f"recognisable amount-vocabulary tokens "
                    f"(rupees / only / hundred / thousand / lakh / "
                    f"crore / one / two / ... — even after fuzzy "
                    f"matching, so OCR substitutions like 'Ropeos' or "
                    f"'Ouly' would still have counted). Expected "
                    f"{expected_words_wrapped!r}. Operator should "
                    f"eyeball the cheque face."
                ),
                evidence=tuple(evidence),
            )

    # Focused-pass diagnostic: when the geometric-band focused pass
    # produced text BUT every word is an OCR substitution (no
    # recognised rupee/number vocabulary), reporting either a
    # parser-driven FAIL or a generic 'couldn't parse' NOT_VERIFIED
    # would lose the diagnostic the operator most needs: the
    # focused-pass text itself, plus the DOM-derived expected
    # words, side-by-side. Specialise the verdict here so both
    # show up in the summary and the operator can confirm visually
    # in one glance ("oh, 'Ropeos Iwo lulch Ouly' IS 'Rupees Two
    # Lakh Only' with cursive-OCR substitutions").
    #
    # This branch runs BEFORE the generic words_value-is-None and
    # words_value-vs-DOM branches: the focused pass produces noise
    # that often parses as a tiny number (a stray digit leaked
    # from the courtesy-amount box) or to None — both of which
    # would otherwise route to summaries that hide the focused-
    # pass text from the operator.
    if from_focused_words_pass and cheque_words_raw:
        tokens = re.findall(r"[a-zA-Z]+", cheque_words_raw.lower())
        # Fuzzy vocab match: cursive-OCR substitutions of any
        # amount-vocab word (the preprinted 'Rupees' label most
        # often, but also handwritten 'Only', 'Lakh', or any
        # number word) count as "OCR caught real amount text,
        # just garbled the letters". When the fuzzy threshold
        # still fails for EVERY token, we genuinely don't have
        # enough signal to drive a verdict — surface the raw
        # text and the expected words side-by-side so the
        # operator can compare visually.
        #
        # Note: this rule was previously phrased as "missing the
        # 'Rupees ... Only' wrapper". That framing was wrong —
        # 'Rupees' is preprinted and 'Only' is a customer
        # convention; the rule never strictly required either.
        # What it actually checks is "at least one
        # amount-vocabulary token (rupees / only / lakh /
        # hundred / two / three / ...) was readable". The new
        # message reflects that honestly.
        if not _has_amount_vocab(tokens):
            return CheckResult(
                check_id="amount_words",
                label="Amount in Words Verification",
                status="NOT_VERIFIED",
                summary=(
                    f"Focused OCR pass read {cheque_words_raw!r} on the "
                    f"amount-in-words band but couldn't recognise any "
                    f"amount-vocabulary token (rupees / only / hundred "
                    f"/ thousand / lakh / crore / one / two / ...) — "
                    f"even with fuzzy matching that would have caught "
                    f"OCR substitutions like 'Ropeos' for 'Rupees' or "
                    f"'Ouly' for 'Only'. Expected "
                    f"{expected_words_wrapped!r}. Operator should "
                    f"compare visually — the band geometry is correct, "
                    f"so the customer's writing is on the cheque, just "
                    f"too mangled by OCR to auto-verify."
                ),
                evidence=tuple(evidence),
            )

    if words_value is None:
        # Always echo the raw OCR text so the operator can diff it
        # against the expected words at a glance — distinguishing
        # "OCR got nothing" (empty raw text) from "OCR caught real
        # writing but every magnitude/number token was garbled by
        # cursive recognition" (e.g. 'Ropeos Iwo lulch Ouly').
        raw_display = (
            f"{cheque_words_raw!r}" if cheque_words_raw else "(empty)"
        )
        # Recovery path (a) — fuzzy parse recovered the EXACT system
        # amount. Auto-PASS ONLY when the independent figures box also
        # equals the DOM amount (the corroborated policy: two reads
        # agreeing). Otherwise WARN for one-click operator confirm —
        # we never silently auto-accept on fuzzy evidence alone.
        if fuzzy_value is not None and fuzzy_value == dom_value:
            # Confidence gate: only auto-PASS a fuzzy read that came from
            # the focused band if that band's OCR confidence clears the
            # floor. A read from the full-page pass has no per-band
            # confidence to judge, so it isn't gated (corroboration
            # stands on its own there).
            conf_ok = (
                (not from_focused_words_pass)
                or focused_words_conf >= _AMOUNT_WORDS_MIN_PASS_CONF
            )
            if figures_corroborated and conf_ok:
                evidence.append(("verdict_basis", "fuzzy_corroborated"))
                return CheckResult(
                    check_id="amount_words",
                    label="Amount in Words Verification",
                    status="PASS",
                    summary=(
                        f"Handwritten line {cheque_words_raw!r} was too "
                        f"garbled for a strict parse, but a fuzzy read "
                        f"recovers {fuzzy_value} — matching the system "
                        f"amount ({dom_value} → {expected_words_wrapped!r}) "
                        f"AND the cheque's figures box ({figures_value}). "
                        f"Two independent reads agree: confirmed."
                    ),
                    evidence=tuple(evidence),
                )
            if figures_corroborated and not conf_ok:
                # Both reads agree on the DOM amount, but the focused
                # handwriting read is too low-confidence to auto-accept.
                evidence.append(
                    ("verdict_basis", "fuzzy_corroborated_low_conf"),
                )
                return CheckResult(
                    check_id="amount_words",
                    label="Amount in Words Verification",
                    status="WARN",
                    summary=(
                        f"A fuzzy read of {cheque_words_raw!r} recovers "
                        f"{fuzzy_value}, matching the system amount "
                        f"({dom_value} → {expected_words_wrapped!r}) and the "
                        f"figures box ({figures_value}) — but the handwriting "
                        f"read confidence ({focused_words_conf:.2f}) is below "
                        f"the auto-pass floor ({_AMOUNT_WORDS_MIN_PASS_CONF}). "
                        f"Operator should confirm the handwritten amount."
                    ),
                    evidence=tuple(evidence),
                )
            evidence.append(("verdict_basis", "fuzzy_uncorroborated"))
            return CheckResult(
                check_id="amount_words",
                label="Amount in Words Verification",
                status="WARN",
                summary=(
                    f"A fuzzy read of the handwritten line {cheque_words_raw!r} "
                    f"recovers {fuzzy_value}, matching the system amount "
                    f"({dom_value} → {expected_words_wrapped!r}), but the "
                    f"figures box did not independently corroborate it. "
                    f"Operator should confirm the handwritten amount."
                ),
                evidence=tuple(evidence),
            )
        # Recovery path (b) — no number recovered, but the expected
        # words are largely recognisable on the line (band geometry is
        # right, customer's writing is present). Surface WARN ("confirm")
        # instead of a bare NOT_VERIFIED.
        if token_coverage >= _AMOUNT_WORDS_COVERAGE_WARN:
            evidence.append(("verdict_basis", "fuzzy_uncorroborated"))
            return CheckResult(
                check_id="amount_words",
                label="Amount in Words Verification",
                status="WARN",
                summary=(
                    f"The handwritten line {raw_display} couldn't be parsed "
                    f"to a number, but {int(round(token_coverage * 100))}% of "
                    f"the expected words {expected_words_wrapped!r} are "
                    f"recognisable on it — it likely reads the right amount. "
                    f"Operator should confirm the handwritten amount line."
                ),
                evidence=tuple(evidence),
            )
        evidence.append(("verdict_basis", "unparsed"))
        return CheckResult(
            check_id="amount_words",
            label="Amount in Words Verification",
            status="NOT_VERIFIED",
            summary=(
                f"Amount-in-words couldn't be parsed into a numeric "
                f"value from the OCR text {raw_display}. Expected "
                f"(from system amount) {expected_words_wrapped!r}. "
                f"Operator should eyeball the handwritten amount "
                f"line on the cheque face."
            ),
            evidence=tuple(evidence),
        )
    if words_value == dom_value:
        evidence.append(("verdict_basis", "strict"))
        return CheckResult(
            check_id="amount_words",
            label="Amount in Words Verification",
            status="PASS",
            summary=(
                f"Amount-in-words {cheque_words_raw!r} (= {words_value}) "
                f"matches the system amount ({dom_value} → "
                f"{expected_words_wrapped!r})."
            ),
            evidence=tuple(evidence),
        )
    evidence.append(("verdict_basis", "strict"))
    return CheckResult(
        check_id="amount_words",
        label="Amount in Words Verification",
        status="FAIL",
        summary=(
            f"Amount-in-words {cheque_words_raw!r} (= {words_value}) "
            f"does NOT match the system amount ({dom_value} → "
            f"{expected_words_wrapped!r})."
        ),
        evidence=tuple(evidence),
    )


def _rule_amount_in_figures(
    *,
    front: ChequeFields | None,
    dom: dict[str, Any],
    **_kwargs: Any,
) -> CheckResult:
    """Compare the digit-box amount on the cheque against the
    system (SC) amount. Mirrors `_rule_amount_in_words` but with
    a defensive raw-text fallback for cases where the structured
    extractor mis-fired (e.g. picked up a stray date digit or
    cheque-number suffix).

    Decision matrix (figures_value = F, search verdict on raw OCR = V):
      F == DOM                → PASS  (most common happy path)
      F != DOM, V=PASS        → PASS  (extractor wrong, raw text correct)
      F != DOM, V=WARN        → WARN  (operator should eyeball)
      F != DOM, V=FAIL        → FAIL  (raw text agrees figure isn't there)
      F is None, V=PASS       → PASS
      F is None, V=WARN       → WARN
      F is None, V=FAIL/no_ocr → NOT_VERIFIED
      DOM is None             → NOT_VERIFIED

    VLM short-circuit on `amount_in_figures_matches`.
    """
    cheque_figures_raw = (front.amount if front else None) or ""
    figures_value = figures_to_decimal(cheque_figures_raw)
    dom_amount_raw, dom_value = _amount_evidence_dom(front, dom)
    raw_text = (front.raw_text if front else "") or ""

    evidence: list[tuple[str, Any]] = [
        ("cheque_amount_in_figures", cheque_figures_raw),
        ("cheque_amount_in_figures_parsed",
         str(figures_value) if figures_value is not None else None),
        ("system_amount", dom_amount_raw),
        ("system_amount_parsed",
         str(dom_value) if dom_value is not None else None),
    ]

    vlm_f, vlm_f_conf = _vlm_field(
        front, "amount_in_figures_matches", "amount_in_figures_confidence",
    )
    if _vlm_payload(front):
        evidence.extend(
            _vlm_evidence_keys(
                "amount_in_figures_matches",
                "amount_in_figures_confidence",
                vlm_f, vlm_f_conf,
            )
        )
    elif front is not None:
        evidence.append(("vlm_agreement", "vlm_unavailable"))

    if (
        dom_value is not None
        and vlm_f is not None
        and vlm_f_conf >= _VLM_TRUST_THRESHOLD
    ):
        if vlm_f is True:
            return CheckResult(
                check_id="amount_figures",
                label="Amount in Figures Verification",
                status="PASS",
                summary=(
                    f"Local VLM confirmed the digit-box amount on the "
                    f"cheque matches the system amount {dom_value} "
                    f"({int(vlm_f_conf * 100)}% confidence)."
                ),
                evidence=tuple(evidence),
            )
        if vlm_f is False:
            return CheckResult(
                check_id="amount_figures",
                label="Amount in Figures Verification",
                status="FAIL",
                summary=(
                    f"Local VLM reports the digit-box amount on the "
                    f"cheque does NOT match the system amount {dom_value}."
                ),
                evidence=tuple(evidence),
            )

    if dom_value is None:
        return CheckResult(
            check_id="amount_figures",
            label="Amount in Figures Verification",
            status="NOT_VERIFIED",
            summary="No system amount available to compare against.",
            evidence=tuple(evidence),
        )

    # Mis-target guard for figures: a >10-digit raw string is
    # structurally impossible as an Indian cheque amount (>1000
    # crore). When we see one, force the structured value out
    # of the comparison and rely solely on the raw-text rescue
    # below — otherwise a 15-digit garbage extraction silently
    # FAILs a cheque whose real amount IS visible elsewhere in
    # the OCR text.
    figures_mis_targeted = bool(
        cheque_figures_raw
        and re.search(r"\d{11,}", cheque_figures_raw)
    )
    if figures_mis_targeted:
        figures_value = None
        evidence.append((
            "structured_figures_mis_targeted",
            f"raw extractor returned {cheque_figures_raw!r} which "
            "looks like a printed account-number band, not an "
            "amount — falling back to raw-text search.",
        ))

    if figures_value is not None and figures_value == dom_value:
        return CheckResult(
            check_id="amount_figures",
            label="Amount in Figures Verification",
            status="PASS",
            summary=(
                f"Amount-in-figures {cheque_figures_raw!r} "
                f"(= {figures_value}) matches the system amount "
                f"({dom_value})."
            ),
            evidence=tuple(evidence),
        )

    # Either figures_value is None OR it disagrees with DOM —
    # fall back to a raw-text similarity search for the DOM
    # amount on the cheque. Try DOM as-is and a paise-stripped
    # int variant (operators write '51060' not '51060.00').
    verdict, sim, kind = _search_dom_in_ocr(
        str(dom_amount_raw), raw_text, numeric=True,
    )
    int_part = int(dom_value) if dom_value == int(dom_value) else None
    if int_part is not None and verdict != "pass":
        alt_v, alt_s, alt_k = _search_dom_in_ocr(
            str(int_part), raw_text, numeric=True,
        )
        rank = {"pass": 3, "warn": 2, "fail": 1, "no_ocr": 0}
        if rank[alt_v] > rank[verdict] or (
            rank[alt_v] == rank[verdict] and alt_s > sim
        ):
            verdict, sim, kind = alt_v, alt_s, alt_k

    evidence.append(("ocr_search_similarity", round(sim, 3)))
    evidence.append(("ocr_search_kind", kind))
    if figures_value is not None:
        evidence.append((
            "extractor_disagreed",
            f"structured extractor returned {figures_value} but "
            f"raw-text search produced verdict={verdict}",
        ))

    if verdict == "pass":
        # Raw text confirms DOM IS visible on the cheque even
        # though the structured extractor missed/misread.
        suffix = (
            f" (Structured extractor returned {figures_value} — "
            f"likely picked up a stray digit; raw-text search "
            f"is more reliable here.)"
            if figures_value is not None else ""
        )
        return CheckResult(
            check_id="amount_figures",
            label="Amount in Figures Verification",
            status="PASS",
            summary=(
                f"System amount {dom_value} found in the cheque OCR "
                f"text ({kind} match)." + suffix
            ),
            evidence=tuple(evidence),
        )
    if verdict == "warn":
        extracted_note = (
            f"Extracted figure was {cheque_figures_raw!r}. "
            if cheque_figures_raw else ""
        )
        return CheckResult(
            check_id="amount_figures",
            label="Amount in Figures Verification",
            status="WARN",
            summary=(
                f"Near-miss: system amount {dom_value} appears in the "
                f"OCR with noise ({int(sim * 100)}% similarity). "
                f"{extracted_note}Operator should eyeball the "
                f"figures box."
            ),
            evidence=tuple(evidence),
        )
    if verdict == "no_ocr" and figures_value is None:
        return CheckResult(
            check_id="amount_figures",
            label="Amount in Figures Verification",
            status="NOT_VERIFIED",
            summary=(
                "OCR text is empty; can't compare cheque amount "
                "to the system amount."
            ),
            evidence=tuple(evidence),
        )
    # Structured says X (≠ DOM) and raw-text didn't find DOM either
    # → FAIL. Or structured is None but raw-text search came back
    # FAIL with non-empty raw text → also FAIL.
    #
    # Special case: when figures was forcibly cleared due to the
    # mis-target guard above, do NOT fall through to the "no
    # structured value, raw-text didn't find DOM → FAIL" path —
    # we have no real signal either way. Surface NOT_VERIFIED.
    if figures_mis_targeted and figures_value is None:
        return CheckResult(
            check_id="amount_figures",
            label="Amount in Figures Verification",
            status="NOT_VERIFIED",
            summary=(
                f"Amount-in-figures extractor mis-targeted (raw "
                f"string was a long digit run, not an amount) and "
                f"the system amount ({dom_value}) couldn't be "
                f"located in the OCR text either. Operator should "
                f"eyeball the cheque face."
            ),
            evidence=tuple(evidence),
        )
    if figures_value is not None:
        return CheckResult(
            check_id="amount_figures",
            label="Amount in Figures Verification",
            status="FAIL",
            summary=(
                f"Amount-in-figures {cheque_figures_raw!r} "
                f"(= {figures_value}) does NOT match the system amount "
                f"({dom_value}), and the system amount could not be "
                f"located anywhere in the OCR text (best similarity "
                f"{int(sim * 100)}%)."
            ),
            evidence=tuple(evidence),
        )
    return CheckResult(
        check_id="amount_figures",
        label="Amount in Figures Verification",
        status="FAIL",
        summary=(
            f"System amount {dom_value} not found on the cheque image "
            f"(best OCR similarity {int(sim * 100)}%)."
        ),
        evidence=tuple(evidence),
    )


# ---------------------------------------------------------------------------
# Rule 4: Cheque Number Verification
# ---------------------------------------------------------------------------


def _digits_only(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def _longest_dom_run_in_ocr(
    dom_digits: str, ocr_text: str,
) -> tuple[int, str]:
    """Find the longest contiguous prefix OR suffix of `dom_digits`
    that appears as a substring of the OCR-digits view of
    `ocr_text` — tried under every OCR letter↔digit substitution
    variant the cheque-ocr layer knows about.

    Returns `(length, matched_substring)`. When no portion of
    dom_digits at least 4 chars long matches, returns `(0, "")`.

    Designed for the account-no rule's partial-match WARN tier:
    when full-DOM substring search FAILs but a significant prefix
    of the DOM (e.g. first 8 of 14 digits) IS visible on the
    cheque, surface that as 'operator should eyeball the rest'
    rather than an opaque FAIL. This is the signal we ship when
    OCR captures `9999 11 88 I RR` from a stamp printed as
    `9999 11 88 11 88 18` — 8-9 of 14 digits in correct sequence
    is a strong 'likely yours' signal even when the last 5 are
    too mangled to confirm.
    """
    if not dom_digits or not ocr_text:
        return (0, "")

    # cheque_ocr exposes _ocr_letter_digit_variants which enumerates
    # candidate digit-only strings derived from the OCR text by
    # substituting letter↔digit confusions inside tokens that
    # contain digits. Import lazily to avoid an import cycle.
    try:
        from aakaar_caps.cheque.cheque_ocr import (  # noqa: PLC0415
            _ocr_letter_digit_variants,
        )
        variants = _ocr_letter_digit_variants(ocr_text)
    except Exception:  # noqa: BLE001
        variants = [re.sub(r"\D", "", ocr_text)]

    best_len = 0
    best_str = ""
    # Try shrinking prefixes (DOM[0:n] for n=len..4) and shrinking
    # suffixes (DOM[-n:] for n=len..4). The first n that matches
    # in any variant is the longest viable partial.
    n = len(dom_digits)
    while n >= 4:
        prefix = dom_digits[:n]
        suffix = dom_digits[-n:]
        for variant in variants:
            if variant and prefix in variant:
                if n > best_len:
                    best_len = n
                    best_str = prefix
                break
            if variant and suffix != prefix and suffix in variant:
                if n > best_len:
                    best_len = n
                    best_str = suffix
                break
        if best_len:
            return (best_len, best_str)
        n -= 1
    return (best_len, best_str)


# Common Indian-English rupee-amount vocabulary. When the
# "amount in words" extractor latches onto the wrong cheque
# region (most often the drawer's printed A/C No band), the
# result it returns NEVER contains any of these tokens — a
# real amount-in-words line always has at least one of
# "rupees" / "only" / a magnitude word ("hundred", "thousand",
# "lakh", "crore"). We use this as one input to
# `_amount_extractor_mis_targeted`.
_RUPEE_WORD_TOKENS: frozenset[str] = frozenset({
    "rupee", "rupees", "rs", "rs.", "only", "paise", "paisa",
    "hundred", "thousand", "lakh", "lakhs", "crore", "crores",
    # Single-digit number words also count as evidence —
    # extraction routinely catches "Forty Six Thousand" etc.
    "zero", "one", "two", "three", "four", "five",
    "six", "seven", "eight", "nine", "ten", "eleven",
    "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen", "twenty", "thirty",
    "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
})


# Fuzzy threshold for matching an OCR'd token against
# `_RUPEE_WORD_TOKENS`. Calibrated so the common
# preprinted-text OCR substitutions count as evidence the
# extractor IS on the right band, without admitting genuinely
# unrelated tokens:
#
#   "ropeos" vs "rupees"  → 0.667  (PASS — same word, garbled)
#   "ouly"   vs "only"    → 0.750  (PASS)
#   "iwo"    vs "two"     → 0.667  (PASS)
#   "lulch"  vs "lakh"    → 0.444  (FAIL — too garbled to count)
#   "and"    vs any word  → ~0.3   (FAIL — connector noise)
#   "xxxx"   vs any word  → ~0.0   (FAIL — pure garbage)
#
# Setting it any lower (e.g. 0.5) starts admitting common
# connectors like "and"/"the". Setting it any higher
# (e.g. 0.75) drops "ropeos" — defeating the whole purpose,
# since "Rupees" is preprinted and routinely the most-OCR'd
# word on the band.
_RUPEE_VOCAB_FUZZY_THRESHOLD: float = 0.6


def _has_amount_vocab(tokens: Iterable[str]) -> bool:
    """Return True if any token in `tokens` looks like an
    amount-vocabulary word — EXACT match against
    `_RUPEE_WORD_TOKENS` first, then a fuzzy fallback using
    `difflib.SequenceMatcher` so common OCR substitutions of
    the cheque's PREPRINTED "Rupees" prefix (e.g. 'Ropeos',
    'Rupees>', 'Rurees') still count as evidence the
    extractor is on the right band.

    This is deliberately MORE permissive than a strict
    set-membership check: 'Rupees' is preprinted on the
    cheque so cursive-OCR substitutions are the norm, not the
    exception, and we don't want to gate the rule on a literal
    'Rupees ... Only' wrapper. Tokens shorter than 3 chars
    skip the fuzzy fallback so single-letter OCR noise doesn't
    spuriously match 'rs' / 'one' / 'two'.

    The exact-match fast path is preserved so 99% of real
    cheques (where OCR DOES read 'Rupees' / 'Only' / 'Two' /
    etc. correctly) hit a single set lookup and pay nothing
    for the fuzzy fallback.
    """
    vocab = _RUPEE_WORD_TOKENS
    fuzzy_candidates: list[str] = []
    for token in tokens:
        if token in vocab:
            return True
        if len(token) >= 3:
            fuzzy_candidates.append(token)
    if not fuzzy_candidates:
        return False
    # Fuzzy fallback runs only for tokens that didn't hit the
    # exact set. Use SequenceMatcher with ratio threshold.
    threshold = _RUPEE_VOCAB_FUZZY_THRESHOLD
    for token in fuzzy_candidates:
        for vocab_word in vocab:
            if len(vocab_word) < 3:
                # 'rs' is too short for a meaningful fuzzy
                # ratio — it would near-match almost anything
                # with an 'r' and an 's'.
                continue
            ratio = difflib.SequenceMatcher(
                a=token, b=vocab_word, autojunk=False,
            ).ratio()
            if ratio >= threshold:
                return True
    return False


def _amount_extractor_mis_targeted(
    words_raw: str,
    figures_raw: str,
    words_value: float | None,
    figures_value: float | None,
    dom_value: float | None,
) -> bool:
    """Return True when the structured words/figures amount
    extractors clearly grabbed text that ISN'T an amount.

    Heuristics — any one is enough to flag mis-targeting:

      (i)  The "amount in words" string contains NONE of the
           canonical rupee vocabulary (rupees / only / magnitude
           words) AT ALL. Real amount-in-words lines always have
           at least one such token.

      (ii) The words RAW string contains a digit run of ≥10
           digits — this is a structural impossibility for a real
           amount-in-words line (which spells numbers out as
           words) and a dead giveaway that the extractor grabbed
           a printed A/C No band or MICR digits. Note we apply
           this even when (i) passed — the raw string can carry
           a single header word like 'Rupee>' followed by a 15-
           digit account number; the digit run is the stronger
           signal.

      (iii) The figures RAW string contains a digit run of >10
            digits. Indian cheques are capped well below a
            quadrillion rupees, so anything beyond 10 digits is
            structurally impossible as an amount (10 digits =
            ~1000 crore, far above any retail cheque ceiling).

      (iv)  Words parsed value is implausibly large (>1e10 rupees
            = ₹10,000 crore). No Indian cheque is for that much;
            getting that number out of the parser means it fell
            back to digit-extraction mode on a non-amount string.

      (v)   Words and figures parsed values have wildly different
            digit lengths (one is >2× the other) AND they
            disagree with each other. Real OCR noise on a single
            amount field perturbs digits but doesn't change the
            digit count — when one extractor returns a 5-digit
            number and the other returns a 15-digit number, they
            are clearly reading different cheque regions.

    Designed to be CONSERVATIVE — we never want to defer a real
    "words and figures genuinely disagree" case (operator must
    see that). All five checks are explicit "this clearly isn't
    an amount" signals, not "this might be wrong".
    """

    def looks_word_targeted(s: str) -> bool:
        if not s:
            return False
        # Tokens lower-cased and stripped of trailing punctuation
        # so 'Rupees,' and 'rupees' both count. Falls through to
        # a fuzzy match for OCR substitutions of preprinted text
        # (e.g. 'Ropeos' for 'Rupees') — see `_has_amount_vocab`.
        tokens = re.findall(r"[a-zA-Z]+", s.lower())
        return _has_amount_vocab(tokens)

    # (i) Words extractor produced text but no rupee vocabulary
    # anywhere. (Stripped of false positives like 'Rupee>' as a
    # bare header — that path is caught by (ii) below.)
    words_text_present = bool(words_raw and words_raw.strip())
    if words_text_present and not looks_word_targeted(words_raw):
        return True

    # (ii) Words RAW string contains a long bare digit run. Real
    # amount-in-words lines spell numbers out — 'Forty Six Thousand
    # Two Hundred Seven' — and never contain a 10+-digit run.
    # When the extractor catches a printed account-number band
    # (e.g. 'Ac NO 924030007246023') the digits-only is a dead
    # giveaway even if 'Rupee>' sneaks in as a header.
    words_digits = _digits_only(words_raw)
    if len(words_digits) >= 10:
        return True

    # (iii) Figures extractor produced an implausibly long digit
    # run. Measured on the RAW string so a stray group of >10
    # digits is caught even if the parser silently truncated.
    figures_digits = _digits_only(figures_raw)
    if len(figures_digits) > 10:
        return True

    # (iv) Words parsed value is structurally impossible as an
    # Indian cheque amount. 1e10 = ₹10,000 crore.
    if words_value is not None and words_value > 1e10:
        return True

    # (v) Words and figures parsed-value digit lengths differ by
    # more than 2× AND the parsed values disagree. A single
    # amount field with OCR noise might swap one digit for
    # another but won't change the length by 2×.
    if (
        words_value is not None
        and figures_value is not None
        and words_value != figures_value
    ):
        w_len = len(str(int(words_value))) if words_value > 0 else 0
        f_len = len(str(int(figures_value))) if figures_value > 0 else 0
        if w_len >= 4 and f_len >= 1 and (
            w_len > 2 * f_len or f_len > 2 * w_len
        ):
            return True

    return False


def _rule_cheque_no(
    *,
    front: ChequeFields | None,
    dom: dict[str, Any],
    **_kwargs: Any,
) -> CheckResult:
    """Rule 4 — printed cheque number on the cheque (MICR-derived
    preferred, body-text extractor fallback) must equal the
    system cheque number. Comparison is digit-only so OCR
    whitespace artefacts don't cause false fails.

    Three-stage match strategy, escalating from cleanest to most
    permissive:
      1. Structured field equality (`front.cheque_no` == DOM
         digits) — MICR-perfect, the common case.
      2. Suffix alignment (system value has a routing prefix).
      3. Raw-text fallback — when the structured extractors
         left `front.cheque_no` blank, search for the DOM
         cheque digits inside the unstructured `front.raw_text`.
         A clean digit substring → PASS; a fuzzy near-miss
         (operator-recognisable but with OCR noise) → WARN.
         This is what catches the typical 'OCR read 378781 as
         37878l' case the user reported."""

    cheque_no_raw = (front.cheque_no if front else None) or ""
    dom_no_raw = (
        dom.get("Cheque No") or dom.get("Cheque No.")
        or dom.get("Cheque Number") or ""
    )

    cheque_digits = _digits_only(cheque_no_raw)
    dom_digits = _digits_only(str(dom_no_raw))
    raw_text = (front.raw_text if front else "") or ""

    evidence: list[tuple[str, Any]] = [
        ("cheque_no_on_cheque", cheque_no_raw),
        ("cheque_no_in_system", str(dom_no_raw)),
        ("cheque_no_on_cheque_digits", cheque_digits),
        ("cheque_no_in_system_digits", dom_digits),
    ]

    # VLM cross-check — answers a boolean ('cheque_no on image
    # matches expected_cheque_no'). When high-confidence we surface
    # it directly. The OCR cheque_no extractor is usually right on
    # this rule (MICR-derived), so VLM's role here is mainly to
    # rescue cases where MICR + body-text extraction both failed.
    vlm_match, vlm_conf = _vlm_field(
        front, "cheque_no_matches", "cheque_no_confidence",
    )
    if _vlm_payload(front):
        evidence.extend(
            _vlm_evidence_keys(
                "cheque_no_matches", "cheque_no_confidence",
                vlm_match, vlm_conf,
            )
        )
    elif front is not None:
        evidence.append(("vlm_agreement", "vlm_unavailable"))

    if not dom_digits:
        return CheckResult(
            check_id="cheque_no",
            label="Cheque Number Verification",
            status="NOT_VERIFIED",
            summary="No cheque number available in the system panel.",
            evidence=tuple(evidence),
        )

    # VLM short-circuit — only when OCR has NOT already produced a
    # structured cheque number. (When OCR has, the MICR-derived
    # read is more authoritative than the VLM and we let it
    # decide.) When VLM says match=true with high confidence and
    # OCR is empty, accept; when VLM says match=false with high
    # confidence, FAIL.
    if (
        vlm_match is not None
        and vlm_conf >= _VLM_TRUST_THRESHOLD
        and not cheque_digits
    ):
        if vlm_match is True:
            return CheckResult(
                check_id="cheque_no",
                label="Cheque Number Verification",
                status="PASS",
                summary=(
                    f"Local VLM confirmed cheque number on image "
                    f"matches system value {dom_digits} "
                    f"({int(vlm_conf * 100)}% confidence)."
                ),
                evidence=tuple(evidence),
            )
        if vlm_match is False:
            return CheckResult(
                check_id="cheque_no",
                label="Cheque Number Verification",
                status="FAIL",
                summary=(
                    f"Local VLM reports cheque number on image does "
                    f"NOT match system value {dom_digits} "
                    f"({int(vlm_conf * 100)}% confidence)."
                ),
                evidence=tuple(evidence),
            )

    # Stage 1: structured match (exact or suffix-aligned).
    if cheque_digits and cheque_digits == dom_digits:
        return CheckResult(
            check_id="cheque_no",
            label="Cheque Number Verification",
            status="PASS",
            summary=f"Cheque number {cheque_digits} matches the system value.",
            evidence=tuple(evidence),
        )
    if cheque_digits and (
        dom_digits.endswith(cheque_digits) or cheque_digits.endswith(dom_digits)
    ):
        return CheckResult(
            check_id="cheque_no",
            label="Cheque Number Verification",
            status="PASS",
            summary=(
                f"Cheque number {cheque_digits} matches the system "
                f"value {dom_digits} (suffix-aligned)."
            ),
            evidence=tuple(evidence),
        )

    # Stage 2: structured extractor returned a CONTRADICTING
    # number. Before FAILing, give the raw OCR text one more
    # chance — the structured `front.cheque_no` is itself an
    # OCR output and can disagree with what's literally printed
    # on the cheque (e.g. MICR-strip OCR misread a different
    # digit row, or the body-text extractor latched onto a
    # routing field number).
    #
    # We run the standard 4-tier `_search_dom_in_ocr` which
    # includes the new OCR-letter-tolerant tier (i/l/L→1, O→0,
    # S→5, …). The motivating production case: the printed
    # cheque number `143144` came through OCR as `143iL4`,
    # which digits-only stripped to `1434`; the structured
    # extractor happened to latch onto a different number
    # (`107274`) and the rule FAILed even though `143144` was
    # visibly on the cheque. With the tolerant search the raw
    # text rescue rule sees `143iL4 → 143144` and lets the
    # PASS verdict through with an `extractor_disagreed` note
    # so the operator understands why two views disagree.
    if cheque_digits:
        rescue_verdict, rescue_sim, rescue_kind = _search_dom_in_ocr(
            str(dom_no_raw), raw_text, numeric=True,
        )
        if rescue_verdict == "pass":
            evidence.append(("ocr_search_similarity", round(rescue_sim, 3)))
            evidence.append(("ocr_search_kind", rescue_kind))
            evidence.append((
                "extractor_disagreed",
                (
                    f"structured extractor returned {cheque_digits} but "
                    f"raw-text search located the system value via "
                    f"{rescue_kind} match"
                ),
            ))
            return CheckResult(
                check_id="cheque_no",
                label="Cheque Number Verification",
                status="PASS",
                summary=(
                    f"Cheque number {dom_digits} found in the OCR text "
                    f"({rescue_kind} match). Structured extractor "
                    f"returned {cheque_digits} — likely picked up a "
                    f"stray digit run; raw-text search is more "
                    f"reliable here."
                ),
                evidence=tuple(evidence),
            )
        # Raw text didn't rescue → trust the structured value as
        # before and FAIL.
        return CheckResult(
            check_id="cheque_no",
            label="Cheque Number Verification",
            status="FAIL",
            summary=(
                f"Cheque number on cheque ({cheque_digits}) does not "
                f"match the system value ({dom_digits})."
            ),
            evidence=tuple(evidence),
        )

    # Stage 3: structured extractor came back EMPTY. Fall back
    # to searching the raw OCR text for the system's cheque
    # digits — handles the common case where the MICR pipeline
    # missed but the body text contains the printed cheque
    # number with OCR noise.
    verdict, sim, kind = _search_dom_in_ocr(
        str(dom_no_raw), raw_text, numeric=True,
    )
    evidence.append(("ocr_search_similarity", round(sim, 3)))
    evidence.append(("ocr_search_kind", kind))
    if verdict == "no_ocr":
        return CheckResult(
            check_id="cheque_no",
            label="Cheque Number Verification",
            status="NOT_VERIFIED",
            summary=(
                "OCR couldn't extract a cheque number from the "
                "cheque (neither MICR nor body text matched)."
            ),
            evidence=tuple(evidence),
        )
    if verdict == "pass":
        return CheckResult(
            check_id="cheque_no",
            label="Cheque Number Verification",
            status="PASS",
            summary=(
                f"Cheque number {dom_digits} found in the OCR text "
                f"({kind} match)."
            ),
            details=(
                "Structured extractor didn't isolate a clean cheque "
                "number, but the raw OCR text contains the digits "
                "the bank's panel surfaced.",
            ),
            evidence=tuple(evidence),
        )
    if verdict == "warn":
        return CheckResult(
            check_id="cheque_no",
            label="Cheque Number Verification",
            status="WARN",
            summary=(
                f"Near-miss: cheque number {dom_digits} appears in "
                f"the OCR with noise ({int(sim * 100)}% similarity). "
                f"Operator should eyeball the printed number."
            ),
            evidence=tuple(evidence),
        )
    return CheckResult(
        check_id="cheque_no",
        label="Cheque Number Verification",
        status="FAIL",
        summary=(
            f"Cheque number {dom_digits} not found on the cheque "
            f"image (best OCR similarity {int(sim * 100)}%)."
        ),
        evidence=tuple(evidence),
    )


# ---------------------------------------------------------------------------
# Rule 5: Account Number Verification
# ---------------------------------------------------------------------------


def _rule_account_no(
    *,
    back: ChequeFields | None,
    front: ChequeFields | None,
    dom: dict[str, Any],
    back_flip_status: dict[str, Any] | None = None,
    **_kwargs: Any,
) -> CheckResult:
    """Rule 5 — account number ENDORSED ON THE BACK of the cheque
    (i.e. the depositor's account where the funds go) must equal
    the system 'Account No' column. Comparison is digit-only.

    IMPORTANT — the front of a cheque carries the DRAWER's printed
    A/C No (the account the funds come FROM), which is a different
    account than the system 'Account No'. Mixing the two creates
    false near-misses, so this rule deliberately does NOT use
    `front.account_no` as a structured value or as a high-priority
    raw-text source. The front's raw text is only scanned as a
    last-ditch sanity check (the depositor occasionally writes
    the receiving account on the front instead of/in addition to
    the back).

    Match ladder:
      1. Back-side structured `account_no` == DOM (or suffix-
         aligned) → PASS.
      2. Back-side structured `account_no` CONTRADICTS DOM → FAIL.
      3. Back-side raw-text search → PASS / WARN / FAIL.
      4. Back-side OCR empty → NOT_VERIFIED with operator hint
         to verify the back-image capture worked.
      5. Front-side raw-text scan ONLY as auxiliary evidence
         (never overrides a NOT_VERIFIED back result; only
         narrows the failure summary).
    """

    back_acct_raw = (back.account_no if back else None) or ""
    dom_acct_raw = (
        dom.get("Account No") or dom.get("Account No.")
        or dom.get("A/C No") or dom.get("A/C No.")
        or dom.get("A/c No") or ""
    )

    cheque_digits = _digits_only(back_acct_raw)
    dom_digits = _digits_only(str(dom_acct_raw))
    back_text = (back.raw_text if back else "") or ""
    front_text = (front.raw_text if front else "") or ""

    source_side = "back" if back and back.account_no else "none"
    # Flip-status diagnostic — surfaced to the operator so a back-
    # side FAIL isn't silently trusted when the back image is
    # actually a duplicate of the front (Alt+F1 didn't fire). The
    # capability layer hashes back-bytes vs front-bytes and stamps
    # the verdict here; we both record it as evidence and (below)
    # downgrade non-PASS verdicts to NOT_VERIFIED when the flip
    # clearly failed.
    flip_changed: bool | None = None
    if isinstance(back_flip_status, dict) and "changed" in back_flip_status:
        flip_changed = bool(back_flip_status["changed"])

    evidence: list[tuple[str, Any]] = [
        ("account_no_on_cheque_back", back_acct_raw),
        ("account_no_in_system", str(dom_acct_raw)),
        ("account_no_on_cheque_back_digits", cheque_digits),
        ("account_no_in_system_digits", dom_digits),
        ("source_side", source_side),
        ("back_image_captured", bool(back is not None)),
        ("back_raw_text_chars", len(back_text)),
        ("back_flip_changed",
         flip_changed if flip_changed is not None else "unknown"),
    ]
    if isinstance(back_flip_status, dict):
        if back_flip_status.get("requested"):
            evidence.append(
                ("back_flip_keystroke", str(back_flip_status["requested"]))
            )
        if back_flip_status.get("retries") is not None:
            evidence.append(
                ("back_flip_retries", int(back_flip_status["retries"]))
            )

    # VLM cross-check — note the VLM only sees the FRONT, so its
    # account_no answer is "is the expected number visible on the
    # front anywhere?". On most CTS cheques the drawer's account
    # number IS printed on the front (top-left A/C No band), and
    # operators often confirm via that band rather than the back
    # endorsement when the back capture is poor. So a VLM "yes"
    # here is meaningful even though the canonical spec checks
    # the back.
    vlm_acct, vlm_acct_conf = _vlm_field(
        front, "account_no_matches", "account_no_confidence",
    )
    if _vlm_payload(front):
        evidence.extend(
            _vlm_evidence_keys(
                "account_no_matches", "account_no_confidence",
                vlm_acct, vlm_acct_conf,
            )
        )
    elif front is not None:
        evidence.append(("vlm_agreement", "vlm_unavailable"))

    if not dom_digits:
        return CheckResult(
            check_id="account_no",
            label="Account Number Verification",
            status="NOT_VERIFIED",
            summary="No account number available in the system panel.",
            evidence=tuple(evidence),
        )

    # Flip-failed short-circuit — when we KNOW the back capture is
    # actually a duplicate of the front (Alt+F1 keystroke landed
    # somewhere other than the cheque viewer), no positive or
    # negative verdict on the back-side account number is honest.
    # Downgrade to NOT_VERIFIED with a clear operator-actionable
    # summary instead of FAILing the cheque for an account we
    # literally never saw. We still let the rule run when the back
    # OCR happens to produce a digit run that matches the DOM
    # (Stage 1 below): the front sometimes carries the same account
    # number in its A/C No band, and if the digits match exactly
    # the verdict is trustworthy regardless of which side we shot.
    if flip_changed is False and not (
        cheque_digits and (
            cheque_digits == dom_digits
            or dom_digits.endswith(cheque_digits)
            or cheque_digits.endswith(dom_digits)
        )
    ):
        return CheckResult(
            check_id="account_no",
            label="Account Number Verification",
            status="NOT_VERIFIED",
            summary=(
                "Back-side image was not captured — the Alt+F1 viewer "
                "flip did not change the on-screen image (back bytes are "
                "identical to front). Re-run with a working back flip "
                "before judging the account number."
            ),
            evidence=tuple(evidence),
        )

    # Stage 1: structured BACK-side match (exact or suffix-aligned).
    if cheque_digits and (
        cheque_digits == dom_digits
        or dom_digits.endswith(cheque_digits)
        or cheque_digits.endswith(dom_digits)
    ):
        return CheckResult(
            check_id="account_no",
            label="Account Number Verification",
            status="PASS",
            summary=f"Account number {cheque_digits} matches the system value.",
            evidence=tuple(evidence),
        )

    # Stage 2: structured BACK-side extractor returned a different
    # number. Before failing, give the back-side raw OCR text one
    # more chance — the extractor picks the LONGEST 9+-digit run,
    # and on backs that carry both a deposit-stamp account number
    # AND a longer transaction-reference line the picker is prone
    # to grabbing the wrong run. The OCR-tolerant raw-text search
    # (letter↔digit substitution variants) may still locate the
    # system value with high confidence.
    #
    # Production motivator: a cheque whose back stamp
    # `9999 11 88 11 88 18` came through OCR as `88 9994` (only
    # 6 of 14 digits readable) while a transaction reference line
    # `2306282614 3428500000110 380240002 24285 1 N` OCR'd cleanly
    # — the picker grabbed `12238024900224285` from the reference
    # and the rule FAILed. When OCR DOES recover the stamp text
    # under any substitution variant, the rescue lets the rule
    # PASS with a clear `extractor_disagreed` audit note instead
    # of failing on the wrong-region pickup.
    if cheque_digits:
        rescue_v, rescue_s, rescue_k = _search_dom_in_ocr(
            str(dom_acct_raw), back_text, numeric=True,
        )
        if rescue_v == "pass":
            evidence.append(("ocr_search_side", "back"))
            evidence.append(("ocr_search_similarity", round(rescue_s, 3)))
            evidence.append(("ocr_search_kind", rescue_k))
            evidence.append((
                "extractor_disagreed",
                (
                    f"structured extractor returned {cheque_digits} but "
                    f"raw-text search located the system value via "
                    f"{rescue_k} match on the back"
                ),
            ))
            return CheckResult(
                check_id="account_no",
                label="Account Number Verification",
                status="PASS",
                summary=(
                    f"Account number {dom_digits} found on the cheque "
                    f"back ({rescue_k} match). Structured extractor "
                    f"returned {cheque_digits} — likely picked up a "
                    f"transaction-reference line instead of the deposit "
                    f"stamp; raw-text search is more reliable here."
                ),
                evidence=tuple(evidence),
            )
        # Raw text didn't rescue with a full match — try the
        # partial-match WARN tier before falling all the way to
        # FAIL. When the OCR text contains a long contiguous
        # prefix/suffix of the DOM digits (e.g. 8 of 14), the
        # operator should eyeball the remaining digits rather
        # than see an opaque "wrong number" verdict.
        partial_len, partial_str = _longest_dom_run_in_ocr(
            dom_digits, back_text,
        )
        partial_threshold = max(6, int(len(dom_digits) * 0.6))
        if partial_len >= partial_threshold and partial_str:
            evidence.append(("back_partial_match_length", partial_len))
            evidence.append(("back_partial_match_digits", partial_str))
            evidence.append((
                "back_partial_match_coverage",
                f"{partial_len}/{len(dom_digits)} digits",
            ))
            position = (
                "prefix" if partial_str == dom_digits[:partial_len]
                else "suffix"
            )
            return CheckResult(
                check_id="account_no",
                label="Account Number Verification",
                status="WARN",
                summary=(
                    f"Structured extractor returned {cheque_digits} "
                    f"(a different number on the cheque), but "
                    f"{partial_len} of {len(dom_digits)} digits of "
                    f"the system account {dom_digits} ARE visible in "
                    f"the back OCR (the {position} '{partial_str}'). "
                    f"Operator should eyeball the stamp to confirm."
                ),
                evidence=tuple(evidence),
            )
        # Neither full nor partial rescue → trust the structured
        # value as before and FAIL.
        return CheckResult(
            check_id="account_no",
            label="Account Number Verification",
            status="FAIL",
            summary=(
                f"Account number on cheque back ({cheque_digits}) does "
                f"not match the system value ({dom_digits})."
            ),
            evidence=tuple(evidence),
        )

    # Stage 3: back-side raw-text scan.
    back_v, back_s, back_k = _search_dom_in_ocr(
        str(dom_acct_raw), back_text, numeric=True,
    )
    evidence.append(("ocr_search_side", "back"))
    evidence.append(("ocr_search_similarity", round(back_s, 3)))
    evidence.append(("ocr_search_kind", back_k))

    if back_v == "pass":
        return CheckResult(
            check_id="account_no",
            label="Account Number Verification",
            status="PASS",
            summary=(
                f"Account number {dom_digits} found on the cheque "
                f"back ({back_k} match)."
            ),
            evidence=tuple(evidence),
        )
    if back_v == "warn":
        # Annotate Stage 3's fuzzy WARN with the partial-match
        # evidence (when applicable) so operators see WHICH digits
        # were recognised, not just the aggregate similarity %.
        # The fuzzy match and the partial match are complementary
        # signals and operators benefit from seeing both.
        partial_len, partial_str = _longest_dom_run_in_ocr(
            dom_digits, back_text,
        )
        partial_threshold = max(6, int(len(dom_digits) * 0.6))
        if partial_len >= partial_threshold and partial_str:
            evidence.append(("back_partial_match_length", partial_len))
            evidence.append(("back_partial_match_digits", partial_str))
            evidence.append((
                "back_partial_match_coverage",
                f"{partial_len}/{len(dom_digits)} digits",
            ))
        return CheckResult(
            check_id="account_no",
            label="Account Number Verification",
            status="WARN",
            summary=(
                f"Near-miss: account number {dom_digits} appears on "
                f"the cheque back with noise ({int(back_s * 100)}% "
                f"similarity). Operator should eyeball the handwritten "
                f"endorsement."
            ),
            evidence=tuple(evidence),
        )

    # Stage 4: back-side OCR empty → NOT_VERIFIED. Also peek at
    # the front raw text as auxiliary evidence (does the number
    # AT LEAST appear somewhere on the front?), but don't change
    # the verdict — the spec says back is canonical, so we
    # surface the back-capture problem rather than papering over
    # it with a front match.
    if back_v == "no_ocr":
        front_v, front_s, front_k = _search_dom_in_ocr(
            str(dom_acct_raw), front_text, numeric=True,
        )
        evidence.append(("aux_front_search_similarity", round(front_s, 3)))
        evidence.append(("aux_front_search_kind", front_k))
        hint = (
            "OCR couldn't extract an account number from the cheque "
            "back. Verify the back-side image actually loaded (Alt+F1 "
            "should flip the viewer)."
        )
        if front_v in ("pass", "warn"):
            hint += (
                f" Auxiliary signal: the system account number was "
                f"located on the FRONT ({int(front_s * 100)}% sim) — "
                f"the depositor may have written it on the wrong side, "
                f"or the back failed to capture."
            )
        # VLM rescue — when the back image was unreadable but the
        # VLM confidently saw the expected account number on the
        # front, surface that as a WARN (not a clean PASS — the
        # spec wants the back, this is a front-derived signal that
        # the operator should confirm).
        if vlm_acct is True and vlm_acct_conf >= _VLM_TRUST_THRESHOLD:
            return CheckResult(
                check_id="account_no",
                label="Account Number Verification",
                status="WARN",
                summary=(
                    f"Back OCR couldn't read an account number, but "
                    f"the local VLM confirmed the expected account "
                    f"number {dom_digits} is visible on the FRONT "
                    f"({int(vlm_acct_conf * 100)}% confidence). "
                    f"Operator should re-capture the back to confirm "
                    f"the endorsement."
                ),
                evidence=tuple(evidence),
            )
        if vlm_acct is False and vlm_acct_conf >= _VLM_TRUST_THRESHOLD:
            return CheckResult(
                check_id="account_no",
                label="Account Number Verification",
                status="FAIL",
                summary=(
                    f"Local VLM reports the expected account number "
                    f"{dom_digits} is NOT visible on the cheque "
                    f"({int(vlm_acct_conf * 100)}% confidence); back "
                    f"OCR also returned empty."
                ),
                evidence=tuple(evidence),
            )
        return CheckResult(
            check_id="account_no",
            label="Account Number Verification",
            status="NOT_VERIFIED",
            summary=hint,
            evidence=tuple(evidence),
        )

    # Stage 4.5: PARTIAL-MATCH WARN — before failing, check whether
    # a significant CONTIGUOUS prefix or suffix of the DOM digits
    # is present in the back OCR (under any letter↔digit variant).
    # This rescues the very common production scenario where the
    # OCR captured the FIRST several digits of a stamped account
    # number cleanly but the rest are mangled — operator should
    # eyeball the few missing digits, not see an opaque FAIL.
    #
    # Threshold: at least 6 contiguous digits AND at least 60% of
    # the DOM digit count. Both must hold so we don't WARN on a
    # 4-digit accidental overlap with an 18-digit DOM.
    partial_len, partial_str = _longest_dom_run_in_ocr(dom_digits, back_text)
    partial_threshold = max(6, int(len(dom_digits) * 0.6))
    if partial_len >= partial_threshold and partial_str:
        evidence.append(("back_partial_match_length", partial_len))
        evidence.append(("back_partial_match_digits", partial_str))
        evidence.append((
            "back_partial_match_coverage",
            f"{partial_len}/{len(dom_digits)} digits",
        ))
        position = "prefix" if partial_str == dom_digits[:partial_len] else "suffix"
        return CheckResult(
            check_id="account_no",
            label="Account Number Verification",
            status="WARN",
            summary=(
                f"Partial match: {partial_len} of {len(dom_digits)} "
                f"digits of the system account number {dom_digits} "
                f"are visible on the cheque back (the "
                f"{position} '{partial_str}' was found in the OCR "
                f"text). Operator should eyeball the printed stamp "
                f"to confirm the remaining digits."
            ),
            details=(
                "OCR couldn't recognise the full account number "
                "cleanly enough for an automatic PASS, but the "
                "portion it DID read matches the system value — "
                "this is usually a faint or partially-stamped "
                "deposit endorsement, not a wrong account.",
            ),
            evidence=tuple(evidence),
        )

    # Stage 5: back read clearly, but the system account number
    # is nowhere on it → FAIL.
    return CheckResult(
        check_id="account_no",
        label="Account Number Verification",
        status="FAIL",
        summary=(
            f"Account number {dom_digits} not found on the cheque "
            f"back (best OCR similarity {int(back_s * 100)}%)."
        ),
        evidence=tuple(evidence),
    )


# ---------------------------------------------------------------------------
# Rule 6: Signature Verification (presence only)
# ---------------------------------------------------------------------------


def _rule_signature(
    *,
    front: ChequeFields | None,
    **_kwargs: Any,
) -> CheckResult:
    """Rule 6 — drawee signature must be present. We read the
    pre-computed signature verdict from `front.signature_verdict`
    (populated by `signature_detector.detect_signature` during
    OCR extraction)."""

    vlm_sig, vlm_sig_conf = _vlm_field(
        front, "signature_present", "signature_confidence",
    )

    if front is None:
        return CheckResult(
            check_id="signature",
            label="Signature Verification",
            status="NOT_VERIFIED",
            summary="No front-side capture available to check the signature on.",
        )

    if front.signature_missing_dep:
        # When the ink-density detector is unavailable (OpenCV
        # missing on the host), fall back to the VLM if it has a
        # confident answer. VLM-based signature detection is more
        # robust to scanner artefacts anyway — it understands
        # "this looks like a signature" beyond just "there's ink
        # here".
        if vlm_sig is not None and vlm_sig_conf >= _VLM_TRUST_THRESHOLD:
            status = "PASS" if vlm_sig else "FAIL"
            return CheckResult(
                check_id="signature",
                label="Signature Verification",
                status=status,
                summary=(
                    f"OpenCV signature detector unavailable; local "
                    f"VLM reports signature "
                    f"{'present' if vlm_sig else 'absent'} "
                    f"({int(vlm_sig_conf * 100)}% confidence)."
                ),
                evidence=(
                    ("signature_density", front.signature_density),
                    *_vlm_evidence_keys(
                        "signature_present", "signature_confidence",
                        vlm_sig, vlm_sig_conf, agree="vlm_primary",
                    ),
                ),
            )
        return CheckResult(
            check_id="signature",
            label="Signature Verification",
            status="NOT_VERIFIED",
            summary=(
                f"Signature detector unavailable: "
                f"{front.signature_missing_dep}"
            ),
            evidence=(
                ("signature_density", front.signature_density),
            ),
        )

    verdict = front.signature_verdict or "absent"
    density = front.signature_density
    base_evidence: list[tuple[str, Any]] = [
        ("signature_verdict", verdict),
        ("signature_density", round(density, 4)),
    ]
    if _vlm_payload(front):
        ocr_present = verdict == "present"
        if vlm_sig is None:
            agree = "ocr_only"
        elif vlm_sig == ocr_present:
            agree = "agree"
        else:
            agree = "disagree"
        base_evidence.extend(
            _vlm_evidence_keys(
                "signature_present", "signature_confidence",
                vlm_sig, vlm_sig_conf, agree=agree,
            )
        )
    elif front is not None:
        base_evidence.append(("vlm_agreement", "vlm_unavailable"))
    evidence = tuple(base_evidence)

    if verdict == "present":
        return CheckResult(
            check_id="signature",
            label="Signature Verification",
            status="PASS",
            summary=(
                f"Signature detected in the drawee panel "
                f"(ink density {density * 100:.2f}%)."
            ),
            evidence=evidence,
        )
    if verdict == "maybe":
        return CheckResult(
            check_id="signature",
            label="Signature Verification",
            status="WARN",
            summary=(
                f"Faint mark in the signature panel "
                f"(ink density {density * 100:.2f}%); operator "
                f"should eyeball the cheque to confirm."
            ),
            details=(
                "We saw a small amount of ink in the signature "
                "region but it was below the 'definitely signed' "
                "threshold. Could be a partial signature, a stamp "
                "outline, or scanner noise.",
            ),
            evidence=evidence,
        )
    return CheckResult(
        check_id="signature",
        label="Signature Verification",
        status="FAIL",
        summary=(
            f"No signature detected in the drawee panel "
            f"(ink density {density * 100:.2f}%)."
        ),
        evidence=evidence,
    )


__all__ = [
    "ChequeValidationReport",
    "CheckResult",
    "DEFAULT_FUTURE_TOLERANCE_DAYS",
    "DEFAULT_VALIDITY_DAYS",
    "validate_cheque",
]
