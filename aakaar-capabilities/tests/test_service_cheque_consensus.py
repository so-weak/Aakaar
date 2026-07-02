"""Unit tests for aakar.services.cheque_consensus.

Covers:

* Normalizers — name, amount, amount_words, date, digits
* build_consensus_for_field voting math and trust_score
* build_consensus field-order preservation
* Convenience helpers (consensus_values, review_required_fields,
  default_bbox_for_field, make_vote)

Test fixtures use realistic OCR outputs (case mismatches, comma
formatting, the `0`/`O` confusion, etc.) so a regression in the
normalizers shows up as a test failure rather than a subtle drift
in production trust scores.
"""

from __future__ import annotations

import pytest

from aakaar_caps.cheque.cheque_consensus import (
    REVIEW_THRESHOLD,
    FieldConsensus,
    FieldVote,
    build_consensus,
    build_consensus_for_field,
    consensus_values,
    default_bbox_for_field,
    make_vote,
    normalize_amount,
    normalize_amount_words,
    normalize_date,
    normalize_digits,
    normalize_for_field,
    normalize_name,
    review_required_fields,
)


class TestNormalizeName:
    def test_case_fold(self) -> None:
        assert normalize_name("john doe") == normalize_name("JOHN DOE")

    def test_strip_punctuation(self) -> None:
        assert normalize_name("JOHN-DOE, JR.") == "JOHN DOE JR"

    def test_collapse_whitespace(self) -> None:
        assert normalize_name("JOHN    DOE") == "JOHN DOE"

    def test_ocr_digit_letter_fold(self) -> None:
        # 0 -> O, 1 -> I, 5 -> S, 8 -> B
        assert normalize_name("J0HN D0E") == normalize_name("JOHN DOE")
        assert normalize_name("PR1MA") == normalize_name("PRIMA")
        assert normalize_name("5MITH") == normalize_name("SMITH")
        assert normalize_name("RO8ERT") == normalize_name("ROBERT")

    def test_accent_fold(self) -> None:
        assert normalize_name("José") == normalize_name("JOSE")
        assert normalize_name("MÜLLER") == normalize_name("MULLER")

    def test_empty_inputs(self) -> None:
        assert normalize_name(None) == ""
        assert normalize_name("") == ""
        assert normalize_name("   ") == ""

    def test_different_names_stay_distinct(self) -> None:
        assert normalize_name("JOHN DOE") != normalize_name("JANE DOE")


class TestNormalizeAmount:
    def test_strips_punctuation_and_currency(self) -> None:
        assert normalize_amount("1,234") == "1234"
        assert normalize_amount("Rs 1234/-") == "1234"
        assert normalize_amount("₹1,234.00") == "1234"

    def test_drops_trailing_decimal_zeros(self) -> None:
        # ".00" / ",00" trailing should fold so "1234.00" == "1234"
        assert normalize_amount("1234.00") == normalize_amount("1234")
        assert normalize_amount("12,345.00") == normalize_amount("12345")

    def test_preserves_legitimate_amounts(self) -> None:
        # "100" must NOT become "1" via overzealous trailing-zero fold
        assert normalize_amount("100") == "100"
        # The decimal-zero fold only fires for ≥5 digits with a
        # ".00"/",00" suffix in the source
        assert normalize_amount("1000") == "1000"

    def test_empty_inputs(self) -> None:
        assert normalize_amount(None) == ""
        assert normalize_amount("") == ""


class TestNormalizeAmountWords:
    def test_strips_rupees_noise(self) -> None:
        assert normalize_amount_words(
            "Rupees Fifty One Thousand Sixty Only",
        ) == "FIFTY ONE THOUSAND SIXTY"

    def test_strips_conjunctions(self) -> None:
        a = normalize_amount_words(
            "One Lakh Four Thousand And Sixty One",
        )
        b = normalize_amount_words("One Lakh Four Thousand Sixty One")
        assert a == b

    def test_case_fold(self) -> None:
        assert (
            normalize_amount_words("FIFTY")
            == normalize_amount_words("fifty")
            == "FIFTY"
        )

    def test_empty_inputs(self) -> None:
        assert normalize_amount_words(None) == ""
        assert normalize_amount_words("") == ""


