"""Shared 1-based page-selector parsing for the PDF capabilities.

Helper module (underscore-prefixed, so the capability loader skips it).
``cap.pdf_tools`` and ``cap.pdf_extract`` accept the same selector shape —
a list mixing single integers and inclusive ``"start-end"`` range strings,
e.g. ``[1, "3-5", 8]`` — so the parsing and its error messages live here
once, parameterized by the reporting capability's ref.
"""

from __future__ import annotations

# A single 'lo-hi' range entry is expanded into a concrete list, so a hostile
# selector like "1-2000000000" would materialize billions of ints (tens of GB)
# before any per-page bound check runs. Cap the *span* a single range may
# expand to and reject over-wide ranges by arithmetic (hi - lo) without ever
# building the list. No legitimate PDF page selection approaches this; the
# per-page page_count bound in parse_page_selector still rejects anything that
# overruns the actual document.
_MAX_RANGE_SPAN = 1_000_000


def expand_entry(entry: int | str, *, cap_ref: str) -> list[int]:
    """Expand one page-selector entry into a list of 1-based page numbers."""
    if isinstance(entry, bool):  # bool is an int subclass; reject explicitly
        raise RuntimeError(f"{cap_ref}: invalid page entry {entry!r}")
    if isinstance(entry, int):
        return [entry]
    if isinstance(entry, str):
        text = entry.strip()
        if "-" in text:
            lo_s, _, hi_s = text.partition("-")
            try:
                lo, hi = int(lo_s.strip()), int(hi_s.strip())
            except ValueError as e:
                raise RuntimeError(
                    f"{cap_ref}: malformed page range {entry!r}"
                ) from e
            if lo > hi:
                raise RuntimeError(
                    f"{cap_ref}: page range {entry!r} is reversed "
                    f"(start > end)"
                )
            if hi - lo + 1 > _MAX_RANGE_SPAN:
                # Refuse by arithmetic before list(range(...)) allocates the
                # whole span — a memory-DoS guard, not a correctness one.
                raise RuntimeError(
                    f"{cap_ref}: page range {entry!r} spans more than "
                    f"{_MAX_RANGE_SPAN} pages; refusing"
                )
            return list(range(lo, hi + 1))
        try:
            return [int(text)]
        except ValueError as e:
            raise RuntimeError(
                f"{cap_ref}: malformed page entry {entry!r}"
            ) from e
    raise RuntimeError(f"{cap_ref}: invalid page entry {entry!r}")


def parse_page_selector(
    pages: list[int | str] | None, page_count: int, *, cap_ref: str
) -> list[int]:
    """Resolve a 1-based page selector into a list of 0-based page indices.

    Accepts integers and inclusive 'start-end' range strings. Preserves the
    caller's order (and allows duplicates — useful for extract). Raises
    RuntimeError on a malformed entry or an out-of-range page.
    """
    if page_count <= 0:
        raise RuntimeError(f"{cap_ref}: source PDF has no pages")
    indices: list[int] = []
    for entry in pages or []:
        for one_based in expand_entry(entry, cap_ref=cap_ref):
            if one_based < 1 or one_based > page_count:
                raise RuntimeError(
                    f"{cap_ref}: page {one_based} out of range; the "
                    f"document has {page_count} page(s)"
                )
            indices.append(one_based - 1)
    return indices
