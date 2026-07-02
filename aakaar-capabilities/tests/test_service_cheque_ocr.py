"""Unit tests for aakar.services.cheque_ocr's field extractors.

We intentionally DO NOT exercise the OCR engine here (no tesseract
binary, no PIL decode). The field extractors are pure-Python regex
walks over an OCR'd text blob, so feeding them representative noisy
text gives full coverage with zero env dependency. The capability
test (`test_capability_cts_uat_read_cheques.py`) covers the
end-to-end "bytes in, dict out" surface.

What we're protecting:
  - The Pay / Cheque-No / Amount / Account-No regexes pick the
    right value out of typical multi-line cheque OCR output.
  - The MICR-line preference for Cheque No (prefer the bottom-most
    digit run) wins over a column-misaligned earlier hit.
  - Amount-in-figures prefers labelled tokens (Rs./₹/INR) over a
    bare number.
  - Account-no rejects too-short / too-long digit runs that aren't
    realistic accounts.
"""

from __future__ import annotations

from aakaar_caps.cheque.cheque_ocr import (
    ChequeFields,
    _digits_only,
    _extract_back_fields,
    _extract_front_fields,
)


def _front(text: str) -> ChequeFields:
    return _extract_front_fields(text)


def _back(text: str) -> ChequeFields:
    return _extract_back_fields(text)


# ---------- front: beneficiary --------------------------------------------


def test_beneficiary_extracted_from_pay_line() -> None:
    text = """\
HDFC BANK
Pay  ACME CORPORATION PVT LTD ........
Rupees Ten Thousand Only
Rs. 10,000.00
123456
"""
    out = _front(text)
    assert out.beneficiary == "ACME CORPORATION PVT LTD"


def test_beneficiary_strips_inline_amount() -> None:
    text = "Pay JOHN DOE                        Rs. 5,500.00"
    out = _front(text)
    assert out.beneficiary == "JOHN DOE"


def test_beneficiary_handles_pay_to_the_order_of() -> None:
    text = "Pay to the order of MARY SMITH"
    out = _front(text)
    assert out.beneficiary == "MARY SMITH"


def test_beneficiary_missing_returns_none() -> None:
    text = "Some random OCR junk\n12345"
    assert _front(text).beneficiary is None


def test_beneficiary_extracted_from_line_per_region_format() -> None:
    """REGRESSION GUARD: Apple Vision returns each detected text
    region as its own line. So instead of 'PAY ACME CORP', we
    get 'PAY' on one line and 'ACME CORP' on the next. The
    extractor MUST walk forward 1-3 lines from 'PAY' to find
    the name, skipping template boilerplate ('RUPEES', 'VALID',
    'MULTI-CITY', etc.).

    Reproducer is the real SBI cheque Apple Vision text that
    revealed this bug — the extractor previously returned None,
    causing `paddle_focused_payee_line` to fire redundantly
    (~309ms wasted per cheque)."""
    text = """\
State Bank Of India
PAY
SMARTWAY WELLNESS PVT LTD
ET RUPEES ONE LACK FOUR THOUSAND THREE
AND SIXTY ONE ONLY
VALID UPTO 1 CRORE AT NON-HOME BRANCH"""
    assert _front(text).beneficiary == "SMARTWAY WELLNESS PVT LTD"


def test_beneficiary_skips_template_boilerplate_in_line_per_region() -> None:
    """When PAY is followed by template / boilerplate lines
    BEFORE the actual name, the extractor MUST skip past them.
    A bank that puts the disclaimer above the payee field would
    otherwise misread 'MULTI-CITY CHEQUE' as the payee name."""
    text = """\
HDFC BANK
PAY
MULTI-CITY CHEQUE
Payable at All Branches
ACME CORPORATION PVT LTD
Rupees Ten Thousand Only"""
    assert _front(text).beneficiary == "ACME CORPORATION PVT LTD"


def test_beneficiary_line_per_region_rejects_pure_digit_lines() -> None:
    """If the line after PAY is a digit run (cheque number,
    MICR, date), the extractor MUST skip it — those are NOT
    payee names."""
    text = """\
PAY
123456789
ACME CORP"""
    assert _front(text).beneficiary == "ACME CORP"


# ---------- front: cheque no ----------------------------------------------


def test_cheque_no_picks_micr_serial_first_group() -> None:
    """The MICR line is the most reliable occurrence of the cheque
    number. Per the CTS-2010 layout
    ``[cheque_no:6] [city:3][bank:3][branch:3] [account:N] [tc:2]``
    the cheque serial is the FIRST 6-digit group — NOT the last
    digit run. (Operator-reported June 2026: the AKOLA JANATA
    cheque's MICR line '008064 444364452 001258 11' must yield
    serial '008064', and the date-band fragment '162026' picked
    up by the old last-token heuristic was wrong.)"""
    text = """\
HDFC BANK     900001
Pay ACME CORP
Rupees Ten Thousand Only
Rs. 10,000.00
123456 110240017 000123
"""
    # MICR-line FIRST group is the cheque serial under CTS layout.
    out = _front(text)
    assert out.cheque_no == "123456"


def test_cheque_no_micr_serial_beats_date_fragment() -> None:
    """Regression for the AKOLA JANATA cheque — the date band
    ('21/06/2026') leaves a 6-digit fragment '162026' in the body
    OCR that the old last-token heuristic grabbed. The MICR line
    must win and yield the true serial '008064'."""
    text = """\
THE AKOLA JANATA COMMERCIAL CO-OPERATIVE BANK LTD
Date 1 06 2026
Pay Smarlway Wellness Pvt Ltd
Rupees Eighteen Thousand Six Hundred Sixty Eight Only 18668
162026
008064 444364452 001258 11
"""
    out = _front(text)
    assert out.cheque_no == "008064"


def test_cheque_no_missing_returns_none() -> None:
    assert _front("Pay X\nRs. 1.00").cheque_no is None


def test_cheque_no_ignores_7_digit_account_fragment() -> None:
    """Operator-reported June 2026 (IDBI / Smartway cheque): the
    MICR strip read garbled so the full-face OCR never captured a
    clean MICR line. The body text carried a misread 7-digit
    account fragment '6567000'. CTS serials are ALWAYS 6 digits,
    so the extractor must NOT return the 7-digit run — it must fall
    through to the true 6-digit serial '017424'."""
    text = """\
IDBI BANK
Pay Smartway Wellness Pvt Ltd
Rupees Twenty One Thousand Seven Hundred Fifteen Only
Rs. 21715.00
A/C No 6567000
017424
"""
    assert _front(text).cheque_no == "017424"


