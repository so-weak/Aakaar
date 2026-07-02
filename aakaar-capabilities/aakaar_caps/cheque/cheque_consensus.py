"""Per-field consensus across multiple OCR engines.

This module turns the existing pipeline output ("engine A read
the payee as JOHN DOE at 0.85 conf; engine B read it as JOHN DOE
at 0.92 conf; engine C read it as J0HN DOE at 0.71 conf") into a
single trust-scored answer per field ("payee=JOHN DOE at
trust_score=0.94, all 3 engines agree after OCR-confusion fold").

Why a separate module instead of growing cheque_ocr.py:

  * cheque_ocr.py is already 3000+ lines, mostly about wiring the
    engines and per-band extraction. Adding the voting logic
    inline would mix two distinct concerns.
  * The consensus engine is GENERIC — it knows nothing about
    cheque-specific fields; cheque_ocr.py is the bridge that
    translates `engine_runs` + extracted fields into votes.
  * Future Phase 6 (visual evidence cards) reads from this
    module's `FieldConsensus.votes`; keeping it separate means
    the UI/API code doesn't have to import the OCR pipeline.

Vote shape — keeping the source_bbox per vote is critical for
Phase 6, which crops the cheque region the engine "read from"
to show the operator. For per-band engines (paddle_focused_*,
date_boxes) the bbox is the band crop. For full-page engines
(apple_vision, easy_ocr, doctr) we attach the CANONICAL band
bbox from handwriting_ocr.DEFAULT_REGIONS — the engine technically
read the whole page, but the value we extracted came from the
known band on the cheque template.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldVote:
    """One engine's vote on a single field's value.

    The `normalized_value` is what the consensus engine groups by;
    the `raw_value` is preserved so we can show the operator the
    actual OCR output (different engines may format the same
    logical value differently — e.g. "1,234.00" vs "1234").
    """

    engine: str
    raw_value: str
    normalized_value: str
    confidence: float
    source_bbox: tuple[float, float, float, float] | None = None


@dataclass(frozen=True, slots=True)
class FieldConsensus:
    """Aggregate result of all engines voting on a single field.

    Operator UI (Phase 5/6) reads this to decide whether to show a
    "Verified" badge or a "Review" badge, and to render the
    per-vote breakdown when the operator drills in.
    """

    field_name: str
    value: str | None
    normalized_value: str | None
    trust_score: float
    votes: tuple[FieldVote, ...]
    winning_vote_count: int
    review_reason: str | None

    def to_dict(self) -> dict:
        return {
            "field_name": self.field_name,
            "value": self.value,
            "normalized_value": self.normalized_value,
            "trust_score": round(self.trust_score, 3),
            "winning_vote_count": self.winning_vote_count,
            "review_reason": self.review_reason,
            "votes": [
                {
                    "engine": v.engine,
                    "raw_value": v.raw_value,
                    "normalized_value": v.normalized_value,
                    "confidence": round(v.confidence, 3),
                    "source_bbox": (
                        list(v.source_bbox) if v.source_bbox else None
                    ),
                }
                for v in self.votes
            ],
        }


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
#
# The normalization functions decide what counts as "agreement"
# for each field type. Two engines that read "JOHN DOE" and "John
# Doe" should agree. Two engines that read "JOHN DOE" and
# "J0HN D0E" (digit-zero confusion) should ALSO agree. Two engines
# that read "JOHN DOE" and "JANE DOE" must NOT agree.


_PUNCTUATION_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")
_NON_DIGIT_RE = re.compile(r"\D")
_NON_ALPHA_RE = re.compile(r"[^A-Z]")

# OCR letter <-> digit confusion folding. Applied bidirectionally
# in NAME normalization: both "JOHN DOE" and "J0HN D0E" fold to
# the same key. Keep the set narrow (genuine look-alikes only) so
# we don't accidentally group "1.00" and "I.OO" as the same name.
_OCR_DIGIT_TO_LETTER = str.maketrans({
    "0": "O", "1": "I", "5": "S", "8": "B",
})

_RUPEES_NOISE_RE = re.compile(
    r"\b(?:rupees?|rs|inr|only|paisa|paise)\b",
    re.IGNORECASE,
)

# Date normalization accepts the canonical formats we already
# match elsewhere (DD-MM-YYYY, DD/MM/YYYY, DD.MM.YYYY, DDMMYYYY).
# All fold to DDMMYYYY.
_DATE_TOKEN_RE = re.compile(
    r"\b(\d{1,2})[-./\s]?(\d{1,2})[-./\s]?(\d{4})\b",
)


def _ascii_fold(s: str) -> str:
    """Strip accents and other combining marks so "JOSÉ" matches
    "JOSE". NFKD then drop combining-character category."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(c)
    )


