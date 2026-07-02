"""Indian English amount-in-words → numeric Decimal converter.

Used by `cheque_validation.check_amount` to cross-validate the
amount-in-words line a customer wrote against the amount-in-figures
box (rule 3 of the cheque validation spec).

Why we built this in-house instead of using `word2number`:

  * The `word2number` PyPI package supports US/UK English only —
    it does NOT understand `lakh` / `crore`, which is how every
    Indian cheque writes amounts > 99,999.
  * It also doesn't tolerate the typical noise we see on a cheque:
    inconsistent casing, trailing 'Only', leading 'Rupees',
    'Paise' suffix on the fractional part, '&' / 'and' as a
    separator, OCR-introduced glyph swaps like 'O' for '0'.
  * Pure-python, zero deps, fast: a single cheque amount parses
    in <1ms, so we can call this on every cheque without
    impacting the OCR pipeline's throughput.

Indian English number system:

  | Word     | Value      |
  |----------|------------|
  | Crore    | 10^7       |
  | Lakh     | 10^5       |
  | Thousand | 10^3       |
  | Hundred  | 10^2       |

  Below 100 we use the standard one/two/three … ninety-nine
  vocabulary. Compound numbers are space- or hyphen-separated
  ('twenty-five thousand', 'twenty five thousand').

  Cheques in India universally write amounts in 'lakh' / 'crore'
  rather than 'million' / 'billion', but we accept both so a
  bank that's gone international doesn't trip the validator.

Contract:
  * Never raises. Returns None when the input can't be parsed —
    callers downgrade the check to NOT_VERIFIED rather than
    treating a parser miss as a substantive failure.
  * Handles paise (the fractional rupee unit, 1/100 of a rupee)
    via 'and X paise' or 'and X/100' suffixes.
  * Tolerates the typical cheque preamble ('Rupees') and
    terminator ('only') silently.
"""

from __future__ import annotations

import difflib
import logging
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Final

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vocabulary tables
# ---------------------------------------------------------------------------

# Below-twenty unit numerals. Cheques sometimes spell 'fourteen'
# as 'forteen' or 'eight' as 'eigth' — we keep a permissive table
# of the most common OCR misreads alongside the canonical forms.
_UNITS: Final[dict[str, int]] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
    # Common OCR misreads, kept conservative — we accept only
    # variants that are unambiguous (no risk of colliding with a
    # different number in the table).
    "forteen": 14, "eigth": 8, "ninteen": 19,
}

_TENS: Final[dict[str, int]] = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    # Common spelling drifts we've seen on Indian cheques.
    "fourty": 40,  # extremely common UK misspelling
}

# Scale multipliers. `crore` / `lakh` are Indian; the rest are
# universal. We accept both Indian and international vocabulary
# in the same parse — easier than asking the caller to choose
# a 'locale'.
_SCALES: Final[dict[str, int]] = {
    "hundred": 100,
    "thousand": 1_000,
    "lakh": 100_000,
    "lac":  100_000,                # legitimate alternate spelling
    "lakhs": 100_000,
    "lacs":  100_000,
    "million": 1_000_000,
    "crore": 10_000_000,
    "crores": 10_000_000,
    "billion": 1_000_000_000,
}


# ---------------------------------------------------------------------------
# Fuzzy token classification (for `words_to_decimal(..., fuzzy=True)`)
# ---------------------------------------------------------------------------
#
# RapidOCR is a PRINTED-text recognizer; the handwritten 'Rupees ... Only'
# line on a cheque comes back as cursive garble ('Ropeos Iwo lulch Ouly').
# Strict tokenisation then leaves the parser with nothing to chew on and
# the amount-in-words rule lands at NOT_VERIFIED. Fuzzy mode snaps each
# garbled token to its NEAREST entry in the closed number vocabulary
# before parsing, so 'Iwo' -> 'two', recovering the value.
#
# Why this is still safe against a real numeral swap (the reason the
# whole-string char-similarity score is only diagnostic): each token is
# classified INDEPENDENTLY against the closed vocab and accepted only when
# the best match is BOTH confident (>= _FUZZY_MIN_RATIO) AND unambiguous
# (beats the runner-up by >= _FUZZY_MIN_MARGIN). A token that genuinely
# reads "ninety" snaps to ninety, never collapses into twenty — so a
# cheque that really says a different number still parses to a different
# value and FAILs, rather than being smeared onto the expected answer.

