"""Unit tests for aakar.services.words_to_number.

We exercise the Indian English number system (lakh / crore) end
to end because that's why we built this module — the standard
`word2number` PyPI package only understands US/UK English.
"""

from decimal import Decimal

import pytest

from aakaar_caps.cheque.words_to_number import (
    decimal_to_words,
    expected_token_coverage,
    figures_to_decimal,
    words_to_decimal,
)


class TestWordsToDecimalBasics:
    """The base cases first — these have to be rock-solid before
    we trust the parser on real-cheque noise."""

    def test_simple_unit(self) -> None:
        assert words_to_decimal("five") == Decimal("5")

    def test_compound_below_100(self) -> None:
        assert words_to_decimal("twenty five") == Decimal("25")
        assert words_to_decimal("twenty-five") == Decimal("25")
        assert words_to_decimal("ninety nine") == Decimal("99")

    def test_hundreds(self) -> None:
        assert words_to_decimal("five hundred") == Decimal("500")
        assert words_to_decimal("two hundred and fifty") == Decimal("250")

    def test_thousands(self) -> None:
        assert words_to_decimal("fifty one thousand sixty") == Decimal("51060")
        assert (
            words_to_decimal("five thousand two hundred and fifty")
            == Decimal("5250")
        )


class TestIndianScales:
    """The actual reason this module exists."""

    def test_one_lakh(self) -> None:
        # Most common Indian cheque amount form.
        assert words_to_decimal("one lakh") == Decimal("100000")

    def test_one_lakh_twenty_five_thousand(self) -> None:
        # Real-world cheque from the fixture set —
        # "One Lakh Twenty Five Thousand Only".
        assert (
            words_to_decimal("one lakh twenty five thousand only")
            == Decimal("125000")
        )

    def test_lakh_spelling_variants(self) -> None:
        # 'lac' and 'lacs' are legitimate alternate spellings.
        assert words_to_decimal("one lac") == Decimal("100000")
        assert words_to_decimal("five lakhs") == Decimal("500000")

    def test_crore(self) -> None:
        assert words_to_decimal("three crore") == Decimal("30000000")
        assert (
            words_to_decimal("three crore twenty lakh")
            == Decimal("32000000")
        )

    def test_crore_lakh_thousand_combo(self) -> None:
        # 1,23,45,678 in lakhs-comma notation = 12,345,678
        assert (
            words_to_decimal(
                "one crore twenty three lakh forty five thousand "
                "six hundred and seventy eight"
            )
            == Decimal("12345678")
        )


class TestPaiseHandling:
    """Paise (the fractional rupee unit) can show up in two
    standard forms on a cheque: 'and X paise' or 'X/100'."""

    def test_paise_word_form(self) -> None:
        assert (
            words_to_decimal("fifty thousand and fifty paise only")
            == Decimal("50000.50")
        )

    def test_paise_fraction_form(self) -> None:
        # "Ten Thousand and 50/100 Only" — the bank's printed form.
        assert (
            words_to_decimal("ten thousand and 50/100 only")
            == Decimal("10000.50")
        )

    def test_paise_zero_does_not_add_fraction(self) -> None:
        # No paise → integer-valued Decimal (no trailing '.00').
        result = words_to_decimal("five hundred only")
        assert result == Decimal("500")
        # …and the string repr stays clean (no '.00') so equality
        # against `Decimal('500')` from figures_to_decimal works.
        assert str(result) == "500"


