r"""alibi.link — attach each date to the obligation it actually belongs to.

Why this exists as its own deterministic stage
----------------------------------------------
Grounding proves *a quote exists*. It proves nothing about **which** entity a number inside that
quote describes. Both of the failures that matter most in this product come from that gap:

  * Over-attribution. A line like `guys dbms lab 4 extended to Monday` next to an export
    timestamp `18/09/2026` will yield due_at=2026-09-18 to any bag-of-dates extractor. That date
    is *earlier* than the real deadline, so it wins `earliest_safe` — a conservative policy is
    corrupted precisely by the extra confidence of a wrong-but-earlier value. Measured on the
    demo corpus: cross-product attribution emitted 17 claims, 5 of them attributed to a task that
    never asserted them, and reconciliation fell from 8/9 correct to 3/9.
  * Silent guessing. When two obligations genuinely tie for one date (`Internal Assessment 2 …
    15 Oct` sitting in a merged three-PDF view), the honest answer is "ask a human", and the
    ledger has a queue for exactly that.

So: pair by proximity within a single document, refuse to pair when it is a tie or too far away.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .ground import dates_in

# Words that *attach* a date to an obligation. Kept separate from the corpus's task ids so this
# module stays a general component; the caller supplies the label vocabulary it cares about.
DEFAULT_HINTS: dict[str, str] = {
    "lab": r"\blab[- ]?\d+\b",
    "assignment": r"\bassignment\s*\d+\b|\ba\d+\b",
    "internal_assessment": r"\binternal assessment\s*\d+\b|\bia\s*\d\b",
    "registration": r"\bregistration\b",
    "idea_submission": r"\bidea submission\b",
    "presentation": r"\bfinalists present\b|\bpresentation\b|\bdemo\b",
    "project": r"\bterm project\b|\bproject demo\b",
    "exam": r"\b(exam|end semester|ee)\b",
    "submission": r"\bsubmission\b|\bsubmit\b",
}

# A date as written in the wild on Indian academic artefacts. Deliberately does NOT cross a
# comma-then-word boundary for the year: `9 Oct, 10:00` must not read as day 9 / Oct / year 10.
_WS = r"[ \t\u00a0]"
_MON = (r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?")
_ORD = r"(?:st|nd|rd|th)?"
DATE_TOKEN = (
    r"(?:\d{4}-\d{1,2}-\d{1,2}"                                        # 2026-10-11
    r"|\d{1,2}" + _ORD + r"\s*[./-]\s*\d{1,2}" + _ORD + r"\s*[./-]\s*\d{2,4}"  # 18/09/2026
    r"|(?:\d{1,2}\s*[–—-]\s*)?\d{1,2}" + _ORD + r"[ \t\u00a0]+" + _MON          # 1–5 Oct / 11 Oct
    + r"(?:[ \t\u00a0],?[ \t\u00a0]?\d{4})?"                                   # … , 2026
    r"|" + _MON + r"[ \t\u00a0]*\d{1,2}" + _ORD                                  # Oct 11
    + r"(?:[ \t\u00a0],?[ \t\u00a0]*\d{4})?)"                                   # Oct 11, 2026
)
_DATE_RE = re.compile(DATE_TOKEN, re.I)
_NEAR_YEAR = re.compile(r"\b(\d{4})\b")


@dataclass
class Link:
    label: str
    iso: str
    quote: str
    distance: float
    confident: bool
    note: str = ""


def link_dates(text: str, hints: dict[str, str] | None = None, *, anchor_year: int = 2026,
               max_link: int = 90, attached_within: int = 28) -> tuple[list[Link], list[Link]]:
    """Return (paired, ambiguous).

    `attached_within` wins outright: a date sitting right after 'registration closes' belongs to
    it even if some other label appears 20 characters later — the sentence ordering, not the
    distance contest, decides that. Only when *no* label is adjacent do we compare distances, and
    a tie becomes an `ambiguous` item instead of a coin flip.
    """
    hints = hints or DEFAULT_HINTS
    refs: dict[str, list[int]] = {}
    for label, pat in hints.items():
        pos = [m.start() + (m.end() - m.start()) / 2 for m in re.finditer(pat, text, re.I)]
        if pos:
            refs[label] = pos
    if not refs:
        return [], []

    paired: list[Link] = []
    ambiguous: list[Link] = []
    for m in _DATE_RE.finditer(text):
        frag = m.group(0).strip()
        cands = [d for d in dates_in(frag, anchor_year)
                 if d[:4] in (m.group(0) + text[max(0, m.end()):m.end() + 6]) or not _NEAR_YEAR.search(
                     text[max(0, m.start() - 10):m.end() + 10])]
        if len(cands) != 1:
            continue                     # ambiguous inside the token itself -> not ours to assert
        iso = cands[0]
        mid = (m.start() + m.end()) / 2
        scored = sorted((min(abs(p - mid) for p in pos), lbl) for lbl, pos in refs.items())
        if not scored or scored[0][0] > max_link:
            continue                     # nothing nearby owns this date; it is not a deadline
        quote = _sentence(text, m.start(), m.end())
        if len(scored) > 1 and scored[0][0] > attached_within and scored[1][0] == scored[0][0]:
            for _, lbl in scored[:2]:
                ambiguous.append(Link(lbl, iso, quote, scored[0][0], False,
                                       "two obligations tie for this date"))
            continue
        paired.append(Link(scored[0][1], iso, quote, round(scored[0][0], 1), True))
    return paired, ambiguous


def _sentence(text: str, a: int, b: int) -> str:
    """The sentence (or line) containing the date — a verbatim span, because the verifier only
    accepts verbatim spans. Newline-first, then period: chat blocks are one line per message."""
    ls = text.rfind("\n", 0, a) + 1
    le = text.find("\n", b)
    le = len(text) if le < 0 else le
    seg = text[ls:le]
    if len(seg) > 300:                                   # long syllabus paragraph: tighten
        s2 = max(seg.rfind(". ", 0, a - ls), seg.rfind("; ", 0, a - ls)) + 2
        e2 = seg.find(". ", b - ls)
        e2 = len(seg) if e2 < 0 else e2 + 1
        if e2 - s2 > 30:
            seg = seg[s2:e2]
    return seg.strip()


def gold_pairs(text: str, task_ids: dict[str, str], *, anchor_year: int = 2026,
               doc_hint: str = "") -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Convenience for the evaluation corpus: map our generic labels onto gold task ids.

    `task_ids` is {gold_id: label-regex}. Course-agnostic labels (IA2!) are gated on `doc_hint`,
    the same way the real pipeline scopes a claim to the document it came from — three syllabi
    concatenated create an ambiguity that no student ever faced.
    """
    hint_pat = {g: re.compile(p, re.I) for g, p in task_ids.items()}
    paired: list[tuple[str, str]] = []
    amb: list[tuple[str, str]] = []
    for m in _DATE_RE.finditer(text):
        frag = m.group(0).strip()
        cands = dates_in(frag, anchor_year)
        if len(cands) != 1:
            continue
        iso = cands[0]
        mid = (m.start() + m.end()) / 2
        scored: list[tuple[float, str]] = []
        for gid, pat in hint_pat.items():
            if gid in _COURSE_SCOPED and doc_hint and not any(
                    re.search(rf"(?<![a-z]){re.escape(h)}", doc_hint, re.I)
                    for h in _COURSE_SCOPED[gid]):
                # short aliases ("os") must match as words, or "DAA" would contain "OS"'… and any
                # hint starting with the alias ("OS" itself) would be skipped by a naive `in`.
                continue
                continue
            for r in pat.finditer(text):
                scored.append((abs(r.start() + (r.end() - r.start()) / 2 - mid), gid))
        if not scored:
            continue
        scored.sort()
        if scored[0][0] > 90:
            continue
        if len(scored) > 1 and scored[0][0] > 28 and scored[1][0] == scored[0][0]:
            amb += [(scored[0][1], iso), (scored[1][1], iso)]
            continue
        paired.append((scored[0][1], iso))
    return paired, amb


# Gold ids whose phrase does not name the course, so they need document scoping. An id absent
# from this table is matched anywhere in the block.
_COURSE_SCOPED = {"dbms_ia2": ("dbms", "database", "cs8586"),
                  "dbms_lab4": ("dbms", "cs8586", "database"),
                  "dbms_project": ("dbms", "database", "cs8586"),
                  "os_ia2": ("operating systems", "cs8591", "os"),
                  "os_assign3": ("operating systems", "cs8591", "os"),
                  "daa_assign4": ("design and analysis", "cs8491", "daa")}
