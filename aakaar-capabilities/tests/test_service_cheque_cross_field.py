"""Unit tests for aakar.services.cheque_cross_field.

Covers all four cross-field validators (amount_words_vs_figures,
date_plausibility, payee_shape, micr_vs_printed_cheque_no) plus
the orchestrator (`run_all_cross_field_checks`) and the trust-
downgrade applicator (`apply_findings_to_consensus`).

Each validator gets:
  * The happy-path negative test (no finding when everything is OK)
  * The fail-path test (correct finding when things go wrong)
  * The edge cases (missing data, partial data, etc.)

Test fixtures use realistic OCR outputs — the actual SBI / IDIB /
HDFC cheque values from the user's bug reports — so any regression
on the original bug shows up as a test failure.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from aakaar_caps.cheque.cheque_consensus import (
    FieldConsensus,
    build_consensus,
    make_vote,
)
from aakaar_caps.cheque.cheque_cross_field import (
    CrossFieldFinding,
    _looks_like_payee,
    _parse_ddmmyyyy,
    apply_findings_to_consensus,
    check_amount_words_vs_figures,
    check_date_plausibility,
    check_micr_vs_printed_cheque_no,
    check_payee_shape,
    run_all_cross_field_checks,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consensus_from(votes_by_field: dict) -> tuple[FieldConsensus, ...]:
    """Build a consensus tuple by feeding raw vote dicts directly."""
    return build_consensus(votes_by_field)


def _fc(
    field_name: str,
    value: str | None,
    trust_score: float = 1.0,
    review_reason: str | None = None,
) -> FieldConsensus:
    """Build a synthetic FieldConsensus for tests that don't care
    about votes — only the resulting values."""
    from aakaar_caps.cheque.cheque_consensus import normalize_for_field
    return FieldConsensus(
        field_name=field_name,
        value=value,
        normalized_value=normalize_for_field(field_name, value),
        trust_score=trust_score,
        votes=(),
        winning_vote_count=1 if value else 0,
        review_reason=review_reason,
    )


# ---------------------------------------------------------------------------
# Rule 1: amount_words vs figures
# ---------------------------------------------------------------------------


class TestAmountWordsVsFigures:
    def test_match_returns_none(self) -> None:
        c = (
            _fc("amount", "50000"),
            _fc(
                "amount_words",
                "RUPEES FIFTY THOUSAND ONLY",
            ),
        )
        assert check_amount_words_vs_figures(c) is None

    def test_user_bug_words_say_3_figures_say_16388(self) -> None:
        """Regression: the exact user-reported case where the
        OCR picked up 'VALID FOR THREE MONTHS ONLY' as the
        amount-in-words (parsed=3) but the figures said 16388."""
        c = (
            _fc("amount", "16388"),
            _fc("amount_words", "VALID FOR THREE MONTHS ONLY"),
        )
        finding = check_amount_words_vs_figures(c)
        assert finding is not None
        assert finding.severity == "fail"
        assert finding.rule_id == "amount_words_vs_figures"
        assert "amount" in finding.affected_fields
        assert "amount_words" in finding.affected_fields
        assert "3" in finding.summary or "16388" in finding.summary

    def test_genuine_mismatch_is_fail(self) -> None:
        c = (
            _fc("amount", "50000"),
            _fc("amount_words", "ONE LAKH ONLY"),
        )
        finding = check_amount_words_vs_figures(c)
        assert finding is not None
        assert finding.severity == "fail"

    def test_only_words_present_is_warn(self) -> None:
        c = (
            _fc("amount", None),
            _fc("amount_words", "FIFTY THOUSAND ONLY"),
        )
        finding = check_amount_words_vs_figures(c)
        assert finding is not None
        assert finding.severity == "warn"
        # "amount" should be the missing field flagged
        assert "amount" in finding.affected_fields

    def test_only_figures_present_is_warn(self) -> None:
        c = (
            _fc("amount", "50000"),
            _fc("amount_words", None),
        )
        finding = check_amount_words_vs_figures(c)
        assert finding is not None
        assert finding.severity == "warn"
        assert "amount_words" in finding.affected_fields

    def test_both_absent_returns_none(self) -> None:
        c = (
            _fc("amount", None),
            _fc("amount_words", None),
        )
        assert check_amount_words_vs_figures(c) is None

    def test_words_unparseable_figures_present_is_warn(self) -> None:
        c = (
            _fc("amount", "50000"),
            _fc("amount_words", "asdfgh"),
        )
        finding = check_amount_words_vs_figures(c)
        assert finding is not None
        assert finding.severity == "warn"


# ---------------------------------------------------------------------------
# Rule 2: date plausibility
# ---------------------------------------------------------------------------


class TestDatePlausibility:
    _TODAY = _dt.date(2026, 6, 18)

    def test_current_date_no_finding(self) -> None:
        c = (_fc("date", "18062026"),)
        assert check_date_plausibility(c, today=self._TODAY) is None

    def test_recent_past_no_finding(self) -> None:
        # 30 days old — within the 90-day CTS window
        c = (_fc("date", "19052026"),)
        assert check_date_plausibility(c, today=self._TODAY) is None

    def test_stale_cheque_is_warn(self) -> None:
        # 100 days old — beyond the 90-day window
        c = (_fc("date", "10032026"),)
        finding = check_date_plausibility(c, today=self._TODAY)
        assert finding is not None
        assert finding.severity == "warn"
        assert "stale" in finding.summary.lower() or "validity" in finding.summary.lower()

    def test_far_future_date_is_warn(self) -> None:
        # 30 days in the future — beyond the 7-day post-dating grace
        c = (_fc("date", "18072026"),)
        finding = check_date_plausibility(c, today=self._TODAY)
        assert finding is not None
        assert finding.severity == "warn"
        assert "future" in finding.summary.lower()

    def test_small_post_dating_no_finding(self) -> None:
        # 5 days post-dated — within the grace window
        c = (_fc("date", "23062026"),)
        assert check_date_plausibility(c, today=self._TODAY) is None

    def test_invalid_gregorian_is_fail(self) -> None:
        # Feb 30, 2026 — impossible.
        # 30022026 won't pass strict ddmmyyyy parsing
        c = (_fc("date", "30022026"),)
        finding = check_date_plausibility(c, today=self._TODAY)
        assert finding is not None
        assert finding.severity == "fail"

    def test_no_date_returns_none(self) -> None:
        c = (_fc("date", None),)
        assert check_date_plausibility(c, today=self._TODAY) is None

    def test_validity_days_kwarg_respected(self) -> None:
        # 30 days old; with validity_days=20 should fire stale warn
        c = (_fc("date", "19052026"),)
        finding = check_date_plausibility(
            c, today=self._TODAY, validity_days=20,
        )
        assert finding is not None
        assert finding.severity == "warn"

    def test_parse_ddmmyyyy_helper(self) -> None:
        assert _parse_ddmmyyyy("21062026") == _dt.date(2026, 6, 21)
        assert _parse_ddmmyyyy("30022026") is None  # invalid Feb 30
        assert _parse_ddmmyyyy("123") is None
        assert _parse_ddmmyyyy(None) is None


# ---------------------------------------------------------------------------
# Rule 3: payee shape
# ---------------------------------------------------------------------------


class TestPayeeShape:
    def test_real_name_no_finding(self) -> None:
        c = (_fc("beneficiary", "JOHN DOE"),)
        assert check_payee_shape(c) is None

    def test_company_name_no_finding(self) -> None:
        c = (_fc("beneficiary", "SMARTWAY WELLNESS PVT LTD"),)
        assert check_payee_shape(c) is None

    def test_account_payee_template_is_fail(self) -> None:
        c = (_fc("beneficiary", "ACCOUNT PAYEE ONLY"),)
        finding = check_payee_shape(c)
        assert finding is not None
        assert finding.severity == "fail"
        assert "ACCOUNT PAYEE" in finding.summary

    def test_or_bearer_template_is_fail(self) -> None:
        c = (_fc("beneficiary", "OR BEARER"),)
        finding = check_payee_shape(c)
        assert finding is not None
        assert finding.severity == "fail"

    def test_all_digits_is_fail(self) -> None:
        c = (_fc("beneficiary", "12345"),)
        finding = check_payee_shape(c)
        assert finding is not None
        assert finding.severity == "fail"
        assert "alphabetic" in finding.summary.lower() or "numeric" in finding.summary.lower()

    def test_too_short_is_fail(self) -> None:
        c = (_fc("beneficiary", "A"),)
        finding = check_payee_shape(c)
        assert finding is not None

    def test_no_payee_returns_none(self) -> None:
        c = (_fc("beneficiary", None),)
        assert check_payee_shape(c) is None

    def test_looks_like_payee_helper_positive(self) -> None:
        assert _looks_like_payee("JOHN DOE")[0] is True
        assert _looks_like_payee("SMARTWAY WELLNESS PVT LTD")[0] is True
        assert _looks_like_payee("MR & MRS RAVI KUMAR")[0] is True

    def test_looks_like_payee_helper_negative(self) -> None:
        assert _looks_like_payee("")[0] is False
        assert _looks_like_payee("12345")[0] is False
        assert _looks_like_payee("ACCOUNT PAYEE")[0] is False
        assert _looks_like_payee("NOT NEGOTIABLE")[0] is False
        assert _looks_like_payee("OR BEARER")[0] is False


# ---------------------------------------------------------------------------
# Rule 4: MICR vs printed cheque_no
# ---------------------------------------------------------------------------


class TestMicrVsPrintedChequeNo:
    def test_agreement_returns_none(self) -> None:
        votes_by_field = {
            "cheque_no": [
                make_vote("micr_strip", "cheque_no", "123456", 0.95),
                make_vote("apple_vision", "cheque_no", "123456", 0.85),
            ],
        }
        c = _consensus_from(votes_by_field)
        assert check_micr_vs_printed_cheque_no(c) is None

    def test_disagreement_is_fail(self) -> None:
        votes_by_field = {
            "cheque_no": [
                make_vote("micr_strip", "cheque_no", "123456", 0.95),
                make_vote("apple_vision", "cheque_no", "789012", 0.85),
            ],
        }
        c = _consensus_from(votes_by_field)
        finding = check_micr_vs_printed_cheque_no(c)
        assert finding is not None
        assert finding.severity == "fail"
        assert finding.affected_fields == ("cheque_no",)
        assert "123456" in finding.summary
        assert "789012" in finding.summary

    def test_micr_only_returns_none(self) -> None:
        votes_by_field = {
            "cheque_no": [
                make_vote("micr_strip", "cheque_no", "123456", 0.95),
            ],
        }
        c = _consensus_from(votes_by_field)
        assert check_micr_vs_printed_cheque_no(c) is None

    def test_printed_only_returns_none(self) -> None:
        votes_by_field = {
            "cheque_no": [
                make_vote("apple_vision", "cheque_no", "123456", 0.85),
            ],
        }
        c = _consensus_from(votes_by_field)
        assert check_micr_vs_printed_cheque_no(c) is None

    def test_tail_match_returns_none(self) -> None:
        """When MICR carries a longer string but the last 6 match,
        treat as agreement (the printed corner often shows just
        the serial tail)."""
        votes_by_field = {
            "cheque_no": [
                # MICR strip: full 10-digit cheque-serial
                make_vote("micr_strip", "cheque_no", "0000123456", 0.95),
                # Printed corner: just the last 6
                make_vote("apple_vision", "cheque_no", "123456", 0.85),
            ],
        }
        c = _consensus_from(votes_by_field)
        assert check_micr_vs_printed_cheque_no(c) is None

    def test_no_votes_returns_none(self) -> None:
        c = (_fc("cheque_no", None),)
        assert check_micr_vs_printed_cheque_no(c) is None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class TestRunAllCrossFieldChecks:
    _TODAY = _dt.date(2026, 6, 18)

    def test_clean_cheque_no_findings(self) -> None:
        votes_by_field = {
            "beneficiary": [
                make_vote("apple_vision", "beneficiary", "JOHN DOE", 0.9),
                make_vote("doctr", "beneficiary", "JOHN DOE", 0.85),
            ],
            "amount": [
                make_vote("apple_vision", "amount", "50000", 0.9),
                make_vote("doctr", "amount", "50000", 0.85),
            ],
            "amount_words": [
                make_vote(
                    "apple_vision", "amount_words",
                    "RUPEES FIFTY THOUSAND ONLY", 0.9,
                ),
            ],
            "date": [
                make_vote("apple_vision", "date", "18062026", 0.9),
            ],
            "cheque_no": [
                make_vote("micr_strip", "cheque_no", "123456", 0.95),
                make_vote("apple_vision", "cheque_no", "123456", 0.85),
            ],
        }
        c = _consensus_from(votes_by_field)
        findings = run_all_cross_field_checks(c, today=self._TODAY)
        assert findings == ()

    def test_multiple_findings_fire_independently(self) -> None:
        """The amount mismatch AND the boilerplate payee should
        both fire on the user's reported regression."""
        votes_by_field = {
            "beneficiary": [
                make_vote(
                    "apple_vision", "beneficiary",
                    "ACCOUNT PAYEE ONLY", 0.85,
                ),
            ],
            "amount": [
                make_vote("apple_vision", "amount", "16388", 0.9),
            ],
            "amount_words": [
                make_vote(
                    "apple_vision", "amount_words",
                    "VALID FOR THREE MONTHS ONLY", 0.7,
                ),
            ],
        }
        c = _consensus_from(votes_by_field)
        findings = run_all_cross_field_checks(c, today=self._TODAY)
        rule_ids = {f.rule_id for f in findings}
        assert "payee_shape" in rule_ids
        assert "amount_words_vs_figures" in rule_ids