class TestNoiseTolerance:
    """OCR adds noise. We need to silently absorb the common
    cases without failing the whole parse."""

    def test_rupees_prefix_and_only_suffix_dropped(self) -> None:
        assert (
            words_to_decimal("Rupees Fifty One Thousand Sixty Only")
            == Decimal("51060")
        )

    def test_case_insensitive(self) -> None:
        assert words_to_decimal("FIFTY") == Decimal("50")
        assert words_to_decimal("Fifty") == Decimal("50")

    def test_punctuation_inside_run(self) -> None:
        # Cheque writers sometimes use commas mid-amount.
        assert (
            words_to_decimal("One Lakh, Twenty Five Thousand Only")
            == Decimal("125000")
        )

    def test_unknown_word_is_skipped_not_fatal(self) -> None:
        # Smudged 'sii' between known tokens should be skipped.
        assert (
            words_to_decimal("five hundred sii fifty")
            == Decimal("550")
        )

    def test_common_misspellings(self) -> None:
        # 'fourty' is an extremely common UK misspelling.
        assert words_to_decimal("fourty") == Decimal("40")

    def test_empty_or_garbage_returns_none(self) -> None:
        assert words_to_decimal(None) is None
        assert words_to_decimal("") is None
        assert words_to_decimal("   ") is None
        assert words_to_decimal("xx yy zz") is None


class TestFiguresToDecimal:
    """The amount-in-figures side. Must handle Indian lakhs
    grouping (1,25,000 not 125,000) and the trailing '/-' box
    terminator."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("51,060.00", Decimal("51060.00")),
            ("1,25,000.50", Decimal("125000.50")),
            ("₹ 1,00,000/-", Decimal("100000")),
            ("Rs. 500/-", Decimal("500")),
            ("Rs 500/-", Decimal("500")),
            ("INR 1234", Decimal("1234")),
            ("500", Decimal("500")),
        ],
    )
    def test_common_formats(self, text: str, expected: Decimal) -> None:
        assert figures_to_decimal(text) == expected

    def test_empty_returns_none(self) -> None:
        assert figures_to_decimal(None) is None
        assert figures_to_decimal("") is None
        assert figures_to_decimal("   ") is None

    def test_garbage_returns_none(self) -> None:
        assert figures_to_decimal("not a number") is None


class TestDecimalToWords:
    """The inverse path — given the DOM/system amount as a number,
    render the canonical Indian English words form so the cheque
    validator can show the operator what the handwritten line is
    expected to read, and fuzzy-compare against the OCR'd words
    when the strict numeric parser can't pin down the read."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            (0, "Zero"),
            (1, "One"),
            (7, "Seven"),
            (19, "Nineteen"),
            (20, "Twenty"),
            (25, "Twenty Five"),
            (99, "Ninety Nine"),
            (100, "One Hundred"),
            (105, "One Hundred Five"),
            (999, "Nine Hundred Ninety Nine"),
            (1000, "One Thousand"),
            (1234, "One Thousand Two Hundred Thirty Four"),
            (51060, "Fifty One Thousand Sixty"),
            (100000, "One Lakh"),
            (125000, "One Lakh Twenty Five Thousand"),
            (190000, "One Lakh Ninety Thousand"),
            (1000000, "Ten Lakh"),
            (10000000, "One Crore"),
            (32000000, "Three Crore Twenty Lakh"),
            (
                12345678,
                "One Crore Twenty Three Lakh Forty Five Thousand "
                "Six Hundred Seventy Eight",
            ),
        ],
    )
    def test_integer_rupees(self, value: int, expected: str) -> None:
        assert decimal_to_words(value) == expected

    def test_accepts_decimal_and_str_inputs(self) -> None:
        # Same semantic value via different input types.
        assert decimal_to_words(Decimal("500")) == "Five Hundred"
        assert decimal_to_words("500") == "Five Hundred"
        assert decimal_to_words("500.00") == "Five Hundred"

    @pytest.mark.parametrize(
        "value,expected",
        [
            (Decimal("0.50"),  "Fifty Paise"),
            (Decimal("1.05"),  "One And Five Paise"),
            (Decimal("1500.50"),
             "One Thousand Five Hundred And Fifty Paise"),
            (Decimal("190000.25"),
             "One Lakh Ninety Thousand And Twenty Five Paise"),
        ],
    )
    def test_paise(self, value: Decimal, expected: str) -> None:
        assert decimal_to_words(value) == expected

    @pytest.mark.parametrize(
        "value,expected",
        [
            (Decimal("500"),       "Rupees Five Hundred Only"),
            (Decimal("190000"),    "Rupees One Lakh Ninety Thousand Only"),
            (
                Decimal("1500.50"),
                "Rupees One Thousand Five Hundred And Fifty Paise Only",
            ),
            (Decimal("0.50"),      "Rupees Fifty Paise Only"),
            # Zero amount is unusual but the rule still has to
            # render SOMETHING — the wrapper form makes that
            # explicit for the operator.
            (0,                    "Rupees Zero Only"),
        ],
    )
    def test_wrapper_form_matches_cheque_writer_convention(
        self, value: object, expected: str,
    ) -> None:
        assert decimal_to_words(
            value,  # type: ignore[arg-type]
            with_rupees_wrapper=True,
        ) == expected

    def test_rounds_to_paise_precision(self) -> None:
        # ROUND_HALF_UP: 1.005 rounds to 1.01 (one and one paise),
        # not banker-rounded to 1.00. Operators read amounts the
        # 'obvious' way, not the banker's way.
        assert decimal_to_words(Decimal("1.005")) == "One And One Paise"

    @pytest.mark.parametrize(
        "bad",
        [
            None,
            "",
            "not a number",
            float("nan"),
            float("inf"),
            Decimal("-1"),
            -100,
        ],
    )
    def test_rejects_bad_input(self, bad: object) -> None:
        assert decimal_to_words(bad) is None  # type: ignore[arg-type]


