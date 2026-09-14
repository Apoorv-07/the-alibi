"""taskfacts — the deterministic reading of "which obligation is this line about, and what does it say".

This module exists because of a measured bug, not for tidiness. The first version of the twin derived a
task's key from `subject_id(first 120 chars of the block)`, which produced rows like
`os-submitted_work_3_cpu` for a syllabus and `dbms-dbms_says_12_oct` for the chat line that contradicts it.
Both claims were *individually* verified — every quote verbatim, every date present in its span — and the
system still found zero conflicts, because two true statements about two different keys do not disagree.

Mis-attribution corrupts the same way over-attribution does, and a content verifier cannot see it: "is the
quote real, is the date in the quote" says nothing about *which obligation* the line was about. So subject
choice gets its own rules, listed here in the order they cost us recall:

1. **A key comes from a title shape, never from prose.** `LAB 4`, `dbms ia2`, `Assignment 3`,
   `Internal Assessment 2`. Numbered kinds must show their ordinal, which is what stops "75% theory AND lab
   counted separately" from becoming a task called `os-lab`. A phantom task carrying a genuine quote is the
   worst row a ledger can hold: it survives every check and means nothing.
2. **Title, date and deadline wording must sit in one window.** The window is the line (or the `·`-separated
   piece). It widens by exactly one neighbouring line, only under the conditions in `_window`, and never
   onto a line that names a *different* task.
3. **`due_at` needs deadline words, or an exam-sitting shape.** `Due:`, `deadline`, `closes`, `moved to`, or
   `... 15% (Thu 15 Oct 2026, Hall C)` from a grading table. A date beside a room number is a schedule
   detail; inventing a deadline out of it is the failure this product exists to prevent.
4. **No date in the window ⇒ no due date, and no weight row unless the same line names its own task.**
   `Lab (record + submission) 20%` is a real requirement with no deadline in this document: "unknown, ask".
5. **Ambiguity is escalated, never coin-flipped.** Two candidate dates in the window produce a note and no
   claim. This is what keeps the measured false-trust rate at zero on group-chat noise.
6. **Relative dates are not facts for the rule path.** "extended to Monday" is exactly the sentence an
   assistant must not be confident about, so weekday words are never resolved here.

Quotes are the raw source substring when the offsets are sane and the cleaned window otherwise; either way
the verifier decides, and a bad span fails loudly as `span_not_verbatim` rather than being stored. Every
refusal is returned in `notes` and counted by the caller, so "the twin is quiet" and "the twin is blind"
stay distinguishable in the UI.
"""

from __future__ import annotations

import json
import re

# Longest name first: "internal assessment" must win over "ia", "assignments" over "assignment".
NUMBERED_KINDS = [
    (r"internal[\s\-]+assessment", "ia"), (r"assignments?", "assignment"), (r"labs?", "lab"),
    (r"ias?", "ia"), (r"quizzes|quiz", "quiz"), (r"vivas?", "viva"), (r"tutorials?", "tutorial"),
]
BARE_KINDS = [
    (r"term[\s\-]+project(?:[\s\-]+demo)?", "project"), (r"end[\s\-]?sem(?:ester)?", "end_sem"),
    (r"registration", "registration"), (r"idea[\s\-]+submission", "submission"),
    (r"final[\s\-]+exam", "end_sem"),
]
# Resolved by `_kind_of`, not by dict lookup: the alternation matches the *text*, so "IA2" arrives as
# "IA" (the `s` of `ias?` is optional) and a dict lookup would KeyError on the first real syllabus.
_KIND_PATTERNS = NUMBERED_KINDS + BARE_KINDS


def _kind_of(raw: str) -> str:
    r = _clean(raw).lower()
    for pat, out in _KIND_PATTERNS:
        if re.fullmatch(pat, r, re.I):
            return out
        if re.fullmatch(pat.replace(r"[\s\-]+", " "), r, re.I):
            return out
    # "ia" from `ias?`, "assignment" from `assignments?`, "end_sem" from `end-sem`: strip the plural tail.
    for pat, out in _KIND_PATTERNS:
        core = re.sub(r"\??\(\?:(s|e\w*\?)\)|[?]+$|s\?", "", pat)
        if re.fullmatch(core, r, re.I):
            return out
    if re.match(r"^ia$", r):
        return "ia"
    if r.startswith("internal"):
        return "ia"
    if r.startswith("end"):
        return "end_sem"
    if r.startswith("term"):
        return "project"
    return ""