# ---------------------------------------------------------------------------
# Trust-downgrade applicator
# ---------------------------------------------------------------------------


class TestApplyFindingsToConsensus:
    def test_no_findings_returns_unchanged(self) -> None:
        c = (
            _fc("amount", "50000", trust_score=1.0),
            _fc("date", "18062026", trust_score=0.9),
        )
        out = apply_findings_to_consensus(c, [])
        assert out == c

    def test_fail_finding_downgrades_trust(self) -> None:
        c = (_fc("amount", "16388", trust_score=1.0),)
        findings = [
            CrossFieldFinding(
                rule_id="amount_words_vs_figures",
                severity="fail",
                summary="words say 3 but figures say 16388",
                affected_fields=("amount",),
            ),
        ]
        out = apply_findings_to_consensus(c, findings)
        assert out[0].trust_score == 0.25  # fail factor

    def test_warn_finding_partial_downgrade(self) -> None:
        c = (_fc("date", "10032026", trust_score=1.0),)
        findings = [
            CrossFieldFinding(
                rule_id="date_plausibility",
                severity="warn",
                summary="stale",
                affected_fields=("date",),
            ),
        ]
        out = apply_findings_to_consensus(c, findings)
        assert out[0].trust_score == 0.60

    def test_multiple_findings_pick_lowest_factor(self) -> None:
        c = (_fc("amount", "16388", trust_score=1.0),)
        findings = [
            CrossFieldFinding(
                rule_id="warn_rule", severity="warn",
                summary="warn", affected_fields=("amount",),
            ),
            CrossFieldFinding(
                rule_id="fail_rule", severity="fail",
                summary="fail", affected_fields=("amount",),
            ),
        ]
        out = apply_findings_to_consensus(c, findings)
        # The FAIL factor (0.25) wins over the warn factor (0.60)
        assert out[0].trust_score == 0.25

    def test_review_reason_combined(self) -> None:
        c = (
            _fc("amount", "16388",
                trust_score=1.0, review_reason="existing reason"),
        )
        findings = [
            CrossFieldFinding(
                rule_id="amount_words_vs_figures",
                severity="fail",
                summary="words say 3 but figures say 16388",
                affected_fields=("amount",),
            ),
        ]
        out = apply_findings_to_consensus(c, findings)
        assert "existing reason" in out[0].review_reason
        assert "amount_words_vs_figures" in out[0].review_reason
        assert "16388" in out[0].review_reason

    def test_unaffected_fields_untouched(self) -> None:
        c = (
            _fc("amount", "16388", trust_score=0.9),
            _fc("date", "18062026", trust_score=0.85),
        )
        findings = [
            CrossFieldFinding(
                rule_id="amount_words_vs_figures",
                severity="fail", summary="mismatch",
                affected_fields=("amount",),
            ),
        ]
        out = apply_findings_to_consensus(c, findings)
        # Date is unaffected
        assert out[1].trust_score == 0.85
        # Amount is downgraded
        assert out[0].trust_score == 0.225  # 0.9 * 0.25