# Words that carry no numeric value but appear on cheques. Garbled OCR
# variants ('Ropeos' for 'Rupees', 'Ouly' for 'Only') snap here and are
# dropped rather than mis-snapped onto a value word.
_FUZZY_DROP_WORDS: Final[frozenset[str]] = frozenset({
    "rupees", "rupee", "only", "paise", "paisa", "and",
})

# Confidence floor and ambiguity margin for accepting a fuzzy snap. 0.62
# admits the common cursive substitutions ('iwo'->'two' ~0.67,
# 'ouly'->'only' ~0.75) while rejecting connector noise and pure garbage.
# The 0.08 margin is the numeral-swap guard described above.
_FUZZY_MIN_RATIO: Final[float] = 0.62
_FUZZY_MIN_MARGIN: Final[float] = 0.08

# (word, is_value) candidates the fuzzy classifier scores a token against.
# Value words (units / tens / scales) snap to themselves; drop words snap
# to None. Built once at import.
_FUZZY_CANDIDATES: Final[tuple[tuple[str, bool], ...]] = tuple(
    [(w, True) for w in (*_UNITS, *_TENS, *_SCALES)]
    + [(w, False) for w in _FUZZY_DROP_WORDS]
)


def _fuzzy_snap_token(token: str) -> str | None:
    """Snap one OCR token to its nearest closed-vocabulary word.

    Returns the canonical VALUE word (a key of `_UNITS` / `_TENS` /
    `_SCALES`) when the token confidently and unambiguously matches one;
    returns None when the token should be DROPPED (it matched a no-value
    drop word, or no candidate was confident/unambiguous enough — the
    parser skips unknowns either way, so dropping is equivalent and
    keeps junk out of the accumulator).

    Exact hits short-circuit (value word kept, digit kept, drop word
    dropped) so a clean OCR pays nothing for the fuzzy scan.
    """
    if token in _UNITS or token in _TENS or token in _SCALES:
        return token
    if token.isdigit():
        return token
    if token in _FUZZY_DROP_WORDS:
        return None
    # Too short for a meaningful ratio — a 1-2 char glyph would
    # near-match half the table. Drop it.
    if len(token) < 3:
        return None

    best_ratio = 0.0
    best_word: str | None = None
    best_is_value = False
    second_ratio = 0.0
    for cand, is_value in _FUZZY_CANDIDATES:
        if len(cand) < 3:
            continue
        ratio = difflib.SequenceMatcher(a=token, b=cand, autojunk=False).ratio()
        if ratio > best_ratio:
            second_ratio = best_ratio
            best_ratio = ratio
            best_word = cand
            best_is_value = is_value
        elif ratio > second_ratio:
            second_ratio = ratio
    if (
        best_word is not None
        and best_ratio >= _FUZZY_MIN_RATIO
        and (best_ratio - second_ratio) >= _FUZZY_MIN_MARGIN
    ):
        return best_word if best_is_value else None
    return None


def _fuzzy_canonicalise(tokens: list[str]) -> list[str]:
    """Snap every token to its nearest closed-vocab value word, dropping
    tokens that snap to a drop word or fail the confidence/margin guard."""
    out: list[str] = []
    for tok in tokens:
        snapped = _fuzzy_snap_token(tok)
        if snapped is not None:
            out.append(snapped)
    return out


# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------


def _tokenise(text: str) -> list[str]:
    """Lower-case and split on whitespace / hyphens / commas. Drop
    the cheque preamble ('rupees') and terminator ('only') because
    they carry no value but appear on every cheque, and drop the
    'and' separator (we don't need it for parsing the integer
    side; the paise extractor below handles its own '& X paise'
    suffix)."""
    # Lower, then split on any non-alphanumeric.
    pieces = re.split(r"[\s\-,]+", text.lower().strip())
    drop = {"", "rupees", "rupee", "rs", "rs.", "only", "and", "&"}
    return [p for p in pieces if p not in drop]


# ---------------------------------------------------------------------------
# Integer rupee side
# ---------------------------------------------------------------------------