_COURSE = re.compile(r"(?i)\b(dbms|os|daa|aot|cn|se|mpc|python|maths?|coa|cd)\b")
_EVENT = re.compile(r"(?i)\b(sih|hackathon|placement[\s\-]+drive|placement|seminar|workshop|internship)\b")
LINE_GATE = re.compile(r"(?i)\b(due|deadline|submit|submission|close|closes|closed|extend|moved|shifted|"
                       r"postponed|reshuffled|held|conducted|cancelled|late|penalty|internal|lab|assignment"
                       r"|project|viva|quiz|tutorial|semester|registration|grading)\b|\bia\d?\b|\d{1,2}\s*%")
DUE_LINE = re.compile(r"(?i)(\bdue(\s+date)?\s*(?:is|:|on|by)?\b|\bdeadline\b|\bsubmission\b|\bsubmitted\b"
                      r"|\bsubmit\b|\bcloses?\b|\bclosed\b|\bconducted\b|\bmoved\s+to\b|\bshifted\s+to\b"
                      r"|\breshuffled\s+to\b|\bextended\s+to\b|\bpostponed\b|\blast\s+date\b"
                      r"|\bon\s+or\s+before\b)")
# A grading table states an exam sitting in the same breath as its weight: `IA2 15% (Fri 16 Oct 2026)`.
# Abbreviated weekday names are optional; `\\w+day` alone would not match "Fri".
_DAYNAME = r"(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day)?(?:\s*,)?"
GRADING_DATE = re.compile(rf"(?i)\b\d{{1,2}}\s*%\s*\(?\s*(?:on\s+)?{_DAYNAME}?\.?\s*\d{{1,2}}\s+"
                          r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\b")
# An actionable, explicit date. `24.09` is included because an intranet form writes dates that way, and
# `12 Oct` because people do. No weekday arithmetic anywhere in this module (rule 6).
# Numeric dates must carry a year. "10.00", "12.00" and "24.09" (a form field, a clock time, a partial
# stamp) are all shaped like a date and none of them is a deadline; requiring the year is a recall cost
# the demo pays happily, because a fabricated date in a schedule is a much more expensive error than a
# missing one. The month-name form does not need a year (the term context supplies it, and `dates_in`
# anchors it) but it must not run into more digits.
EXPLICIT_DATE = re.compile(r"(?i)\b\d{1,2}\s*,?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)(?![a-z])"
                           r"|\b(?:\d{1,2}[./]\d{1,2}[./]\d{2,4}|20\d\d-\d\d-\d\d)\b")
_WEIGHT = re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d)?)\s*%")
_LATE = re.compile(r"(?i)\blate\s+(?:submissions?|policy|penalty)\b[^\n.;]{0,120}")
# `18/09/2026, 21:41 - Name:` is a fact about the message, not about the deadline. Reading it as a
# candidate date made every chat line look ambiguous (stamp + date) and silently disabled the chat path.
_HEADER = re.compile(r"^\s*\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}[^\-\n]{0,16}-\s*[^:\n]{1,44}:\s")
_DOTS = re.compile(r"\s\.{2,}\s*")            # "Internal Assessment 1 ...... 15%" → "… 1 15%"

_NUM_ALT = "|".join(p for p, _ in NUMBERED_KINDS)
_BARE_ALT = "|".join(p for p, _ in BARE_KINDS)
_TITLE_NUM = re.compile(rf"(?i)\b(?P<kind>{_NUM_ALT})[\s\-\.]{{0,4}}(?P<ord>\d{{1,2}})(?![0-9])")
_BARE_T = re.compile(rf"(?i)\b(?P<kind2>{_BARE_ALT})(?![\s\-]?\d)")
# "this line has its own subject" — used both to scope a block and to detect a grading table.
_ANY_TITLE = re.compile(rf"(?i)\b(?:{_NUM_ALT})[\s\-\.]{{0,4}}\d{{1,2}}(?![0-9])"
                        rf"|\b(?:internal[\s\-]+assessment|term[\s\-]+project|end[\s\-]?sem(?:ester)?|"
                        rf"registration)\b")


def _clean(text: str) -> str:
    return _DOTS.sub(" ", re.sub(r"\s+", " ", text or "")).strip()


def _norm_kind(raw: str) -> str:
    r = _clean(raw).lower()
    for pat, out in NUMBERED_KINDS + BARE_KINDS:
        if re.fullmatch(pat, r, re.I) or re.fullmatch(pat.replace(r"[\s\-]+", " "), r, re.I):
            return out
    if r.startswith("internal"):
        return "ia"
    if r.startswith("end"):
        return "end_sem"
    if r.startswith("term"):
        return "project"
    return ""