def test_cheque_no_anchor_relative_rescues_split_micr_row() -> None:
    """When the MICR row is split so step-1's strict single-line gate
    (3+ groups / 14+ digits on ONE line) fails, the 9-digit
    city-bank-branch anchor still pins the serial as the 6-digit
    run to its immediate left."""
    text = """\
IDBI BANK
Pay ACME CORP
017424 534259502
156700 13
"""
    assert _front(text).cheque_no == "017424"


# ---------- front: amount in figures --------------------------------------


def test_amount_prefers_labelled_token() -> None:
    text = """\
Pay JOHN DOE
Rupees Ten Thousand Only
Rs. 10,000.00
123456
9999
"""
    out = _front(text)
    assert out.amount == "10,000.00"


def test_amount_picks_decimal_over_bare_int() -> None:
    text = "Rupees Five Hundred Only\nRs. 500 / Rs. 500.00"
    out = _front(text)
    assert out.amount == "500.00"


def test_amount_currency_glyph_supported() -> None:
    text = "Pay X ₹ 1,234.56"
    out = _front(text)
    assert out.amount == "1,234.56"


def test_amount_missing_returns_none() -> None:
    assert _front("Pay X\nRupees nothing").amount is None


def test_amount_rejects_ddmmyyyy_date_box() -> None:
    """REGRESSION: the cheque date is printed in DDMMYYYY boxes
    (top-right) and OCRs as a single 8-digit run plus a literal
    'DDMMYYYY' label. That run (e.g. '21062026' = 21 Jun 2026) must
    NOT be returned as the amount-in-figures. Reproduces the SBI
    cheque where the date leaked into the amount field."""
    text = "21062026\nDDMMYYYY\nPAY SMARTWAY WELLNESS\nRupees Forty Seven"
    assert _front(text).amount is None


def test_amount_reads_indian_equals_decimal_box() -> None:
    """The handwritten amount box uses '=' as the decimal point on
    most CTS cheques: '47605=00' means ₹47,605.00. The extractor must
    parse it (normalised to '47605.00')."""
    assert _front("Pay X\n47605=00").amount == "47605.00"
    assert _front("Pay X\nRs. 12,345=50").amount == "12,345.50"


def test_amount_keeps_8_digit_amount_that_is_not_a_date() -> None:
    """An 8-digit run that is NOT a plausible DDMMYYYY date (month
    '00') is a real amount and must survive the date filter."""
    assert _front("Pay X\n21000000=00").amount == "21000000.00"


def test_amount_decorated_only_mode() -> None:
    """The focused amount-box pass calls `_find_amount_in_figures` with
    decorated_only=True: it must return a value ONLY for structurally
    decorated amounts ('=' / '/-' / decimal / comma / currency marker),
    and None for a bare digit run (which could be stray ink in the
    cropped box)."""
    from aakaar_caps.cheque.cheque_ocr import _find_amount_in_figures

    assert _find_amount_in_figures(["47605=00"], decorated_only=True) == "47605.00"
    assert _find_amount_in_figures(["18668/-"], decorated_only=True) == "18668"
    assert _find_amount_in_figures(["Rs. 250000"], decorated_only=True) == "250000"
    # Bare run → not trusted in decorated-only mode.
    assert _find_amount_in_figures(["18098"], decorated_only=True) is None


def test_amount_ignores_cts_form_code() -> None:
    """REGRESSION (Akola cheque): the pre-printed CTS form code in the
    margin ('DDIPL-CTS 2010') must NOT be picked as the amount — its
    trailing '2010' is the CTS-2010 spec year. The handwritten amount
    box (read with noise as '189981') should win instead."""
    text = "Pay X\nDDIPL-CTS 2010\nRupees Eighteen Thousand\n189981"
    assert _front(text).amount == "189981"


def test_amount_keeps_six_digit_bare_amount() -> None:
    """A bare 6-digit run IS a plausible amount (₹1,00,000-9,99,999)
    and must survive the last-ditch tier when no decorated token and
    no meta/MICR context is present."""
    assert _front("Pay X\n189981").amount == "189981"


def test_amount_ignores_postal_pin_address_line() -> None:
    """A 6-digit PIN code on a postal-address line (parenthesised
    2-letter state code) must not be mistaken for the amount."""
    text = "Pay X\nTEHS-NARNAULMCHINDERGARH(HR)123001\nRupees nothing"
    assert _front(text).amount is None


def test_amount_rejects_micr_clearband_fragment() -> None:
    """The MICR clear band OCRs with stray quote/colon delimiters
    around a long digit run ('\"678303\"123002055:00020830'). Those
    fragments must never be picked as the amount — better None."""
    text = '"678303"123002055:00020830\n21062026\nDDMMYYYY'
    assert _front(text).amount is None


# ---------- front: amount in words ----------------------------------------


def test_amount_in_words_extracted_and_truncated_at_only() -> None:
    text = "Pay X\nRupees Ten Thousand Five Hundred Only blah blah"
    out = _front(text)
    assert out.amount_words == "Ten Thousand Five Hundred Only"


# ---- amount-in-words: 1-line layouts -----------------------------------

class TestAmountInWordsSingleLine:
    """Layout A: amount and 'Only' on the SAME line as 'Rupees'.
    The most common case — Paddle/EasyOCR joins adjacent text
    regions into one long line."""

    def test_single_line_with_inline_rupees_and_only(self) -> None:
        text = "Pay X\nRupees Ten Thousand Only"
        assert _front(text).amount_words == "Ten Thousand Only"

    def test_single_line_indian_lakh_lac_notation(self) -> None:
        """Indian cheques use 'Lac' / 'Lakh' / 'Lakhs' for
        100,000 units. All three MUST be recognised."""
        for variant in ("One Lac", "One Lakh", "One Lakhs"):
            text = f"Pay X\nRupees {variant} Fifty Thousand Only"
            out = _front(text)
            assert out.amount_words is not None, variant
            assert variant in out.amount_words, variant
            assert "Only" in out.amount_words, variant

    def test_single_line_with_crore_unit(self) -> None:
        text = "Pay X\nRupees Two Crore Fifty Lakh Only"
        out = _front(text)
        assert out.amount_words == "Two Crore Fifty Lakh Only"

    def test_single_line_with_OCR_misread_rupes(self) -> None:
        """OCR sometimes drops a letter — 'Rupess' / 'Rupes'.
        The extractor MUST accept these variants too."""
        for variant in ("Rupess", "Rupes", "Rupee"):
            text = f"Pay X\n{variant} Five Thousand Only"
            assert _front(text).amount_words == "Five Thousand Only", variant

    def test_single_line_truncates_trailing_junk(self) -> None:
        """Anything after 'Only' on the same line MUST be
        truncated — OCR sometimes appends signature noise to
        the amount line."""
        text = (
            "Pay X\nRupees Ten Thousand Only random signature noise "
            "PROPRIETOR junk"
        )
        out = _front(text)
        assert out.amount_words == "Ten Thousand Only"


