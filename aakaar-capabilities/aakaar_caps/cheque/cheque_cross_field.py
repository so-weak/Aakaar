"""Cross-field validation for cheque OCR results.

After per-field consensus is built (`cheque_consensus.build_consensus`)
the resulting field values can still be individually-high-trust BUT
logically inconsistent — e.g. amount words say "FIFTY THOUSAND" but
amount figures say "16388", or the OCR'd date is 2199-12-31. These
are the operator's red flags that pure per-engine voting can't
catch.

This module runs the four rule-of-thumb cross-field checks the user
listed in Phase 3 of the plan:

  1. amount_words integer == amount figures
  2. date plausibility (Gregorian + cheque CTS validity window)
  3. payee shape (name-like, not boilerplate template text)
  4. MICR vs printed-face cheque_no agreement

Each check returns a `CrossFieldFinding`. The findings are then
applied to the consensus tuple — the affected fields' trust_score
is multiplied by a downgrade factor and a review_reason is set so
the operator UI surfaces "this passed per-engine voting but failed
the cross-field check, please review".

We DON'T mutate field VALUES — only trust_score and review_reason.
The operator (or downstream Validation rules) gets the final say
on the value; cross-field checks just lower confidence.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from aakaar_caps.cheque import cheque_consensus
from aakaar_caps.cheque.cheque_consensus import FieldConsensus
from aakaar_caps.cheque.words_to_number import (
    figures_to_decimal,
    words_to_decimal,
)


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


Severity = Literal["info", "warn", "fail"]


@dataclass(frozen=True, slots=True)
class CrossFieldFinding:
    """One cross-field rule's outcome.

    `affected_fields` lists the consensus-field names whose
    trust_score should be downgraded if the operator considers
    this finding actionable. `severity` is "info" when the rule
    couldn't run (missing data), "warn" when something looks odd
    but might be OCR noise, "fail" when the cross-field
    relationship is clearly broken.
    """

    rule_id: str
    severity: Severity
    summary: str
    affected_fields: tuple[str, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "summary": self.summary,
            "affected_fields": list(self.affected_fields),
            "detail": dict(self.detail),
        }


# How much to multiply trust_score by when a finding flags a field.
# "fail" findings cut trust to ~25% of the engine consensus; "warn"
# findings to ~60%. Both push fields below REVIEW_THRESHOLD (0.85)
# so the operator UI lights them up for inspection.
_TRUST_DOWNGRADE: dict[Severity, float] = {
    "fail": 0.25,
    "warn": 0.60,
    "info": 1.0,  # informational — no downgrade
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find(
    consensus_tuple: Sequence[FieldConsensus],
    field_name: str,
) -> FieldConsensus | None:
    for c in consensus_tuple:
        if c.field_name == field_name:
            return c
    return None


# ---------------------------------------------------------------------------
# Rule 1: amount_words integer == amount figures
# ---------------------------------------------------------------------------
#
# The classic cheque fraud / OCR-misread check. Banks legally
# treat the amount in words as authoritative, but if the OCR'd
# words don't parse OR don't match the figures, that's an
# immediate operator review item.


def check_amount_words_vs_figures(
    consensus_tuple: Sequence[FieldConsensus],
) -> CrossFieldFinding | None:
    """Compare consensus amount_words (parsed to a Decimal) with
    consensus amount (parsed to a Decimal). Findings:

      * fail: both parsed AND non-equal
      * warn: one parsed, the other didn't (operator must
              eyeball — possible OCR miss on one side)
      * None: both missing entirely (out of scope for this rule
              — the per-field consensus already flagged "no
              engine produced a value")
      * None: both parsed AND equal (no finding needed)

    On a fail, both `amount` and `amount_words` fields get
    flagged so the operator sees BOTH per-field trust badges
    light up — they don't know which side was wrong.
    """
    words = _find(consensus_tuple, "amount_words")
    figures = _find(consensus_tuple, "amount")

    words_val = words.value if words else None
    figs_val = figures.value if figures else None

    if not words_val and not figs_val:
        return None  # Nothing to compare

    words_dec = words_to_decimal(words_val) if words_val else None
    figs_dec = figures_to_decimal(figs_val) if figs_val else None

    if words_dec is None and figs_dec is None:
        # Both fields HAD raw values but neither parsed — flag warn
        return CrossFieldFinding(
            rule_id="amount_words_vs_figures",
            severity="warn",
            summary=(
                f"Neither amount words ({words_val!r}) nor amount "
                f"figures ({figs_val!r}) parsed to a number — both "
                f"need operator review."
            ),
            affected_fields=("amount", "amount_words"),
            detail={
                "words_raw": words_val,
                "figures_raw": figs_val,
                "words_parsed": None,
                "figures_parsed": None,
            },
        )

    if words_dec is None or figs_dec is None:
        # One parsed, one didn't — likely an OCR miss on one side.
        missing = "amount_words" if words_dec is None else "amount"
        return CrossFieldFinding(
            rule_id="amount_words_vs_figures",
            severity="warn",
            summary=(
                f"Only one of (amount words, amount figures) parsed "
                f"cleanly — cannot cross-check. {missing!r} did not "
                f"parse."
            ),
            affected_fields=(missing,),
            detail={
                "words_raw": words_val,
                "figures_raw": figs_val,
                "words_parsed": (
                    str(words_dec) if words_dec is not None else None
                ),
                "figures_parsed": (
                    str(figs_dec) if figs_dec is not None else None
                ),
            },
        )

    if words_dec == figs_dec:
        return None  # All good

    return CrossFieldFinding(
        rule_id="amount_words_vs_figures",
        severity="fail",
        summary=(
            f"Amount words say {words_dec} but amount figures say "
            f"{figs_dec} — mismatch on the cheque itself."
        ),
        affected_fields=("amount", "amount_words"),
        detail={
            "words_raw": words_val,
            "figures_raw": figs_val,
            "words_parsed": str(words_dec),
            "figures_parsed": str(figs_dec),
        },
    )


# ---------------------------------------------------------------------------
# Rule 2: date plausibility
# ---------------------------------------------------------------------------
#
# A date that can't possibly be on a real cheque (Gregorian
# impossible, future-dated beyond a stale window, older than
# CTS validity) is almost certainly OCR noise.

_DDMMYYYY_RE = re.compile(r"^(\d{2})(\d{2})(\d{4})$")


def _parse_ddmmyyyy(value: str | None) -> _dt.date | None:
    """Parse the consensus' normalized DDMMYYYY date format to a
    `datetime.date`. Returns None when the string isn't a clean
    DDMMYYYY or when the date is Gregorian-impossible (Feb 30,
    month 13, etc.)."""
    if not value:
        return None
    m = _DDMMYYYY_RE.match(value)
    if not m:
        return None
    dd, mm, yyyy = (int(g) for g in m.groups())
    try:
        return _dt.date(yyyy, mm, dd)
    except ValueError:
        return None


def check_date_plausibility(
    consensus_tuple: Sequence[FieldConsensus],
    *,
    today: _dt.date | None = None,
    validity_days: int = 90,
    future_grace_days: int = 7,
) -> CrossFieldFinding | None:
    """Cross-check the consensus date against:
       * Gregorian validity (Feb 30 / month 13 / etc.)
       * Not absurdly old (older than `validity_days` is stale
         per CTS-2010; the cheque can be returned).
       * Not absurdly far in the future (post-dated cheques are
         allowed; OCR-noise dates 50 years out are not. The
         `future_grace_days` accepts post-dated up to ~30 days
         which covers the common business case.)

    Returns None when the consensus has no date value (out of
    scope — per-field consensus already flagged that).
    """
    if today is None:
        today = _dt.date.today()

    date_c = _find(consensus_tuple, "date")
    if date_c is None or not date_c.value:
        return None

    parsed = _parse_ddmmyyyy(date_c.normalized_value)
    if parsed is None:
        return CrossFieldFinding(
            rule_id="date_plausibility",
            severity="fail",
            summary=(
                f"OCR'd date {date_c.value!r} does not parse to a "
                f"valid Gregorian date — likely a misread."
            ),
            affected_fields=("date",),
            detail={"raw": date_c.value, "normalized": date_c.normalized_value},
        )

    days_old = (today - parsed).days
    days_ahead = -days_old

    if days_old > validity_days:
        return CrossFieldFinding(
            rule_id="date_plausibility",
            severity="warn",
            summary=(
                f"Date {parsed.isoformat()} is {days_old} days old — "
                f"older than the CTS-2010 {validity_days}-day "
                f"validity window. Cheque may be returnable as stale."
            ),
            affected_fields=("date",),
            detail={
                "parsed_iso": parsed.isoformat(),
                "today_iso": today.isoformat(),
                "days_old": days_old,
                "validity_days": validity_days,
            },
        )

    if days_ahead > future_grace_days:
        # Post-dating up to ~7 days is common; anything more
        # likely means OCR misread "21062026" as "21062036".
        return CrossFieldFinding(
            rule_id="date_plausibility",
            severity="warn",
            summary=(
                f"Date {parsed.isoformat()} is {days_ahead} days in "
                f"the future — implausible cheque date, likely OCR "
                f"misread (only post-dating up to {future_grace_days} "
                f"days is plausible)."
            ),
            affected_fields=("date",),
            detail={
                "parsed_iso": parsed.isoformat(),
                "today_iso": today.isoformat(),
                "days_ahead": days_ahead,
                "future_grace_days": future_grace_days,
            },
        )

    return None  # Looks plausible


# ---------------------------------------------------------------------------
# Rule 3: payee shape
# ---------------------------------------------------------------------------
#
# The most common failure mode the user reported: OCR picks up
# "VALID FOR THREE MONTHS ONLY" as the amount-in-words because
# the template phrase happens to be near the words band. The
# same boilerplate trap exists for the payee band ("ACCOUNT
# PAYEE ONLY", "OR BEARER", "NOT NEGOTIABLE"). A payee that
# matches any of these templates is almost certainly not the
# real payee.

_PAYEE_BOILERPLATE_PHRASES = (
    "ACCOUNT PAYEE",
    "AC PAYEE",
    "A C PAYEE",
    "ACC PAYEE",
    "NOT NEGOTIABLE",
    "OR BEARER",
    "OR ORDER",
    "VALID FOR",
    "VALID UPTO",
    "VALID UP TO",
    "PAY TO ORDER",
    "CROSSED",
    "MULTI CITY CHEQUE",
    "AT PAR",
    "PAYABLE AT",
    "NON CASH",
    "NON HOME BRANCH",
    "RUPEES",
    "PAID",
    "FOR ",  # "FOR XYZ ENTERPRISES" footer stamp
)


def _looks_like_payee(name: str) -> tuple[bool, str | None]:
    """Return (ok, failure_reason). The payee should:

      * be at least 2 chars long after stripping
      * contain at least 2 alphabetic characters (rejects "12345")
      * not be entirely boilerplate template text
      * not be all-digits

    Returns ok=False with a reason string when any check fails;
    ok=True, reason=None on the happy path.
    """
    if not name:
        return False, "empty"
    stripped = name.strip()
    if len(stripped) < 2:
        return False, "too short"

    alpha_count = sum(1 for c in stripped if c.isalpha())
    if alpha_count < 2:
        return False, "fewer than 2 alphabetic chars (looks numeric)"

    upper = stripped.upper()
    for phrase in _PAYEE_BOILERPLATE_PHRASES:
        if phrase in upper:
            return False, f"contains boilerplate phrase {phrase!r}"

    return True, None


def check_payee_shape(
    consensus_tuple: Sequence[FieldConsensus],
) -> CrossFieldFinding | None:
    """Validate that the consensus beneficiary actually LOOKS
    like a payee name vs. cheque-template boilerplate."""
    payee = _find(consensus_tuple, "beneficiary")
    if payee is None or not payee.value:
        return None

    ok, reason = _looks_like_payee(payee.value)
    if ok:
        return None

    return CrossFieldFinding(
        rule_id="payee_shape",
        severity="fail",
        summary=(
            f"Consensus payee {payee.value!r} does not look like a "
            f"real payee name: {reason}. Likely OCR captured a "
            f"template phrase (e.g. 'ACCOUNT PAYEE') instead of the "
            f"handwritten beneficiary."
        ),
        affected_fields=("beneficiary",),
        detail={"value": payee.value, "reason": reason},
    )


# ---------------------------------------------------------------------------
# Rule 4: MICR-derived cheque_no vs printed-face cheque_no
# ---------------------------------------------------------------------------
#
# The MICR strip is the BANK's machine-printed cheque number;
# the printed-face cheque-no extractor reads the same number off
# the top-right printed corner. They MUST agree on a real
# cheque. Disagreement points to either a forgery, an OCR misread
# of the MICR (less common — the magnetic ink font is OCR-easy)
# or an OCR misread of the printed corner (more common).


def check_micr_vs_printed_cheque_no(
    consensus_tuple: Sequence[FieldConsensus],
) -> CrossFieldFinding | None:
    """Look at the per-engine VOTES on cheque_no. If we have both
    a `micr_strip` vote AND a vote from any full-page printed
    engine, compare them. Mismatch → fail finding.

    Returns None when only one source voted (no cross-check
    possible) or when both sources agree."""
    cn = _find(consensus_tuple, "cheque_no")
    if cn is None or not cn.votes:
        return None

    micr_votes = [v for v in cn.votes if v.engine == "micr_strip"]
    printed_votes = [
        v for v in cn.votes
        if v.engine != "micr_strip" and v.normalized_value
    ]

    if not micr_votes or not printed_votes:
        return None  # Need both sources to cross-check

    micr_value = micr_votes[0].normalized_value
    # Pick the highest-conf printed-face vote
    printed_top = max(printed_votes, key=lambda v: v.confidence)
    printed_value = printed_top.normalized_value

    if not micr_value or not printed_value:
        return None

    # Tolerance: the printed corner often shows just the
    # cheque-serial digits (6 chars), while MICR shows the full
    # 6-digit cheque-serial as ITS first group. Compare on the
    # last 6 digits of each to allow this.
    micr_tail = micr_value[-6:]
    printed_tail = printed_value[-6:]
    if micr_tail == printed_tail:
        return None

    return CrossFieldFinding(
        rule_id="micr_vs_printed_cheque_no",
        severity="fail",
        summary=(
            f"MICR strip reads cheque_no {micr_value!r} but the "
            f"printed face reads {printed_value!r} (from "
            f"{printed_top.engine}). These MUST agree on a "
            f"genuine cheque."
        ),
        affected_fields=("cheque_no",),
        detail={
            "micr_value": micr_value,
            "printed_value": printed_value,
            "printed_engine": printed_top.engine,
        },
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


_ALL_CHECKS = (
    ("amount_words_vs_figures", check_amount_words_vs_figures),
    ("date_plausibility", check_date_plausibility),
    ("payee_shape", check_payee_shape),
    ("micr_vs_printed_cheque_no", check_micr_vs_printed_cheque_no),
)


def run_all_cross_field_checks(
    consensus_tuple: Sequence[FieldConsensus],
    *,
    today: _dt.date | None = None,
    validity_days: int = 90,
) -> tuple[CrossFieldFinding, ...]:
    """Run every cross-field check against `consensus_tuple` and
    return the non-None findings.

    `today` and `validity_days` are forwarded to the date
    plausibility check (default: today=date.today(), 90-day CTS
    validity window). Other checks ignore them.
    """
    out: list[CrossFieldFinding] = []
    for _rule_id, fn in _ALL_CHECKS:
        # Date check takes the extra kwargs; the others don't.
        if fn is check_date_plausibility:
            finding = fn(
                consensus_tuple, today=today, validity_days=validity_days,
            )
        else:
            finding = fn(consensus_tuple)
        if finding is not None:
            out.append(finding)
    return tuple(out)


def apply_findings_to_consensus(
    consensus_tuple: Sequence[FieldConsensus],
    findings: Sequence[CrossFieldFinding],
) -> tuple[FieldConsensus, ...]:
    """Downgrade trust_score and append the finding's summary to
    review_reason for every field flagged by a 'fail' or 'warn'
    finding. Returns a NEW tuple (FieldConsensus is frozen) so
    the original is untouched.
    """
    # Build {field_name: (downgrade_factor, [reasons])} from findings
    overrides: dict[str, tuple[float, list[str]]] = {}
    for f in findings:
        factor = _TRUST_DOWNGRADE.get(f.severity, 1.0)
        for fname in f.affected_fields:
            cur_factor, cur_reasons = overrides.get(fname, (1.0, []))
            new_factor = min(cur_factor, factor)
            new_reasons = cur_reasons + [f"[{f.rule_id}] {f.summary}"]
            overrides[fname] = (new_factor, new_reasons)

    out: list[FieldConsensus] = []
    for c in consensus_tuple:
        if c.field_name not in overrides:
            out.append(c)
            continue
        factor, extra_reasons = overrides[c.field_name]
        new_trust = round(c.trust_score * factor, 4)
        old_reason = c.review_reason or ""
        combined = "; ".join(
            ([old_reason] if old_reason else []) + extra_reasons,
        )
        out.append(
            cheque_consensus.FieldConsensus(
                field_name=c.field_name,
                value=c.value,
                normalized_value=c.normalized_value,
                trust_score=new_trust,
                votes=c.votes,
                winning_vote_count=c.winning_vote_count,
                review_reason=combined or None,
            ),
        )
    return tuple(out)