def task_key(text: str, course_hint: str = "", allow_bare: bool = True) -> str | None:
    """Return `dbms-lab_4` for text naming that obligation, else None.

    Numbered kinds must show their ordinal; unnumbered ones (`project`, `registration`) stand alone. The
    course prefix comes from the text, then from the caller's hint, then from the event's owning department
    — never from a model. Two sources of the same deadline must reach the same key or the ledger holds two
    true facts that never meet, which is worse than one missing fact: nothing looks broken.
    """
    t = _clean(text)
    if not t:
        return None
    kind = ordv = None
    m = _TITLE_NUM.search(t)
    if m:
        kind, ordv = _kind_of(m.group("kind")), m.group("ord")
    elif allow_bare:
        m = _BARE_T.search(t)
        if m:
            kind = _kind_of(m.group("kind2"))
    if not kind:
        return None
    key = f"{kind}_{ordv}" if ordv else kind
    cm = _COURSE.search(t)
    if cm:
        course = cm.group(1).lower()
    elif _EVENT.search(t):
        course = course_hint.split()[0].lower() if course_hint else "cs"
    else:
        course = course_hint.split()[0].lower() if course_hint else ""
    return f"{course or 'misc'}-{key}"[:48]


def message_body(line: str) -> tuple[str, int]:
    """Strip a WhatsApp header, returning `(body, chars_stripped)` so offsets stay honest."""
    m = _HEADER.match(line or "")
    return (line[m.end():], m.end()) if m else (line, 0)


def clause_dates(text: str, anchor_year: int = 2026) -> list[str]:
    from .ground import dates_in                       # local import: ground must not import us
    return sorted(set(dates_in(text, anchor_year=anchor_year)))


def facts(text: str, *, anchor_year: int = 2026, course_hint: str = "", heading: str = "",
          allow_context: bool = True, block_start: int = 0) -> tuple[list[dict], list[str], int]:
    """`([facts], [skip notes], n_before_dedupe)` for one block of `text`.

    The third value counts what the extractor *read* before de-duplication, which is what a caller needs to
    know: on a re-ingest every fact is already on file, so the de-duplicated list is empty, and a caller
    that treats "nothing new" as "nothing found" will hand the block to a language model and let it invent a
    second, worse key for the same deadline. That exact cascade produced eight duplicate rows on the second
    demo sync until this number existed.

    `allow_context=True` is for *documents* (syllabus, notice photo, LMS page), where a heading genuinely
    owns the lines under it. `False` is for chat, where each message is its own document: inheriting the
    previous message's task is how "portal shows 13 Oct" would rewrite every deadline in the term while
    staying verbatim-true.
    """
    out: list[dict] = []
    notes: list[str] = []
    lines = (text or "").split("\n")
    if not "".join(lines).strip():
        return [], notes, 0
    offsets, pos = [], 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1
    # A document's first line seeds the scope *only* through a numbered title. "CS8586 DATABASE
    # MANAGEMENT SYSTEMS — Lab + Theory" mentions a lab but is not a lab; seeding from bare kinds turned
    # every unnumbered line in the file into a deadline for a task that does not exist.
    ctx: str | None = task_key(_clean(lines[0]), course_hint, allow_bare=False) if allow_context else None
    raw_n = 0        # facts read before de-duplication; see the docstring
    for i, raw in enumerate(lines):
        body, off = message_body(raw)
        clean = _clean(body)
        if len(clean) < 8 or not LINE_GATE.search(clean):
            continue
        # Count *title occurrences*, not distinct keys: on a grading row ("IA1 15% · IA2 15% (Fri 16 Oct
        # 2026)") the naive distinct-key scan can find only the first one — and if the first one is then
        # taken as the owner of the line, `IA1` inherits `IA2`'s exam date, verified verbatim, invisible to
        # the conflict detector. Two titles in one line is a table; every piece must stand alone.
        keys = _keys_in(clean, course_hint)
        n_titles = len(_ANY_TITLE.findall(clean)) + len(re.findall(r"(?i)i[ae]s?\s*\d{1,2}", clean))
        base = block_start + offsets[i] + off + (raw.find(body) if body in raw else 0)
        multi = len(keys) > 1 or n_titles > 1
        passes = [[(body, base)]] if not multi else []
        if multi:
            cur, sp = 0, []
            for m in re.finditer(r"[·•|]|--", body):
                sp.append((body[cur:m.start()], base + cur))
                cur = m.end()
            sp.append((body[cur:], base + cur))
            if len(sp) > 1:
                passes.append(sp)
        for chosen in passes or [[(body, base)]]:
            before = len(out)
            for piece, pbase in chosen:
                ctx = _piece(piece, pbase, i, lines, offsets, block_start, ctx, course_hint, anchor_year,
                             allow_context, out, notes, allow_bare=(i == 0),
                             )
            raw_n += len(out) - before
            if len(out) > before:
                break
    seen: set[tuple] = set()
    keep = []
    for f in out:
        k = (f["subject_id"], f["predicate"], json.dumps(f["value"], sort_keys=True))
        if k not in seen:
            seen.add(k)
            keep.append(f)
    return keep, notes, raw_n or len(out)


