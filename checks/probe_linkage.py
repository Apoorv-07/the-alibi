r"""Date -> subject linkage probe. This is the hard part of extraction, and no one demos it.

Every extractor in this space puts (date, phrase) pairs into one bag and lets the reader sort
out who they belong to. Here we measure what that costs: pairing each date with the *nearest*
task mention on the same line, and refusing to guess when two mentions tie.

Run: python3 checks/probe_linkage.py          # prints per-line pairings from the real corpus
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alibi.ground import dates_in                    # noqa: E402
import corpus.demo_corpus as C                       # noqa: E402

# Task references. Deliberately narrow: a bare "lab 4" in chat noise is not an obligation claim.
GOLD_PATS = {
    "dbms_lab4":   r"\blab[- ]?4\b",
    "os_assign3":  r"\b(assignment|a) ?3\b",
    "daa_assign4": r"\bassignment ?4\b|\ba4\b",
    "os_ia2":      r"\bos (internal assessment|ia) ?2\b|\binternal assessment 2\b",
    "dbms_ia2":    r"\b(internal assessment ?2|ia ?2)\b",   # needs a doc hint: IA2 is course-agnostic
    "sih_reg":     r"\bregistration clos\w*",
    "dbms_project": r"\bterm project\b|\bproject demo\b",
    "sih_idea":    r"\bidea submission\b",
    "sih_present": r"\bfinalists present\b",
}

# A date token as written in the wild: numeric with separator, or day+month(+year), with the
# ordinal and the optional trailing year that Indian notices love.
DATE_TOKEN = (
    r"(?:\d{4}-\d{1,2}-\d{1,2}"                                   # 2026-10-11
    r"|\d{1,2}(?:st|nd|rd|th)?\s*[./-]\s*\d{1,2}(?:st|nd|rd|th)?\s*[./-]\s*\d{2,4}"  # 11/10/26
    r"|(?:\d{1,2}\s*[–—-]\s*)?\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?,?\s*\d{0,4}"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?)"
)
DATE_RE = re.compile(DATE_TOKEN, re.I)
MONTH_HINT = re.compile(r"(?i)(\d{4})")


def candidates(line: str, year: int) -> list[tuple[int, int, str]]:
    """(start, end, iso) for every date token that resolves to exactly one ISO date."""
    out = []
    for m in DATE_RE.finditer(line):
        frag = m.group(0)
        assert frag.count("Oct,") == 0 or "finalists" not in frag, frag
        ds = dates_in(frag, year)
        if len(ds) != 1:
            continue                                   # ambiguous inside the token -> not ours
        iso = ds[0]
        stated = MONTH_HINT.findall(line[max(0, m.start() - 12):m.end() + 12])
        if stated and iso[:4] not in stated:
            continue                                   # year explicitly said, and not this one
        out.append((m.start(), m.end(), iso))
    return out


# Which document scopes an otherwise course-agnostic phrase. Without this, "Internal
# Assessment 2" inside the OS syllabus also matches DBMS's pattern and the two tie — an
# ambiguity invented by concatenating files, not one the student actually faces.
DOC_HINT = {"dbms_ia2": ("dbms", "database"), "os_ia2": ("operating systems", " cs8591"),
            "dbms_lab4": ("dbms", "database-systems", "cs8586")}


def pair(line: str, year: int = 2026, doc: str = "") -> tuple[list[tuple[str, str]], list[str]]:
    """Pair each date with its nearest task mention. Returns (pairs, quarantined_tasks).

    A task is quarantined (not guessed) when it ties with another task for the same date, or
    when the nearest mention is further than MAX_LINK characters away — 'Portal closes 11 Oct'
    three sentences later is not evidence that the assignment is due 11 Oct.
    """
    MAX_LINK = 90
    ATTACHED = 28      # a date within this many chars of a phrase belongs to it, full stop
    refs: dict[str, list[int]] = {}
    low_doc = doc.lower()
    for sid, pat in GOLD_PATS.items():
        if sid in DOC_HINT and DOC_HINT[sid] and not any(h in low_doc for h in DOC_HINT[sid]):
            continue
        pos = [m.start() for m in re.finditer(pat, line, re.I)]
        if pos:
            refs[sid] = pos
    if not refs:
        return [], []
    pairs, quarantined = [], []
    for s, e, iso in candidates(line, year):
        mid = (s + e) / 2                      # measure to the token's centre, not its start:
        scored = sorted((min(abs(p - mid) for p in pos), sid)   # 'finalists present' sits just
        ) if False else sorted((min(abs(p - mid) for p in pos), sid) for sid, pos in refs.items())
        if not scored or scored[0][0] > MAX_LINK:
            continue
        if len(scored) > 1 and scored[0][0] > ATTACHED and scored[1][0] == scored[0][0]:
            for sid in (scored[0][1], scored[1][1]):
                quarantined.append(f"{sid}:{iso}")
            continue
        pairs.append((scored[0][1], iso))
    return pairs, quarantined


def main() -> None:
    from alibi.ingest import parse_whatsapp, group_for_model, blocks_from_msg_groups
    body_only = "\n".join(b.text for b in blocks_from_msg_groups(
        group_for_model(parse_whatsapp(C.WHATSAPP_CHAT, 2026))))
    docs = [("syllabus_pdf", C.SYLLABUS_OS), ("syllabus_pdf", C.SYLLABUS_DBMS),
            ("syllabus_pdf", C.SYLLABUS_DAA), ("chat_export", body_only),
            ("notice_photo", C.NOTICE_BOARD_PHOTO), ("attendance_screen", C.ERP_ATTENDANCE)]
    texts = dict(docs)
    gold = {g["id"]: g["due"] for g in C.GOLD["task"] if g.get("due")}
    planted = {p[0]: p[2] for p in C.GOLD["planted_conflicts"] if p[1] == "due_at"}
    got: dict[str, set] = {}
    n_lines = n_pairs = n_q = 0
    # Iterate at the product's own granularity: a syllabus block (a multi-line entry), not a
    # raw line. 'LAB 4 — Normalisation' and its 'Submission: 11 Oct' sit on separate lines, and
    # a line-scoped extractor would call that un-linkable. Blocks are how the router sees text.
    # One document at a time. Concatenating three syllabi creates *fake* ambiguity: 'Internal
    # Assessment 2 … 15 Oct' is only ambiguous if another course's IA2 can also claim it, and
    # that course lives in a different PDF. Document is the natural disambiguation scope.
    from alibi.ingest import blocks_from_syllabus
    units: list[tuple[str, str]] = []
    for kind, text in docs:
        head = text[:400]
        if kind == "syllabus_pdf":
            units += [(head, b.text) for b in blocks_from_syllabus(text)]
        else:
            units += [(head, l) for l in text.splitlines()]
    for doc, line in units:
        if not line.strip():
            continue
        prs, q = pair(line, 2026, doc)
        if not prs and not q:
            continue
        n_lines += 1
        for sid, iso in prs:
            got.setdefault(sid, set()).add(iso)
        n_pairs += len(prs)
        n_q += len(q)
        if q:
            print(f"  QUARANTINE {q}   <- {line.strip()[:78]!r}")
    print(f"\n{n_lines} lines carried task+date tokens; {n_pairs} claims emitted, {n_q} quarantined\n")
    right = wrong = 0
    for sid, exp in gold.items():
        g = got.get(sid, set())
        cands = planted.get(sid)
        want = min(cands) if cands else exp
        ok = bool(g) and want in g
        right += ok
        wrong += not ok
        if not ok:
            print(f"  MISS {sid:14} want {want}  got {sorted(g)}   "
                  f"{'(correctly deferred: two subjects tie for this date)' if not g else ''}")
    print(f"\ngold tasks whose asserted dates were all attributed correctly: {right}/{len(gold)}")
    print(f"over-attributed (task got a date some other task owns): "
          f"{sum(1 for sid, g in got.items() for d in g if d not in _allowed(sid))}")


def _allowed(sid: str) -> set[str]:
    """Every date legitimately assertable about this task, from any source: gold plus the
    contradiction the corpus deliberately planted for it."""
    out = set()
    for g in C.GOLD["task"]:
        if g["id"] == sid and g.get("due"):
            out.add(g["due"])
    for s, p, vals in C.GOLD["planted_conflicts"]:
        if s == sid and p == "due_at":
            out.update(vals)
    return out


if __name__ == "__main__":
    main()