# ---- amount-in-words: 2-line layouts -----------------------------------

class TestAmountInWordsTwoLines:
    """Layout B/C: amount-in-words SPANS TWO LINES on the physical
    cheque. The handwriting wraps to the second printed line.
    All variations MUST be reconstructed into one canonical
    string with 'Only' as the terminator.

    This is what the user reported — 'amount in words can be in
    1 line and also in 2 lines sometimes' — and is the case where
    both PaddleOCR and Apple Vision return the band as 2 separate
    text regions.
    """

    def test_two_line_inline_rupees_split_by_handwriting_wrap(
        self,
    ) -> None:
        """Layout B (PaddleOCR / EasyOCR typical):
            Rupees One Lac Four Thousand Three
            Hundred Sixty One Only
        """
        text = (
            "Pay X\n"
            "Rupees One Lac Four Thousand Three\n"
            "Hundred Sixty One Only"
        )
        out = _front(text)
        assert out.amount_words == (
            "One Lac Four Thousand Three Hundred Sixty One Only"
        )

    def test_two_line_rupees_on_own_line(self) -> None:
        """Layout C (Apple Vision line-per-region):
            Rupees
            Ten Thousand Five Hundred Only
        """
        text = "Pay X\nRupees\nTen Thousand Five Hundred Only"
        assert _front(text).amount_words == "Ten Thousand Five Hundred Only"

    def test_two_line_words_split_with_no_rupees_detected(self) -> None:
        """Layout D (faded template):
            One Lac Four Thousand Three
            Hundred Sixty One Only
        The printed 'Rupees' label is lost to faded ink. Strategy
        2 walks backward from 'Only' collecting amount-vocab
        lines."""
        text = (
            "Pay X\n"
            "One Lac Four Thousand Three\n"
            "Hundred Sixty One Only"
        )
        out = _front(text)
        assert out.amount_words is not None
        assert "One Lac" in out.amount_words
        assert "Hundred Sixty One Only" in out.amount_words

    def test_two_line_only_on_own_line(self) -> None:
        """Layout E (rare but real):
            Rupees Ten Thousand Five Hundred
            Only
        """
        text = "Pay X\nRupees Ten Thousand Five Hundred\nOnly"
        out = _front(text)
        assert out.amount_words is not None
        assert "Ten Thousand Five Hundred Only" in out.amount_words

    def test_two_line_with_and_connective(self) -> None:
        """The connective 'AND' is common in 2-line amounts —
            Rupees One Lakh Forty Thousand
            And Five Hundred Sixty Only
        Extractor MUST keep 'And' in the joined string and
        terminate at 'Only'."""
        text = (
            "Pay X\n"
            "Rupees One Lakh Forty Thousand\n"
            "And Five Hundred Sixty Only"
        )
        out = _front(text)
        assert out.amount_words == (
            "One Lakh Forty Thousand And Five Hundred Sixty Only"
        )

    def test_two_line_apple_vision_with_template_prefix(self) -> None:
        """The real SBI cheque Apple Vision case — the printed
        template prefix 'ET' from 'BUDGET RUPEES' merges into
        the first line:
            ET RUPEES ONE LACK FOUR THOUSAND THREE
            AND SIXTY ONE ONLY
        Extractor MUST find Rupees mid-line and walk forward."""
        text = (
            "Pay X\n"
            "ET RUPEES ONE LACK FOUR THOUSAND THREE\n"
            "AND SIXTY ONE ONLY"
        )
        out = _front(text)
        assert out.amount_words == (
            "ONE LACK FOUR THOUSAND THREE AND SIXTY ONE ONLY"
        )

    def test_two_line_continuation_when_line2_is_pure_garbage(
        self,
    ) -> None:
        """Regression: production cheque (AKOLA JANATA, June 2026)
        where the handwritten amount wrapped to a second line that
        OCR rendered as zero-vocab cursive garbage:

            Rupees t Eighekeen Housend Six
            tAerAchModiadu Sixtr Eighd Rr andy sareti

        ('Eighteen Thousand Six Hundred Sixty Eight Rs only')

        Previously the forward-walker broke at the second line
        because `_count_amount_number_words` returned 0 on the
        garbled tokens, so the user only saw 'Eighekeen Housend
        Six' as the captured amount-in-words. The trust-continuation
        branch now treats offset=1 as a legitimate handwriting
        continuation whenever the Rupees-line tail already gave us
        vocab but no 'Only' terminator.
        """
        text = (
            "Pay Smarlway Wellness Pvt. Ltd\n"
            "Rupees t Eighekeen Housend Six\n"
            "tAerAchModiadu Sixtr Eighd Rr andy sareti\n"
            "Current A/c No.:096103301001258"
        )
        out = _front(text)
        # The captured value must include BOTH handwriting lines
        # so the downstream amount-words parser at least has a
        # chance at the trailing 'Eight' / 'Only' tokens.
        assert out.amount_words is not None
        assert "Eighekeen" in out.amount_words
        assert "Sixtr" in out.amount_words

    def test_continuation_does_not_keep_walking_past_offset_1(
        self,
    ) -> None:
        """The trust-continuation only fires at offset=1 — once
        we've already included one zero-vocab line, the next
        zero-vocab line MUST break the walk. Otherwise a single
        misread on the wrap line would silently slurp the rest of
        the cheque (date / account / signature) into amount-words.
        """
        text = (
            "Rupees Ten Thousand\n"
            "garblegarble five\n"
            "moregarble moregarble"
        )
        out = _front(text)
        # 'five' is recognised vocab so line 2 is included via
        # has_vocab. Line 3 has zero vocab and we're at offset=2 —
        # trust_continuation requires offset=1 only — so the walk
        # MUST stop and line 3 must NOT appear in the output.
        assert out.amount_words is not None
        assert "moregarble" not in out.amount_words


# ---- amount-in-words: defensive (don't slurp garbage) -----------------