class TestDecimalToWordsRoundTrip:
    """End-to-end: numeric value → words → numeric value MUST be
    a fixed point for every realistic cheque amount, otherwise
    the validator can't trust the fuzzy fallback's word-form
    expectation.
    """

    @pytest.mark.parametrize(
        "value",
        [
            0, 1, 7, 25, 100, 105, 999, 1000, 1234, 51060,
            100000, 125000, 190000, 1_000_000, 10_000_000, 32_000_000,
            12_345_678,
        ],
    )
    def test_integer_round_trip(self, value: int) -> None:
        words = decimal_to_words(value)
        assert words is not None
        assert words_to_decimal(words) == Decimal(value)

    @pytest.mark.parametrize(
        "value",
        [
            Decimal("0.50"), Decimal("1500.50"),
            Decimal("190000.25"), Decimal("99999.99"),
        ],
    )
    def test_paise_round_trip(self, value: Decimal) -> None:
        words = decimal_to_words(value)
        assert words is not None
        assert words_to_decimal(words) == value


class TestCrossSideConsistency:
    """The real test: the rule 3 amount-internal check compares
    `words_to_decimal(words)` against `figures_to_decimal(figures)`.
    Both sides MUST round-trip to the same Decimal for the
    common cheque cases."""

    @pytest.mark.parametrize(
        "words,figures",
        [
            ("Rupees Five Hundred Only", "500"),
            ("Rupees Five Hundred Only", "500.00"),
            ("Rupees Five Hundred Only", "Rs. 500/-"),
            ("Fifty One Thousand Sixty Only", "51,060.00"),
            ("One Lakh Twenty Five Thousand Only", "1,25,000.00"),
            ("One Lakh Twenty Five Thousand Only", "125000"),
        ],
    )
    def test_words_equal_figures(self, words: str, figures: str) -> None:
        w = words_to_decimal(words)
        f = figures_to_decimal(figures)
        assert w is not None and f is not None
        assert w == f, (
            f"Round-trip mismatch: words={words!r} → {w}, "
            f"figures={figures!r} → {f}"
        )