def _parse_int_words(tokens: list[str]) -> int | None:
    """Convert a list of cleaned tokens to an integer using the
    standard "current + accumulator" parser.

    The parser walks tokens left-to-right maintaining:
      - `current`  : in-progress numeric value below the next scale
                     (e.g. "twenty five" → 25, "two hundred and
                     fifty" → 250)
      - `total`    : sum of completed (current × scale) groups
                     (e.g. "one lakh ..." adds 100000 to total
                     and resets current to 0)

    When we hit a scale word ('hundred', 'thousand', 'lakh',
    'crore'), we multiply the current accumulator by that scale.
    The trick that handles "one lakh twenty thousand" correctly:
    'thousand' is smaller than 'lakh', so we only ADD current to
    total when we see a NEW scale that's smaller than or equal to
    the previously-flushed scale. We track this via `last_scale`.
    """
    if not tokens:
        return None

    current = 0
    total = 0
    saw_any_number = False

    for tok in tokens:
        if tok in _UNITS:
            current += _UNITS[tok]
            saw_any_number = True
            continue
        if tok in _TENS:
            current += _TENS[tok]
            saw_any_number = True
            continue
        if tok in _SCALES:
            scale = _SCALES[tok]
            # 'hundred' multiplies the current accumulator in
            # place (e.g. "two hundred" → current = 2 × 100 = 200);
            # larger scales finalise the group into `total` and
            # reset.
            if current == 0:
                # "lakh" with no leading digit is invalid — but
                # treat it as 1 (1 lakh) for permissiveness.
                current = 1
            if scale == 100:
                current = current * 100
            else:
                total += current * scale
                current = 0
            saw_any_number = True
            continue
        # Pure-digit fallbacks — sometimes OCR drops the spelt
        # word entirely and a numeric digit appears mid-string
        # (e.g. "fifty 1 thousand").
        if tok.isdigit():
            try:
                current += int(tok)
                saw_any_number = True
                continue
            except ValueError:
                pass
        # Unknown token — log at debug, skip. Common case: a
        # smudged glyph the OCR rendered as 'sii'. We don't want
        # to fail the parse on a single unrecognised word; the
        # confidence score is `present + parseable + matches` and
        # ignoring noise generally improves recall on the
        # consistency check.
        logger.debug("words_to_number: unknown token %r — skipping", tok)

    if not saw_any_number:
        return None
    return total + current


# ---------------------------------------------------------------------------
# Fractional paise side
# ---------------------------------------------------------------------------

# Patterns that frame the paise component on a typical cheque:
#   "... and fifty paise only"   — the canonical form
#   "... and 50/100 only"        — the bank-printed form
#   "... fifty paise only"       — paise-only or operator dropped 'and'
#   "... 50/100"
_PAISE_FRACTION_RE = re.compile(
    r"\b(?:and\s+)?(\d{1,2})\s*/\s*100\b",
    re.IGNORECASE,
)
# Matches "and X paise" — explicit separator, makes the
# integer/paise split unambiguous (everything BEFORE the 'and'
# is the rupee side).
_PAISE_WITH_AND_RE = re.compile(
    r"\band\s+([0-9a-z\-\s]+?)\s+pais[ae]\b",
    re.IGNORECASE,
)
# Plain ' paise' anchor — used by the fallback path when there's
# no explicit 'and' separator. We walk BACKWARDS from this anchor
# collecting up to 3 number tokens (Indian English numbers below
# 100 never exceed 3 tokens, e.g. 'twenty five').
_PAISE_ANCHOR_RE = re.compile(r"\bpais[ae]\b", re.IGNORECASE)

# A 4-6 letter word — the band a cursive-OCR'd 'paise'/'paisa' lands in.
_PAISE_WORDISH_RE = re.compile(r"[A-Za-z]{4,6}")
# How close a garbled token must be to 'paise'/'paisa' to be snapped to
# the literal so the anchors above catch it. High enough to avoid
# snapping unrelated short words ('lakh', 'rupee', 'paid' all fall
# below); fuzzy paise is opt-in (handwriting recall) so the risk is low.
_PAISE_SNAP_RATIO: Final[float] = 0.7


def _fuzzy_paise_normalise(text: str) -> str:
    """Snap any token that closely resembles 'paise'/'paisa' to the
    literal 'paise' so `_extract_paise`'s anchors fire on cursive-OCR
    garble like 'fifly paisc only'. Used only in fuzzy mode."""
    def _repl(m: "re.Match[str]") -> str:
        tok = m.group(0).lower()
        if tok in ("paise", "paisa"):
            return m.group(0)
        ratio = max(
            difflib.SequenceMatcher(a=tok, b="paise", autojunk=False).ratio(),
            difflib.SequenceMatcher(a=tok, b="paisa", autojunk=False).ratio(),
        )
        return "paise" if ratio >= _PAISE_SNAP_RATIO else m.group(0)
    return _PAISE_WORDISH_RE.sub(_repl, text)


def _maybe_fuzzy(tokens: list[str], fuzzy: bool) -> list[str]:
    return _fuzzy_canonicalise(tokens) if fuzzy else tokens