class TestAmountInWordsDefensive:
    """Regression guards against the extractor reaching too far
    forward/backward and including non-amount text in the field
    (signature block, MICR, address, etc.)."""

    def test_stops_at_signature_block(self) -> None:
        """When 'PROPRIETOR' / 'AUTHORISED SIGNATORY' appears
        after the amount line, the extractor MUST stop there.
        Walking into the signature block produces garbage."""
        text = (
            "Pay X\n"
            "Rupees Ten Thousand\n"
            "PROPRIETOR\n"
            "Authorised Signatory"
        )
        out = _front(text)
        assert out.amount_words is not None
        # 'PROPRIETOR' and 'Signatory' MUST NOT appear
        assert "PROPRIETOR" not in out.amount_words
        assert "Signatory" not in out.amount_words

    def test_stops_at_ifsc_label(self) -> None:
        """When 'IFSC' / 'MICR' appears after the amount line,
        the extractor MUST stop. Without this guard, the
        amount-words slurped IFSC codes on faded cheques where
        no 'Only' terminator was detected."""
        text = (
            "Pay X\n"
            "Rupees Five Thousand\n"
            "IFSC HDFC0001234\n"
            "MICR 400240001"
        )
        out = _front(text)
        assert out.amount_words is not None
        assert "IFSC" not in out.amount_words
        assert "MICR" not in out.amount_words

    def test_stops_when_next_line_has_no_vocab(self) -> None:
        """After 'Rupees X' with no Only terminator, the
        extractor MUST stop at the first line that has no
        amount-vocab words (signature line, date, etc.)."""
        text = (
            "Pay X\n"
            "Rupees Ten Thousand\n"
            "John Doe Signature\n"
            "ANOTHER UNRELATED LINE"
        )
        out = _front(text)
        # Should NOT contain 'Signature' or 'UNRELATED'
        if out.amount_words is not None:
            assert "Signature" not in out.amount_words
            assert "UNRELATED" not in out.amount_words

    def test_only_alone_in_text_is_not_amount(self) -> None:
        """A stray 'Only' with no amount-vocab nearby MUST NOT
        false-trigger as the amount-words. Some cheques have
        'For office use only' or 'Cash only' template text."""
        text = "Pay X\nFor Cash Use Only"
        out = _front(text)
        # 'cash' isn't a number-word and 'use' isn't either — so
        # vocab count for "For Cash Use Only" is just 1 ('only') —
        # below the threshold of 2.
        assert out.amount_words is None

    def test_rupees_in_header_line_skipped(self) -> None:
        """If 'RUPEES' appears in a header / boundary line
        (e.g. 'TOTAL RUPEES IFSC ...'), the extractor MUST skip
        that occurrence and find the real one. Otherwise we'd
        return header noise as the amount."""
        text = (
            "TOTAL RUPEES IFSC 1234567\n"
            "Pay X\n"
            "Rupees Five Thousand Only"
        )
        out = _front(text)
        assert out.amount_words == "Five Thousand Only"

    def test_valid_for_three_months_only_is_not_amount(self) -> None:
        """REAL PRODUCTION REGRESSION: Indian Bank cheque had the
        printed template stamp 'VALID FOR THREE MONTHS ONLY'
        somewhere on the face. Strategy 2 (anchor on 'Only')
        previously picked this up because 'THREE' is in the
        amount-vocab and 'ONLY' is the terminator — total of
        2 vocab tokens met the threshold and the whole template
        string got returned as `amount_words`, parsing to '3'.

        With 'VALID FOR' and 'MONTHS ONLY' added to the boundary
        list, Strategy 2 MUST skip this line entirely and either
        return the real amount-words from elsewhere or return
        None (which the validator surfaces as 'no amount-words
        extracted' rather than a wrong amount of 3)."""
        text = "Pay X\nVALID FOR THREE MONTHS ONLY"
        out = _front(text)
        assert out.amount_words is None, (
            f"template stamp leaked as amount: {out.amount_words!r}"
        )

    def test_valid_for_three_months_with_real_amount_above(self) -> None:
        """Same Indian Bank cheque but WITH the real handwritten
        amount-words preserved earlier in the OCR text.

        The extractor MUST pick the real amount-words
        ('Sintlees thousand three nundeed a eiglty eight' — the
        OCR misread of 'Sixteen thousand three hundred and
        eighty eight') and NOT the template stamp."""
        text = (
            "PAY Smartway Wellness pvt. ltd\n"
            "RUPEEST Sintlees thousand three nundeed a eiglty eight\n"
            "STeT Rt OCC\n"
            "Alo No. 7728170782\n"
            "VALID FOR THREE MONTHS ONLY\n"
            "21062026 DDMMY\n"
            "OR BEARER\n"
            "16,388)"
        )
        out = _front(text)
        # MUST pick the real amount-words (Strategy 1 anchors on
        # the permissive RUPEEST → consumes the 'T' → tail starts
        # at 'Sintlees thousand three nundeed a eiglty eight').
        assert out.amount_words is not None
        assert "thousand" in out.amount_words.lower()
        assert "three" in out.amount_words.lower()
        # MUST NOT pick the template stamp.
        assert "VALID" not in out.amount_words.upper()
        assert "MONTHS" not in out.amount_words.upper()

    def test_valio_upto_template_skipped(self) -> None:
        """SBI cheque variant: 'VALIO UPTOR 1 CRORE' is the
        printed 'VALID UPTO Rs.1 CRORE' template (OCR misread).
        Must NOT be picked as amount-words."""
        text = "Pay X\nRupees Ten Thousand Only\nVALIO UPTOR 1 CRORE"
        out = _front(text)
        # Strategy 1 finds the real amount; the boundary check
        # stops the forward walk at VALIO UPTOR so it doesn't
        # get appended.
        assert out.amount_words == "Ten Thousand Only"