class TestNormalizeDate:
    def test_canonical_ddmmyyyy(self) -> None:
        assert normalize_date("21062026") == "21062026"

    def test_various_separators(self) -> None:
        assert (
            normalize_date("21-06-2026")
            == normalize_date("21/06/2026")
            == normalize_date("21.06.2026")
            == "21062026"
        )

    def test_pads_single_digit(self) -> None:
        assert normalize_date("1/6/2026") == "01062026"

    def test_no_date_returns_empty(self) -> None:
        assert normalize_date("hello world") == ""
        assert normalize_date(None) == ""

    def test_embedded_date_in_longer_string(self) -> None:
        # Real-world: OCR text often has the date embedded in a
        # larger phrase. The DDMMYYYY normalizer must find it.
        assert normalize_date("Date: 21-06-2026 sample") == "21062026"


class TestNormalizeDigits:
    def test_digits_only(self) -> None:
        assert normalize_digits("AC-No: 7728170782") == "7728170782"
        assert normalize_digits("50200108341941") == "50200108341941"

    def test_empty_inputs(self) -> None:
        assert normalize_digits(None) == ""
        assert normalize_digits("abc") == ""


class TestNormalizeForField:
    def test_dispatches_to_correct_normalizer(self) -> None:
        assert normalize_for_field("beneficiary", "JOHN DOE") == "JOHN DOE"
        assert normalize_for_field("amount", "1,234") == "1234"
        assert normalize_for_field("date", "21-06-2026") == "21062026"
        assert normalize_for_field("cheque_no", "AB12345") == "12345"
        assert normalize_for_field("account_no", "AC 50200108341941") == "50200108341941"

    def test_unknown_field_falls_back_to_name(self) -> None:
        assert (
            normalize_for_field("future_field", "JOHN DOE")
            == normalize_name("JOHN DOE")
        )


class TestMakeVote:
    def test_normalizes_automatically(self) -> None:
        v = make_vote("apple_vision", "beneficiary", "JOHN DOE", 0.92)
        assert v.engine == "apple_vision"
        assert v.raw_value == "JOHN DOE"
        assert v.normalized_value == "JOHN DOE"
        assert v.confidence == 0.92
        assert v.source_bbox is None

    def test_propagates_bbox(self) -> None:
        bbox = (0.06, 0.18, 0.86, 0.32)
        v = make_vote(
            "apple_vision", "beneficiary", "JOHN DOE", 0.92, bbox,
        )
        assert v.source_bbox == bbox

    def test_handles_none_value(self) -> None:
        v = make_vote("doctr", "amount", None, 0.7)
        assert v.raw_value == ""
        assert v.normalized_value == ""