def _extract_paise(text: str, *, fuzzy: bool = False) -> tuple[str, int]:
    """Split `text` at the paise marker. Returns `(int_text,
    paise_int)`. `paise_int` is 0 when no paise component is
    present. The returned `int_text` has the paise clause
    stripped so `_parse_int_words` can run cleanly on it.

    When ``fuzzy`` is True, a garbled paise WORD is first snapped to the
    literal 'paise' and the paise NUMBER tokens are fuzzy-canonicalised
    before parsing, so cursive garble like 'fifly paisc' still recovers
    the .50 fractional part."""
    if fuzzy:
        text = _fuzzy_paise_normalise(text)
    # X/100 form is unambiguous — try that first.
    m = _PAISE_FRACTION_RE.search(text)
    if m:
        paise = int(m.group(1))
        return text[:m.start()] + " " + text[m.end():], paise

    # Form A: 'and X paise' — explicit 'and' separator. We can
    # split cleanly at the 'and'.
    m = _PAISE_WITH_AND_RE.search(text)
    if m:
        value = _parse_int_words(_maybe_fuzzy(_tokenise(m.group(1)), fuzzy))
        if value is not None and 0 <= value < 100:
            return text[:m.start()], value

    # Form B: 'X paise' without 'and' — walk backwards from
    # 'paise' and find the smallest valid 1..3-token <100 number
    # that precedes it. Everything before those tokens is the
    # integer side.
    m = _PAISE_ANCHOR_RE.search(text)
    if m:
        prefix = text[:m.start()].rstrip()
        prefix_tokens = _tokenise(prefix)
        for n in (1, 2, 3):
            if n > len(prefix_tokens):
                break
            tail = _maybe_fuzzy(prefix_tokens[-n:], fuzzy)
            value = _parse_int_words(tail)
            if value is not None and 0 < value < 100:
                int_tokens = prefix_tokens[:-n]
                return " ".join(int_tokens), value
    return text, 0


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def words_to_decimal(text: str | None, *, fuzzy: bool = False) -> Decimal | None:
    """Convert an Indian English amount-in-words string to a
    Decimal in rupees (with paise as the fractional part). Returns
    None when the input is empty / unparseable.

    When ``fuzzy`` is True, each tokenised word is first snapped to its
    nearest closed-vocabulary number word via `_fuzzy_canonicalise`
    before parsing — so cursive-OCR garble like 'Ropeos Iwo lulch Ouly'
    recovers as much of the value as the per-token confidence/margin
    guards allow ('Iwo' -> 'two'). Strict mode (the default) is
    unchanged so existing callers and the strict PASS path keep their
    exact semantics.

    Examples:
      "Rupees Fifty One Thousand Sixty Only"        → Decimal('51060')
      "One Lakh Twenty Five Thousand and 50 Paise"  → Decimal('125000.50')
      "Five Hundred Only"                           → Decimal('500')
      "Three Crore Twenty Lakh"                     → Decimal('32000000')
      "" / None / "garbage that's not a number"     → None
    """
    if not text:
        return None
    text = str(text).strip()
    if not text:
        return None

    int_text, paise = _extract_paise(text, fuzzy=fuzzy)
    tokens = _tokenise(int_text)
    if fuzzy:
        tokens = _fuzzy_canonicalise(tokens)
    rupees = _parse_int_words(tokens)
    if rupees is None and paise == 0:
        return None
    rupees = rupees or 0
    # Build a Decimal as 'rupees.paise' so callers can compare
    # against the amount-in-figures Decimal exactly.
    fractional = f"{paise:02d}" if paise else ""
    if fractional:
        return Decimal(f"{rupees}.{fractional}")
    return Decimal(str(rupees))


# Per-token fuzzy floor for the EXPECTED-guided coverage scorer. More
# lenient than the open-vocab `_FUZZY_MIN_RATIO` snap because here we're
# testing one specific hypothesis ("does this OCR token look like the
# expected word 'lakh'?") rather than classifying against the whole
# vocabulary — so a looser ratio is safe and recovers scale-word garble
# ('lulch' ~ 'lakh') that the strict snap drops.
_COVERAGE_TOKEN_MIN_RATIO: Final[float] = 0.5