class TestRupeesMarkerPermissive:
    """Regression guards for the permissive `_RUPEES_MARKER_RE`
    pattern that handles real-world OCR-mangled 'Rupees' labels.
    """

    def test_rupeest_with_trailing_t(self) -> None:
        """REAL PRODUCTION CASE (Indian Bank cheque):
        OCR returned 'RUPEEST' where the 'T' is from the
        adjacent STAMP-template glyph getting merged into the
        same token. The regex MUST consume the trailing 'T' so
        the tail starts at the real amount-words."""
        text = "PAY ACME\nRUPEEST Sintlees thousand three nundeed Only"
        out = _front(text)
        assert out.amount_words is not None
        assert out.amount_words.startswith("Sintlees")

    def test_rupess_misread(self) -> None:
        """OCR drops one 'e' and doubles the 's' → 'Rupess'."""
        text = "Pay X\nRupess Ten Thousand Only"
        out = _front(text)
        assert out.amount_words == "Ten Thousand Only"

    def test_rupes_misread(self) -> None:
        """OCR drops one 'e' → 'Rupes' (single s)."""
        text = "Pay X\nRupes Twenty Thousand Only"
        out = _front(text)
        assert out.amount_words == "Twenty Thousand Only"

    def test_rupee_no_trailing_s(self) -> None:
        """OCR drops the trailing s → 'Rupee'."""
        text = "Pay X\nRupee Five Thousand Only"
        out = _front(text)
        assert out.amount_words == "Five Thousand Only"

    def test_rupeess_double_s(self) -> None:
        """OCR doubles the s → 'Rupeess'."""
        text = "Pay X\nRupeess Five Thousand Only"
        out = _front(text)
        assert out.amount_words == "Five Thousand Only"

    def test_rs_short_form(self) -> None:
        """Short form 'Rs' followed by amount-in-words."""
        text = "Pay X\nRs Ten Thousand Only"
        out = _front(text)
        assert out.amount_words == "Ten Thousand Only"

    def test_rupees_followed_by_colon(self) -> None:
        """'Rupees:' (colon delimiter) — the colon MUST be
        stripped from the tail."""
        text = "Pay X\nRupees: Five Thousand Only"
        out = _front(text)
        assert out.amount_words == "Five Thousand Only"


def test_amount_in_words_spanning_multiple_lines() -> None:
    """REGRESSION GUARD: Apple Vision returns each text region as
    its own line. So 'RUPEES ONE LACK FOUR THOUSAND THREE AND
    SIXTY ONE ONLY' may arrive split across 2 lines. The
    extractor MUST walk forward until it finds 'Only' and
    concatenate the lines.

    Reproducer is the real SBI cheque Apple Vision text that
    previously returned just 'ONE LACK FOUR THOUSAND THREE' —
    missing 'AND SIXTY ONE ONLY' from the next line, causing
    `paddle_focused_amount_words` to fire redundantly."""
    text = """\
PAY ACME
ET RUPEES ONE LACK FOUR THOUSAND THREE
AND SIXTY ONE ONLY
VALID UPTO 1 CRORE"""
    out = _front(text)
    assert out.amount_words == (
        "ONE LACK FOUR THOUSAND THREE AND SIXTY ONE ONLY"
    )


def test_amount_in_words_does_not_slurp_unrelated_lines() -> None:
    """When walking forward from RUPEES, we MUST stop at the
    next 'Rupees' marker OR after 3 lines. Otherwise the
    extractor would slurp the signature block / template text
    into the amount-words band."""
    text = """\
PAY ACME
RUPEES TEN THOUSAND
random line one
random line two
random line three
random line four
SHOULD NOT BE INCLUDED"""
    # No 'Only' terminator and 3 lines walked — extractor
    # MUST return the partial amount, not the whole tail.
    out = _front(text)
    assert out.amount_words is not None
    assert "SHOULD NOT BE INCLUDED" not in out.amount_words


def test_amount_in_figures_word_boundary_rejects_stray_r() -> None:
    """REGRESSION GUARD: the old regex `(?:Rs?\\.?|...)` had no
    word-boundary anchor, so a stray 'R' in template text
    ('UPTOR 1 CRORE' / 'PROPRIETOR 100') matched as "Rs" and
    produced bogus tiny amounts like '1' / '100'. The fixed
    regex MUST require '\\bRs' (literal Rs, word-bounded)."""
    # 'UPTOR' contains 'R' followed by ' 1' — old regex matched.
    text = "Pay X\nVALID UPTOR 1 CRORE AT NON-HOME BRANCH"
    out = _front(text)
    assert out.amount is None, (
        f"expected None (no real amount in text); got {out.amount!r} "
        f"— the regex is matching template-text 'R' as 'Rs'"
    )


def test_amount_in_figures_word_boundary_keeps_real_rs() -> None:
    """The word-boundary fix MUST NOT regress on the common
    case of a real 'Rs.' amount marker."""
    text = "Pay X\nRs. 12,345.00"
    out = _front(text)
    assert out.amount == "12,345.00"


def test_amount_skips_bank_branch_code_in_parens() -> None:
    """REGRESSION GUARD: SBI cheque header has '(06715) -KASARAGOD'.
    Without filtering, the bare-digit fallback picked '06715' as
    the amount. The extractor MUST skip lines with parens-wrapped
    digit codes."""
    text = """\
State Bank Of India
(06715) -KASARAGOD
KALLARACKAL BUILDING
PAY ACME"""
    out = _front(text)
    assert out.amount is None, (
        f"expected None (no real amount); got {out.amount!r} — "
        f"bank branch code (06715) is leaking as amount"
    )


def test_amount_skips_contact_info_lines() -> None:
    """Cheque headers often have 'Tel: 4994-220082' and 'IFS Code:
    SBIN0006715' — these contain digit runs that aren't amounts.
    Extractor MUST skip lines with Tel / IFS / MICR labels."""
    text = """\
PAY ACME
Tel: 4994-220082
IFS Code : SBIN0006715
BSR 470001"""
    out = _front(text)
    assert out.amount is None


def test_amount_skips_micr_row() -> None:
    """The MICR strip has '1976532 671002002 000563 29' — without
    filtering, bare-digit fallback picked one of those as the
    amount. The MICR-line heuristic MUST skip it."""
    text = """\
PAY ACME
1976532 671002002 000563 29"""
    out = _front(text)
    assert out.amount is None, (
        f"expected None (no real amount); got {out.amount!r} — "
        f"MICR digits are leaking as amount"
    )


def test_beneficiary_rejects_not_on_image_watermark() -> None:
    """CTS UAT test cheques have 'NOT ON IMAGE' printed as a
    watermark. The extractor MUST treat it as boilerplate and
    skip past it to the real payee."""
    text = """\
PAY
NOT ON IMAGE
JAYSHIVSAKTHI TRADERS"""
    out = _front(text)
    assert out.beneficiary == "JAYSHIVSAKTHI TRADERS"


def test_beneficiary_rejects_short_noise_with_alpha() -> None:
    """REGRESSION GUARD: a noise crop like 'I1 <8' has alpha
    chars but no 3+ consecutive letters. The extractor MUST
    reject it as not-a-name. Without this tightening, faded
    cheques where paddle_focused_payee_line returned garbage
    were overriding the field with junk."""
    text = """\
PAY
I1 <8
9191
Is"""
    out = _front(text)
    assert out.beneficiary is None, (
        f"expected None (all candidates are noise); got "
        f"{out.beneficiary!r}"
    )