class TestFuzzyWordsToDecimal:
    """Fuzzy mode snaps cursive-OCR garble onto the nearest closed-vocab
    number word before parsing, so the amount-in-words rule recovers
    value (or partial value) the strict parser can't see — WITHOUT
    smearing a genuinely different number onto the expected answer."""

    def test_fuzzy_recovers_mild_garble(self) -> None:
        # 'Iwo' is a classic cursive-OCR read of 'Two'. Strict parsing
        # drops it (unknown token); fuzzy snaps it back.
        assert words_to_decimal("Iwo thousand") is None or words_to_decimal(
            "Iwo thousand"
        ) != Decimal("2000")
        assert words_to_decimal("Iwo thousand", fuzzy=True) == Decimal("2000")

    def test_fuzzy_leaves_clean_input_identical(self) -> None:
        # Fuzzy mode must be a no-op on already-clean text.
        for s in (
            "Rupees Fifty One Thousand Sixty Only",
            "One Lakh Twenty Five Thousand Only",
            "Five Hundred Only",
        ):
            assert words_to_decimal(s, fuzzy=True) == words_to_decimal(s)

    def test_fuzzy_does_not_smear_a_real_numeral_swap(self) -> None:
        # The whole reason char-similarity can't drive the verdict: a
        # cheque that genuinely says NINETY must not collapse onto a
        # DOM-expected TWENTY. Per-token classification keeps them apart.
        assert words_to_decimal("Ninety Thousand Only", fuzzy=True) == Decimal(
            "90000"
        )
        assert words_to_decimal("Twenty Thousand Only", fuzzy=True) == Decimal(
            "20000"
        )

    def test_fuzzy_rejects_pure_garbage(self) -> None:
        assert words_to_decimal("xxxx yyyy zzzz", fuzzy=True) is None
        assert words_to_decimal("", fuzzy=True) is None
        assert words_to_decimal(None, fuzzy=True) is None

    def test_fuzzy_drops_garbled_wrapper_words(self) -> None:
        # 'Ropeos' (Rupees) and 'Ouly' (Only) carry no value and must
        # snap to the drop set, never onto a number word.
        assert words_to_decimal("Ropeos Two Lakh Ouly", fuzzy=True) == Decimal(
            "200000"
        )

    def test_fuzzy_recovers_garbled_paise_word(self) -> None:
        # The paise WORD itself is mangled ('paisc'); fuzzy mode snaps it
        # back to 'paise' so the .50 fractional part is recovered.
        assert words_to_decimal(
            "Five Hundred and Fifty paisc only", fuzzy=True,
        ) == Decimal("500.50")
        # Strict mode (no snap) misreads it: with the paise anchor lost,
        # 'fifty' folds into the rupee side as 550 — the wrong amount.
        assert words_to_decimal(
            "Five Hundred and Fifty paisc only",
        ) == Decimal("550")

    def test_fuzzy_recovers_garbled_paise_number_and_word(self) -> None:
        # Both the paise number ('Fifly') and word ('paisc') garbled.
        assert words_to_decimal(
            "Five Hundred Fifly paisc only", fuzzy=True,
        ) == Decimal("500.50")

    def test_fuzzy_paise_leaves_clean_fraction_identical(self) -> None:
        for s in (
            "One Lakh Twenty Five Thousand and 50 Paise",
            "Five Hundred and 50/100 Only",
        ):
            assert words_to_decimal(s, fuzzy=True) == words_to_decimal(s)


class TestExpectedTokenCoverage:
    """Expected-guided coverage: how much of the DOM-derived expected
    words is recognisable on the OCR'd line. Drives the WARN ('likely
    right, confirm') outcome when no numeric value can be parsed."""

    def test_full_match_is_one(self) -> None:
        exp = decimal_to_words(200000)  # 'Two Lakh'
        assert expected_token_coverage("Two Lakh", exp) == 1.0

    def test_partial_garble_is_partial(self) -> None:
        # 'Iwo' ≈ 'Two' (matched), 'lulch' ≈ 'Lakh' too garbled to
        # snap (missed) → 1 of 2 expected value tokens = 0.5.
        exp = decimal_to_words(200000)
        cov = expected_token_coverage("Ropeos Iwo lulch Ouly", exp)
        assert cov == pytest.approx(0.5, abs=1e-6)

    def test_unrelated_amount_is_low(self) -> None:
        exp = decimal_to_words(200000)  # expecting 'Two Lakh'
        assert expected_token_coverage("Ninety Thousand", exp) == 0.0

    def test_empty_inputs_are_zero(self) -> None:
        assert expected_token_coverage("", "Two Lakh") == 0.0
        assert expected_token_coverage("Two Lakh", "") == 0.0
        assert expected_token_coverage(None, None) == 0.0