def normalize_name(value: str | None) -> str:
    """Canonicalize a person/entity name for consensus matching.

    Case-fold, strip punctuation/accents, collapse whitespace,
    AND fold OCR digit/letter confusions (`0->O`, `1->I`, `5->S`,
    `8->B`) so the same handwritten name read with different
    OCR-look-alike noise still groups as one vote.
    """
    if not value:
        return ""
    s = _ascii_fold(value).upper()
    s = _PUNCTUATION_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    s = s.translate(_OCR_DIGIT_TO_LETTER)
    return s


_DECIMAL_CENTS_SUFFIX_RE = re.compile(r"[.,]\d{2}\s*[/\-]?\s*$")


def normalize_amount(value: str | None) -> str:
    """Canonicalize an amount-in-figures for consensus matching.

    Digits-only — "1,234.00", "1234", "Rs 1234/-", and "₹1,234.00"
    all fold to "1234". The ".00" cents suffix is dropped so that
    engines that report the cents and engines that don't still
    agree on the rupee value.

    Care: the cents-suffix detection looks at the END of the
    source string for `.dd` / `,dd` (followed by optional `/-`
    trailer common on Indian cheques). It MUST NOT fire for
    "50,000" — where the trailing "000" is the thousands group,
    not cents — so it requires the decimal separator to be
    immediately followed by EXACTLY two digits at end-of-string.

    Returns "" for empty input.
    """
    if not value:
        return ""
    digits = _NON_DIGIT_RE.sub("", value)
    # Drop the cents suffix only when the source string ends in
    # `.dd` or `,dd` (with optional whitespace and a `/-` Indian
    # cheque trailer). "50,000" doesn't match; "1,234.00" does.
    if _DECIMAL_CENTS_SUFFIX_RE.search(value) and len(digits) >= 3:
        digits = digits[:-2]
    return digits