def test_beneficiary_rejects_bank_header() -> None:
    """Bank identifier lines (HDFC BANK LTD, etc.) MUST NOT be
    picked as payee. Without filtering, on cheques where the
    real payee field was empty, the extractor walked past PAY
    and landed on the bank header."""
    text = """\
PAY
HDFCBANK LTD.
NAVRANGPURA BRANCH
ALICE BOB PVT LTD"""
    out = _front(text)
    assert out.beneficiary == "ALICE BOB PVT LTD"


# ---------- back: account no ----------------------------------------------


def test_account_no_with_label() -> None:
    text = "A/C No: 50100123456789"
    out = _back(text)
    assert out.account_no == "50100123456789"


def test_account_no_without_label_picks_longest_run() -> None:
    text = "Endorsement\n50100123456789\nDate 19-JUN-2026"
    out = _back(text)
    assert out.account_no == "50100123456789"


def test_account_no_rejects_short_digit_runs() -> None:
    # 6-digit number is below the 9-digit floor for an account number.
    assert _back("ref 12345").account_no is None


def test_account_no_rejects_overlong_runs() -> None:
    # 25-digit blob isn't a plausible Indian bank account number —
    # finder should drop it instead of surfacing OCR garbage.
    text = "noise 1234567890123456789012345 noise"
    assert _back(text).account_no is None


# ---------- combined field result -----------------------------------------


def test_front_returns_typed_result_with_raw_text() -> None:
    text = "Pay X\nRs. 1,000.00\n223344"
    out = _front(text)
    assert out.side == "front"
    assert out.raw_text == text
    # Empty-string sentinel: front-side result never has account_no.
    assert out.account_no is None


def test_back_result_carries_only_account() -> None:
    out = _back("A/C 50100100200300")
    assert out.side == "back"
    assert out.beneficiary is None
    assert out.cheque_no is None
    assert out.amount is None
    assert out.account_no == "50100100200300"


# ---------- validate_dom_presence -----------------------------------------
#
# Operator-side cross-check: does every value the bank's bottom-panel
# UI surfaced actually appear on the cheque image OCR text? We feed
# canned (DOM, front_text, back_text) tuples and assert the per-field
# match decisions — exact / digits / fuzzy / not-present.


from aakaar_caps.cheque.cheque_ocr import validate_dom_presence


def _result(dom: dict[str, str], front: str = "", back: str = "") -> dict:
    return validate_dom_presence(dom=dom, front_text=front, back_text=back)


def test_presence_exact_match_on_front() -> None:
    out = _result(
        {"Beneficiary": "JAYSHIVSAKTHI TRADERS"},
        front="Pay JAYSHIVSAKTHI TRADERS Rs. 1000",
    )
    f = out["fields"]["beneficiary"]
    assert f["present"] is True
    assert f["match_kind"] == "exact"


def test_presence_digit_only_match_for_numeric_field() -> None:
    """DOM 50200100315661 / OCR text contains spaced version
    '5020 0100 3156 61' — digit-only strategy should match."""
    out = _result(
        {"Account No": "50200100315661"},
        back="MICR  5020 0100 3156 61  Endorsed",
    )
    f = out["fields"]["account_no"]
    assert f["present"] is True
    assert f["match_kind"] == "digits"


def test_presence_ocr_letter_tolerant_digit_match() -> None:
    """DOM cheque number `143144` rendered by OCR as the literal
    string `"143iL4"'` — production regression. The digit-only
    tier strips to `1434` and doesn't match. The OCR-tolerant
    tier substitutes letter↔digit confusions (i→1, L→4) inside
    digit-bearing tokens and finds the match. Match kind is
    'digits_ocr_tolerant' so operators can audit when the
    rescue path fired."""
    out = _result(
        {"Cheque No": "143144"},
        front='Pay X Rupees Fifty Thousand Only "143iL4"\' 38c068',
    )
    f = out["fields"]["cheque_no"]
    assert f["present"] is True
    assert f["match_kind"] == "digits_ocr_tolerant"
    assert f["similarity"] == 1.0


def test_presence_ocr_letter_tolerance_does_not_match_word_only_tokens() -> None:
    """The OCR-tolerant tier MUST NOT invent digits from plain
    English words. Tokens like 'BOB', 'OIL', 'ill' contain
    no digits, so the substitution table is never applied to
    them — otherwise 'OIL' would falsely match a DOM cheque
    number `010` etc."""
    out = _result(
        {"Cheque No": "010"},
        front="Pay BOB OIL CO Rupees Five Hundred Only legal text",
    )
    f = out["fields"]["cheque_no"]
    # The tolerant tier requires DOM digits >= 4 anyway; pin
    # the broader contract by using a 6-digit DOM value that
    # WOULD be reachable via letter substitution if we ran it
    # on word-only tokens. Verify it doesn't match.
    out2 = _result(
        {"Cheque No": "808080"},  # would match 'BOBOBO' under naive subst
        front="BOB BOB BOB and other plain words",
    )
    assert out2["fields"]["cheque_no"]["present"] is False


def test_presence_fuzzy_match_with_one_letter_swap() -> None:
    """DOM 'JAY SHIVSAKTHI TRADERS' vs OCR 'JAY SHIVSAKTI TRADERS'
    (missing H in SAKTI). Should match via fuzzy substring."""
    out = _result(
        {"Beneficiary": "JAY SHIVSAKTHI TRADERS"},
        front="Pay JAY SHIVSAKTI TRADERS Only",
    )
    f = out["fields"]["beneficiary"]
    assert f["present"] is True
    assert f["match_kind"] == "fuzzy"
    assert f["similarity"] >= 0.85


def test_presence_not_found_when_value_absent() -> None:
    """DOM amount 51,060.00 vs OCR amount Rs. 99,999.99 — totally
    different number, must NOT match."""
    out = _result(
        {"Amount": "51,060.00"},
        front="Rupees Ninety Nine Thousand Rs. 99,999.99",
    )
    f = out["fields"]["amount"]
    assert f["present"] is False


def test_presence_short_cheque_no_not_matched_inside_longer_run() -> None:
    """Field-reported false positive: a short, zero-padded cheque
    number `000017` must NOT be reported present just because its
    digits happen to appear INSIDE a longer digit run (here the
    account/MICR band `40000017xxxx`). Matching one number inside
    another is never a real field match — the rescue would
    otherwise override the real structured read with a bogus PASS."""
    out = _result(
        {"Cheque No": "000017"},
        front="Pay X  A/C 4000001785213  012398  Rupees only",
    )
    f = out["fields"]["cheque_no"]
    assert f["present"] is False, (
        f"000017 wrongly matched inside a longer digit run: {f}"
    )