class TestBuildConsensusForField:
    def test_unanimous_two_voters(self) -> None:
        votes = [
            make_vote("apple_vision", "amount", "1234", 0.9),
            make_vote("easy_ocr", "amount", "1234", 0.8),
        ]
        c = build_consensus_for_field("amount", votes)
        assert c.value == "1234"
        assert c.normalized_value == "1234"
        assert c.winning_vote_count == 2
        # Two unanimous voters → both winner_conf_sum and
        # all_conf_sum are equal → agreement ratio = 1.0; voter
        # scaler = 1.0; trust = 1.0
        assert c.trust_score == 1.0
        assert c.review_reason is None

    def test_single_voter_caps_trust_at_half(self) -> None:
        votes = [make_vote("apple_vision", "amount", "1234", 1.0)]
        c = build_consensus_for_field("amount", votes)
        assert c.value == "1234"
        assert c.winning_vote_count == 1
        # voter_scaler = min(1, 1/2) = 0.5; agreement = 1.0
        assert c.trust_score == 0.5
        # Single voter MUST trigger review
        assert c.review_reason is not None
        assert "only 1 engine" in c.review_reason

    def test_dissent_lowers_trust(self) -> None:
        votes = [
            make_vote("apple_vision", "amount", "1234", 0.9),
            make_vote("easy_ocr", "amount", "1234", 0.8),
            make_vote("doctr", "amount", "1235", 0.7),
        ]
        c = build_consensus_for_field("amount", votes)
        assert c.value == "1234"
        # winner_conf_sum = 1.7; all_conf_sum = 2.4
        # agreement = 1.7/2.4 ≈ 0.708
        # voter_scaler = 1.0 (2 voters)
        # trust ≈ 0.708 → below REVIEW_THRESHOLD (0.85)
        assert c.trust_score < REVIEW_THRESHOLD
        assert c.review_reason is not None
        assert "1235" in c.review_reason

    def test_normalization_groups_ocr_confusion(self) -> None:
        # All three engines saw effectively the same name; one
        # had an OCR digit/letter glitch — they MUST group.
        votes = [
            make_vote("apple_vision", "beneficiary", "JOHN DOE", 0.9),
            make_vote("easy_ocr",     "beneficiary", "John Doe", 0.85),
            make_vote("doctr",        "beneficiary", "J0HN D0E", 0.8),
        ]
        c = build_consensus_for_field("beneficiary", votes)
        assert c.winning_vote_count == 3
        assert c.trust_score == 1.0
        # The raw_value surfaced should be the highest-conf vote's
        # raw form — preserves the cleanest OCR for the operator.
        assert c.value == "JOHN DOE"

    def test_empty_votes_returns_no_engine_consensus(self) -> None:
        c = build_consensus_for_field("amount", [])
        assert c.value is None
        assert c.trust_score == 0.0
        assert c.winning_vote_count == 0
        assert c.review_reason is not None
        assert "no engine" in c.review_reason

    def test_only_empty_normalized_votes(self) -> None:
        # All engines voted but with empty/whitespace values
        votes = [
            make_vote("apple_vision", "amount", "", 0.9),
            make_vote("easy_ocr",     "amount", None, 0.8),
        ]
        c = build_consensus_for_field("amount", votes)
        assert c.value is None
        assert c.winning_vote_count == 0
        assert c.review_reason is not None

    def test_winner_is_highest_conf_sum_not_highest_count(self) -> None:
        # 1 high-conf engine vs 2 low-conf engines on a different
        # value — the high-conf engine should win on conf sum.
        votes = [
            make_vote("apple_vision", "amount", "1234", 0.95),
            make_vote("easy_ocr",     "amount", "1235", 0.30),
            make_vote("doctr",        "amount", "1235", 0.40),
        ]
        c = build_consensus_for_field("amount", votes)
        # winner_conf_sum (1234) = 0.95; alt_sum (1235) = 0.70
        assert c.value == "1234"
        # Only 1 voter on the winner → review required
        assert c.review_reason is not None

    def test_review_reason_lists_dissenters_when_multi_voter_low_trust(
        self,
    ) -> None:
        votes = [
            make_vote("apple_vision", "beneficiary", "JOHN DOE", 0.8),
            make_vote("easy_ocr",     "beneficiary", "JOHN DOE", 0.7),
            make_vote("doctr",        "beneficiary", "JANE DOE", 0.9),
            make_vote("got_ocr2",     "beneficiary", "JOHN SMITH", 0.6),
        ]
        c = build_consensus_for_field("beneficiary", votes)
        assert c.value == "JOHN DOE"
        # winner = 0.8 + 0.7 = 1.5 / all = 3.0 → agreement 0.5
        # → trust 0.5 < 0.85 → review required
        assert c.trust_score < REVIEW_THRESHOLD
        assert c.review_reason is not None
        # Should mention at least one dissenter
        assert (
            "JANE DOE" in c.review_reason
            or "JOHN SMITH" in c.review_reason
        )

    def test_bbox_preserved_in_votes_tuple(self) -> None:
        bbox = (0.06, 0.18, 0.86, 0.32)
        votes = [
            make_vote("apple_vision", "beneficiary", "JOHN DOE", 0.9, bbox),
            make_vote("easy_ocr",     "beneficiary", "JOHN DOE", 0.8, bbox),
        ]
        c = build_consensus_for_field("beneficiary", votes)
        for v in c.votes:
            assert v.source_bbox == bbox

    def test_amount_decimal_zero_fold_groups_votes(self) -> None:
        votes = [
            make_vote("apple_vision", "amount", "16,388.00", 0.9),
            make_vote("easy_ocr",     "amount", "16388",     0.8),
        ]
        c = build_consensus_for_field("amount", votes)
        assert c.winning_vote_count == 2  # they must group


class TestBuildConsensus:
    def test_preserves_canonical_field_order(self) -> None:
        votes_by_field = {
            "amount": [make_vote("apple_vision", "amount", "1234", 0.9)],
            "beneficiary": [
                make_vote("apple_vision", "beneficiary", "JOHN DOE", 0.9),
            ],
            "date": [make_vote("apple_vision", "date", "21062026", 0.9)],
        }
        results = build_consensus(votes_by_field)
        # Canonical order: beneficiary, amount, amount_words, date, ...
        field_order = [r.field_name for r in results]
        assert field_order.index("beneficiary") < field_order.index("amount")
        assert field_order.index("amount") < field_order.index("date")

    def test_unknown_fields_emitted_after_canonical(self) -> None:
        votes_by_field = {
            "custom_field": [make_vote("custom", "custom_field", "X", 0.9)],
            "beneficiary": [
                make_vote("apple_vision", "beneficiary", "JOHN DOE", 0.9),
            ],
        }
        results = build_consensus(votes_by_field)
        field_order = [r.field_name for r in results]
        assert field_order.index("beneficiary") < field_order.index("custom_field")

    def test_empty_input(self) -> None:
        assert build_consensus({}) == ()


