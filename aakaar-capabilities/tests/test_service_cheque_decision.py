"""Decision-table tests for aakar.services.cheque_decision.

The module under test is intentionally simple — pure function over
a `ChequeValidationReport`. These tests exercise EVERY cell in
the decision table from the module's docstring:

  * 6/6 PASS, ocr_health='ok'              → AUTO_APPROVE
  * 6/6 PASS, ocr_health='handwriting_fallback' → NEEDS_REVIEW
  * Any FAIL                               → AUTO_REJECT
  * Any WARN (no FAIL)                     → NEEDS_REVIEW
  * Any NOT_VERIFIED (no FAIL)             → NEEDS_REVIEW
  * Empty report / None                    → NEEDS_REVIEW
"""

from __future__ import annotations

from aakaar_caps.cheque.cheque_decision import decide
from aakaar_caps.cheque.cheque_validation import (
    CheckResult,
    ChequeValidationReport,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _check(
    check_id: str, status: str,
    *, summary: str = "", details: tuple[str, ...] = (),
    label: str | None = None,
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        label=label or check_id.replace("_", " ").title(),
        status=status,
        summary=summary or f"{check_id} {status}",
        details=details,
    )


def _report(
    checks: list[CheckResult],
    *,
    ocr_health: str = "ok",
) -> ChequeValidationReport:
    r = ChequeValidationReport(checks=checks, ocr_health=ocr_health)
    for c in checks:
        if c.status == "PASS":
            r.pass_count += 1
        elif c.status == "FAIL":
            r.fail_count += 1
        elif c.status == "WARN":
            r.warn_count += 1
        else:
            r.not_verified_count += 1
    return r


# ---------------------------------------------------------------------------
# Decision table
# ---------------------------------------------------------------------------


class TestAutoApprove:
    def test_all_pass_with_healthy_ocr_is_auto_approve(self) -> None:
        report = _report([
            _check("date", "PASS"),
            _check("payee", "PASS"),
            _check("amount", "PASS"),
            _check("cheque_no", "PASS"),
            _check("account_no", "PASS"),
            _check("signature", "PASS"),
        ])
        d = decide(report)
        assert d.status == "AUTO_APPROVE"
        assert "approve" in d.summary.lower()
        assert d.rejection_reason == ""
        assert d.failed_rule_ids == ()
        assert d.ocr_health == "ok"

    def test_all_pass_with_handwriting_fallback_downgrades_to_review(
        self,
    ) -> None:
        # Even when all rules pass, an active handwriting fallback
        # means the underlying OCR is noisier than nominal — push
        # to manual review so the operator eyeballs before approving.
        report = _report(
            [_check(c, "PASS") for c in
                ("date", "payee", "amount", "cheque_no",
                 "account_no", "signature")],
            ocr_health="handwriting_fallback",
        )
        d = decide(report)
        assert d.status == "NEEDS_REVIEW"
        assert "handwriting fallback" in d.summary.lower()


class TestAutoReject:
    def test_single_fail_is_auto_reject_with_reason(self) -> None:
        report = _report([
            _check("date", "PASS"),
            _check(
                "payee", "FAIL",
                summary="Cheque payee does not match any beneficiary.",
                details=("No name tokens located in OCR.",),
            ),
            _check("amount", "PASS"),
            _check("cheque_no", "PASS"),
            _check("account_no", "PASS"),
            _check("signature", "PASS"),
        ])
        d = decide(report)
        assert d.status == "AUTO_REJECT"
        assert "Payee" in d.summary
        assert "Payee" in d.rejection_reason
        assert "does not match" in d.rejection_reason.lower()
        assert d.failed_rule_ids == ("payee",)

    def test_multiple_fails_enumerated_in_reason(self) -> None:
        report = _report([
            _check("date", "PASS"),
            _check(
                "payee", "FAIL",
                summary="Payee mismatch",
                details=("Token score 0%.",),
            ),
            _check("amount", "PASS"),
            _check("cheque_no", "PASS"),
            _check(
                "account_no", "FAIL",
                summary="Account number not found on back.",
            ),
            _check("signature", "PASS"),
        ])
        d = decide(report)
        assert d.status == "AUTO_REJECT"
        assert "2 of 6" in d.summary
        # Reason mentions BOTH failing rules.
        assert "1. Payee" in d.rejection_reason
        assert "2. Account" in d.rejection_reason
        assert d.failed_rule_ids == ("payee", "account_no")

    def test_fail_takes_priority_over_warns_and_not_verifieds(
        self,
    ) -> None:
        report = _report([
            _check("date", "WARN"),
            _check("payee", "FAIL", summary="No match"),
            _check("amount", "NOT_VERIFIED"),
            _check("cheque_no", "PASS"),
            _check("account_no", "WARN"),
            _check("signature", "PASS"),
        ])
        d = decide(report)
        assert d.status == "AUTO_REJECT"
        # Warning/not-verified ids still surfaced for the UI to
        # cross-link.
        assert "date" in d.warning_rule_ids
        assert "account_no" in d.warning_rule_ids
        assert "amount" in d.not_verified_rule_ids


class TestNeedsReview:
    def test_any_warn_is_needs_review(self) -> None:
        report = _report([
            _check("date", "WARN", summary="Date within 7d future."),
            _check("payee", "PASS"),
            _check("amount", "PASS"),
            _check("cheque_no", "PASS"),
            _check("account_no", "PASS"),
            _check("signature", "PASS"),
        ])
        d = decide(report)
        assert d.status == "NEEDS_REVIEW"
        assert d.rejection_reason == ""
        assert "review" in d.summary.lower()
        assert d.warning_rule_ids == ("date",)

    def test_not_verified_without_fail_is_review(self) -> None:
        report = _report([
            _check("date", "NOT_VERIFIED"),
            _check("payee", "PASS"),
            _check("amount", "PASS"),
            _check("cheque_no", "PASS"),
            _check("account_no", "PASS"),
            _check("signature", "PASS"),
        ])
        d = decide(report)
        assert d.status == "NEEDS_REVIEW"
        assert d.not_verified_rule_ids == ("date",)

    def test_mixed_warns_summarised_inline(self) -> None:
        report = _report([
            _check("date", "WARN"),
            _check("payee", "WARN"),
            _check("amount", "PASS"),
            _check("cheque_no", "PASS"),
            _check("account_no", "PASS"),
            _check("signature", "PASS"),
        ])
        d = decide(report)
        assert d.status == "NEEDS_REVIEW"
        assert "2 near-miss" in d.summary

    def test_many_warns_collapse_to_count_only(self) -> None:
        report = _report([
            _check(c, "WARN") for c in
            ("date", "payee", "amount", "cheque_no",
             "account_no", "signature")
        ])
        d = decide(report)
        assert d.status == "NEEDS_REVIEW"
        assert "6 near-misses" in d.summary

    def test_empty_report_defaults_to_review(self) -> None:
        d = decide(None)
        assert d.status == "NEEDS_REVIEW"
        assert "manual review required" in d.summary.lower()

    def test_zero_checks_defaults_to_review(self) -> None:
        d = decide(ChequeValidationReport())
        assert d.status == "NEEDS_REVIEW"

    def test_serialises_to_dict(self) -> None:
        report = _report([
            _check("date", "PASS"),
            _check("payee", "WARN"),
            _check("amount", "PASS"),
            _check("cheque_no", "PASS"),
            _check("account_no", "PASS"),
            _check("signature", "PASS"),
        ])
        d = decide(report)
        as_dict = d.to_dict()
        assert as_dict["status"] == "NEEDS_REVIEW"
        assert isinstance(as_dict["warning_rule_ids"], list)
        assert as_dict["warning_rule_ids"] == ["payee"]
        assert as_dict["rejection_reason"] == ""