def test_presence_short_cheque_no_matched_as_standalone_token() -> None:
    """The flip side: when the same `000017` genuinely appears as a
    STANDALONE number on the cheque (bounded by non-digits, OCR
    spacing tolerated), it must still be reported present."""
    out = _result(
        {"Cheque No": "000017"},
        front="MICR  000017  40000003151885  Rupees only",
    )
    f = out["fields"]["cheque_no"]
    assert f["present"] is True
    assert f["match_kind"] in {"exact", "digits"}


def test_presence_falls_back_to_combined_when_primary_side_misses() -> None:
    """Account No is primarily searched on the BACK side. But if the
    bank prints it on the front (some cheque types do), the cap
    should still find it via the combined-corpus fallback."""
    out = _result(
        {"Account No": "50200100315661"},
        front="A/C No. 50200100315661 Rs. 1000",
        back="Deposit signature",
    )
    f = out["fields"]["account_no"]
    assert f["present"] is True
    # Note explains we matched on the combined corpus (not the
    # primary back-side text) so the operator knows.
    assert f["note"] and "combined" in f["note"]


def test_presence_summary_counts_match_attempts() -> None:
    """`matched` / `total` should reflect (a) how many DOM fields had
    a value at all and (b) how many of those were found in the OCR
    text — used by the UI's "3/4 verified" header chip."""
    out = _result(
        {
            "Beneficiary": "ACME",          # present
            "Amount": "1,000.00",            # present
            "Cheque No": "999999",           # absent
            # Other fields absent from DOM → not counted.
        },
        front="Pay ACME Rupees One Thousand Rs. 1,000.00",
    )
    # We had 3 DOM values; 2 were found (Beneficiary + Amount).
    assert out["total"] == 3
    assert out["matched"] == 2


def test_presence_handles_empty_ocr_gracefully() -> None:
    """Empty OCR text (e.g. capture failed) → every field reports
    not present with a 'OCR text empty' note. Must not raise."""
    out = _result(
        {"Beneficiary": "ACME", "Amount": "1,000.00"},
        front="", back="",
    )
    for canonical in ("beneficiary", "amount"):
        f = out["fields"][canonical]
        assert f["present"] is False
        assert f["note"] and "empty" in f["note"].lower()
    assert out["matched"] == 0
    assert out["total"] == 2


# ---------- RapidOCR engine module ----------------------------------------
#
# The cheque pipeline uses a SINGLE OCR engine now: RapidOCR (PP-OCR via
# onnxruntime). We don't exercise the real ONNX models here (the
# `cheque-ocr` extra is optional in CI); instead we prove the module is
# importable on a fresh checkout and its entry points are total
# functions that return empty / 0.0 on junk input instead of raising.


def test_rapid_ocr_imports_without_extras_installed() -> None:
    from aakaar_caps.cheque import rapid_ocr  # noqa: PLC0415, F401


def test_rapid_ocr_entry_points_are_total_functions() -> None:
    """On junk bytes (or a host without rapidocr installed) the public
    functions return empty / 0.0 instead of raising — this is what lets
    the capability keep walking the batch when OCR can't run."""
    from aakaar_caps.cheque import rapid_ocr  # noqa: PLC0415

    png = b"\x89PNG\r\n"  # not a real PNG
    text, conf = rapid_ocr.run_ocr_text(png)
    assert text == ""
    assert conf == 0.0
    assert rapid_ocr.run_ocr_detail(png) == []


def test_rapid_ocr_missing_dep_message_is_actionable() -> None:
    """When rapidocr isn't installed the diagnostic MUST tell the
    operator what to install. Skipped when the extra is present."""
    from aakaar_caps.cheque import rapid_ocr  # noqa: PLC0415

    msg = rapid_ocr.missing_dep()
    if msg is None:
        import pytest  # noqa: PLC0415
        pytest.skip("rapidocr installed — no missing-dep message to verify")
    assert "cheque-ocr" in msg
    assert "pip install" in msg


# ---------- extract_fields: single-engine orchestration -------------------
#
# extract_fields() lazily imports rapid_ocr / micr / signature_detector,
# so we monkeypatch those module singletons to drive the pipeline
# deterministically without loading any ONNX model.


def _region(text: str, conf: float = 0.95):
    from aakaar_caps.cheque import rapid_ocr  # noqa: PLC0415
    return rapid_ocr.OcrRegion(
        text=text, confidence=conf, bbox=[[0, 0], [10, 0], [10, 10], [0, 10]],
    )


def _patch_rapid(monkeypatch, regions) -> None:
    from aakaar_caps.cheque import rapid_ocr  # noqa: PLC0415
    monkeypatch.setattr(rapid_ocr, "missing_dep", lambda: None)
    monkeypatch.setattr(rapid_ocr, "run_ocr_detail", lambda _p: list(regions))


def _patch_micr_off(monkeypatch) -> None:
    from aakaar_caps.cheque import micr  # noqa: PLC0415
    monkeypatch.setattr(
        micr, "run_micr_ocr",
        lambda _p, **_k: micr.MicrResult(
            text="", parsed={}, regions=(), variants_tried=(),
        ),
    )


def _patch_sig_off(monkeypatch) -> None:
    from aakaar_caps.cheque import signature_detector as sd  # noqa: PLC0415
    monkeypatch.setattr(
        sd, "detect_signature",
        lambda _p: sd.SignatureResult(missing_dep="(test stub) sig disabled"),
    )


def test_extract_fields_front_basic(monkeypatch) -> None:
    from aakaar_caps.cheque import cheque_ocr  # noqa: PLC0415
    _patch_rapid(monkeypatch, [
        _region("Pay JOHN SMITH"),
        _region("Rupees One Thousand Only"),
        _region("Rs. 1,000.00"),
        _region("123456"),
    ])
    _patch_micr_off(monkeypatch)
    _patch_sig_off(monkeypatch)

    out = cheque_ocr.extract_fields(b"x", side="front")
    assert out.missing_dep is None
    assert out.error is None
    assert out.beneficiary == "JOHN SMITH"
    assert out.amount == "1,000.00"
    engines = [r[0] for r in out.engine_runs]
    assert "rapidocr_ppocr" in engines
    # raw_text is the joined regions.
    assert "JOHN SMITH" in (out.raw_text or "")