def _piece(piece: str, pbase: int, i: int, lines: list, offsets: list, block_start: int,
           ctx: str | None, course_hint: str, anchor_year: int, allow_context: bool,
           out: list, notes: list, allow_bare: bool = True) -> str | None:
    """Read one clause. Appends facts to `out`; returns the task scope in force afterwards."""
    clean = _clean(piece)
    if len(clean) < 8:
        return ctx
    # A bare kind (`registration closes 30 Sep 2026`, with no course or number anywhere) counts only when
    # the line itself states a deadline. Without that qualifier a stray "Lab 2" in prose would claim every
    # undated line below it, which is the very mis-attribution this module refuses to do.
    bare = allow_bare or bool(DUE_LINE.search(clean) and EXPLICIT_DATE.search(clean))
    own = task_key(clean, course_hint, allow_bare=bare)
    if own and _ANY_TITLE.search(clean) and not re.search(r"(?i)^\s*(grading|assessment|marks)", clean):
        ctx = own            # a titled line owns the facts it states — but a *summary* row names many
    subj = own or (ctx if allow_context else None)
    if not subj:
        if EXPLICIT_DATE.search(clean):
            notes.append("a date with no obligation named in the same line — not claimed")
        return ctx
    win, w_start, w_end, scoped = _window(clean, piece, pbase, i, lines, offsets, block_start, subj, own,
                                          allow_context, course_hint, len(_keys_in(clean, course_hint)))
    if not win:
        return ctx
    dm = EXPLICIT_DATE.search(win)
    if own and dm and len(win[0:dm.start()].replace(" ", "")) > 90:
        # The date is real but it sits at the far end of a long line, past several other clauses. Reading
        # it as *this* task's deadline is a guess about a document's layout, and a guess that survives
        # verification is the exact failure mode this module was written to prevent.
        notes.append(f"{subj}: date {cands_hint(win)} is too far from the title to attribute — not claimed")
        return ctx
    cands = clause_dates(win, anchor_year)
    span = {"subject_id": subj, "start": w_start, "end": w_end,
            "quote": _verbatim("\n".join(lines), block_start, w_start, w_end, win)}
    wm, lm = _WEIGHT.search(win), _LATE.search(win)
    if len(cands) > 1:
        notes.append(f"{subj}: {len(cands)} candidate dates in this line ({', '.join(cands)}) —"
                     " not claimed; a human or a verified model picks")
        return ctx
    if not cands:
        if own and wm and _is_weight(win, wm) and not subj.endswith("-end_sem"):
            out.append({**span, "predicate": "weight",
                        "value": {"weight": round(float(wm.group(1)) / 100, 4)}})
        if own and lm:
            out.append({**span, "predicate": "late_policy",
                        "value": {"late_policy": _clean(lm.group(0)).rstrip(".")}})
        return ctx
    if not (DUE_LINE.search(win) or GRADING_DATE.search(win)):
        notes.append(f"{subj}: a date in a line without deadline wording ({cands[0]}) — not a due date")
        return ctx
    if own is None and len(clean) > 90:
        # A long line with no title of its own, claiming a date because a *previous* line named a task: that
        # is scope inheritance over distance, the least defensible link in this file. Documents still
        # promote (a heading owns its sub-lines); chat, where every message is its own document, does not.
        notes.append(f"{subj}: an untitled 90+ character line inheriting a due date — needs a human")
        return ctx
    f = {**span, "predicate": "due_at", "value": {"date": cands[0], "precision": "day"}}
    if scoped:
        f["scoped"] = True
    out.append(f)
    if wm and _is_weight(win, wm) and not subj.endswith("-end_sem"):
        out.append({**span, "predicate": "weight", "value": {"weight": round(float(wm.group(1)) / 100, 4)}})
    if lm:
        out.append({**span, "predicate": "late_policy",
                    "value": {"late_policy": _clean(lm.group(0)).rstrip(".")}})
    return ctx