def expected_token_coverage(
    observed: str | None, expected_words: str | None,
) -> float:
    """Fraction of the EXPECTED amount-words' value tokens (units / tens
    / scales) that appear, in order, in the observed OCR string.

    `expected_words` is the canonical words form of the system amount
    (from `decimal_to_words(dom_value)`); `observed` is the cheque-side
    OCR text. Each expected value token is matched against the observed
    tokens greedily left-to-right, accepting either an exact hit or a
    lenient per-token fuzzy match (`_COVERAGE_TOKEN_MIN_RATIO`) so
    cursive-OCR substitutions still count.

    A high coverage means "the handwriting clearly resembles the
    expected amount even though the strict/fuzzy numeric parse choked" —
    used by the amount-in-words rule to surface a WARN (operator
    confirm) instead of a bare NOT_VERIFIED. Returns 0.0 when either
    side is empty or the expected form carries no value tokens.
    """
    if not observed or not expected_words:
        return 0.0
    exp = [
        t for t in _tokenise(str(expected_words))
        if t in _UNITS or t in _TENS or t in _SCALES
    ]
    if not exp:
        return 0.0
    obs = _tokenise(str(observed))
    matched = 0
    start = 0
    for et in exp:
        for k in range(start, len(obs)):
            ot = obs[k]
            hit = ot == et or (
                len(ot) >= 3
                and len(et) >= 3
                and difflib.SequenceMatcher(
                    a=ot, b=et, autojunk=False,
                ).ratio() >= _COVERAGE_TOKEN_MIN_RATIO
            )
            if hit:
                matched += 1
                start = k + 1
                break
    return matched / len(exp)


# ---------------------------------------------------------------------------
# Inverse: number → Indian English words
# ---------------------------------------------------------------------------
#
# Used by `cheque_validation._rule_amount_in_words` to convert the
# DOM/system amount (a numeric figure) into the canonical
# 'Rupees ... Only' words form, so the rule can be displayed to the
# operator as "expected words: 'One Lakh Ninety Thousand Only'" and
# can fuzzy-match the OCR'd amount-in-words line against that
# expected form when the words → Decimal parse fails (handwritten
# OCR noise that the numeric parser can't disambiguate but a token
# similarity score can still verify against).
#
# Convention follows what Indian banks print on amount-in-words
# stamps and what cheque writers spell out:
#   - Indian grouping (lakh / crore, never million / billion)
#   - No "And" between hundred and the tens place (cheques write it
#     either way; we drop it to keep the output canonical)
#   - "And" only as the rupees/paise separator
#   - Paise rendered as "<NN> Paise" when nonzero

_BELOW_TWENTY: Final[tuple[str, ...]] = (
    "Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven",
    "Eight", "Nine", "Ten", "Eleven", "Twelve", "Thirteen",
    "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen",
)

_TENS_PREFIXES: Final[tuple[str, ...]] = (
    "", "", "Twenty", "Thirty", "Forty", "Fifty",
    "Sixty", "Seventy", "Eighty", "Ninety",
)


def _two_digit_to_words(n: int) -> str:
    """Render an integer 0..99 in English."""
    if n < 20:
        return _BELOW_TWENTY[n]
    tens, ones = divmod(n, 10)
    if ones == 0:
        return _TENS_PREFIXES[tens]
    return f"{_TENS_PREFIXES[tens]} {_BELOW_TWENTY[ones]}"


def _three_digit_to_words(n: int) -> str:
    """Render an integer 0..999 in English."""
    if n < 100:
        return _two_digit_to_words(n)
    hundreds, rest = divmod(n, 100)
    head = f"{_BELOW_TWENTY[hundreds]} Hundred"
    if rest == 0:
        return head
    return f"{head} {_two_digit_to_words(rest)}"


def _int_rupees_to_words(rupees: int) -> str:
    """Render a non-negative integer rupee value in Indian English.

    Walks the Indian-grouping scales (crore → lakh → thousand →
    hundreds-or-below), emitting the most-significant group first.
    Zero is "Zero" so the caller can wrap it in "Rupees Zero Only"
    if it wants to (e.g. a cheque with the figures box smudged
    and the system amount actually zero — vanishingly rare but
    surfaceable).
    """
    if rupees < 0:
        raise ValueError("rupees must be non-negative")
    if rupees == 0:
        return "Zero"
    parts: list[str] = []
    # Recurse on the crore-count to render values ≥ 100 crore
    # (₹10^9, well beyond any realistic cheque) without a
    # special case. For crore_part < 100 this single divmod
    # would suffice, but real-world rare-case correctness
    # costs almost nothing here.
    crore_part, rest = divmod(rupees, 10_000_000)
    if crore_part:
        parts.append(f"{_int_rupees_to_words(crore_part)} Crore")
    lakh_part, rest = divmod(rest, 100_000)
    if lakh_part:
        parts.append(f"{_two_digit_to_words(lakh_part)} Lakh")
    thousand_part, rest = divmod(rest, 1_000)
    if thousand_part:
        parts.append(f"{_two_digit_to_words(thousand_part)} Thousand")
    if rest:
        parts.append(_three_digit_to_words(rest))
    return " ".join(parts)


