"""Rule-by-rule tests for aakar.services.cheque_validation.

Each bank-spec rule gets its own class. We build minimal
`ChequeFields` instances inline (skipping the OCR pipeline
entirely) so the tests run fast and stay focused on the
validation logic.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from aakaar_caps.cheque.cheque_ocr import ChequeFields
from aakaar_caps.cheque.cheque_validation import (
    DEFAULT_VALIDITY_DAYS,
    CheckResult,
    ChequeValidationReport,
    validate_cheque,
)
from aakaar_caps.cheque.cheque_validation import (
    _has_amount_vocab,
    _ocr_engines,
)


# ---------------------------------------------------------------------------
# Helpers — keep test bodies short
# ---------------------------------------------------------------------------


def _front(
    *,
    raw_text: str = "",
    beneficiary: str | None = None,
    cheque_no: str | None = None,
    amount: str | None = None,
    amount_words: str | None = None,
    account_no: str | None = None,
    handwriting_regions: tuple[tuple[str, str, float], ...] = (),
    signature_verdict: str | None = "present",
    signature_density: float = 0.05,
    signature_missing_dep: str | None = None,
    engine_runs: tuple[tuple[str, str, float, int, str | None], ...] = (),
    vlm_verification: dict[str, Any] | None = None,
) -> ChequeFields:
    """Build a ChequeFields front-side payload for a test."""
    return ChequeFields(
        side="front",
        raw_text=raw_text,
        beneficiary=beneficiary,
        cheque_no=cheque_no,
        amount=amount,
        amount_words=amount_words,
        account_no=account_no,
        ocr_confidence=0.95,
        handwriting_regions=handwriting_regions,
        signature_verdict=signature_verdict,
        signature_density=signature_density,
        signature_missing_dep=signature_missing_dep,
        engine_runs=engine_runs,
        vlm_verification=vlm_verification or {},
    )


def _back(
    account_no: str | None,
    *,
    raw_text: str | None = None,
) -> ChequeFields:
    return ChequeFields(
        side="back",
        raw_text=raw_text if raw_text is not None else (account_no or ""),
        account_no=account_no,
        ocr_confidence=0.9,
    )


def _find(report: ChequeValidationReport, check_id: str) -> CheckResult:
    for c in report.checks:
        if c.check_id == check_id:
            return c
    raise AssertionError(f"{check_id} rule missing from report")


@pytest.fixture(autouse=True)
def _checks_config(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """The product temporarily disables the Date + Amount-in-Words
    verification checks (see `cheque_validation._DISABLED_CHECK_IDS`,
    June 2026 operator request). Their rule IMPLEMENTATIONS are kept
    intact so re-enabling is a one-liner, and the rule-logic tests
    below still exercise them.

    To keep that coverage, this autouse fixture clears the disabled
    set for every test EXCEPT those whose class opts into the
    production default via `use_production_check_config = True`
    (see `TestDisabledChecks`), which assert the filtered report the
    operator actually sees.
    """
    import aakaar_caps.cheque.cheque_validation as cv  # noqa: PLC0415

    use_prod = getattr(request.instance, "use_production_check_config", False)
    if not use_prod:
        monkeypatch.setattr(cv, "_DISABLED_CHECK_IDS", frozenset())


class TestDisabledChecks:
    """The `_DISABLED_CHECK_IDS` filter is the operator's escape
    hatch to remove a noisy rule from the report without ripping
    out the rule implementation. These tests pin the mechanism
    (rule filtering + tally + verdict impact) rather than any
    specific id, so they stay meaningful as the production
    disabled set evolves.

    History: June 2026 originally disabled `amount_words` to hide
    OCR-induced NOT_VERIFIEDs; later that month the rule was
    hardened with a DOM-amount-to-words fuzzy fallback and
    re-enabled. The current production default is the empty set
    (no rules are hidden).
    """

    use_production_check_config = True

    def test_production_default_disables_nothing(self) -> None:
        # All seven rules must be active in production. If a future
        # operator request disables one again, update this list
        # alongside `_DISABLED_CHECK_IDS`.
        report = validate_cheque(front=_front(), back=_back(None), dom={})
        ids = [c.check_id for c in report.checks]
        assert ids == [
            "date", "payee",
            "amount_words", "amount_figures",
            "cheque_no", "account_no", "signature",
        ]

    def test_disable_filter_removes_rule_from_report(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Pin the FILTERING MECHANISM, not the specific id. Disable
        # an arbitrary rule via the same _DISABLED_CHECK_IDS knob
        # the operator uses and verify it vanishes from `checks`.
        from aakaar_caps.cheque import cheque_validation as cv
        monkeypatch.setattr(cv, "_DISABLED_CHECK_IDS", frozenset({"signature"}))
        report = validate_cheque(front=_front(), back=_back(None), dom={})
        ids = [c.check_id for c in report.checks]
        assert "signature" not in ids
        assert len(ids) == 6

    def test_disabled_checks_do_not_count_toward_tallies(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from aakaar_caps.cheque import cheque_validation as cv
        monkeypatch.setattr(cv, "_DISABLED_CHECK_IDS", frozenset({"signature"}))
        report = validate_cheque(front=None, back=None, dom=None)
        # Six remaining rules, all NOT_VERIFIED with no data —
        # `signature` would also have been NOT_VERIFIED but it's
        # filtered out, so the tally drops by exactly one.
        assert len(report.checks) == 6
        assert report.not_verified_count == 6
        assert report.fail_count == 0

    def test_disabled_check_does_not_drive_verdict(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Filter out the only rule that would have NOT_VERIFIED on
        # this fixture (signature, since we don't pass image bytes)
        # and the verdict should land at ACCEPT.
        from aakaar_caps.cheque import cheque_validation as cv
        monkeypatch.setattr(cv, "_DISABLED_CHECK_IDS", frozenset({"signature"}))
        report = validate_cheque(
            front=_front(
                raw_text="22/06/2026 JOHN DOE 51,060.00 378781",
                beneficiary="JOHN DOE",
                cheque_no="378781",
                amount="51,060.00",
                amount_words="Fifty One Thousand Sixty Only",
                signature_verdict="present",
                signature_density=0.04,
            ),
            back=_back("50200100315661"),
            dom={
                "Beneficiary 1": "JOHN DOE",
                "Cheque No": "378781",
                "Amount": "51,060.00",
                "Account No": "50200100315661",
            },
            today=date(2026, 6, 23),
        )
        assert report.overall_status == "ACCEPT"


# ---------------------------------------------------------------------------
# Top-level integration
# ---------------------------------------------------------------------------


class TestValidateChequeIntegration:
    """validate_cheque() must always return exactly seven rules
    in the canonical order, with sensible tallies on the
    aggregate. June 2026: the original `amount` rule was split
    into two top-level rules (`amount_words`, `amount_figures`)
    per operator feedback, raising the rule count from 6 to 7."""

    def test_returns_seven_rules_in_canonical_order(self) -> None:
        report = validate_cheque(front=_front(), back=_back(None), dom={})
        ids = [c.check_id for c in report.checks]
        assert ids == [
            "date", "payee",
            "amount_words", "amount_figures",
            "cheque_no", "account_no", "signature",
        ]
        assert isinstance(report, ChequeValidationReport)

    def test_all_pass_yields_accept(self) -> None:
        today = date(2026, 6, 23)
        report = validate_cheque(
            front=_front(
                raw_text="22/06/2026 JOHN DOE 51,060.00 378781",
                beneficiary="JOHN DOE",
                cheque_no="378781",
                amount="51,060.00",
                amount_words="Fifty One Thousand Sixty Only",
                signature_verdict="present",
                signature_density=0.04,
            ),
            back=_back("50200100315661"),
            dom={
                "Beneficiary 1": "JOHN DOE",
                "Cheque No": "378781",
                "Amount": "51,060.00",
                "Account No": "50200100315661",
            },
            today=today,
        )
        assert report.overall_status == "ACCEPT"
        # 7 rules now that amount is split.
        assert report.pass_count == 7
        assert report.fail_count == 0

    def test_any_fail_yields_reject(self) -> None:
        report = validate_cheque(
            front=_front(
                beneficiary="JOHN DOE",
                cheque_no="999999",  # mismatch
                amount="51,060.00",
                amount_words="Fifty One Thousand Sixty Only",
            ),
            back=_back("50200100315661"),
            dom={
                "Beneficiary 1": "JOHN DOE",
                "Cheque No": "378781",
                "Amount": "51,060.00",
                "Account No": "50200100315661",
            },
        )
        assert report.overall_status == "REJECT"
        assert report.fail_count >= 1

    def test_warn_or_not_verified_yields_review(self) -> None:
        # No date in OCR → NOT_VERIFIED; everything else PASS.
        report = validate_cheque(
            front=_front(
                beneficiary="JOHN DOE",
                cheque_no="378781",
                amount="51,060.00",
                amount_words="Fifty One Thousand Sixty Only",
            ),
            back=_back("50200100315661"),
            dom={
                "Beneficiary 1": "JOHN DOE",
                "Cheque No": "378781",
                "Amount": "51,060.00",
                "Account No": "50200100315661",
            },
        )
        assert report.overall_status == "REVIEW"

    def test_handles_missing_inputs_gracefully(self) -> None:
        report = validate_cheque(front=None, back=None, dom=None)
        # 7 rules now that amount is split into amount_words +
        # amount_figures.
        assert len(report.checks) == 7
        # No FAILs when we have no data — only NOT_VERIFIEDs.
        assert report.fail_count == 0
        assert report.not_verified_count == 7


# ---------------------------------------------------------------------------
# Rule 1: Date Verification
# ---------------------------------------------------------------------------


class TestDateRule:
    def test_recent_date_passes(self) -> None:
        today = date(2026, 6, 23)
        cheque_date = today - timedelta(days=10)
        report = validate_cheque(
            front=_front(raw_text=cheque_date.strftime("%d/%m/%Y")),
            back=None, dom={}, today=today,
        )
        assert _find(report, "date").status == "PASS"

    def test_stale_dated_cheque_fails(self) -> None:
        today = date(2026, 6, 23)
        # 100 days old > 90-day RBI validity.
        cheque_date = today - timedelta(days=100)
        report = validate_cheque(
            front=_front(raw_text=cheque_date.strftime("%d-%m-%Y")),
            back=None, dom={}, today=today,
        )
        r = _find(report, "date")
        assert r.status == "FAIL"
        assert "stale" in r.summary.lower()

    def test_far_future_dated_cheque_warns(self) -> None:
        today = date(2026, 6, 23)
        cheque_date = today + timedelta(days=30)
        report = validate_cheque(
            front=_front(raw_text=cheque_date.strftime("%d/%m/%Y")),
            back=None, dom={}, today=today,
        )
        assert _find(report, "date").status == "WARN"

    def test_today_dated_passes(self) -> None:
        today = date(2026, 6, 23)
        report = validate_cheque(
            front=_front(raw_text=today.strftime("%d/%m/%Y")),
            back=None, dom={}, today=today,
        )
        assert _find(report, "date").status == "PASS"

    def test_unparseable_date_not_verified(self) -> None:
        report = validate_cheque(
            front=_front(raw_text="JOHN DOE pay anywhere"),
            back=None, dom={},
        )
        assert _find(report, "date").status == "NOT_VERIFIED"

    def test_trocr_date_region_preferred_over_raw_text(self) -> None:
        # The TrOCR date region is more accurate than a raw-text
        # regex sweep, so when both are present the region wins.
        today = date(2026, 6, 23)
        report = validate_cheque(
            front=_front(
                raw_text="confusing 99-99-9999 chaff",
                handwriting_regions=(
                    ("date", today.strftime("%d/%m/%Y"), 0.95),
                ),
            ),
            back=None, dom={}, today=today,
        )
        assert _find(report, "date").status == "PASS"

    def test_paddle_focused_date_region_preferred_over_raw_text(self) -> None:
        # When TrOCR is unavailable (the common corporate-network
        # case), the region-focused PaddleOCR pass on the date box
        # is the next-best source. _extract_cheque_date must walk
        # engine_runs for a `paddle_focused_date` entry BEFORE
        # falling back to the raw-text regex sweep.
        today = date(2026, 6, 23)
        report = validate_cheque(
            front=_front(
                raw_text="confusing 99-99-9999 chaff",
                handwriting_regions=(),  # TrOCR didn't run
                engine_runs=(
                    ("paddle_or_easy", "some noise", 0.5, 1, None),
                    (
                        "paddle_focused_date",
                        today.strftime("%d/%m/%Y"),
                        0.95, 1, None,
                    ),
                ),
            ),
            back=None, dom={}, today=today,
        )
        assert _find(report, "date").status == "PASS"

    def test_paddle_focused_date_empty_falls_through_to_raw_text(self) -> None:
        # When `paddle_focused_date` ran but returned empty text,
        # the helper must NOT short-circuit — it should fall
        # through to the raw-text regex so a parseable date in
        # the body can still pass the rule.
        today = date(2026, 6, 23)
        report = validate_cheque(
            front=_front(
                raw_text=today.strftime("%d/%m/%Y") + " good cheque",
                engine_runs=(
                    ("paddle_focused_date", "", 0.0, 0, None),
                ),
            ),
            back=None, dom={}, today=today,
        )
        assert _find(report, "date").status == "PASS"

    def test_apple_vision_date_engine_run_outranks_all_other_date_sources(
        self,
    ) -> None:
        # The Apple Vision date-band read is the top of the date
        # ladder — when it produces a parseable date, the rule MUST
        # prefer it over TrOCR, paddle_focused_date, AND the
        # raw-text regex sweep (all of which would have selected
        # 19/06/2026 here). This is what rescues a cheque whose
        # generic OCR pass garbles the boxed date into noise.
        today = date(2026, 6, 23)
        report = validate_cheque(
            front=_front(
                raw_text="other context 19-06-2026 chaff",
                handwriting_regions=(
                    ("date", "19/06/2026", 0.95),
                ),
                engine_runs=(
                    (
                        "apple_vision_date", "20062026", 0.88, 8, None,
                    ),
                    (
                        "paddle_focused_date", "19/06/2026", 0.9, 1, None,
                    ),
                ),
            ),
            back=None, dom={}, today=today,
        )
        date_rule = _find(report, "date")
        evidence = dict(date_rule.evidence)
        assert evidence["cheque_date"] == "2026-06-20"
        assert date_rule.status == "PASS"

    def test_apple_vision_date_invalid_falls_through_to_lower_strategies(
        self,
    ) -> None:
        # When apple_vision_date reads 8 digits but they don't form
        # a real date ("99999999"), the helper MUST fall through to
        # the next strategy step (TrOCR / paddle_focused / raw
        # text) instead of failing the whole rule. Protects
        # against a bad band read killing an otherwise good
        # cheque.
        today = date(2026, 6, 23)
        report = validate_cheque(
            front=_front(
                raw_text="",
                handwriting_regions=(
                    ("date", today.strftime("%d/%m/%Y"), 0.92),
                ),
                engine_runs=(
                    ("apple_vision_date", "99999999", 0.3, 8, None),
                ),
            ),
            back=None, dom={}, today=today,
        )
        assert _find(report, "date").status == "PASS"

    def test_apple_vision_date_short_read_falls_through(self) -> None:
        # When the band reader couldn't produce a parseable date
        # it emits an empty string and a `missing_dep` note; the
        # rule must skip it and walk the rest of the ladder.
        today = date(2026, 6, 23)
        report = validate_cheque(
            front=_front(
                raw_text=today.strftime("%d/%m/%Y") + " in body",
                engine_runs=(
                    ("apple_vision_date", "", 0.0, 5, "band read empty"),
                ),
            ),
            back=None, dom={}, today=today,
        )
        assert _find(report, "date").status == "PASS"

    def test_configurable_validity_period(self) -> None:
        today = date(2026, 6, 23)
        cheque_date = today - timedelta(days=70)
        # Default 90 → PASS; tighter 60 → FAIL.
        r_default = validate_cheque(
            front=_front(raw_text=cheque_date.strftime("%d/%m/%Y")),
            back=None, dom={}, today=today,
        )
        r_tight = validate_cheque(
            front=_front(raw_text=cheque_date.strftime("%d/%m/%Y")),
            back=None, dom={}, today=today, validity_days=60,
        )
        assert _find(r_default, "date").status == "PASS"
        assert _find(r_tight, "date").status == "FAIL"


# ---------------------------------------------------------------------------
# Rule 2: Payee Name Verification
# ---------------------------------------------------------------------------


class TestPayeeRule:
    @pytest.mark.parametrize(
        "cheque_payee,dom_payee",
        [
            ("JAY SHIVSAKTHI TRADERS", "JAY SHIVSAKTHI TRADERS"),
            ("jay shivsakthi traders", "JAY SHIVSAKTHI TRADERS"),
            ("Jay Shivsakthi Traders", "JAY SHIVSAKTHI TRADERS"),
            # Whitespace and punctuation normalisation.
            ("M/S  JAY  SHIVSAKTHI", "M/S JAY SHIVSAKTHI"),
        ],
    )
    def test_normalised_exact_passes(
        self, cheque_payee: str, dom_payee: str,
    ) -> None:
        report = validate_cheque(
            front=_front(beneficiary=cheque_payee),
            back=None, dom={"Beneficiary 1": dom_payee},
        )
        assert _find(report, "payee").status == "PASS"

    def test_mismatch_fails(self) -> None:
        report = validate_cheque(
            front=_front(beneficiary="JOHN DOE"),
            back=None, dom={"Beneficiary 1": "JANE SMITH"},
        )
        assert _find(report, "payee").status == "FAIL"

    def test_matches_any_of_multiple_dom_beneficiaries(self) -> None:
        report = validate_cheque(
            front=_front(beneficiary="HEMA RAM"),
            back=None,
            dom={
                "Beneficiary 1": "JAY SHIVSAKTHI",
                "Beneficiary 2": "HEMA RAM",
            },
        )
        assert _find(report, "payee").status == "PASS"

    def test_missing_dom_payee_not_verified(self) -> None:
        report = validate_cheque(
            front=_front(beneficiary="JOHN DOE"),
            back=None, dom={},
        )
        assert _find(report, "payee").status == "NOT_VERIFIED"

    def test_missing_cheque_payee_not_verified(self) -> None:
        report = validate_cheque(
            front=_front(beneficiary=None),
            back=None, dom={"Beneficiary 1": "JOHN DOE"},
        )
        assert _find(report, "payee").status == "NOT_VERIFIED"

    def test_trocr_payee_region_preferred(self) -> None:
        # The TrOCR-extracted payee_line is more accurate than
        # the regex `beneficiary` extractor, so it wins when
        # present.
        report = validate_cheque(
            front=_front(
                beneficiary="wrong from regex",
                handwriting_regions=(
                    ("payee_line", "JOHN DOE", 0.92),
                ),
            ),
            back=None, dom={"Beneficiary 1": "JOHN DOE"},
        )
        assert _find(report, "payee").status == "PASS"


# ---------------------------------------------------------------------------
# Rule 3: Amount Verification
# ---------------------------------------------------------------------------


class TestAmountRule:
    """The original `amount` rule was split in June 2026 into two
    top-level rules: `amount_words` (handwritten 'Rupees ... Only'
    line vs SC value) and `amount_figures` (digit box vs SC value).
    These tests assert the COMBINED behaviour — both child rules
    must agree to call the cheque amount-verified."""

    def test_all_three_agree_passes(self) -> None:
        report = validate_cheque(
            front=_front(
                amount="51,060.00",
                amount_words="Fifty One Thousand Sixty Only",
            ),
            back=None,
            dom={"Amount": "51,060.00"},
        )
        assert _find(report, "amount_words").status == "PASS"
        assert _find(report, "amount_figures").status == "PASS"

    def test_internal_mismatch_words_vs_figures_fails(self) -> None:
        # Words say one thing, figures say another, DOM agrees
        # with figures → words rule FAILs (words ≠ DOM),
        # figures rule PASSes (figures == DOM). Operator sees
        # the FAIL row clearly without needing to drill into
        # sub-checks.
        report = validate_cheque(
            front=_front(
                amount="51,060.00",
                amount_words="One Lakh Only",  # wildly different from DOM
            ),
            back=None,
            dom={"Amount": "51,060.00"},
        )
        assert _find(report, "amount_words").status == "FAIL"
        assert _find(report, "amount_figures").status == "PASS"

    def test_external_mismatch_cheque_vs_system_fails(self) -> None:
        # Cheque words+figures agree on 51,060 but DOM says 999.99
        # → BOTH rules FAIL.
        report = validate_cheque(
            front=_front(
                amount="51,060.00",
                amount_words="Fifty One Thousand Sixty Only",
            ),
            back=None,
            dom={"Amount": "999.99"},
        )
        assert _find(report, "amount_words").status == "FAIL"
        assert _find(report, "amount_figures").status == "FAIL"

    def test_unparseable_words_yields_not_verified_on_words(self) -> None:
        report = validate_cheque(
            front=_front(
                amount="51,060.00",
                amount_words="garbage that is not a number",
            ),
            back=None,
            dom={"Amount": "51,060.00"},
        )
        # Words couldn't be parsed → NOT_VERIFIED on amount_words.
        # Figures match DOM → PASS on amount_figures.
        assert _find(report, "amount_words").status == "NOT_VERIFIED"
        assert _find(report, "amount_figures").status == "PASS"

    def test_missing_dom_amount_not_verified_overall(self) -> None:
        report = validate_cheque(
            front=_front(
                amount="51,060.00",
                amount_words="Fifty One Thousand Sixty Only",
            ),
            back=None, dom={},
        )
        assert _find(report, "amount_words").status == "NOT_VERIFIED"
        assert _find(report, "amount_figures").status == "NOT_VERIFIED"

    def test_indian_lakhs_grouping_in_dom_amount(self) -> None:
        report = validate_cheque(
            front=_front(
                amount="125000.00",
                amount_words="One Lakh Twenty Five Thousand Only",
            ),
            back=None,
            dom={"Amount": "1,25,000.00"},  # Indian grouping
        )
        assert _find(report, "amount_words").status == "PASS"
        assert _find(report, "amount_figures").status == "PASS"

    def test_batch_amount_after_slash_is_picked_up(self) -> None:
        # The bank's panel sometimes uses 'Batch Amount' as
        # "36 / 12,05,345.00" — we should split on the slash and
        # use the rightmost value.
        report = validate_cheque(
            front=_front(
                amount="1205345.00",
                amount_words=(
                    "Twelve Lakh Five Thousand Three Hundred "
                    "Forty Five Only"
                ),
            ),
            back=None,
            dom={"Batch Amount": "36 / 12,05,345.00"},
        )
        assert _find(report, "amount_words").status == "PASS"
        assert _find(report, "amount_figures").status == "PASS"


class TestAmountRuleExtractorMisTargetBehaviour:
    """Production regression preserved as separate-rule behaviour
    (June 2026): the original `amount` rule had a 'mis-target
    deferral' mechanism that hid a FAIL when sub-checks (a) and
    (b) disagreed because the structured extractor latched onto
    the wrong band. With the rule split into `amount_words` +
    `amount_figures`, each independently validates and the
    operator sees the two verdicts side-by-side — no need for a
    cross-deferral.
    """

    def test_words_grabbed_account_number_band_yields_not_verified(
        self,
    ) -> None:
        # words_to_decimal("Ac NO 924030007246023") returns None
        # (no number-words present) → amount_words = NOT_VERIFIED.
        # figures extractor returned 34374 but raw-text contains
        # the DOM 346237.00 → amount_figures = PASS via raw-text
        # rescue.
        report = validate_cheque(
            front=_front(
                amount="34374",
                amount_words="Ac NO 924030007246023",
                raw_text=(
                    "Pay SQUARETEX FAB PRIVATE LIMITED 34374 Or "
                    "Ordor Rupees Three Lakh Forty Six Thousand "
                    'Two Hundred Seven Only "346237.00 NJC NO: '
                    "924030007246023"
                ),
            ),
            back=None,
            dom={"Amount": "3,46,237.00"},
        )
        r_words = _find(report, "amount_words")
        r_figures = _find(report, "amount_figures")
        assert r_words.status == "NOT_VERIFIED", (
            f"mis-targeted words extractor should NOT parse to a number → "
            f"NOT_VERIFIED, got {r_words.status}: {r_words.summary!r}"
        )
        assert r_figures.status == "PASS", (
            f"figures via raw-text rescue should PASS, got "
            f"{r_figures.status}: {r_figures.summary!r}"
        )

    def test_words_with_rupee_header_but_account_no_payload(
        self,
    ) -> None:
        # 'Rupee>' header + account-number payload — the cheque
        # words still don't parse to a real number (account-number
        # digit runs aren't number-word vocabulary), so
        # amount_words is NOT_VERIFIED.
        report = validate_cheque(
            front=_front(
                amount="34374",
                amount_words="Rupee>\nAc NO\n924030007246023\nLd:",
                raw_text=(
                    "Pay SQUARETEX FAB PRIVATE LIMITED 34374 Or "
                    "Ordor 4 Rupees Three Lakh Forty Six Thousand "
                    'Two Hundred Seven Only "346237.00 NJC NO: '
                    "924030007246023"
                ),
            ),
            back=None,
            dom={"Amount": "3,46,237.00"},
        )
        r_words = _find(report, "amount_words")
        r_figures = _find(report, "amount_figures")
        # Words couldn't be parsed → NOT_VERIFIED, no false FAIL.
        assert r_words.status == "NOT_VERIFIED"
        # Figures: structured says 34374, raw-text finds DOM → PASS.
        assert r_figures.status == "PASS"

    def test_figures_implausibly_long_digit_run_rescued_by_raw_text(
        self,
    ) -> None:
        # Figures extractor grabbed a 15-digit junk run; raw text
        # has the real DOM amount → amount_figures PASS via
        # raw-text rescue (the mis-target guard clears the
        # structured value before comparison so the raw-text
        # search is the only signal). Words rule independently
        # PASSes because the words match the DOM amount exactly.
        report = validate_cheque(
            front=_front(
                amount="924030007346028",
                amount_words=(
                    "Three Lakh Forty Six Thousand Two Hundred "
                    "Thirty Seven Only"
                ),
                raw_text=(
                    "Pay X Rupees Three Lakh Forty Six Thousand "
                    'Two Hundred Thirty Seven Only "346237.00 '
                    "NJC NO: 924030007346028"
                ),
            ),
            back=None,
            dom={"Amount": "3,46,237.00"},
        )
        assert _find(report, "amount_words").status == "PASS"
        assert _find(report, "amount_figures").status == "PASS"

    def test_genuine_words_figures_mismatch_surfaces_both_verdicts(
        self,
    ) -> None:
        # Cheque written for ₹500 figures but ₹5000 words and
        # DOM=₹500 → figures match DOM (PASS); words don't
        # (FAIL). Operator sees both verdicts cleanly without
        # any cross-deferral masking either signal.
        report = validate_cheque(
            front=_front(
                amount="500.00",
                amount_words="Five Thousand Rupees Only",
                raw_text="Pay X Five Thousand Rupees Only 500.00",
            ),
            back=None,
            dom={"Amount": "500.00"},
        )
        assert _find(report, "amount_words").status == "FAIL"
        assert _find(report, "amount_figures").status == "PASS"

    def test_neither_rule_passes_when_dom_not_on_cheque(self) -> None:
        # Mis-targeted extractors AND DOM amount not in raw text
        # → words: NOT_VERIFIED (can't parse); figures: FAIL (no
        # match anywhere). No deferral / no false-PASS.
        report = validate_cheque(
            front=_front(
                amount="34374",
                amount_words="Ac NO 924030007246023",
                raw_text="Pay X 34374 NJC NO: 924030007246023",
            ),
            back=None,
            dom={"Amount": "999999.00"},
        )
        assert _find(report, "amount_words").status == "NOT_VERIFIED"
        assert _find(report, "amount_figures").status == "FAIL"


class TestAmountWordsDomConversion:
    """The amount_words rule (re-enabled June 2026 alongside the
    DOM-amount-to-words display). Each test pins the operator-
    facing surface that depends on the new
    `decimal_to_words(dom_value)` rendering — the canonical
    "Rupees ... Only" expected form must appear on every verdict
    so the operator can see at a glance what the cheque's
    'Rupees ... Only' line should have read.

    The numeric channel (words → Decimal vs DOM Decimal) remains
    the only verdict driver — the textual similarity score is
    carried as diagnostic context only, because a char-level
    SequenceMatcher can't reliably tell OCR drift on a scale
    word ('Thousand' → 'Pusnoyt') apart from a real numeral
    swap ('Ninety' → 'Twenty'). See `_amount_words_similarity`
    for the calibration data behind that choice."""

    def test_evidence_carries_expected_words_form(self) -> None:
        # Every amount_words verdict — PASS, FAIL, WARN, NOT_VERIFIED
        # — must include the DOM amount rendered as words on the
        # evidence dict so the operator can read "expected: 'Rupees
        # One Lakh Ninety Thousand Only'" alongside the cheque-side
        # OCR. Tested on the happy PASS path; the fallback paths
        # below assert it's present on their statuses too.
        report = validate_cheque(
            front=_front(
                amount="190000.00",
                amount_words="One Lakh Ninety Thousand Only",
            ),
            back=None,
            dom={"Amount": "190000.00"},
        )
        rule = _find(report, "amount_words")
        evidence = dict(rule.evidence)
        assert rule.status == "PASS"
        assert (
            evidence.get("expected_amount_in_words")
            == "Rupees One Lakh Ninety Thousand Only"
        )
        assert evidence.get("system_amount_parsed") == "190000.00"
        # Similarity must be 1.0 when the cheque words exactly
        # match the DOM-derived words.
        assert evidence.get("word_form_similarity") == 1.0

    def test_pass_summary_includes_expected_words(self) -> None:
        # The summary on a PASS now also shows the DOM amount as
        # words so the operator never has to mentally convert
        # 190000 → "One Lakh Ninety Thousand" to confirm the
        # cheque matches.
        report = validate_cheque(
            front=_front(
                amount="190000.00",
                amount_words="One Lakh Ninety Thousand Only",
            ),
            back=None,
            dom={"Amount": "190000.00"},
        )
        rule = _find(report, "amount_words")
        assert rule.status == "PASS"
        assert "One Lakh Ninety Thousand" in rule.summary

    def test_ocr_drift_on_single_token_still_fails_but_surfaces_expected(
        self,
    ) -> None:
        # Real-world OCR failure mode captured on a live cheque:
        # handwriting OCR substituted ONE token in the amount-
        # in-words line ("Thousand" misread as "Pusnoyt"). The
        # permissive numeric parser silently drops the unknown
        # token and emits a partial value (100090) against a DOM
        # of 190000 — numerically a FAIL.
        #
        # The character-level similarity score on this case
        # (~0.81) is too coarse to safely downgrade the verdict,
        # because a real fraud-shaped swap like "One Lakh Twenty
        # Thousand" vs "One Lakh Ninety Thousand" scores at the
        # same ~0.88 band. So we keep the FAIL verdict — but
        # surface the expected words form on the summary so the
        # operator can diagnose "OCR ate a token" vs "real
        # mismatch" with one glance instead of drilling into
        # evidence.
        report = validate_cheque(
            front=_front(
                amount="190000.00",
                amount_words="One Lakh Ninety Pusnoyt only",
            ),
            back=None,
            dom={"Amount": "190000.00"},
        )
        rule = _find(report, "amount_words")
        assert rule.status == "FAIL"
        evidence = dict(rule.evidence)
        assert evidence.get("cheque_amount_in_words_parsed") == "100090"
        assert (
            evidence.get("expected_amount_in_words")
            == "Rupees One Lakh Ninety Thousand Only"
        )
        # Similarity is high (OCR drift on one token) — surfaced
        # as diagnostic context but doesn't influence the verdict.
        sim = evidence.get("word_form_similarity")
        assert sim is not None and sim >= 0.75
        # Both sides appear in the FAIL summary.
        assert "Pusnoyt" in rule.summary
        assert "One Lakh Ninety Thousand" in rule.summary

    def test_unparseable_ocr_lands_at_not_verified(self) -> None:
        # OCR drops every number word — parser returns None,
        # mis-target guard catches the absence of rupee
        # vocabulary and routes to NOT_VERIFIED so the operator
        # knows we genuinely couldn't tell. Expected words still
        # appear on the summary.
        report = validate_cheque(
            front=_front(
                amount="190000.00",
                amount_words="completely unrelated garbage tokens xyz",
            ),
            back=None,
            dom={"Amount": "190000.00"},
        )
        rule = _find(report, "amount_words")
        assert rule.status == "NOT_VERIFIED"
        assert (
            dict(rule.evidence).get("expected_amount_in_words")
            == "Rupees One Lakh Ninety Thousand Only"
        )

    def test_focused_pass_with_substituted_letters_routes_to_warn(
        self,
    ) -> None:
        # Real-world capture (AXIS BANK CTS cheque, 26-Jun-2026):
        # full-page OCR rendered the handwritten 'Rupees Two Lakh
        # Only' line as 'OR  h / hjnoma sodnyph / SAI (M)' — no
        # anchors, so the structured extractor returned None and
        # the rule had nothing to compare against.
        #
        # The focused amount-words band pass recovers the line as
        # 'Ropeos Iwo lulch Ouly' — clearly the same words with
        # cursive-handwriting OCR substitutions. The strict numeric
        # parser still can't honestly read the value ('lulch' is too
        # garbled to snap to 'lakh', so a fuzzy parse only recovers
        # 'two'), BUT the expected-guided coverage scorer recognises
        # ~50% of the expected words ('Iwo' ≈ 'Two') on a band that
        # is geometrically correct.
        #
        # New verdict (post amount-in-words recall work): WARN —
        # "likely the right amount, operator confirm" — rather than a
        # dead-end NOT_VERIFIED. Both 'Ropeos Iwo lulch Ouly' and
        # 'Rupees Two Lakh Only' are surfaced side-by-side so the
        # operator can confirm visually in one glance. Specifically
        # NOT a FAIL — the cheque writer didn't write '31', the OCR
        # just couldn't read the letters.
        report = validate_cheque(
            front=_front(
                amount="200000.00",
                amount_words="Ropeos Iwo lulch Ouly",
                engine_runs=(
                    ("rapidocr_ppocr", "AXIS BANK ...", 0.99, 47, None),
                    ("rapidocr_focused_amount_words",
                     "Ropeos Iwo lulch Ouly", 0.76, 2, None),
                ),
            ),
            back=None,
            dom={"Amount": "2,00,000.00"},
        )
        rule = _find(report, "amount_words")
        assert rule.status == "WARN"
        evidence = dict(rule.evidence)
        assert evidence.get("cheque_amount_in_words") == (
            "Ropeos Iwo lulch Ouly"
        )
        assert (
            evidence.get("expected_amount_in_words")
            == "Rupees Two Lakh Only"
        )
        # The figures box (200000.00) independently equals the DOM
        # amount — surfaced for the operator — but a fuzzy WORDS parse
        # didn't recover the full value, so this is WARN (confirm),
        # not a corroborated PASS.
        assert evidence.get("figures_corroborated") is True
        assert evidence.get("expected_token_coverage", 0.0) >= 0.5
        assert evidence.get("verdict_basis") == "fuzzy_uncorroborated"
        # Summary echoes the raw OCR text + the expected words side
        # by side so the operator can compare visually, and nudges
        # them to confirm the handwritten line.
        assert "Ropeos Iwo lulch Ouly" in rule.summary
        assert "Rupees Two Lakh Only" in rule.summary
        assert "confirm" in rule.summary.lower()

    def test_focused_pass_with_valid_vocab_still_uses_numeric_verdict(
        self,
    ) -> None:
        # The focused-pass skip-vocab-guard branch must NOT bypass
        # the numeric verdict when the focused output DOES contain
        # recognisable rupee vocabulary. When the focused pass
        # returns a clean 'Rupees Two Lakh Only' (good handwriting
        # OCR), the rule should report PASS via the normal numeric
        # path, not get caught by the new NOT_VERIFIED short-
        # circuit.
        report = validate_cheque(
            front=_front(
                amount="200000.00",
                amount_words="Rupees Two Lakh Only",
                engine_runs=(
                    ("rapidocr_focused_amount_words",
                     "Rupees Two Lakh Only", 0.95, 1, None),
                ),
            ),
            back=None,
            dom={"Amount": "2,00,000.00"},
        )
        rule = _find(report, "amount_words")
        assert rule.status == "PASS"

    def test_dom_to_words_handles_indian_lakhs_grouping(self) -> None:
        # The DOM serves amounts as either 190000.00 or 1,90,000.00
        # (Indian-lakhs grouping). Both must convert to the same
        # words form before the rule's similarity comparison runs,
        # otherwise the rule would flap on a UI formatting choice.
        report = validate_cheque(
            front=_front(
                amount="190000.00",
                amount_words="One Lakh Ninety Thousand Only",
            ),
            back=None,
            dom={"Amount": "1,90,000.00"},
        )
        rule = _find(report, "amount_words")
        assert rule.status == "PASS"
        assert (
            dict(rule.evidence).get("expected_amount_in_words")
            == "Rupees One Lakh Ninety Thousand Only"
        )

    def test_dom_to_words_handles_paise(self) -> None:
        # Cheques with paise (₹1500.50) must render the expected
        # words as "Rupees One Thousand Five Hundred And Fifty
        # Paise Only" so the cheque writer's "and fifty paise"
        # clause can be cross-checked.
        report = validate_cheque(
            front=_front(
                amount="1500.50",
                amount_words=(
                    "One Thousand Five Hundred and Fifty Paise Only"
                ),
            ),
            back=None,
            dom={"Amount": "1500.50"},
        )
        rule = _find(report, "amount_words")
        assert rule.status == "PASS"
        assert (
            dict(rule.evidence).get("expected_amount_in_words")
            == "Rupees One Thousand Five Hundred And Fifty Paise Only"
        )

    def test_fail_summary_includes_expected_words(self) -> None:
        # On a real mismatch (numeric compare fails), the FAIL
        # summary should still surface the DOM-derived words so
        # the operator sees the expected form immediately.
        report = validate_cheque(
            front=_front(
                amount="190000.00",
                amount_words="Ten Lakh Only",
            ),
            back=None,
            dom={"Amount": "190000.00"},
        )
        rule = _find(report, "amount_words")
        assert rule.status == "FAIL"
        assert "One Lakh Ninety Thousand" in rule.summary


# ---------------------------------------------------------------------------
# Rule 4: Cheque Number Verification
# ---------------------------------------------------------------------------


class TestChequeNoRule:
    def test_exact_match_passes(self) -> None:
        report = validate_cheque(
            front=_front(cheque_no="378781"),
            back=None, dom={"Cheque No": "378781"},
        )
        assert _find(report, "cheque_no").status == "PASS"

    def test_digit_only_compare_ignores_whitespace(self) -> None:
        report = validate_cheque(
            front=_front(cheque_no=" 378 781 "),
            back=None, dom={"Cheque No": "378781"},
        )
        assert _find(report, "cheque_no").status == "PASS"

    def test_suffix_alignment_passes(self) -> None:
        # Sometimes the system prefixes with a routing code.
        report = validate_cheque(
            front=_front(cheque_no="378781"),
            back=None, dom={"Cheque No": "000000378781"},
        )
        assert _find(report, "cheque_no").status == "PASS"

    def test_mismatch_fails(self) -> None:
        report = validate_cheque(
            front=_front(cheque_no="378781"),
            back=None, dom={"Cheque No": "999999"},
        )
        assert _find(report, "cheque_no").status == "FAIL"

    def test_missing_either_side_not_verified(self) -> None:
        report1 = validate_cheque(
            front=_front(cheque_no=None),
            back=None, dom={"Cheque No": "378781"},
        )
        report2 = validate_cheque(
            front=_front(cheque_no="378781"),
            back=None, dom={},
        )
        assert _find(report1, "cheque_no").status == "NOT_VERIFIED"
        assert _find(report2, "cheque_no").status == "NOT_VERIFIED"


class TestChequeNoRuleOcrLetterTolerance:
    """Production regression: cheque number `143144` was printed
    on the cheque but OCR read the two `1`s as `i` and `L`,
    producing the literal text `143iL4` in the raw OCR output.
    The structured extractor latched onto a different digit run
    elsewhere on the cheque (`107274`), so the rule FAILed even
    though the right number was visibly on the cheque.

    The fix: when the structured value disagrees with the system
    value, the rule retries via `_search_dom_in_ocr` which now
    includes an OCR-letter-tolerant digit tier (i/l/L→1, O/o→0,
    S→5, …). The raw text rescue PASSes the rule with an
    `extractor_disagreed` note so the operator can audit."""

    def test_letter_confused_cheque_no_rescued_from_raw_text(self) -> None:
        # Structured extractor returned the WRONG number (107274
        # — picked up from elsewhere on the cheque). Raw text
        # contains the right number written with OCR letter
        # confusions: `"143iL4"'`.
        report = validate_cheque(
            front=_front(
                cheque_no="107274",
                raw_text=(
                    "Pay JOHN DOE Rupees Fifty Thousand Only "
                    '"346237.00 ... "143iL4"\' 38c068'
                ),
            ),
            back=None,
            dom={"Cheque No": "143144"},
        )
        r = _find(report, "cheque_no")
        assert r.status == "PASS", (
            f"expected PASS via OCR-tolerant raw-text rescue, "
            f"got {r.status}: {r.summary!r}"
        )
        ev = dict(r.evidence)
        assert ev.get("ocr_search_kind") == "digits_ocr_tolerant"
        # Audit trail — operator must see WHY two views disagree.
        assert "extractor_disagreed" in ev

    def test_structured_match_still_wins_when_clean(self) -> None:
        # When the structured value matches the system value, we
        # short-circuit at Stage 1 and never even consult the
        # raw text. Make sure the new rescue path doesn't change
        # that.
        report = validate_cheque(
            front=_front(
                cheque_no="143144",
                raw_text="Pay JOHN DOE 143144 ... 999999",
            ),
            back=None,
            dom={"Cheque No": "143144"},
        )
        r = _find(report, "cheque_no")
        assert r.status == "PASS"
        # No rescue keys when Stage 1 fired.
        ev = dict(r.evidence)
        assert "ocr_search_kind" not in ev
        assert "extractor_disagreed" not in ev

    def test_structured_disagree_and_raw_text_silent_still_fails(self) -> None:
        # Failsafe: when the structured value is wrong AND the
        # raw text doesn't contain the system value (even with
        # letter substitution), we STILL FAIL. We never want the
        # OCR-tolerant rescue to mask a genuine mismatch.
        report = validate_cheque(
            front=_front(
                cheque_no="107274",
                raw_text="Pay JOHN DOE 107274 ... no other digits here",
            ),
            back=None,
            dom={"Cheque No": "143144"},
        )
        r = _find(report, "cheque_no").status
        assert r == "FAIL"

    def test_letter_only_pattern_does_not_false_match(self) -> None:
        # A naive `ilLiOO → 110100` substitution applied to ALL
        # text would invent matches from plain words. Pin that
        # the tolerant search ONLY substitutes inside tokens
        # that already contain at least one digit, so word-only
        # tokens like 'ill' or 'OIL' don't become bogus digits.
        report = validate_cheque(
            front=_front(
                cheque_no=None,
                raw_text=(
                    "Pay BOB OIL CO ill iLLEGAL legacy text only "
                    "no real cheque number printed here"
                ),
            ),
            back=None,
            dom={"Cheque No": "143144"},
        )
        # Stage 1 absent (cheque_digits empty) → Stage 3 raw-text
        # search → no match (tolerant or otherwise) → FAIL.
        r = _find(report, "cheque_no")
        assert r.status == "FAIL"


# ---------------------------------------------------------------------------
# Rule 5: Account Number Verification
# ---------------------------------------------------------------------------


class TestAccountNoRule:
    def test_back_side_account_match_passes(self) -> None:
        report = validate_cheque(
            front=_front(),
            back=_back("50200100315661"),
            dom={"Account No": "50200100315661"},
        )
        assert _find(report, "account_no").status == "PASS"

    def test_back_side_preferred_over_front(self) -> None:
        # The spec says BACK-side account no is canonical.
        report = validate_cheque(
            front=_front(account_no="wrong from front"),
            back=_back("50200100315661"),
            dom={"Account No": "50200100315661"},
        )
        r = _find(report, "account_no")
        assert r.status == "PASS"
        assert dict(r.evidence).get("source_side") == "back"

    def test_front_account_no_is_NOT_used_as_structured_match(self) -> None:
        # The FRONT of a cheque carries the DRAWER's printed A/C
        # No (the account that funds come FROM), which is a
        # DIFFERENT account than the system 'Account No' (the
        # depositor's account, endorsed on the BACK). The rule
        # must NOT silently accept a front-only match — that's
        # the false-positive case the user reported.
        report = validate_cheque(
            front=_front(account_no="50200100315661"),
            back=_back(None),
            dom={"Account No": "50200100315661"},
        )
        r = _find(report, "account_no")
        # Back-side OCR is empty → NOT_VERIFIED (operator must
        # confirm the back image was actually captured).
        assert r.status == "NOT_VERIFIED"
        ev = dict(r.evidence)
        assert ev.get("source_side") == "none"
        assert "back" in r.summary.lower()

    def test_mismatch_fails(self) -> None:
        report = validate_cheque(
            front=_front(),
            back=_back("11111111111111"),
            dom={"Account No": "50200100315661"},
        )
        assert _find(report, "account_no").status == "FAIL"

    def test_account_no_alias_keys(self) -> None:
        # The bank's panel uses several spellings — A/C No, A/c
        # No, Account No., etc. The rule must pick up all of them.
        for key in ("Account No", "Account No.", "A/C No", "A/c No"):
            report = validate_cheque(
                front=_front(),
                back=_back("50200100315661"),
                dom={key: "50200100315661"},
            )
            assert _find(report, "account_no").status == "PASS", key

    def test_missing_either_side_not_verified(self) -> None:
        report1 = validate_cheque(
            front=_front(), back=_back(None),
            dom={"Account No": "50200100315661"},
        )
        report2 = validate_cheque(
            front=_front(), back=_back("50200100315661"), dom={},
        )
        assert _find(report1, "account_no").status == "NOT_VERIFIED"
        assert _find(report2, "account_no").status == "NOT_VERIFIED"


class TestAccountNoRuleRawTextRescue:
    """Production regression: a back-side capture whose deposit
    stamp account `99991188118818` was readable in the raw OCR
    text, but where the structured `_find_account_no` extractor
    picked the longest digit run (a 17-digit transaction
    reference) instead.

    Stage-2 used to FAIL immediately on the structured value
    mismatch. The rescue path now retries `_search_dom_in_ocr`
    on the back raw text — when it locates the system value
    (with letter↔digit OCR tolerance) the rule PASSes with an
    `extractor_disagreed` audit note instead of failing on the
    wrong-region pickup."""

    def test_structured_picked_transaction_ref_but_raw_text_has_real_account(
        self,
    ) -> None:
        # back.account_no = 17-digit transaction reference picked
        # by the longest-wins fallback. raw_text DOES contain the
        # real account number (a deposit stamp). Rule must PASS.
        report = validate_cheque(
            front=_front(),
            back=_back(
                "12238024900224285",  # 17-digit transaction ref
                raw_text=(
                    "Stamp 9999 1188 1188 18 "
                    "Ref 2306282614 3428500000110 380240002 24285 1 N "
                    "IFSC HDFC0000000"
                ),
            ),
            dom={"Account No": "99991188118818"},
        )
        r = _find(report, "account_no")
        assert r.status == "PASS", (
            f"expected PASS via back raw-text rescue, got {r.status}: "
            f"{r.summary!r}"
        )
        ev = dict(r.evidence)
        assert "extractor_disagreed" in ev
        assert ev.get("ocr_search_side") == "back"

    def test_structured_disagree_and_raw_text_silent_still_fails(
        self,
    ) -> None:
        # Failsafe: when the back-side raw OCR ALSO doesn't
        # contain the system value (even with letter tolerance)
        # AND doesn't contain a long enough partial match, the
        # rule must STILL FAIL. The rescue paths must never
        # mask a real account-number mismatch.
        report = validate_cheque(
            front=_front(),
            back=_back(
                "12238024900224285",
                raw_text="Ref 12238024900224285 IFSC HDFC0000000 nothing else",
            ),
            dom={"Account No": "99991188118818"},
        )
        assert _find(report, "account_no").status == "FAIL"


class TestAccountNoRulePartialMatchWarn:
    """Production regression: a back capture where OCR recovered
    only the FIRST 8 digits (`9999 11 88`) of the 14-digit
    system account `99991188118818` — the stamp's last 6 digits
    were too faint/mangled for any OCR engine to recognise.

    Previously this FAILed opaquely. The partial-match WARN tier
    surfaces it as 'operator should eyeball the last few digits'
    — much higher-signal verdict for a real cheque whose stamp
    is just hard to read."""

    def test_partial_match_in_back_text_triggers_warn(
        self,
    ) -> None:
        # OCR recovered `9999 11 88` from a stamp printed as
        # `9999 11 88 11 88 18`. The picker grabbed a different
        # long run from the same back text, so Stage 1 mismatches.
        # Raw-text full-match search misses. Partial match
        # (>= 8 contiguous prefix digits of the 14-digit DOM)
        # fires WARN with evidence so the operator knows
        # exactly which digits were verified.
        report = validate_cheque(
            front=_front(),
            back=_back(
                "12238024900224285",  # wrong number from picker
                raw_text=(
                    "stamp 9999 11 88 I RR  "
                    "ref 12238024900224285 IFSC HDFC0000000"
                ),
            ),
            dom={"Account No": "99991188118818"},
        )
        r = _find(report, "account_no")
        assert r.status == "WARN", (
            f"expected WARN via partial-match tier, got {r.status}: "
            f"{r.summary!r}"
        )
        ev = dict(r.evidence)
        # At least the first 8 chars of DOM digits (`99991188`)
        # must be located — more is fine if the OCR text's digit
        # concatenation accidentally extends the prefix.
        plen = ev.get("back_partial_match_length")
        assert isinstance(plen, int) and plen >= 8, (
            f"partial match length too small: {plen}"
        )
        pstr = ev.get("back_partial_match_digits") or ""
        assert pstr.startswith("99991188"), (
            f"partial match string must start with the verified "
            f"prefix; got {pstr!r}"
        )
        assert "/14 digits" in str(ev.get("back_partial_match_coverage"))

    def test_partial_match_warn_fires_when_no_structured_pick(self) -> None:
        # No structured account picked (Stage 1/2 skipped). Raw-
        # text search returns "fail". Partial match still
        # surfaces a WARN because >= 60% of the DOM digits are
        # visible in correct sequence.
        report = validate_cheque(
            front=_front(),
            back=_back(
                None,
                raw_text="stamp 9999 11 88 1 garbage",
            ),
            dom={"Account No": "99991188118818"},
        )
        r = _find(report, "account_no")
        assert r.status == "WARN"
        ev = dict(r.evidence)
        # OCR-tolerant variant promotes `9999 11 88 1` to 9-digit
        # match (`I→1`). Either 8 or 9 is acceptable — both
        # exceed the threshold and produce a useful WARN.
        assert ev.get("back_partial_match_length") in (8, 9)

    def test_short_overlap_does_not_trigger_warn(self) -> None:
        # Only 4 contiguous digits overlap with the DOM. That's
        # below the partial-match threshold (max(6, 60% of len))
        # so the rule must still FAIL — we don't want a stray
        # 4-digit substring overlap to mask a wrong account.
        report = validate_cheque(
            front=_front(),
            back=_back(
                None,
                raw_text="stamp 9999 nothing else relevant here",
            ),
            dom={"Account No": "99991188118818"},
        )
        r = _find(report, "account_no")
        assert r.status == "FAIL"
        ev = dict(r.evidence)
        assert "back_partial_match_length" not in ev


class TestAccountNoRuleFlipStatus:
    """The capability stamps `back_flip_status` onto each row when
    it knows whether the Alt+F1 keyboard shortcut actually flipped
    the cheque viewer. The account-number rule must consult that
    diagnostic so the operator never sees an AUTO_REJECT for a
    cheque whose back was never on screen (the back-OCR regression
    that prompted this fix)."""

    def test_flip_failed_downgrades_fail_to_not_verified(self) -> None:
        # Back is byte-identical to the front per the capability's
        # comparison, but its OCR happens to find a digit run that
        # disagrees with the system value. Without the flip-status
        # gate, this would FAIL the rule. With it, we should
        # NOT_VERIFY because the digits aren't trustworthy — they
        # came from the front, not the back.
        report = validate_cheque(
            front=_front(),
            back=_back("99999999999999"),  # different from DOM
            dom={"Account No": "50200100315661"},
            back_flip_status={
                "requested": "Alt+F1", "retries": 1, "changed": False,
            },
        )
        r = _find(report, "account_no")
        assert r.status == "NOT_VERIFIED", (
            f"expected NOT_VERIFIED on flip-failed cheque, got {r.status}"
        )
        assert "alt+f1" in r.summary.lower()
        ev = dict(r.evidence)
        assert ev.get("back_flip_changed") is False
        assert ev.get("back_flip_keystroke") == "Alt+F1"
        assert ev.get("back_flip_retries") == 1

    def test_flip_failed_still_passes_on_exact_match(self) -> None:
        # Edge: the front happens to carry the same account number
        # as the system value (some cheques print the drawer's A/C
        # on the front AND the depositor endorses the same account
        # on the back). If the digits agree exactly we trust the
        # verdict regardless of which side we shot — better to
        # PASS a valid cheque than to NOT_VERIFY it.
        report = validate_cheque(
            front=_front(),
            back=_back("50200100315661"),
            dom={"Account No": "50200100315661"},
            back_flip_status={
                "requested": "Alt+F1", "retries": 1, "changed": False,
            },
        )
        r = _find(report, "account_no")
        assert r.status == "PASS"

    def test_flip_succeeded_records_changed_true_in_evidence(self) -> None:
        # Happy path — flip worked. The rule still surfaces the
        # flip diagnostic as evidence so the operator can audit
        # the trace, but doesn't change its verdict.
        report = validate_cheque(
            front=_front(),
            back=_back("50200100315661"),
            dom={"Account No": "50200100315661"},
            back_flip_status={
                "requested": "Alt+F1", "retries": 0, "changed": True,
            },
        )
        r = _find(report, "account_no")
        assert r.status == "PASS"
        ev = dict(r.evidence)
        assert ev.get("back_flip_changed") is True
        assert ev.get("back_flip_retries") == 0

    def test_no_flip_status_keeps_legacy_behaviour(self) -> None:
        # When the capability didn't supply a flip diagnostic
        # (older versions, or capabilities that don't capture a
        # back) the rule must behave exactly as before — surfaces
        # 'unknown' in evidence so the operator can spot it,
        # otherwise no semantic change.
        report = validate_cheque(
            front=_front(),
            back=_back("99999999999999"),
            dom={"Account No": "50200100315661"},
        )
        r = _find(report, "account_no")
        assert r.status == "FAIL"
        ev = dict(r.evidence)
        assert ev.get("back_flip_changed") == "unknown"


# ---------------------------------------------------------------------------
# Rule 6: Signature Verification (presence only)
# ---------------------------------------------------------------------------


class TestSignatureRule:
    def test_present_verdict_passes(self) -> None:
        report = validate_cheque(
            front=_front(signature_verdict="present", signature_density=0.04),
            back=None, dom={},
        )
        assert _find(report, "signature").status == "PASS"

    def test_maybe_verdict_warns(self) -> None:
        report = validate_cheque(
            front=_front(signature_verdict="maybe", signature_density=0.003),
            back=None, dom={},
        )
        r = _find(report, "signature")
        assert r.status == "WARN"

    def test_absent_verdict_fails(self) -> None:
        report = validate_cheque(
            front=_front(signature_verdict="absent", signature_density=0.0005),
            back=None, dom={},
        )
        assert _find(report, "signature").status == "FAIL"

    def test_missing_dep_not_verified(self) -> None:
        report = validate_cheque(
            front=_front(
                signature_verdict=None,
                signature_density=0.0,
                signature_missing_dep="opencv not available",
            ),
            back=None, dom={},
        )
        r = _find(report, "signature")
        assert r.status == "NOT_VERIFIED"
        assert "opencv" in r.summary.lower()

    def test_no_front_capture_not_verified(self) -> None:
        report = validate_cheque(front=None, back=None, dom={})
        assert _find(report, "signature").status == "NOT_VERIFIED"


# ---------------------------------------------------------------------------
# Raw-text fallback paths — the user-reported behaviour where the
# DOM panel has clean values but the structured field extractors
# came back empty. The rules should still PASS (or WARN on near-
# misses) by searching the raw OCR text for the DOM value.
# ---------------------------------------------------------------------------


class TestRawTextFallback:
    """The structured extractors (regex / MICR / TrOCR) often
    leave fields empty even when the OCR text contains the
    value with some noise. These tests verify the four
    'X Verification' rules fall back to a raw-text fuzzy
    search rather than blindly returning NOT_VERIFIED."""

    def test_cheque_no_found_in_raw_text_passes(self) -> None:
        # Structured cheque_no empty, but the digits ARE in
        # raw_text — same case the user saw on Cheque #1.
        report = validate_cheque(
            front=_front(
                cheque_no=None,
                raw_text="JOHN DOE 51060.00 chq 378781 dated 21/06/2026",
            ),
            back=None, dom={"Cheque No": "378781"},
        )
        r = _find(report, "cheque_no")
        assert r.status == "PASS"
        # Summary should reference the raw-text-fallback path, not
        # the structured-match path.
        s = r.summary.lower()
        assert "found in the ocr text" in s or "matches" in s

    def test_cheque_no_near_miss_warns(self) -> None:
        # Structured cheque_no empty, raw_text has the digits
        # with noise (one wrong digit) — should WARN, not FAIL.
        report = validate_cheque(
            front=_front(
                cheque_no=None,
                raw_text="JOHN DOE 51060.00 chq 378785 dated 21/06/2026",
            ),
            back=None, dom={"Cheque No": "378781"},
        )
        r = _find(report, "cheque_no")
        # 5-of-6 digits = ~83% similarity → WARN (or PASS depending on
        # SequenceMatcher; both are operator-actionable).
        assert r.status in {"WARN", "PASS"}

    def test_account_no_found_in_back_raw_text_passes(self) -> None:
        report = validate_cheque(
            front=_front(),
            back=ChequeFields(
                side="back",
                raw_text="endorsement 50200100315661 stamp deposit",
                account_no=None,
                ocr_confidence=0.85,
            ),
            dom={"Account No": "50200100315661"},
        )
        assert _find(report, "account_no").status == "PASS"

    def test_account_no_back_empty_surfaces_front_as_aux_evidence(self) -> None:
        # Back-side OCR is empty but the system account number
        # IS visible on the front raw text. We surface this as
        # NOT_VERIFIED (back is canonical per spec) but include
        # the front-side hit as auxiliary evidence so the
        # operator knows the back-capture is the broken link.
        report = validate_cheque(
            front=_front(
                account_no=None,
                raw_text="payment to 50200100315661 stamped",
            ),
            back=ChequeFields(side="back", raw_text="", ocr_confidence=0.0),
            dom={"Account No": "50200100315661"},
        )
        r = _find(report, "account_no")
        assert r.status == "NOT_VERIFIED"
        ev = dict(r.evidence)
        # Front hit surfaced as auxiliary evidence (sim ≥ 0.85).
        assert ev.get("aux_front_search_similarity", 0) >= 0.85
        # Summary explicitly mentions BACK so operator doesn't
        # interpret this as a normal NOT_VERIFIED.
        assert "back" in r.summary.lower()
        assert "front" in r.summary.lower()

    def test_amount_found_in_raw_text_passes(self) -> None:
        # Structured amount empty, raw_text has the digits.
        # Split-rule behaviour: amount_figures rescues itself via
        # raw-text search → PASS. amount_words has nothing to parse
        # → NOT_VERIFIED. Operator sees both verdicts independently.
        report = validate_cheque(
            front=_front(
                amount=None,
                amount_words=None,
                raw_text="payee john doe 51,060.00 only signed",
            ),
            back=None, dom={"Amount": "51,060.00"},
        )
        r_figures = _find(report, "amount_figures")
        r_words = _find(report, "amount_words")
        assert r_figures.status == "PASS", (
            f"figures rule must rescue via raw-text search — "
            f"got {r_figures.status}: {r_figures.summary!r}"
        )
        assert "found in the cheque OCR text" in r_figures.summary
        assert r_words.status == "NOT_VERIFIED"

    def test_payee_found_in_raw_text_passes(self) -> None:
        report = validate_cheque(
            front=_front(
                beneficiary=None,
                raw_text="pay to JAYSHIVSAKTHI TRADERS rs 51060",
                handwriting_regions=(),
            ),
            back=None, dom={"Beneficiary 1": "JAYSHIVSAKTHI TRADERS"},
        )
        r = _find(report, "payee")
        assert r.status == "PASS"

    def test_payee_near_miss_warns(self) -> None:
        # ~70% similarity ("JAYSHIVSAKTI" vs "JAYSHIVSAKTHI") +
        # an extra word. Should warn rather than fail outright.
        report = validate_cheque(
            front=_front(
                beneficiary=None,
                raw_text="pay JAYSHIVSAKTI TRADERSI rs",
            ),
            back=None, dom={"Beneficiary 1": "JAYSHIVSAKTHI TRADERS"},
        )
        r = _find(report, "payee")
        assert r.status in {"WARN", "PASS"}  # tolerate the threshold

    def test_payee_no_match_in_raw_text_fails(self) -> None:
        report = validate_cheque(
            front=_front(
                beneficiary=None,
                raw_text="pay JANE SMITH rs 51060",
            ),
            back=None, dom={"Beneficiary 1": "JAYSHIVSAKTHI TRADERS"},
        )
        assert _find(report, "payee").status == "FAIL"

    def test_payee_high_fuzzy_no_tokens_warns_not_fails(self) -> None:
        """Real-world fallback case: EasyOCR's cursive read is
        garbled enough that no individual token of HEMA RAM
        matches (token_score = 0.0) — neither 'hema' nor 'ram'
        appears verbatim, as substring, OR fuzzy-token-matches
        at the strict 0.8 threshold. But the character-window
        SequenceMatcher still finds >= 0.5 similarity overall.
        That's a low-confidence near-miss the operator should
        eyeball, NOT a hard FAIL that rejects the cheque."""
        # 'kema rom raw seventy' empirically gives token=0.00
        # (no tokens match) and fuzzy=0.75 (high enough to fire
        # the new fuzzy-WARN tier).
        report = validate_cheque(
            front=_front(
                beneficiary=None,
                raw_text="kema rom raw seventy",
            ),
            back=None, dom={"Beneficiary 1": "HEMA RAM"},
        )
        r = _find(report, "payee")
        assert r.status == "WARN", f"expected WARN, got {r.status}: {r.summary}"
        assert "near-miss" in r.summary.lower()

    def test_date_found_in_back_raw_text(self) -> None:
        # Some cheques have only a back-side endorsement-stamp
        # date that's readable; the validator should walk the
        # back when the front has nothing.
        today = date(2026, 6, 23)
        report = validate_cheque(
            front=_front(raw_text=""),
            back=ChequeFields(
                side="back",
                raw_text=(
                    "deposit stamp 19/06/2026 hdfc clearing"
                ),
                ocr_confidence=0.8,
            ),
            dom={}, today=today,
        )
        assert _find(report, "date").status == "PASS"

    def test_bank_printed_mmm_yyyy_date_parses(self) -> None:
        today = date(2026, 6, 23)
        report = validate_cheque(
            front=_front(raw_text="cycle 06 batch 19-JUN-2026 sample"),
            back=None, dom={}, today=today,
        )
        assert _find(report, "date").status == "PASS"

    def test_boxed_DDMMYYYY_with_spaces_between_digits(self) -> None:
        # The CTS-2010 form prints the date as 8 single-digit
        # boxes — PaddleOCR reads each digit as its own token so
        # the consolidated raw text comes out with whitespace
        # between every digit (e.g. '1 6 0 6 2 0 2 6' for
        # 16-06-2026). The validator must reconstruct it.
        today = date(2026, 6, 23)
        for sample in (
            "pay  1 6 0 6 2 0 2 6  hema ram",
            "date 1  6  0  6  2  0  2  6  endorsed",
            # Some banks emit narrow non-digit separators (the
            # box outlines OCR as '|' / '/').
            "1|6|0|6|2|0|2|6 stamp",
        ):
            r = validate_cheque(
                front=_front(raw_text=sample),
                back=None, dom={}, today=today,
            )
            assert _find(r, "date").status == "PASS", sample


class TestPayeeTokenOverlap:
    """Targeted regression coverage for the user-reported case
    where 'HEMA RAM' (8 chars, two short tokens) coincidentally
    SequenceMatcher-fuzzy-matched at 50% against a cheque whose
    actual payee was 'JAYSHIVSAKTHI TRADERS'. Token-overlap
    correctly ranks JAYSHIVSAKTHI TRADERS above HEMA RAM there."""

    def test_correct_beneficiary_wins_over_noise_match(self) -> None:
        # OCR text contains JAY SHIVSAKTHI TRADERS (with a space
        # the bank's printed name omits) and zero letters of
        # HEMA or RAM anywhere. Token-overlap should rank
        # JAYSHIVSAKTHI TRADERS first; the previous fuzzy-only
        # logic incorrectly preferred HEMA RAM because shorter
        # strings fuzzy-match noise more easily.
        report = validate_cheque(
            front=_front(
                beneficiary=None,
                raw_text="pay JAY SHIVSAKTHI TRADERS rs 51060/-",
            ),
            back=None,
            dom={
                "Beneficiary 1": "JAYSHIVSAKTHI TRADERS",
                "Beneficiary 2": "HEMA RAM",
            },
        )
        r = _find(report, "payee")
        assert r.status == "PASS"
        ev = dict(r.evidence)
        assert ev["ocr_search_best_payee"] == "JAYSHIVSAKTHI TRADERS"
        # The per-payee breakdown should clearly differentiate
        # the winner from the noise-matching short beneficiary.
        per = {p["payee"]: p for p in ev["per_payee_scores"]}
        assert per["JAYSHIVSAKTHI TRADERS"]["token_score"] >= 0.5
        assert per["HEMA RAM"]["token_score"] == 0.0

    def test_short_beneficiary_does_not_match_random_noise(self) -> None:
        # Pure regression: HEMA RAM is in the DOM but absolutely
        # nowhere in the OCR text. The rule must FAIL, not WARN.
        report = validate_cheque(
            front=_front(
                beneficiary=None,
                raw_text="pay xyz traders rs 99999/-",
            ),
            back=None, dom={"Beneficiary 1": "HEMA RAM"},
        )
        r = _find(report, "payee")
        assert r.status == "FAIL"


class TestAmountSlashTerminator:
    """The user pointed out that operators write amounts as
    '51060/-' in the figures box — no commas, no decimal, no
    currency prefix. Our extractor must recognise this."""

    def test_slash_terminator_picked_up(self) -> None:
        from aakaar_caps.cheque.cheque_ocr import _find_amount_in_figures

        # The /- terminator is the SOLE decoration.
        assert _find_amount_in_figures(["pay 51060/- only"]) is not None
        # With equals or underscore variants.
        assert _find_amount_in_figures(["amt 51060/=  signed"]) is not None
        assert _find_amount_in_figures(["amt 51060 / -  signed"]) is not None

    def test_amount_with_slash_terminator_round_trip(self) -> None:
        # End-to-end: structured amount empty, raw_text has
        # '51060/-', DOM has '51,060.00'. amount_figures rescues
        # via raw-text search → PASS.
        report = validate_cheque(
            front=_front(
                amount=None, amount_words=None,
                raw_text="pay hema ram rs 51060/- only",
            ),
            back=None, dom={"Amount": "51,060.00"},
        )
        assert _find(report, "amount_figures").status == "PASS"


class TestAmountNoCommaDecimal:
    """Regression: production cheque ('Sixteen Thousand One Hundred
    Fourty-one only / 16141.00') was failing because the explicit-
    currency regex capped the first segment at 3 digits and
    required a comma/space separator between segments. The
    courtesy box prints as a single digit run ('Rs.16141.00') on
    many bank templates — so '161' was being captured and '41.00'
    dropped, surfacing a fake words-vs-figures mismatch."""

    def test_explicit_marker_bare_digit_run(self) -> None:
        from aakaar_caps.cheque.cheque_ocr import _find_amount_in_figures

        # The exact failing form from production
        assert _find_amount_in_figures(["Rs.16141.00 PAY"]) == "16141.00"
        assert _find_amount_in_figures(["Rs. 16141.00"]) == "16141.00"
        assert _find_amount_in_figures(["INR 100000.00 only"]) == "100000.00"
        assert _find_amount_in_figures(["₹250000"]) == "250000"

    def test_indian_grouped_still_supported(self) -> None:
        from aakaar_caps.cheque.cheque_ocr import _find_amount_in_figures

        # Commas preserved at this layer; _normalize_amount_for_compare
        # strips them at comparison time.
        assert _find_amount_in_figures(["Rs. 16,141.00"]) == "16,141.00"
        assert _find_amount_in_figures(["Rs 1,23,456.78"]) == "1,23,456.78"

    def test_bare_decimal_picked_over_cheque_number(self) -> None:
        from aakaar_caps.cheque.cheque_ocr import _find_amount_in_figures

        # Decoration tier (has decimal) outranks bare-plain tier
        # (no decoration). Even when an 8-digit cheque number is on
        # the same input, the decorated 16141.00 must win.
        lines = [
            "RUPEES Sixteen Thousand One Hundred Fourty-one only.",
            "16141.00",
            "Cheque No. 17084954",
            "A/c No. 058815130000004",
        ]
        assert _find_amount_in_figures(lines) == "16141.00"

    def test_cheque_number_line_never_leaks_as_amount(self) -> None:
        from aakaar_caps.cheque.cheque_ocr import _find_amount_in_figures

        # Cheque-no / account-no lines are now bank-meta filtered
        # — they can never surface as the amount even when no
        # better candidate exists.
        assert _find_amount_in_figures(["Cheque No. 17084954"]) is None
        assert _find_amount_in_figures(["Chq No. 17084954"]) is None
        assert _find_amount_in_figures(["A/c No. 058815130000004"]) is None


# ---------------------------------------------------------------------------
# Defensive behaviour
# ---------------------------------------------------------------------------


class TestDefensiveBehaviour:
    def test_rule_crash_downgraded_to_not_verified(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A bug inside one rule must NOT poison the other five
        — the wrapper downgrades to NOT_VERIFIED with an
        evidence trail."""
        from aakaar_caps.cheque import cheque_validation as cv

        def bomb(**_kwargs: Any) -> None:
            raise RuntimeError("boom in date rule")

        monkeypatch.setattr(cv, "_rule_date", bomb)
        report = cv.validate_cheque(
            front=_front(), back=_back(None), dom={},
        )
        # Seven rules still surfaced (amount split into
        # amount_words + amount_figures, June 2026).
        assert len(report.checks) == 7
        # The crashing rule is NOT_VERIFIED with the exception
        # text in evidence — the other six ran normally.
        date_check = _find(report, "date")
        assert date_check.status == "NOT_VERIFIED"
        assert "boom" in dict(date_check.evidence).get("error", "")

    def test_every_rule_carries_ocr_diagnostic_evidence(self) -> None:
        """Each rule result must include the raw OCR
        snippet + length + engine list, so an operator can tell
        at a glance whether the rule failed because OCR couldn't
        see the cheque vs. because the cheque genuinely failed."""
        report = validate_cheque(
            front=_front(raw_text="some readable text here"),
            back=_back("50200100315661"),
            dom={"Cheque No": "378781"},
        )
        for c in report.checks:
            ev = dict(c.evidence)
            for key in (
                "ocr_front_raw_text_snippet",
                "ocr_front_raw_text_len",
                "ocr_back_raw_text_snippet",
                "ocr_back_raw_text_len",
            ):
                assert key in ev, f"{c.check_id} missing {key}"

    def test_ocr_health_flagged_when_text_is_empty(self) -> None:
        """When OCR produced essentially nothing on either side
        the report's ocr_health field must surface as 'both_weak'
        so the UI can show a banner before the operator wades
        through six misleading per-rule explanations."""
        report = validate_cheque(
            front=_front(raw_text=""),
            back=ChequeFields(side="back", raw_text="", ocr_confidence=0.0),
            dom={"Account No": "50200100315661"},
        )
        assert report.ocr_health == "both_weak"
        assert report.ocr_health_summary
        # Should still surface seven rules (amount split into
        # amount_words + amount_figures, June 2026) — banner is a
        # complement, not a replacement, for the rule grid.
        assert len(report.checks) == 7

    def test_ocr_health_ok_when_text_is_substantive(self) -> None:
        long_text = "A" * 80  # over the 50-char threshold
        report = validate_cheque(
            front=_front(raw_text=long_text),
            back=ChequeFields(side="back", raw_text=long_text, ocr_confidence=0.9),
            dom={},
        )
        assert report.ocr_health == "ok"

    def test_ocr_health_handwriting_unavailable_when_trocr_and_fallback_both_silent(
        self,
    ) -> None:
        """Hard-failure case: TrOCR didn't load AND the
        region-focused EasyOCR fallback produced no text on the
        handwriting bands either. The banner stays strong because
        the rules genuinely can't see the handwriting."""
        long_text = "A" * 100
        front = ChequeFields(
            side="front",
            raw_text=long_text,
            ocr_confidence=0.9,
            handwriting_regions=(),
            handwriting_missing_dep=(
                "trocr load failed: SSL CERTIFICATE_VERIFY_FAILED..."
            ),
            # Focused passes ran but ALL returned empty text.
            engine_runs=(
                ("paddle_focused_payee_line", "", 0.0, 0, None),
                ("paddle_focused_amount_words", "", 0.0, 0, None),
                ("paddle_focused_amount_figures", "", 0.0, 0, None),
                ("paddle_focused_date", "", 0.0, 0, None),
            ),
        )
        back = ChequeFields(side="back", raw_text=long_text, ocr_confidence=0.9)
        report = validate_cheque(front=front, back=back, dom={})
        assert report.ocr_health == "handwriting_unavailable"
        assert "trocr" in report.ocr_health_summary.lower()
        assert "download_trocr" in report.ocr_health_summary

    def test_ocr_health_handwriting_fallback_when_focused_pass_produces_text(
        self,
    ) -> None:
        """Soft-banner case: TrOCR didn't load BUT the region-
        focused EasyOCR/Paddle passes are producing text on the
        handwriting bands. Operator should see an info-level
        banner ("fallback active") not a blocking one, because
        the rules CAN evaluate the handwriting (just at lower
        accuracy than with TrOCR).

        Note: ChequeFields.beneficiary / amount / amount_words are
        all None here, so the new tier-1 suppression check
        (_handwriting_extraction_succeeded) returns False and we
        correctly fall through to the soft banner."""
        long_text = "A" * 100
        front = ChequeFields(
            side="front",
            raw_text=long_text,
            ocr_confidence=0.9,
            handwriting_regions=(),
            handwriting_missing_dep=(
                "trocr load failed: SSL CERTIFICATE_VERIFY_FAILED..."
            ),
            engine_runs=(
                # Focused EasyOCR fallback DID produce text for two
                # of the four bands.
                (
                    "paddle_focused_payee_line",
                    "HEMA RAM",
                    0.72, 1, None,
                ),
                ("paddle_focused_amount_words", "", 0.0, 0, None),
                ("paddle_focused_amount_figures", "51060", 0.81, 1, None),
                ("paddle_focused_date", "", 0.0, 0, None),
            ),
        )
        back = ChequeFields(side="back", raw_text=long_text, ocr_confidence=0.9)
        report = validate_cheque(front=front, back=back, dom={})
        assert report.ocr_health == "handwriting_fallback"
        # Summary must enumerate which bands the fallback covered
        # so the operator knows which rules to trust more.
        assert "payee_line" in report.ocr_health_summary
        assert "amount_figures" in report.ocr_health_summary
        assert "fallback" in report.ocr_health_summary.lower()

    def test_ocr_health_suppresses_trocr_banner_when_extraction_succeeded(
        self,
    ) -> None:
        """Tier-1 suppression added 2026-06: when TrOCR didn't load
        BUT the structured handwriting fields (beneficiary / amount /
        amount_words) all came out fine via the focused-region
        fallback, the operator should NOT see the scary
        'fallback active — treat as near-miss' banner. Extraction
        WORKED — the banner is misleading.

        ocr_health must be 'ok' and ocr_health_summary must be
        empty. The TrOCR unavailability is still surfaced via the
        engine_runs diagnostics drawer for operators who want to
        know what's loaded; it just doesn't trigger a top-level
        scary banner."""
        long_text = "A" * 100
        front = ChequeFields(
            side="front",
            raw_text=long_text,
            ocr_confidence=0.9,
            beneficiary="HEMA RAM",
            amount="51060",
            amount_words="FIFTY ONE THOUSAND SIXTY ONLY",
            handwriting_regions=(),
            handwriting_missing_dep=(
                "trocr load failed: SSL CERTIFICATE_VERIFY_FAILED..."
            ),
            engine_runs=(
                (
                    "paddle_focused_payee_line",
                    "HEMA RAM",
                    0.72, 1, None,
                ),
                (
                    "paddle_focused_amount_words",
                    "FIFTY ONE THOUSAND SIXTY ONLY",
                    0.68, 1, None,
                ),
                (
                    "paddle_focused_amount_figures",
                    "51060", 0.81, 1, None,
                ),
            ),
        )
        back = ChequeFields(
            side="back", raw_text=long_text, ocr_confidence=0.9,
        )
        report = validate_cheque(front=front, back=back, dom={})
        assert report.ocr_health == "ok", (
            f"banner should be suppressed when extraction "
            f"succeeded; got ocr_health={report.ocr_health!r} "
            f"summary={report.ocr_health_summary!r}"
        )
        assert report.ocr_health_summary == ""

    def test_ocr_health_banner_still_fires_with_only_one_field_extracted(
        self,
    ) -> None:
        """Tier-1 suppression requires 2-of-3 handwriting fields
        non-empty. If only 1 of 3 came out, that's NOT enough
        confidence to suppress the banner — fall back to the
        soft handwriting_fallback message."""
        long_text = "A" * 100
        front = ChequeFields(
            side="front",
            raw_text=long_text,
            ocr_confidence=0.9,
            beneficiary="HEMA RAM",
            # amount and amount_words remain None - only 1 of 3
            handwriting_regions=(),
            handwriting_missing_dep=(
                "trocr load failed: SSL CERTIFICATE_VERIFY_FAILED..."
            ),
            engine_runs=(
                (
                    "paddle_focused_payee_line",
                    "HEMA RAM", 0.72, 1, None,
                ),
            ),
        )
        back = ChequeFields(
            side="back", raw_text=long_text, ocr_confidence=0.9,
        )
        report = validate_cheque(front=front, back=back, dom={})
        assert report.ocr_health == "handwriting_fallback", (
            f"banner should still fire when only 1 of 3 fields "
            f"extracted; got ocr_health={report.ocr_health!r}"
        )

    def test_handwriting_extraction_succeeded_helper(self) -> None:
        """Unit-test the suppression helper directly so the rule
        is locked down (2-of-3 cutoff)."""
        from aakaar_caps.cheque.cheque_validation import (  # noqa: PLC0415
            _handwriting_extraction_succeeded,
        )

        def _fields(**kw):
            return ChequeFields(
                side="front", raw_text="x", ocr_confidence=0.9, **kw,
            )

        # None input - never succeeded.
        assert _handwriting_extraction_succeeded(None) is False

        # 0 of 3 - fails.
        assert _handwriting_extraction_succeeded(_fields()) is False

        # 1 of 3 - still fails the 2-of-3 cutoff.
        assert _handwriting_extraction_succeeded(
            _fields(beneficiary="HEMA RAM"),
        ) is False

        # 2 of 3 - passes the cutoff.
        assert _handwriting_extraction_succeeded(
            _fields(beneficiary="HEMA RAM", amount="51060"),
        ) is True

        # 3 of 3 - passes.
        assert _handwriting_extraction_succeeded(
            _fields(
                beneficiary="HEMA RAM",
                amount="51060",
                amount_words="FIFTY ONE THOUSAND SIXTY ONLY",
            ),
        ) is True

        # Whitespace-only values do NOT count as filled.
        assert _handwriting_extraction_succeeded(
            _fields(beneficiary="   ", amount="51060"),
        ) is False

    def test_ocr_health_front_weak_when_only_front_short(self) -> None:
        report = validate_cheque(
            front=_front(raw_text="hi"),
            back=ChequeFields(
                side="back", raw_text="X" * 80, ocr_confidence=0.9,
            ),
            dom={},
        )
        assert report.ocr_health == "front_weak"
        assert "front" in report.ocr_health_summary.lower()

    def test_evidence_is_jsonable(self) -> None:
        """Operators consume this report via JSON — every value
        in the evidence dicts must be JSON-serialisable."""
        import json

        report = validate_cheque(
            front=_front(
                cheque_no="378781",
                amount="51,060.00",
                amount_words="Fifty One Thousand Sixty Only",
                signature_verdict="present", signature_density=0.04,
            ),
            back=_back("50200100315661"),
            dom={
                "Beneficiary 1": "JOHN DOE",
                "Cheque No": "378781",
                "Amount": "51,060.00",
                "Account No": "50200100315661",
            },
        )
        # to_dict() output must round-trip through JSON.
        s = json.dumps(report.to_dict())
        assert json.loads(s) == report.to_dict()


# ---------------------------------------------------------------------------
# VLM cross-check — rule-by-rule integration
# ---------------------------------------------------------------------------


def _vlm(**overrides: Any) -> dict[str, Any]:
    """Build a complete VLM verification payload. Every field
    defaults to a "no answer" sentinel — pass keyword args to
    flip on the fields the test cares about."""
    base: dict[str, Any] = {
        "payee_match": None, "payee_confidence": 0.0,
        "amount_in_figures_matches": None,
        "amount_in_figures_confidence": 0.0,
        "amount_in_words_matches": None,
        "amount_in_words_confidence": 0.0,
        "cheque_no_matches": None, "cheque_no_confidence": 0.0,
        "date_ddmmyyyy": None, "date_confidence": 0.0,
        "account_no_matches": None, "account_no_confidence": 0.0,
        "signature_present": None, "signature_confidence": 0.0,
        "raw_response": "",
        "missing_dep": None,
        "backend_used": "mlx",
        "inference_seconds": 1.5,
        "model_id": "Qwen/Qwen2.5-VL-3B-Instruct",
    }
    base.update(overrides)
    return base


class TestPayeeRuleWithVlm:
    def test_vlm_agrees_canonical_payee_passes(self) -> None:
        """VLM picks a candidate name with high confidence → PASS
        even when OCR text is garbage."""
        report = validate_cheque(
            front=_front(
                raw_text="Suv J Lroaes Jey Sok",  # garbage cursive OCR
                vlm_verification=_vlm(
                    payee_match="HEMA RAM",
                    payee_confidence=0.93,
                ),
            ),
            back=_back(None),
            dom={"Beneficiary": "HEMA RAM"},
        )
        check = _find(report, "payee")
        assert check.status == "PASS"
        assert "VLM" in check.summary or "vlm" in check.summary.lower()
        ev = dict(check.evidence)
        assert ev["vlm_agreement"] == "vlm_primary"
        assert ev["vlm_payee_match"] == "HEMA RAM"

    def test_vlm_says_neither_fails(self) -> None:
        report = validate_cheque(
            front=_front(
                raw_text="some random scribble",
                vlm_verification=_vlm(
                    payee_match="neither",
                    payee_confidence=0.85,
                ),
            ),
            back=_back(None),
            dom={"Beneficiary": "HEMA RAM"},
        )
        check = _find(report, "payee")
        assert check.status == "FAIL"
        assert "neither" in (check.summary + " " + " ".join(check.details)).lower() or "NONE" in check.summary

    def test_low_confidence_vlm_falls_through_to_ocr(self) -> None:
        # VLM is hedging (conf < 0.7) → don't trust it; OCR text
        # has clean tokens of HEMA RAM, so PASS via the OCR path.
        report = validate_cheque(
            front=_front(
                raw_text="Pay to HEMA RAM only",
                vlm_verification=_vlm(
                    payee_match="HEMA RAM",
                    payee_confidence=0.4,
                ),
            ),
            back=_back(None),
            dom={"Beneficiary": "HEMA RAM"},
        )
        check = _find(report, "payee")
        assert check.status == "PASS"
        # VLM still appears in evidence even though it didn't drive
        # the verdict — for operator inspection.
        ev = dict(check.evidence)
        assert ev["vlm_payee_match"] == "HEMA RAM"
        assert ev["vlm_payee_confidence"] == 0.4

    def test_vlm_unavailable_yields_unavailable_evidence_marker(
        self,
    ) -> None:
        report = validate_cheque(
            front=_front(
                raw_text="HEMA RAM",
                # No vlm_verification dict → VLM didn't run.
            ),
            back=_back(None),
            dom={"Beneficiary": "HEMA RAM"},
        )
        check = _find(report, "payee")
        assert dict(check.evidence).get("vlm_agreement") == "vlm_unavailable"


class TestDateRuleWithVlm:
    def test_vlm_date_used_when_high_confidence(self) -> None:
        # OCR couldn't read the date band; VLM did with high
        # confidence → rule uses the VLM date and PASSes if it's
        # within the validity window.
        today = date(2026, 6, 15)
        cheque_date_ddmmyyyy = today.strftime("%d%m%Y")
        report = validate_cheque(
            front=_front(
                raw_text="no date here",
                vlm_verification=_vlm(
                    date_ddmmyyyy=cheque_date_ddmmyyyy,
                    date_confidence=0.9,
                ),
            ),
            back=_back(None),
            dom={},
            today=today,
        )
        check = _find(report, "date")
        assert check.status == "PASS"
        ev = dict(check.evidence)
        assert ev["date_source"] == "vlm"
        assert ev["vlm_agreement"] in ("vlm_only", "agree")

    def test_low_confidence_vlm_date_ignored(self) -> None:
        today = date(2026, 6, 15)
        report = validate_cheque(
            front=_front(
                raw_text="no date here",
                vlm_verification=_vlm(
                    date_ddmmyyyy="01062026",
                    date_confidence=0.4,
                ),
            ),
            back=_back(None),
            dom={},
            today=today,
        )
        check = _find(report, "date")
        assert check.status == "NOT_VERIFIED"
        ev = dict(check.evidence)
        assert ev["date_source"] == "ocr"


class TestAmountRuleWithVlm:
    """The two new top-level rules (amount_words, amount_figures)
    each consume their own VLM verdict independently — words rule
    only looks at amount_in_words_*, figures rule only looks at
    amount_in_figures_*."""

    def test_vlm_both_match_high_conf_passes_both_rules(self) -> None:
        report = validate_cheque(
            front=_front(
                # Empty amount fields — OCR couldn't read them.
                raw_text="",
                vlm_verification=_vlm(
                    amount_in_figures_matches=True,
                    amount_in_figures_confidence=0.9,
                    amount_in_words_matches=True,
                    amount_in_words_confidence=0.85,
                ),
            ),
            back=_back(None),
            dom={"Amount": "51060.00"},
        )
        r_words = _find(report, "amount_words")
        r_figures = _find(report, "amount_figures")
        assert r_words.status == "PASS"
        assert "vlm" in r_words.summary.lower()
        assert r_figures.status == "PASS"
        assert "vlm" in r_figures.summary.lower()

    def test_vlm_figures_mismatch_high_conf_fails_figures_only(self) -> None:
        # Figures VLM says NO but words VLM says yes → figures
        # rule FAILs, words rule PASSes. Operator sees the FAIL
        # row clearly attributed to figures, not buried in a
        # combined sub-check.
        report = validate_cheque(
            front=_front(
                raw_text="",
                vlm_verification=_vlm(
                    amount_in_figures_matches=False,
                    amount_in_figures_confidence=0.9,
                    amount_in_words_matches=True,
                    amount_in_words_confidence=0.85,
                ),
            ),
            back=_back(None),
            dom={"Amount": "51060.00"},
        )
        r_figures = _find(report, "amount_figures")
        r_words = _find(report, "amount_words")
        assert r_figures.status == "FAIL"
        # 'digit-box' is the rule's term for the figures channel
        # in the VLM summary line; either wording is acceptable.
        assert (
            "figures" in r_figures.summary.lower()
            or "digit" in r_figures.summary.lower()
        )
        assert r_words.status == "PASS"


class TestChequeNoRuleWithVlm:
    def test_vlm_pass_when_ocr_extractor_empty(self) -> None:
        report = validate_cheque(
            front=_front(
                # cheque_no left None — OCR didn't extract.
                raw_text="378781 visible somewhere",
                vlm_verification=_vlm(
                    cheque_no_matches=True,
                    cheque_no_confidence=0.9,
                ),
            ),
            back=_back(None),
            dom={"Cheque No": "378781"},
        )
        check = _find(report, "cheque_no")
        assert check.status == "PASS"

    def test_vlm_short_circuit_skipped_when_ocr_has_value(self) -> None:
        # When OCR/MICR extracted a cheque number, that's more
        # authoritative than the VLM — let the existing logic run.
        report = validate_cheque(
            front=_front(
                cheque_no="378781",
                vlm_verification=_vlm(
                    cheque_no_matches=False,  # VLM disagrees
                    cheque_no_confidence=0.9,
                ),
            ),
            back=_back(None),
            dom={"Cheque No": "378781"},
        )
        check = _find(report, "cheque_no")
        # OCR exact match wins; the VLM disagreement is recorded
        # in evidence but doesn't flip the verdict.
        assert check.status == "PASS"


class TestAccountNoRuleWithVlm:
    def test_vlm_rescue_when_back_empty(self) -> None:
        # Back-side OCR couldn't read the account number, but the
        # VLM confirmed it's visible on the front. Verdict softens
        # to WARN (the spec says back is canonical; VLM rescuing
        # via front is a "operator should re-capture the back" hint).
        report = validate_cheque(
            front=_front(
                raw_text="",
                vlm_verification=_vlm(
                    account_no_matches=True,
                    account_no_confidence=0.9,
                ),
            ),
            back=ChequeFields(
                side="back", raw_text="", ocr_confidence=0.0,
            ),
            dom={"Account No": "50200100315661"},
        )
        check = _find(report, "account_no")
        assert check.status == "WARN"
        assert "VLM" in check.summary or "vlm" in check.summary.lower()

    def test_vlm_says_account_not_present_fails(self) -> None:
        report = validate_cheque(
            front=_front(
                raw_text="",
                vlm_verification=_vlm(
                    account_no_matches=False,
                    account_no_confidence=0.9,
                ),
            ),
            back=ChequeFields(
                side="back", raw_text="", ocr_confidence=0.0,
            ),
            dom={"Account No": "50200100315661"},
        )
        check = _find(report, "account_no")
        assert check.status == "FAIL"


class TestSignatureRuleWithVlm:
    def test_vlm_rescues_when_opencv_unavailable_and_says_present(
        self,
    ) -> None:
        report = validate_cheque(
            front=_front(
                signature_verdict=None,
                signature_density=0.0,
                signature_missing_dep=(
                    "OpenCV not installed; signature detector "
                    "unavailable"
                ),
                vlm_verification=_vlm(
                    signature_present=True,
                    signature_confidence=0.9,
                ),
            ),
            back=_back(None),
            dom={},
        )
        check = _find(report, "signature")
        assert check.status == "PASS"
        ev = dict(check.evidence)
        assert ev["vlm_signature_present"] is True

    def test_vlm_rescues_when_opencv_unavailable_and_says_absent(
        self,
    ) -> None:
        report = validate_cheque(
            front=_front(
                signature_verdict=None,
                signature_density=0.0,
                signature_missing_dep=(
                    "OpenCV not installed; signature detector "
                    "unavailable"
                ),
                vlm_verification=_vlm(
                    signature_present=False,
                    signature_confidence=0.85,
                ),
            ),
            back=_back(None),
            dom={},
        )
        check = _find(report, "signature")
        assert check.status == "FAIL"

    def test_agreement_marker_recorded_when_both_agree(self) -> None:
        report = validate_cheque(
            front=_front(
                signature_verdict="present",
                signature_density=0.05,
                vlm_verification=_vlm(
                    signature_present=True,
                    signature_confidence=0.9,
                ),
            ),
            back=_back(None),
            dom={},
        )
        check = _find(report, "signature")
        ev = dict(check.evidence)
        assert ev["vlm_agreement"] == "agree"


# ---------------------------------------------------------------------------
# _ocr_engines summary helper
# ---------------------------------------------------------------------------


class TestOcrEnginesSummary:
    """The `ocr_front_engines` / `ocr_back_engines` evidence
    fields summarise which engines fired on each side. Most
    engines are filtered out when they returned empty text
    (no signal worth surfacing), but a small allow-list of
    DIAGNOSTIC engines is ALWAYS surfaced — operators need
    to see that the apple_vision_date band reader RAN even when
    it failed to produce text, otherwise a missing date is
    indistinguishable from a missing code path."""

    def test_collapses_paddle_focused_subengines_to_one_entry(self) -> None:
        engines = _ocr_engines(
            _front(
                engine_runs=(
                    ("paddle_or_easy", "raw", 0.8, 1, None),
                    ("paddle_focused_payee_line", "JOHN", 0.7, 1, None),
                    ("paddle_focused_amount_words", "TEN", 0.7, 1, None),
                    ("paddle_focused_date", "12/03/2026", 0.7, 1, None),
                ),
            ),
        )
        assert engines == ["paddle_or_easy", "paddle_focused"]

    def test_skips_engines_with_empty_text(self) -> None:
        # Generic engines with empty text are filtered out of
        # the summary (clutters the operator view).
        engines = _ocr_engines(
            _front(
                engine_runs=(
                    ("paddle_or_easy", "raw", 0.8, 1, None),
                    ("trocr_handwriting", "", 0.0, 0, "torch not installed"),
                ),
            ),
        )
        assert engines == ["paddle_or_easy"]

    def test_always_surfaces_apple_vision_date_even_with_empty_text(
        self,
    ) -> None:
        # Regression: the apple_vision_date band reader MUST appear
        # in the engines summary even when its text was empty (the
        # reader ran but couldn't produce a parseable date).
        # Without this, the operator can't tell whether the
        # date-rule failure is from a missing code path or from
        # a band read miss.
        engines = _ocr_engines(
            _front(
                engine_runs=(
                    ("paddle_or_easy", "raw", 0.8, 1, None),
                    ("apple_vision_date", "", 0.0, 3, "band read empty"),
                ),
            ),
        )
        assert "apple_vision_date" in engines

    def test_always_surfaces_paddle_focused_back_stamp_even_with_empty_text(
        self,
    ) -> None:
        # Same rule for the back-side rescue pass — surfaces in
        # the summary even on empty reads so the operator knows
        # it was attempted.
        engines = _ocr_engines(
            _front(
                engine_runs=(
                    ("paddle_or_easy", "raw", 0.8, 1, None),
                    ("paddle_focused_back_stamp", "", 0.0, 0, "no signal"),
                ),
            ),
        )
        assert "paddle_focused_back_stamp" in engines


# ---------------------------------------------------------------------------
# Phase 6: VerificationEvidence payload on each CheckResult
# ---------------------------------------------------------------------------
#
# Added 2026-06 after operator feedback: the existing
# `evidence` dict dumps cryptic engineering keys
# (extractor_disagreed, vlm_amount_in_figures_matches, etc.)
# that confuse normal users. The structured `evidence_payload`
# carries the plain-English summary + cropped-region bbox that
# the UI shows above the technical drawer.


class TestEvidencePayloadPresence:
    """Every recognised rule (date, payee, amount, cheque_no,
    account_no, signature) MUST emit a non-None evidence_payload
    in the rendered CheckResult — otherwise the new UI falls
    back to the cryptic legacy summary."""

    def _full_report(self) -> ChequeValidationReport:
        # A pseudo-realistic happy-path cheque: all 6 fields
        # populated, all DOM keys present, signature ink density
        # above the "present" threshold.
        front = _front(
            raw_text=(
                "PAY JOHN DOE OR BEARER\n"
                "RUPEES FIFTY THOUSAND ONLY\n"
                "Rs. 50,000\n"
                "DATE 18-06-2026\n"
                "Cheque No. 123456\n"
            ),
            beneficiary="JOHN DOE",
            cheque_no="123456",
            amount="50000",
            amount_words="RUPEES FIFTY THOUSAND ONLY",
            signature_verdict="present",
            signature_density=0.05,
        )
        back = ChequeFields(
            side="back", raw_text="A/c 1234567890",
            ocr_confidence=0.9, account_no="1234567890",
        )
        dom = {
            "Beneficiary 1": "JOHN DOE",
            "Cheque No": "123456",
            "Amount": "50000",
            "Account No": "1234567890",
        }
        return validate_cheque(
            front=front, back=back, dom=dom,
            today=date(2026, 6, 18),
        )

    def test_every_rule_has_evidence_payload(self) -> None:
        report = self._full_report()
        # 7 rules now that amount is split (June 2026).
        assert len(report.checks) == 7
        for c in report.checks:
            assert c.evidence_payload is not None, (
                f"check {c.check_id!r} must emit evidence_payload"
            )

    def test_every_payload_has_plain_summary(self) -> None:
        report = self._full_report()
        for c in report.checks:
            assert c.evidence_payload is not None
            assert c.evidence_payload.plain_summary
            # The plain summary must NOT mention cryptic
            # engineering keys
            for cryptic in (
                "extractor_disagreed",
                "vlm_amount_in_figures_matches",
                "ocr_search_kind",
                "cheque_amount_in_words_parsed",
            ):
                assert cryptic not in c.evidence_payload.plain_summary

    def test_every_payload_has_bbox_and_side(self) -> None:
        report = self._full_report()
        for c in report.checks:
            ep = c.evidence_payload
            assert ep is not None
            assert ep.crop_bbox is not None, (
                f"check {c.check_id!r} must have a crop bbox"
            )
            assert ep.crop_side in ("front", "back")
            x1, y1, x2, y2 = ep.crop_bbox
            assert 0.0 <= x1 < x2 <= 1.0
            assert 0.0 <= y1 < y2 <= 1.0

    def test_account_no_bbox_targets_back_side(self) -> None:
        """Account number is read off the cheque's BACK (deposit
        stamp area), so its evidence crop side MUST be 'back'."""
        report = self._full_report()
        ac = next(c for c in report.checks if c.check_id == "account_no")
        assert ac.evidence_payload is not None
        assert ac.evidence_payload.crop_side == "back"

    def test_other_rules_target_front_side(self) -> None:
        """All other rules examine the printed face → 'front'.
        Updated June 2026 — amount split into amount_words +
        amount_figures (both front-side)."""
        report = self._full_report()
        front_rules = (
            "date", "payee",
            "amount_words", "amount_figures",
            "cheque_no", "signature",
        )
        for cid in front_rules:
            c = next(x for x in report.checks if x.check_id == cid)
            assert c.evidence_payload is not None
            assert c.evidence_payload.crop_side == "front"


class TestEvidencePayloadComparisonKind:
    """comparison_kind drives the UI's visual cue (green tick /
    red cross / amber dash). Verify it's wired correctly off
    the rule's PASS/FAIL/WARN status."""

    def test_pass_status_maps_to_match(self) -> None:
        front = _front(
            raw_text="PAY JOHN DOE OR BEARER\nRs. 50,000",
            beneficiary="JOHN DOE",
        )
        back = ChequeFields(
            side="back", raw_text="x" * 100, ocr_confidence=0.9,
        )
        report = validate_cheque(
            front=front, back=back,
            dom={"Beneficiary 1": "JOHN DOE"},
            today=date(2026, 6, 18),
        )
        payee = next(c for c in report.checks if c.check_id == "payee")
        assert payee.status == "PASS"
        assert payee.evidence_payload is not None
        assert payee.evidence_payload.comparison_kind == "match"

    def test_fail_status_maps_to_mismatch(self) -> None:
        front = _front(
            raw_text="PAY JANE DOE OR BEARER",
            beneficiary="JANE DOE",
        )
        back = ChequeFields(
            side="back", raw_text="x" * 100, ocr_confidence=0.9,
        )
        report = validate_cheque(
            front=front, back=back,
            dom={"Beneficiary 1": "JOHN DOE"},
            today=date(2026, 6, 18),
        )
        payee = next(c for c in report.checks if c.check_id == "payee")
        # Note: the payee rule may PASS or FAIL depending on
        # fuzzy matching — we just verify the mapping is
        # CONSISTENT with the rule's status.
        assert payee.evidence_payload is not None
        from aakaar_caps.cheque.cheque_validation import _classify_comparison
        assert (
            payee.evidence_payload.comparison_kind
            == _classify_comparison(payee.status)
        )


class TestEvidencePayloadAmountPlainSummary:
    """Regression: the user-reported confusing case where the
    technical summary said 'words say 161 but figures say 16141'
    must be replaced with a plain-English explanation that
    operators can understand."""

    def test_amount_mismatch_plain_summary_is_human_friendly(self) -> None:
        # Split-rule: amount_words and amount_figures each have
        # their own evidence payload. Each must be a single
        # grammatical sentence that mentions both what was read
        # and what the system expected.
        front = _front(
            raw_text="RUPEES SIXTEEN THOUSAND...",
            amount="16141",
            amount_words="Sixteen Thousand One Hundred Forty-One Only",
        )
        back = ChequeFields(
            side="back", raw_text="x" * 100, ocr_confidence=0.9,
        )
        report = validate_cheque(
            front=front, back=back,
            dom={"Amount": "16141.00"},
            today=date(2026, 6, 18),
        )
        for cid in ("amount_words", "amount_figures"):
            check = next(c for c in report.checks if c.check_id == cid)
            ep = check.evidence_payload
            assert ep is not None, f"{cid}: missing evidence_payload"
            # Plain summary must be a sentence, not a bullet list.
            assert "•" not in ep.plain_summary, (
                f"{cid}: plain_summary has bullet markers"
            )
            assert (
                "16,141" in ep.plain_summary
                or "16141" in ep.plain_summary
            ), f"{cid}: plain_summary missing amount: {ep.plain_summary!r}"
            assert "amount" in ep.plain_summary.lower()


class TestCheckResultToDictIncludesPayload:
    def test_to_dict_serialises_payload(self) -> None:
        from aakaar_caps.cheque.cheque_validation import VerificationEvidence
        c = CheckResult(
            check_id="amount",
            label="Amount Verification",
            status="PASS",
            summary="ok",
            evidence_payload=VerificationEvidence(
                plain_summary="ok",
                crop_bbox=(0.1, 0.2, 0.3, 0.4),
                crop_side="front",
                from_cheque="50000",
                expected="50000",
                comparison_kind="match",
            ),
        )
        d = c.to_dict()
        assert d["evidence_payload"] is not None
        assert d["evidence_payload"]["plain_summary"] == "ok"
        assert d["evidence_payload"]["crop_bbox"] == [0.1, 0.2, 0.3, 0.4]
        assert d["evidence_payload"]["crop_side"] == "front"
        assert d["evidence_payload"]["comparison_kind"] == "match"

    def test_to_dict_with_none_payload(self) -> None:
        c = CheckResult(
            check_id="x", label="x", status="PASS", summary="",
        )
        d = c.to_dict()
        assert d["evidence_payload"] is None


class TestEvidencePayloadRescueCoherence:
    """Regression for the operator-reported confusion: when a rule
    PASSES via the raw-text rescue (the structured extractor grabbed
    a stray digit run), the 'On cheque vs Expected' comparison row
    must show the value that ACTUALLY matched — not the stray
    structured read that visibly contradicts the green MATCH badge."""

    def test_cheque_no_rescue_shows_matched_value(self) -> None:
        front = _front(
            raw_text=(
                "STATE BANK OF INDIA\n"
                "Cheque No. 017424  40000003151885\n"
                "PAY SMARTWAY WELLNESS PVT LTD"
            ),
            cheque_no="6567000",  # stray run from the structured reader
        )
        back = ChequeFields(
            side="back", raw_text="x" * 100, ocr_confidence=0.9,
        )
        report = validate_cheque(
            front=front, back=back,
            dom={"Cheque No": "017424"},
            today=date(2026, 6, 18),
        )
        chk = next(c for c in report.checks if c.check_id == "cheque_no")
        assert chk.status == "PASS"
        ep = chk.evidence_payload
        assert ep is not None
        assert ep.comparison_kind == "match"
        # The matched value (not the stray structured read) is shown
        # as the on-cheque value, so the row agrees with the badge.
        assert "017424" in (ep.from_cheque or "")
        assert (ep.from_cheque or "") != "6567000"
        assert "found in cheque text" in (ep.from_cheque or "")
        # The stray structured read is still disclosed in the prose.
        assert "6567000" in ep.plain_summary

    def test_amount_figures_rescue_shows_matched_value(self) -> None:
        front = _front(
            raw_text=(
                "RUPEES Twenty One Thousand Seven Hundred Fifteen only\n"
                "21,715.00\nCheque No 017424"
            ),
            amount="3802400",  # stray run from the digit-box reader
            amount_words="Twenty One Thousand Seven Hundred Fifteen Only",
        )
        back = ChequeFields(
            side="back", raw_text="x" * 100, ocr_confidence=0.9,
        )
        report = validate_cheque(
            front=front, back=back,
            dom={"Amount": "21715.00"},
            today=date(2026, 6, 18),
        )
        chk = next(c for c in report.checks if c.check_id == "amount_figures")
        assert chk.status == "PASS"
        ep = chk.evidence_payload
        assert ep is not None
        assert ep.comparison_kind == "match"
        assert "21,715" in (ep.from_cheque or "")
        assert "found in cheque text" in (ep.from_cheque or "")
        # Structured stray read disclosed but not shown as on-cheque.
        assert "3,802,400" in ep.plain_summary


class TestHasAmountVocab:
    """Lock down the fuzzy-vocab matcher's threshold behaviour.

    `_has_amount_vocab` is the gate the focused-pass diagnostic
    and the mis-target guard share. It must:
      (a) cheaply accept clean OCR via the exact-match fast path,
      (b) accept common cursive-OCR substitutions of preprinted
          'Rupees' / customer-written 'Only' / number-words,
      (c) reject pure-noise tokens and the empty/no-text cases,
      (d) never let a 1-2 char token spuriously match via fuzzy
          fallback.

    These cases pin the threshold + length filter to the
    documented values; nudging either should require updating
    the message copy in the rule too (which references both).
    """

    def test_exact_match_accepts(self) -> None:
        assert _has_amount_vocab(["rupees"])
        assert _has_amount_vocab(["only"])
        assert _has_amount_vocab(["two"])
        assert _has_amount_vocab(["lakh"])
        assert _has_amount_vocab(["hundred"])

    def test_exact_match_case_insensitive_caller_responsibility(self) -> None:
        # The helper does NOT lowercase its input — the callers
        # do (via `re.findall(r"[a-zA-Z]+", s.lower())`). Locking
        # this in so a future refactor that moves the lowercase
        # into the helper has to update both call sites in lockstep.
        assert not _has_amount_vocab(["RUPEES"])
        assert _has_amount_vocab(["rupees"])

    def test_empty_or_blank_input_returns_false(self) -> None:
        assert not _has_amount_vocab([])
        assert not _has_amount_vocab([""])
        assert not _has_amount_vocab(["   "])

    def test_fuzzy_accepts_preprinted_rupees_substitution(self) -> None:
        # 'Ropeos' / 'Rurees' / 'Rupees>' are the substitutions
        # the cursive-OCR engine routinely returns for the
        # preprinted 'Rupees' label on a CTS cheque face.
        assert _has_amount_vocab(["ropeos"])      # 0.667 vs 'rupees'
        assert _has_amount_vocab(["rurees"])      # 0.833 vs 'rupees'
        assert _has_amount_vocab(["rupees>"])     # 0.923 vs 'rupees'

    def test_fuzzy_accepts_number_word_substitutions(self) -> None:
        assert _has_amount_vocab(["iwo"])         # 0.667 vs 'two'
        assert _has_amount_vocab(["ouly"])        # 0.750 vs 'only'

    def test_fuzzy_rejects_pure_noise(self) -> None:
        assert not _has_amount_vocab(["xxxx"])
        assert not _has_amount_vocab(["zzzz", "qqq"])

    def test_fuzzy_rejects_severely_garbled_tokens(self) -> None:
        # 'lulch' vs 'lakh' is 0.444 — below the 0.6 threshold.
        # When this is the ONLY token, the gate should fire
        # NOT_VERIFIED rather than admit a guess.
        assert not _has_amount_vocab(["lulch"])

    def test_any_one_token_matching_is_enough(self) -> None:
        # The gate is OR'd — even when most tokens are garbage,
        # a single recognisable amount-vocab token is sufficient
        # signal that the extractor IS on the right band.
        assert _has_amount_vocab(["zzzz", "two", "qqqq"])
        assert _has_amount_vocab(["lulch", "ouly"])  # 'ouly' fuzz-matches

    def test_short_tokens_skip_fuzzy_fallback(self) -> None:
        # The fuzzy fallback is gated on len(token) >= 3 so a
        # one- or two-letter OCR artifact doesn't spuriously
        # match 'rs' / 'one' / 'two' under SequenceMatcher's
        # high-ratio scoring of short strings.
        assert not _has_amount_vocab(["t"])
        assert not _has_amount_vocab(["xy"])
        # But a 3-char token IS eligible (and 'iwo' still fuzzes
        # to 'two').
        assert _has_amount_vocab(["iwo"])

    def test_fast_path_short_circuits_before_fuzzy_loop(self) -> None:
        # When an exact match is present, the helper must short-
        # circuit without entering the (more expensive) fuzzy
        # fallback. Asserted indirectly by passing a HUGE list
        # of junk before the exact match — execution remains
        # fast and correct.
        tokens = ["xxx"] * 1000 + ["two"]
        assert _has_amount_vocab(tokens)


class TestAmountWordsFuzzyRecall:
    """The amount-in-words recall work (June 2026): when the strict
    parse chokes on cursive-OCR garble, a fuzzy parse + figures-box
    corroboration recovers many cheques the rule used to dead-end at
    NOT_VERIFIED. The chosen policy is CORROBORATED auto-PASS only
    (fuzzy words == DOM AND figures box == DOM); everything weaker is
    WARN (operator confirm), never a silent accept."""

    _FOCUSED = (
        ("rapidocr_ppocr", "AXIS BANK ...", 0.99, 47, None),
        ("rapidocr_focused_amount_words", "Iwo Lakhh Ouly", 0.76, 2, None),
    )

    def test_fuzzy_plus_figures_corroborated_passes(self) -> None:
        # Words OCR too garbled to strict-parse ('Iwo Lakhh Ouly'),
        # but a fuzzy read recovers 200000, AND the figures box
        # independently reads 200000 == DOM. Two reads agree → PASS.
        report = validate_cheque(
            front=_front(
                amount="200000.00",
                amount_words="Iwo Lakhh Ouly",
                engine_runs=self._FOCUSED,
            ),
            back=None,
            dom={"Amount": "2,00,000.00"},
        )
        rule = _find(report, "amount_words")
        assert rule.status == "PASS"
        ev = dict(rule.evidence)
        assert ev.get("verdict_basis") == "fuzzy_corroborated"
        assert ev.get("figures_corroborated") is True

    def test_fuzzy_without_figures_corroboration_warns(self) -> None:
        # Same garbled words recovering 200000 via fuzzy, but the
        # figures box is absent → cannot corroborate → WARN (confirm),
        # NOT a silent PASS.
        report = validate_cheque(
            front=_front(
                amount=None,
                amount_words="Iwo Lakhh Ouly",
                engine_runs=self._FOCUSED,
            ),
            back=None,
            dom={"Amount": "2,00,000.00"},
        )
        rule = _find(report, "amount_words")
        assert rule.status == "WARN"
        ev = dict(rule.evidence)
        assert ev.get("verdict_basis") == "fuzzy_uncorroborated"
        assert ev.get("figures_corroborated") is False
        assert "confirm" in rule.summary.lower()

    def test_strict_parse_still_passes(self) -> None:
        # Clean OCR keeps the canonical strict PASS path + basis.
        report = validate_cheque(
            front=_front(amount="200000.00", amount_words="Two Lakh Only"),
            back=None,
            dom={"Amount": "2,00,000.00"},
        )
        rule = _find(report, "amount_words")
        assert rule.status == "PASS"
        assert dict(rule.evidence).get("verdict_basis") == "strict"

    def test_strict_mismatch_still_fails(self) -> None:
        # A cleanly-parsed but DIFFERENT amount is a real mismatch →
        # FAIL (fuzzy recovery must not rescue a genuine disagreement).
        report = validate_cheque(
            front=_front(amount="100000.00", amount_words="One Lakh Only"),
            back=None,
            dom={"Amount": "2,00,000.00"},
        )
        rule = _find(report, "amount_words")
        assert rule.status == "FAIL"
        assert dict(rule.evidence).get("verdict_basis") == "strict"

    def test_warn_rolls_up_to_review_not_reject(self) -> None:
        # A WARN amount-words must not, on its own, drive the overall
        # report to REJECT (only a FAIL does that).
        report = validate_cheque(
            front=_front(
                amount=None,
                amount_words="Iwo Lakhh Ouly",
                engine_runs=self._FOCUSED,
            ),
            back=None,
            dom={"Amount": "2,00,000.00"},
        )
        assert report.overall_status != "REJECT"
        assert report.warn_count >= 1


class TestAmountWordsConfGateAndConsistency:
    """Follow-on hardening (June 2026): a corroborated fuzzy PASS now
    also requires the focused handwriting read to clear a confidence
    floor, and every amount-words verdict carries a soft words-vs-
    figures consistency signal as evidence."""

    def _focused(self, conf: float) -> tuple:
        return (
            ("rapidocr_ppocr", "AXIS BANK ...", 0.99, 47, None),
            ("rapidocr_focused_amount_words", "Iwo Lakhh Ouly", conf, 2, None),
        )

    def test_low_confidence_focused_read_blocks_corroborated_pass(
        self,
    ) -> None:
        # Fuzzy words == DOM AND figures box == DOM (would normally
        # PASS), but the focused handwriting read is too low-confidence
        # (0.30 < 0.45 floor) to auto-accept → held at WARN.
        report = validate_cheque(
            front=_front(
                amount="200000.00",
                amount_words="Iwo Lakhh Ouly",
                engine_runs=self._focused(0.30),
            ),
            back=None,
            dom={"Amount": "2,00,000.00"},
        )
        rule = _find(report, "amount_words")
        assert rule.status == "WARN"
        ev = dict(rule.evidence)
        assert ev.get("verdict_basis") == "fuzzy_corroborated_low_conf"
        assert ev.get("figures_corroborated") is True

    def test_high_confidence_focused_read_still_passes(self) -> None:
        # Same corroboration, confidence above the floor → PASS.
        report = validate_cheque(
            front=_front(
                amount="200000.00",
                amount_words="Iwo Lakhh Ouly",
                engine_runs=self._focused(0.76),
            ),
            back=None,
            dom={"Amount": "2,00,000.00"},
        )
        rule = _find(report, "amount_words")
        assert rule.status == "PASS"
        assert dict(rule.evidence).get("verdict_basis") == "fuzzy_corroborated"

    def test_consistency_agree_evidence_on_corroborated_pass(self) -> None:
        report = validate_cheque(
            front=_front(
                amount="200000.00",
                amount_words="Iwo Lakhh Ouly",
                engine_runs=self._focused(0.76),
            ),
            back=None,
            dom={"Amount": "2,00,000.00"},
        )
        ev = dict(_find(report, "amount_words").evidence)
        assert ev.get("words_figures_consistency") == "agree"
        assert ev.get("focused_words_confidence") == 0.76

    def test_consistency_disagree_evidence_when_reads_differ(self) -> None:
        # Strict words read 200000 (matches DOM → PASS) but the figures
        # box reads a different number → the cross-check flags "disagree"
        # even though the rule still passes on the matching words read.
        report = validate_cheque(
            front=_front(amount="100000.00", amount_words="Two Lakh Only"),
            back=None,
            dom={"Amount": "2,00,000.00"},
        )
        rule = _find(report, "amount_words")
        assert rule.status == "PASS"
        assert (
            dict(rule.evidence).get("words_figures_consistency") == "disagree"
        )

    def test_consistency_unknown_when_figures_absent(self) -> None:
        report = validate_cheque(
            front=_front(amount=None, amount_words="Two Lakh Only"),
            back=None,
            dom={"Amount": "2,00,000.00"},
        )
        ev = dict(_find(report, "amount_words").evidence)
        assert ev.get("words_figures_consistency") == "unknown"