def cands_hint(win: str) -> str:
    return ", ".join(clause_dates(win)) or "no-date"


def _keys_in(clean: str, course_hint: str) -> list[str]:
    """Every distinct task key this line names. More than one ⇒ the line is a table, not a statement."""
    keys: list[str] = []
    for m in _ANY_TITLE.finditer(clean):
        k = task_key(clean[max(0, m.start() - 14):m.end() + 16], course_hint)
        if k and k not in keys:
            keys.append(k)
    return keys


def _verbatim(text: str, block_start: int, start: int, end: int, fallback: str) -> str:
    """The raw slice when the offsets are honest, else the cleaned window.

    `verify_claim` compares normalised text and `_promote` re-locates the quote by searching for it, so a
    cleaned window still verifies; what must never happen is storing a *slice of unrelated text* as
    evidence, which is why a bad slice falls back rather than being trusted.
    """
    a, b = start - block_start, end - block_start
    if 0 <= a < b <= len(text):
        got = text[a:b]
        if _clean(got) == fallback:
            return got.strip()
        if len(got.splitlines()) == 1:
            return got.strip()
    return fallback


def _neighbour(line: str) -> tuple[str, int]:
    body, off = message_body(line)
    s = body.strip()
    return body, off + (line[off:].find(s) if s in line[off:] else 0)


def _window(clean: str, piece: str, pbase: int, i: int, lines: list, offsets: list, block_start: int,
            subj: str | None, own: str | None, allow_context: bool, course_hint: str,
            n_keys: int = 1) -> tuple[str, int, int, bool]:
    """`(window_text, start, end, scoped)` — the slice this clause's facts must be provable from.

    The window is the piece. It widens to the line *after* when the piece has a title and no date, because
    that is what "Submission: Saturday 11 Oct 2026" under a `LAB 4` heading means. It widens to the line
    *before* when the piece has a date but the weight sits on the label row above it. Two hard limits: one
    line only, and never onto a line that names a different task. Without them a stray "Hard copy in class
    on 13 Oct" pools its date with its neighbour and a page collapses into one confident, wrong task.
    """
    stripped = piece.strip()
    start = pbase + (piece.find(stripped) if stripped in piece else 0)
    text, end, scoped = clean, start + len(stripped), own is None and allow_context
    if not allow_context:
        return text, start, end, scoped
    if not EXPLICIT_DATE.search(text) and i + 1 < len(lines) and n_keys < 2 and own is not None:
        nb, no = _neighbour(lines[i + 1])
        nxt = _clean(nb)
        nk = task_key(nxt, course_hint)
        gate = (nk is None) if own is not None else (nk is None or nk == subj)
        if nxt and EXPLICIT_DATE.search(nxt) and len(nxt) > 8 and gate:
            text = f"{text} {nxt}"
            end = block_start + offsets[i + 1] + no + len(nb.strip())
            scoped = True
    elif EXPLICIT_DATE.search(text) and not _WEIGHT.search(text) and i > 0 and n_keys < 2:
        pb, po = _neighbour(lines[i - 1])
        prev = _clean(pb)
        if prev and _WEIGHT.search(prev) and len(prev) > 8 and task_key(prev, course_hint) == subj:
            text = f"{prev} {text}"
            start = block_start + offsets[i - 1] + po
    return text, start, end, scoped


def _is_weight(win: str, m: re.Match) -> bool:
    """A percentage is a *weight* only when an assessment word sits before it in the same window.

    Without this, "10% of internal marks" and "−10% per calendar day" both read as weights, and a plan
    starts budgeting for a penalty as if it were a grade — the difference between respecting a documented
    late policy and inventing one.
    """
    head = win[:m.start()]
    return bool(re.search(r"(?i)(grading|weight|marks|internal|\bia\b|lab|project|quiz|viva|end[\s\-]?sem|"
                          r"assignment|assessment|term|record)", head))


def seen_kinds(text: str) -> list[str]:
    """Which obligation kinds a text names — used by the UI to say what it deliberately ignored."""
    out: list[str] = []
    for pat in [p for p, _ in NUMBERED_KINDS + BARE_KINDS]:
        for m in re.finditer(rf"(?i)(?:{pat})(?:[\s\-\.]{{0,4}}\d{{1,2}})?", text or ""):
            k = _kind_of(re.sub(r"[\s\-.]*\d{1,2}$", "", m.group(0)))
            if k and k not in out:
                out.append(k)
    return out