def decimal_to_words(
    value: Decimal | int | str | None,
    *,
    with_rupees_wrapper: bool = False,
) -> str | None:
    """Render an Indian English amount-in-words string for `value`.

    The inverse of `words_to_decimal`. Used by the cheque validator
    to surface 'expected: <words form of the DOM amount>' to the
    operator and to fuzzy-compare against the handwritten 'Rupees
    ... Only' line OCR'd off the cheque face.

    Args:
      value: A Decimal, int, or string-parseable-as-Decimal value
        in rupees (with optional paise as the fractional part).
        Negatives / NaN / Infinity are rejected (cheques can't
        carry them) and return None.
      with_rupees_wrapper: When True, returns the canonical
        cheque-writer's form 'Rupees <words> Only' with paise
        embedded as ' And <NN> Paise' when nonzero. When False
        (default), returns just the inner words component.

    Returns:
      The words string, or None on bad input.

    Examples:
      decimal_to_words(0)                              → 'Zero'
      decimal_to_words(7)                              → 'Seven'
      decimal_to_words(25)                             → 'Twenty Five'
      decimal_to_words(105)                            → 'One Hundred Five'
      decimal_to_words(1234)                           → 'One Thousand Two Hundred Thirty Four'
      decimal_to_words(100000)                         → 'One Lakh'
      decimal_to_words(190000)                         → 'One Lakh Ninety Thousand'
      decimal_to_words(12345678)                       → 'One Crore Twenty Three Lakh Forty Five Thousand Six Hundred Seventy Eight'
      decimal_to_words(Decimal('1500.50'))             → 'One Thousand Five Hundred And Fifty Paise'
      decimal_to_words(Decimal('0.50'))                → 'Fifty Paise'
      decimal_to_words(Decimal('500'),
                       with_rupees_wrapper=True)       → 'Rupees Five Hundred Only'
      decimal_to_words(Decimal('1500.50'),
                       with_rupees_wrapper=True)       → 'Rupees One Thousand Five Hundred And Fifty Paise Only'
      decimal_to_words(None)                           → None
      decimal_to_words(Decimal('-1'))                  → None
    """
    if value is None:
        return None
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if d.is_nan() or d.is_infinite():
        return None
    if d < 0:
        return None

    # Quantize to paise precision (2 decimal places). ROUND_HALF_UP
    # so 1.005 → 1.01 rather than banker-rounding to 1.00 — operators
    # expect the cheque to read the "obvious" round-up.
    cents = int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    rupees, paise = divmod(cents, 100)

    rupees_words = _int_rupees_to_words(rupees)

    if paise > 0:
        paise_words = _two_digit_to_words(paise)
        if rupees == 0:
            body = f"{paise_words} Paise"
        else:
            body = f"{rupees_words} And {paise_words} Paise"
    else:
        body = rupees_words

    if with_rupees_wrapper:
        return f"Rupees {body} Only"
    return body


def figures_to_decimal(text: str | None) -> Decimal | None:
    """Convert a cheque amount-in-figures string (e.g. '51,060.00'
    or '₹ 1,25,000.50' or 'Rs. 500/-') to a Decimal in rupees.

    Indian cheques use the 'lakhs comma' grouping (1,25,000 not
    125,000), and frequently terminate with '/-' or '/' to mark
    the end of the box. Strategy: find the first contiguous run
    of digits / dots / commas in the input, drop the commas, and
    Decimal-parse it. This survives every common cheque-amount
    decoration (Rs / Rs. / ₹ / INR / trailing /-) without an
    inscrutable cascade of substring substitutions.
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    # Find the first numeric token (digits + optional decimal +
    # optional grouping commas). This silently skips currency
    # symbols, 'Rs.' / 'INR' prefixes, and the trailing '/-'.
    m = re.search(r"\d[\d,]*(?:\.\d+)?", s)
    if not m:
        return None
    cleaned = m.group(0).replace(",", "")
    try:
        return Decimal(cleaned)
    except Exception as e:  # noqa: BLE001
        logger.debug("figures_to_decimal: parse failed for %r (%s)", text, e)
        return None