def normalize_amount_words(value: str | None) -> str:
    """Canonicalize an amount-in-words for consensus matching.

    Drop the noise tokens ("Rupees", "Only", "Rs", "Paisa") and
    the punctuation that varies between engines (",", "&", "and"
    as a conjunction). Case-fold, collapse whitespace.

    The remaining tokens are the actual number words ("FIFTY ONE
    THOUSAND SIXTY") which is what we want to compare across
    engines.
    """
    if not value:
        return ""
    s = _ascii_fold(value).upper()
    s = _RUPEES_NOISE_RE.sub(" ", s)
    s = _PUNCTUATION_RE.sub(" ", s)
    # Drop conjunctions used as fillers in Indian amount words
    s = re.sub(r"\b(?:AND|&)\b", " ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s


def normalize_date(value: str | None) -> str:
    """Canonicalize a date to DDMMYYYY.

    Accepts the formats `_DATE_TOKEN_RE` matches: DD-MM-YYYY,
    DD/MM/YYYY, DD.MM.YYYY, DD MM YYYY, DDMMYYYY. Pads single-
    digit day/month with leading zero so "1/6/2026" and
    "01062026" fold to the same key.

    Returns "" when no date-shaped token is found.
    """
    if not value:
        return ""
    m = _DATE_TOKEN_RE.search(value)
    if not m:
        # Already-canonical DDMMYYYY (8 digits) is the common case
        # for date_boxes — check that first as a fast path.
        digits = _NON_DIGIT_RE.sub("", value)
        if len(digits) == 8:
            return digits
        return ""
    dd, mm, yyyy = m.groups()
    return f"{int(dd):02d}{int(mm):02d}{yyyy}"


def normalize_digits(value: str | None) -> str:
    """Digits-only — used for cheque_no, account_no, MICR fields."""
    if not value:
        return ""
    return _NON_DIGIT_RE.sub("", value)


# Map field_name → normalizer. Used by build_consensus when the
# caller doesn't pass per-field overrides.
DEFAULT_NORMALIZERS = {
    "beneficiary": normalize_name,
    "amount": normalize_amount,
    "amount_words": normalize_amount_words,
    "date": normalize_date,
    "cheque_no": normalize_digits,
    "account_no": normalize_digits,
}


def normalize_for_field(field_name: str, value: str | None) -> str:
    """Return the normalized form of `value` appropriate for
    `field_name`. Unknown fields use the name normalizer as a
    permissive default."""
    fn = DEFAULT_NORMALIZERS.get(field_name, normalize_name)
    return fn(value)


# ---------------------------------------------------------------------------
# Consensus building
# ---------------------------------------------------------------------------


# Trust score threshold below which a field needs operator review.
# Tuned for the 2-of-N voter regime: at <0.85 either fewer than
# half the voters agreed OR the agreement came from low-confidence
# engines.
REVIEW_THRESHOLD = 0.85


def build_consensus_for_field(
    field_name: str,
    votes: Sequence[FieldVote],
) -> FieldConsensus:
    """Build the FieldConsensus for one field given the per-engine
    votes.

    Voting rules:
      1. Drop votes with empty normalized_value — those engines
         didn't read the field, can't contribute either way.
      2. Group remaining votes by normalized_value.
      3. Pick winner = group with the highest SUM of confidence
         (NOT highest vote count — a single high-conf engine can
         outweigh two low-conf ones).
      4. trust_score = (winner_conf_sum / all_conf_sum)
                       * min(1.0, winning_vote_count / 2)
         The `/2` cap means: 1 voter at any confidence gets at
         most 0.5 trust; 2 voters in agreement can reach 1.0.
      5. review_reason is non-None when winning_vote_count < 2
         OR trust_score < REVIEW_THRESHOLD.
    """
    # Filter out empty votes
    real_votes = [v for v in votes if v.normalized_value]

    if not real_votes:
        return FieldConsensus(
            field_name=field_name,
            value=None,
            normalized_value=None,
            trust_score=0.0,
            votes=tuple(votes),
            winning_vote_count=0,
            review_reason="no engine produced a value for this field",
        )

    # Group by normalized_value
    groups: dict[str, list[FieldVote]] = {}
    for v in real_votes:
        groups.setdefault(v.normalized_value, []).append(v)

    # Pick winner by SUM of confidence (not by count)
    winner_norm = max(
        groups,
        key=lambda k: sum(v.confidence for v in groups[k]),
    )
    winner_votes = groups[winner_norm]
    winner_conf_sum = sum(v.confidence for v in winner_votes)
    all_conf_sum = sum(v.confidence for v in real_votes)

    # Trust score: agreement ratio * voter-count scaler
    agreement_ratio = (
        winner_conf_sum / all_conf_sum if all_conf_sum > 0 else 0.0
    )
    voter_scaler = min(1.0, len(winner_votes) / 2)
    trust_score = agreement_ratio * voter_scaler

    # Pick the raw_value to surface: the winning vote with the
    # highest individual confidence (the "best representative" of
    # the winning normalization group).
    winner_repr = max(winner_votes, key=lambda v: v.confidence)

    # Review reason — explain WHY trust is low so the UI can show it
    review_reason: str | None = None
    if len(winner_votes) < 2:
        # Single-voter case — even at 1.0 individual conf, we cap
        # trust at 0.5 because no engine confirmed.
        other_values = sorted(set(groups) - {winner_norm})
        if other_values:
            review_reason = (
                f"only 1 engine ({winner_repr.engine}) read this field; "
                f"other engines read different values: "
                f"{', '.join(other_values[:3])}"
            )
        else:
            review_reason = (
                f"only 1 engine ({winner_repr.engine}) read this "
                f"field — no other engines could confirm"
            )
    elif trust_score < REVIEW_THRESHOLD:
        # Multi-voter but low agreement — list the dissenters.
        dissenters = [k for k in groups if k != winner_norm]
        review_reason = (
            f"engines disagree: winning value {winner_norm!r} "
            f"({len(winner_votes)} votes); dissenters: "
            f"{', '.join(dissenters[:3])}"
        )

    return FieldConsensus(
        field_name=field_name,
        value=winner_repr.raw_value,
        normalized_value=winner_norm,
        trust_score=round(trust_score, 4),
        votes=tuple(votes),
        winning_vote_count=len(winner_votes),
        review_reason=review_reason,
    )


def build_consensus(
    votes_by_field: Mapping[str, Sequence[FieldVote]],
) -> tuple[FieldConsensus, ...]:
    """Build the consensus across all fields. Returns a tuple in
    DEFAULT_NORMALIZERS key-order so the UI can iterate
    deterministically."""
    out: list[FieldConsensus] = []
    seen: set[str] = set()
    # First emit known fields in their canonical order
    for field_name in DEFAULT_NORMALIZERS:
        if field_name in votes_by_field:
            out.append(
                build_consensus_for_field(field_name, votes_by_field[field_name]),
            )
            seen.add(field_name)
    # Then emit any unknown / custom fields in input order
    for field_name, votes in votes_by_field.items():
        if field_name not in seen:
            out.append(build_consensus_for_field(field_name, votes))
    return tuple(out)


def make_vote(
    engine: str,
    field_name: str,
    raw_value: str | None,
    confidence: float,
    source_bbox: tuple[float, float, float, float] | None = None,
) -> FieldVote:
    """Convenience constructor that applies the canonical
    normalization for `field_name` automatically. Callers in
    cheque_ocr.py use this so they don't have to manage the
    field-to-normalizer mapping themselves."""
    raw = raw_value or ""
    return FieldVote(
        engine=engine,
        raw_value=raw,
        normalized_value=normalize_for_field(field_name, raw),
        confidence=confidence,
        source_bbox=source_bbox,
    )


# ---------------------------------------------------------------------------
# Canonical band bboxes for source-region crops (Phase 6 input)
# ---------------------------------------------------------------------------
#
# For full-page engines (apple_vision, easy_ocr, doctr) the engine
# technically read the whole cheque image, but the field's value
# came from a known BAND on the cheque template. These bboxes are
# the canonical band locations the operator UI will crop in
# Phase 6 to show "here's the pixels the engine read for this
# field". For per-band engines (paddle_focused_*) the engine's
# own bbox is more specific; this is the fallback.
#
# Mirrors handwriting_ocr.DEFAULT_REGIONS but adds bands for
# cheque_no (MICR strip) and account_no (back-side deposit
# stamp). Coords are (x1, y1, x2, y2) in 0..1 image-relative
# space.

FIELD_BAND_BBOXES: dict[str, tuple[float, float, float, float]] = {
    "beneficiary":    (0.06, 0.18, 0.86, 0.32),
    "amount_words":   (0.06, 0.32, 0.66, 0.52),
    "amount":         (0.78, 0.42, 0.99, 0.54),
    "date":           (0.72, 0.04, 0.99, 0.14),
    "cheque_no":      (0.00, 0.82, 1.00, 1.00),
    # account_no is on the BACK side — typically the deposit
    # stamp band. Front-side cheque_no is the MICR strip above.
    "account_no":     (0.00, 0.00, 1.00, 0.55),
}


def default_bbox_for_field(
    field_name: str,
) -> tuple[float, float, float, float] | None:
    """Return the canonical band bbox for a field name, or None
    when no canonical bbox is known."""
    return FIELD_BAND_BBOXES.get(field_name)


# ---------------------------------------------------------------------------
# Convenience: collapse an iterable of consensus results into a flat dict
# ---------------------------------------------------------------------------


def consensus_values(
    consensus: Iterable[FieldConsensus],
) -> dict[str, str | None]:
    """Flatten a tuple of FieldConsensus into a {field_name: value}
    dict. Convenient for callers that don't care about trust
    scores or votes (e.g. the existing rule engine that just
    wants the final field values)."""
    return {c.field_name: c.value for c in consensus}


def review_required_fields(
    consensus: Iterable[FieldConsensus],
) -> list[FieldConsensus]:
    """Filter to just the fields that need operator review (have
    a `review_reason`). Used by the Phase 5 review-queue panel."""
    return [c for c in consensus if c.review_reason is not None]