def test_extract_fields_front_appends_micr_and_prefers_micr_cheque_no(
    monkeypatch,
) -> None:
    """The MICR strip text must be concatenated into raw_text (so the
    presence validator finds the strip digits) and the MICR-parsed
    cheque_no must win over the body-text guess."""
    from aakaar_caps.cheque import cheque_ocr, micr  # noqa: PLC0415
    _patch_rapid(monkeypatch, [
        _region("Pay JANE DOE"),
        _region("999999"),  # body-text digit run (NOT the real serial)
    ])
    _patch_sig_off(monkeypatch)
    monkeypatch.setattr(
        micr, "run_micr_ocr",
        lambda _p, **_k: micr.MicrResult(
            text="017424 000013 000100 31",
            parsed={
                "cheque_no": "017424", "city": "000",
                "bank": "013", "branch": "000100", "tc": "31",
            },
            regions=(("017424 000013 000100 31", 0.9),),
            variants_tried=("orig",),
        ),
    )

    out = cheque_ocr.extract_fields(b"x", side="front")
    assert out.cheque_no == "017424"           # MICR wins
    assert out.city == "000" and out.tc == "31"
    assert "017424 000013 000100 31" in (out.raw_text or "")
    assert "micr_strip" in [r[0] for r in out.engine_runs]


def test_extract_fields_back_account_no_and_no_micr(monkeypatch) -> None:
    from aakaar_caps.cheque import cheque_ocr  # noqa: PLC0415
    _patch_rapid(monkeypatch, [_region("A/C NO 50100123456789")])

    out = cheque_ocr.extract_fields(
        b"x", side="back", dom={"account_no": "50100123456789"},
    )
    assert out.account_no == "50100123456789"
    engines = [r[0] for r in out.engine_runs]
    assert engines == ["rapidocr_ppocr"]  # no MICR / signature on the back


def test_extract_fields_missing_dep_early_return(monkeypatch) -> None:
    """When RapidOCR can't load, extract_fields returns a result whose
    missing_dep carries the reason and engine_runs has the single
    rapidocr row — it does NOT attempt field extraction."""
    from aakaar_caps.cheque import rapid_ocr, cheque_ocr  # noqa: PLC0415
    monkeypatch.setattr(rapid_ocr, "missing_dep", lambda: "rapidocr not installed")

    def _boom(_p):  # run_ocr_detail must not be called
        raise AssertionError("run_ocr_detail should not run when missing_dep set")
    monkeypatch.setattr(rapid_ocr, "run_ocr_detail", _boom)

    out = cheque_ocr.extract_fields(b"x", side="front")
    assert out.raw_text is None
    assert out.missing_dep == "rapidocr not installed"
    assert [r[0] for r in out.engine_runs] == ["rapidocr_ppocr"]


def test_extract_fields_empty_regions_returns_empty_text(monkeypatch) -> None:
    from aakaar_caps.cheque import cheque_ocr  # noqa: PLC0415
    _patch_rapid(monkeypatch, [])  # engine loads but finds nothing

    out = cheque_ocr.extract_fields(b"x", side="front")
    assert out.raw_text == ""
    assert out.missing_dep is None
    assert [r[0] for r in out.engine_runs] == ["rapidocr_ppocr"]


def test_extract_fields_surfaces_signature_verdict(monkeypatch) -> None:
    from aakaar_caps.cheque import cheque_ocr, signature_detector as sd  # noqa: PLC0415
    _patch_rapid(monkeypatch, [_region("Pay JOHN")])
    _patch_micr_off(monkeypatch)
    monkeypatch.setattr(
        sd, "detect_signature",
        lambda _p: sd.SignatureResult(verdict="present", density=0.12),
    )

    out = cheque_ocr.extract_fields(b"x", side="front")
    assert out.signature_verdict == "present"
    assert out.signature_density == 0.12


def test_extract_fields_engine_runs_carry_elapsed_ms(monkeypatch) -> None:
    from aakaar_caps.cheque import cheque_ocr  # noqa: PLC0415
    _patch_rapid(monkeypatch, [_region("Pay JOHN")])
    _patch_micr_off(monkeypatch)
    _patch_sig_off(monkeypatch)

    out = cheque_ocr.extract_fields(b"x", side="front")
    for run in out.engine_runs:
        assert len(run) == 6              # (name, text, conf, count, missing, ms)
        assert isinstance(run[5], int)    # elapsed_ms slot present


# ---------- _collect_consensus_votes (single engine) ----------------------


def test_collect_consensus_votes_full_page_front() -> None:
    from aakaar_caps.cheque import cheque_ocr  # noqa: PLC0415
    runs = [(
        "rapidocr_ppocr",
        "Pay JOHN SMITH\nRupees One Thousand Only\nRs. 1,000.00\n123456",
        0.9, 4, None, 12,
    )]
    votes = cheque_ocr._collect_consensus_votes(side="front", engine_runs=runs)
    assert "beneficiary" in votes
    assert "amount" in votes
    assert "cheque_no" in votes


def test_collect_consensus_votes_back_account_no() -> None:
    from aakaar_caps.cheque import cheque_ocr  # noqa: PLC0415
    runs = [("rapidocr_ppocr", "A/C NO 50100123456789", 0.9, 1, None, 8)]
    votes = cheque_ocr._collect_consensus_votes(
        side="back", engine_runs=runs, dom_account_hint="50100123456789",
    )
    assert "account_no" in votes
    assert votes["account_no"][0].raw_value == "50100123456789"


def test_collect_consensus_votes_micr_strip_cheque_no() -> None:
    from aakaar_caps.cheque import cheque_ocr  # noqa: PLC0415
    runs = [("micr_strip", "017424 000013 000100 31", 0.9, 1, None, 5)]
    votes = cheque_ocr._collect_consensus_votes(
        side="front", engine_runs=runs, cheque_no_from_micr="017424",
    )
    assert "cheque_no" in votes
    assert votes["cheque_no"][0].raw_value == "017424"


def test_collect_consensus_votes_empty_engine_produces_no_votes() -> None:
    from aakaar_caps.cheque import cheque_ocr  # noqa: PLC0415
    runs = [("rapidocr_ppocr", "", 0.0, 0, None, 3)]
    votes = cheque_ocr._collect_consensus_votes(side="front", engine_runs=runs)
    assert votes == {}


def test_consensus_attached_via_extract_fields(monkeypatch) -> None:
    from aakaar_caps.cheque import cheque_ocr  # noqa: PLC0415
    _patch_rapid(monkeypatch, [
        _region("Pay JOHN SMITH"),
        _region("Rs. 1,000.00"),
        _region("123456"),
    ])
    _patch_micr_off(monkeypatch)
    _patch_sig_off(monkeypatch)

    out = cheque_ocr.extract_fields(b"x", side="front")
    assert out.consensus  # at least one FieldConsensus built
    fields = {c.field_name for c in out.consensus}
    assert "beneficiary" in fields