# ---------------------------------------------------------------------------
# CrossFieldFinding.to_dict
# ---------------------------------------------------------------------------


class TestCrossFieldFindingToDict:
    def test_round_trip(self) -> None:
        f = CrossFieldFinding(
            rule_id="amount_words_vs_figures",
            severity="fail",
            summary="words say 3 but figures say 16388",
            affected_fields=("amount", "amount_words"),
            detail={"words_parsed": "3", "figures_parsed": "16388"},
        )
        d = f.to_dict()
        assert d["rule_id"] == "amount_words_vs_figures"
        assert d["severity"] == "fail"
        assert d["affected_fields"] == ["amount", "amount_words"]
        assert d["detail"]["figures_parsed"] == "16388"


# ---------------------------------------------------------------------------
# End-to-end check via extract_fields stubbed engines
# ---------------------------------------------------------------------------


def test_cross_field_findings_attached_to_chequefields(monkeypatch) -> None:
    """Smoke test: after extract_fields runs on a fixture where
    the words/figures DISAGREE, the returned ChequeFields must
    carry both the consensus and a non-empty
    cross_field_findings tuple."""
    # This test builds a REAL ChequeFields via extract_fields, whose front-side
    # path decodes the image (numpy/PIL) and runs the signature detector (cv2).
    # Skip cleanly when the optional `cheque` OCR deps are absent.
    pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from aakaar_caps.cheque import cheque_ocr  # noqa: PLC0415

    class _StubRegion:
        def __init__(self, text: str, conf: float) -> None:
            self.text = text
            self.confidence = conf
            self.bbox: list = []

    # RapidOCR returns text where words say "THREE MONTHS" but figures
    # say "16388" — should fire amount_words_vs_figures.
    stub_regions = [
        _StubRegion("PAY HEMA RAM OR BEARER", 0.9),
        _StubRegion("RUPEES VALID FOR THREE MONTHS ONLY", 0.85),
        _StubRegion("Rs. 16,388", 0.92),
        _StubRegion("18-06-2026", 0.88),
    ]

    monkeypatch.setattr(
        "aakaar_caps.cheque.rapid_ocr.run_ocr_detail",
        lambda png: stub_regions,
    )
    monkeypatch.setattr(
        "aakaar_caps.cheque.rapid_ocr.missing_dep", lambda: None,
    )
    # Skip the MICR pass (it would re-run real OCR on the fake bytes).
    monkeypatch.setattr(
        "aakaar_caps.cheque.micr.run_micr_ocr",
        lambda _p, **_k: __import__(
            "aakaar_caps.cheque.micr", fromlist=["MicrResult"],
        ).MicrResult(text="", parsed={}, regions=(), variants_tried=()),
    )

    out = cheque_ocr.extract_fields(b"fake-png-bytes", side="front")
    assert out.consensus, "consensus tuple must be populated"
    assert out.cross_field_findings, (
        "cross_field_findings tuple must be populated when words "
        "and figures disagree"
    )
    rule_ids = {f.rule_id for f in out.cross_field_findings}
    assert "amount_words_vs_figures" in rule_ids


@pytest.mark.parametrize(
    "words,figures,expected_severity",
    [
        ("FIFTY THOUSAND", "50000", None),
        ("ONE LAKH", "100000", None),
        ("FIFTY THOUSAND", "60000", "fail"),
        ("VALID FOR THREE MONTHS ONLY", "16388", "fail"),
        ("FIFTY THOUSAND", None, "warn"),
        (None, "50000", "warn"),
    ],
)
def test_amount_words_vs_figures_table(
    words, figures, expected_severity,
) -> None:
    c = (
        _fc("amount", figures),
        _fc("amount_words", words),
    )
    finding = check_amount_words_vs_figures(c)
    if expected_severity is None:
        assert finding is None
    else:
        assert finding is not None
        assert finding.severity == expected_severity