class TestConsensusValuesHelper:
    def test_flattens_to_dict(self) -> None:
        results = (
            FieldConsensus(
                field_name="amount",
                value="1234",
                normalized_value="1234",
                trust_score=1.0,
                votes=(),
                winning_vote_count=2,
                review_reason=None,
            ),
            FieldConsensus(
                field_name="beneficiary",
                value=None,
                normalized_value=None,
                trust_score=0.0,
                votes=(),
                winning_vote_count=0,
                review_reason="no votes",
            ),
        )
        flat = consensus_values(results)
        assert flat == {"amount": "1234", "beneficiary": None}


class TestReviewRequiredFields:
    def test_filters_only_review_fields(self) -> None:
        results = (
            FieldConsensus(
                field_name="amount", value="1234",
                normalized_value="1234", trust_score=1.0,
                votes=(), winning_vote_count=2, review_reason=None,
            ),
            FieldConsensus(
                field_name="date", value="21062026",
                normalized_value="21062026", trust_score=0.5,
                votes=(), winning_vote_count=1,
                review_reason="only 1 engine read this field",
            ),
        )
        review = review_required_fields(results)
        assert len(review) == 1
        assert review[0].field_name == "date"


class TestDefaultBboxForField:
    def test_known_fields_have_bboxes(self) -> None:
        assert default_bbox_for_field("beneficiary") is not None
        assert default_bbox_for_field("amount_words") is not None
        assert default_bbox_for_field("amount") is not None
        assert default_bbox_for_field("date") is not None
        assert default_bbox_for_field("cheque_no") is not None

    def test_unknown_field_returns_none(self) -> None:
        assert default_bbox_for_field("not_a_field") is None

    def test_bboxes_are_image_relative(self) -> None:
        # All coords must be in [0.0, 1.0]
        for field_name in ("beneficiary", "amount_words", "amount", "date"):
            bbox = default_bbox_for_field(field_name)
            assert bbox is not None
            x1, y1, x2, y2 = bbox
            assert 0.0 <= x1 < x2 <= 1.0
            assert 0.0 <= y1 < y2 <= 1.0


class TestFieldConsensusToDict:
    def test_serializes_with_votes(self) -> None:
        v = make_vote("apple_vision", "amount", "1234", 0.9, (0.1, 0.2, 0.3, 0.4))
        c = build_consensus_for_field("amount", [v])
        d = c.to_dict()
        assert d["field_name"] == "amount"
        assert d["value"] == "1234"
        assert d["normalized_value"] == "1234"
        assert d["winning_vote_count"] == 1
        assert "review_reason" in d
        assert len(d["votes"]) == 1
        assert d["votes"][0]["engine"] == "apple_vision"
        assert d["votes"][0]["raw_value"] == "1234"
        assert d["votes"][0]["source_bbox"] == [0.1, 0.2, 0.3, 0.4]

    def test_serializes_with_no_bbox(self) -> None:
        v = make_vote("doctr", "amount", "1234", 0.9)
        c = build_consensus_for_field("amount", [v])
        d = c.to_dict()
        assert d["votes"][0]["source_bbox"] is None


@pytest.mark.parametrize(
    "engine_pairs,expected_value,expected_winning_count",
    [
        # 3-of-3 unanimous
        (
            [("a", "JOHN DOE", 0.9), ("b", "JOHN DOE", 0.8),
             ("c", "JOHN DOE", 0.7)],
            "JOHN DOE", 3,
        ),
        # 2-of-3 majority
        (
            [("a", "JOHN DOE", 0.9), ("b", "JOHN DOE", 0.8),
             ("c", "JANE DOE", 0.7)],
            "JOHN DOE", 2,
        ),
        # 1-of-1 single voter
        (
            [("a", "JOHN DOE", 0.9)],
            "JOHN DOE", 1,
        ),
        # 2 engines, 2 different values, higher conf wins
        (
            [("a", "JOHN DOE", 0.9), ("b", "JANE DOE", 0.8)],
            "JOHN DOE", 1,
        ),
    ],
)
def test_voting_scenarios(
    engine_pairs, expected_value, expected_winning_count,
) -> None:
    votes = [make_vote(e, "beneficiary", v, c) for (e, v, c) in engine_pairs]
    c = build_consensus_for_field("beneficiary", votes)
    assert c.value == expected_value
    assert c.winning_vote_count == expected_winning_count
