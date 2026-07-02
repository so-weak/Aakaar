"""Cheque accept/reject/review decision policy.

Converts a `ChequeValidationReport` (the per-spec rule outcomes
from `cheque_validation.validate_cheque`) into a single operator-
facing recommendation with a pre-filled rejection reason when the
recommendation is REJECT.

Three terminal states:

  * `AUTO_APPROVE` — every rule PASSed. UI shows a green
    "Auto-approve" badge with a one-click confirm button.
  * `AUTO_REJECT` — at least one rule FAILed. UI shows a red
    "Auto-reject" badge with the failing-rule summary pre-filled
    as the rejection reason (operator can edit before
    submitting).
  * `NEEDS_REVIEW` — at least one rule WARN or NOT_VERIFIED (and no
    FAILs), OR the OCR pipeline itself reported a degradation
    (handwriting backend down, weak OCR text). UI shows an amber
    "Review required" badge — operator inspects the image, picks
    Approve or Reject explicitly.

The operator can ALWAYS override the recommendation via the
`POST /api/cts/cheques/{cheque_id}/decide` endpoint — this module
just produces the DEFAULT verdict the UI surfaces and the audit
log records as the system's suggestion.

Design contract:
  * Pure function — no side effects, no globals, no I/O.
  * Total function — never raises. Defensive against missing
    fields on the input report (we don't trust upstream to
    always populate everything when a partial pipeline state
    reaches us).
  * Deterministic — same input always yields the same decision.
  * Reason-traceable — the rejection reason cites the SPECIFIC
    failing rules (label + summary) so the operator immediately
    sees WHY the decision was REJECT.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from aakaar_caps.cheque.cheque_validation import (
    CheckResult,
    ChequeValidationReport,
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


ChequeDecisionStatus = Literal["AUTO_APPROVE", "AUTO_REJECT", "NEEDS_REVIEW"]


@dataclass(frozen=True, slots=True)
class ChequeDecision:
    """The policy's recommendation for one cheque.

    `status` is the headline verdict the UI badges.

    `summary` is a single human sentence the operator reads at
    a glance (e.g. "All 6 checks passed — safe to approve."
    or "Payee Name Verification FAILed — see reason below.").

    `rejection_reason` is pre-filled when status == AUTO_REJECT
    and empty otherwise. Constructed from the FAIL rules'
    `summary` text so it's directly actionable without needing
    the operator to re-read the per-rule panel. Operators can
    edit before submitting via the decide endpoint.

    `failed_rule_ids` / `warning_rule_ids` / `not_verified_rule_ids`
    let the UI cross-link the badge back to the specific rule
    cards that drove the verdict.
    """

    status: ChequeDecisionStatus
    summary: str
    rejection_reason: str = ""
    failed_rule_ids: tuple[str, ...] = field(default_factory=tuple)
    warning_rule_ids: tuple[str, ...] = field(default_factory=tuple)
    not_verified_rule_ids: tuple[str, ...] = field(default_factory=tuple)
    # OCR pipeline health snapshot at decision time — surfaces
    # "we recommended review because the OCR fallback engine was
    # in use" without requiring the UI to cross-reference the
    # validation report.
    ocr_health: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "rejection_reason": self.rejection_reason,
            "failed_rule_ids": list(self.failed_rule_ids),
            "warning_rule_ids": list(self.warning_rule_ids),
            "not_verified_rule_ids": list(self.not_verified_rule_ids),
            "ocr_health": self.ocr_health,
        }


# ---------------------------------------------------------------------------
# Decision helpers
# ---------------------------------------------------------------------------


def _format_rejection_reason(fail_rules: list[CheckResult]) -> str:
    """Build the pre-filled rejection reason from one or more
    failing rules. Format is intentionally enumerable so the
    operator can scan it line-by-line and trim the bits they
    don't want before submitting:

        Auto-rejected by 2 rule(s):
          1. Payee Name Verification: Cheque payee does not match...
          2. Account Number Verification: Account number ... not found...

    When a rule has a non-empty `details` tuple we append the
    first detail line — the rules' design contract is that
    `details[0]` is the most-actionable sentence (the "why" not
    just the "what").
    """
    if not fail_rules:
        return ""
    if len(fail_rules) == 1:
        r = fail_rules[0]
        body = r.summary or "(no summary)"
        if r.details:
            body = f"{body} ({r.details[0]})"
        return f"Auto-rejected: {r.label}: {body}"
    lines = [f"Auto-rejected by {len(fail_rules)} rule(s):"]
    for i, r in enumerate(fail_rules, start=1):
        body = r.summary or "(no summary)"
        if r.details:
            body = f"{body} ({r.details[0]})"
        lines.append(f"  {i}. {r.label}: {body}")
    return "\n".join(lines)


def _format_review_summary(
    warn_rules: list[CheckResult],
    not_verified_rules: list[CheckResult],
    ocr_health: str,
) -> str:
    """Build the one-sentence review banner. Mentions:
      * The count + labels of WARN rules (the operator's primary
        eyeball targets)
      * The count + labels of NOT_VERIFIED rules
      * The OCR pipeline state when it's not OK (e.g.
        'handwriting_fallback' → the fallback engine was used,
        accuracy is lower than nominal)

    Kept short — when 4+ rules are flagged the summary just gives
    counts; the operator clicks into the validation panel for
    detail.
    """
    parts: list[str] = []
    if warn_rules:
        if len(warn_rules) <= 3:
            names = ", ".join(r.label for r in warn_rules)
            parts.append(f"{len(warn_rules)} near-miss ({names})")
        else:
            parts.append(f"{len(warn_rules)} near-misses")
    if not_verified_rules:
        if len(not_verified_rules) <= 3:
            names = ", ".join(r.label for r in not_verified_rules)
            parts.append(f"{len(not_verified_rules)} not-verified ({names})")
        else:
            parts.append(f"{len(not_verified_rules)} not-verified")
    if ocr_health and ocr_health not in ("ok", "handwriting_fallback"):
        parts.append(f"OCR pipeline degraded ({ocr_health})")
    elif ocr_health == "handwriting_fallback":
        parts.append("handwriting fallback engine in use")

    if not parts:
        return "Manual review recommended."
    return "Manual review required: " + "; ".join(parts) + "."


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def decide(report: ChequeValidationReport | None) -> ChequeDecision:
    """Compute the recommendation for one cheque from its
    validation report.

    Decision table (in priority order — first match wins):

      1. report is None / has no checks → NEEDS_REVIEW (defensive;
         a missing report is not a free pass).
      2. ANY rule status == "FAIL"      → AUTO_REJECT with the
         failing rule summaries pre-filled as rejection_reason.
      3. ALL rule statuses == "PASS" AND ocr_health == "ok"
                                        → AUTO_APPROVE.
      4. Anything else                  → NEEDS_REVIEW.

    Rule 3's `ocr_health == "ok"` guard is intentional: when the
    handwriting fallback is active (`handwriting_fallback`) or any
    other OCR degradation is present, even an all-PASS report
    should land in the manual queue so the operator eyeballs the
    cheque against the OCR-fallback-driven evidence rather than
    auto-approving on potentially-noisy reads.
    """
    if report is None or not report.checks:
        return ChequeDecision(
            status="NEEDS_REVIEW",
            summary=(
                "No validation report available — manual review required "
                "to confirm cheque is valid before approval."
            ),
            ocr_health="ok",
        )

    fail_rules = [c for c in report.checks if c.status == "FAIL"]
    warn_rules = [c for c in report.checks if c.status == "WARN"]
    not_verified_rules = [
        c for c in report.checks if c.status == "NOT_VERIFIED"
    ]
    pass_rules = [c for c in report.checks if c.status == "PASS"]

    ocr_health = report.ocr_health or "ok"

    # 1. Any FAIL → AUTO_REJECT.
    if fail_rules:
        reason = _format_rejection_reason(fail_rules)
        if len(fail_rules) == 1:
            r = fail_rules[0]
            summary = f"{r.label} FAILed — safe to reject."
        else:
            summary = (
                f"{len(fail_rules)} of {len(report.checks)} rules "
                f"FAILed — safe to reject."
            )
        return ChequeDecision(
            status="AUTO_REJECT",
            summary=summary,
            rejection_reason=reason,
            failed_rule_ids=tuple(c.check_id for c in fail_rules),
            warning_rule_ids=tuple(c.check_id for c in warn_rules),
            not_verified_rule_ids=tuple(c.check_id for c in not_verified_rules),
            ocr_health=ocr_health,
        )

    # 2. All PASS AND OCR fully healthy → AUTO_APPROVE.
    if (
        not warn_rules
        and not not_verified_rules
        and len(pass_rules) == len(report.checks)
        and ocr_health == "ok"
    ):
        return ChequeDecision(
            status="AUTO_APPROVE",
            summary=(
                f"All {len(pass_rules)} checks passed — safe to approve."
            ),
            ocr_health=ocr_health,
        )

    # 3. Everything else → NEEDS_REVIEW.
    return ChequeDecision(
        status="NEEDS_REVIEW",
        summary=_format_review_summary(
            warn_rules, not_verified_rules, ocr_health,
        ),
        warning_rule_ids=tuple(c.check_id for c in warn_rules),
        not_verified_rule_ids=tuple(c.check_id for c in not_verified_rules),
        ocr_health=ocr_health,
    )
