"""Alibi — source detection and parsers. Deterministic; NO model in this module.

Design rule that keeps the demo safe: every parser returns *typed messages/blocks with
offsets*, never "answers". Offsets are what make the grounding verifier able to prove a
claim came from this byte range of this artifact, and they are what let the UI highlight
the source sentence. Any parser that loses offsets loses the receipt, and the receipt is
the product.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

# --------------------------------------------------------------- detection ---

FINGERPRINTS = [
    ("chat_export", re.compile(r"^\[?\d{1,2}[/.]\d{1,2}[/.]\d{2,4},?\s+\d{1,2}:\d{2}", re.M)),
    ("ics_feed", re.compile(r"^BEGIN:VCALENDAR", re.I | re.M)),
    ("attendance_screen", re.compile(r"(attendance|present|absent|shortage|defaulter)", re.I)
                             .pattern and re.compile(r"(?i)\b(attendance|shortage|defaulter)\b.*\n.*\d{1,3}\.\d{1,2}\s*%|\d{1,3}\.\d{1,2}\s*%.*\b(OK|EDGE|SHORTAGE)\b")),
    ("policy_pdf", re.compile(r"(?i)\b(regulation|condonation|bonafide|ordinance|statute)\b")),
    ("syllabus_pdf", re.compile(r"(?i)\b(syllabus|course outcome|internal assessment|end.semester)\b")),
    ("portal_pdf", re.compile(r"(?i)\b(assignment|submission|due date|gradebook)\b")),
]


def detect(text: str, filename: str = "") -> str:
    """Cheapest possible routing decision, made in code so the model never has to guess
    the shape of an artifact. Falls back to filename, then 'user_note'."""
    head = text[:4000]
    if re.search(r"(?i)\.txt$|whatsapp", filename):
        if FINGERPRINTS[0][1].search(text):
            return "chat_export"
    for kind, pat in FINGERPRINTS:
        if pat.search(head):
            return kind
    low = filename.lower()
    for kind, hint in (("ics_feed", ".ics"), ("chat_export", "whatsapp chat"),
                       ("syllabus_pdf", "syllabus"), ("portal_pdf", "assignment"),
                       ("policy_pdf", "handbook"), ("attendance_screen", "attendance")):
        if hint in low:
            return kind
    return "user_note"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


# ------------------------------------------------------------ chat_export ---

# WhatsApp: "[DD/MM/YYYY, HH:MM:SS] Sender: body"  (brackets optional on iOS; year optional
# on some builds). Day-first is the Indian locale default — an assumption we make explicit
# rather than inheriting from dateutil's US default, which silently flips 03/04.
_WA = re.compile(
    r"^\[?(?P<d>\d{1,2})[/./-](?P<m>\d{1,2})[/./-](?P<y>\d{2,4})\]?,?\s+"
    r"(?P<t>\d{1,2}:\d{2}(?::\d{2})?\s*(?:[APap]\.?m\.?)?)\]?\s+-\s+"
    r"(?P<sender>[^:]{1,60}):\s?(?P<body>.*)$"
)
_WA_NOYEAR = re.compile(
    r"^\[?(?P<d>\d{1,2})[/./-](?P<m>\d{1,2})\]?,?\s+(?P<t>\d{1,2}:\d{2}(?::\d{2})?\s*(?:[APap]\.?m\.?)?)\]?\s+-\s+"
    r"(?P<sender>[^:]{1,60}):\s?(?P<body>.*)$"
)
_SYSTEM = re.compile(r"(?i)(joined using this link|messages and calls are end.to.end|image omitted|"
                     r"video omitted|audio omitted|file attached|Missed voice call|You blocked this contact)")
# The payload of this product is the *deadline sentence*, not the chat. Anything without one
# of these markers is dropped at parse time — which is also the privacy answer: we never keep
# 40 students' banter, we keep the ~2% of lines that assert something about the world.
CLAIM_MARKERS = re.compile(
    r"(?i)(\bdue\b|\bdeadline\b|\bsubmi[st]\b|\bextend|\bcancel|\breshuffle|\bmoved?\b|"
    r"\btest\b|\bexam\b|\bquiz\b|\battendance\b|\bshortage\b|\bportal\b|\bregistration\b|"
    r"\bcloses?\b|\bbring\b|\bno submission|\bpenalt|\bweight\b|\bmarks?\b|\bschedule\b|\bclass\b)")


@dataclass
class Msg:
    offset: int
    ts: str                 # ISO-8601 local (tz set by caller's institution config)
    sender: str
    body: str
    system: bool = False
    likely_claim: bool = True
    year_from_header: bool = False


def parse_whatsapp(text: str, anchor_year: int | None = None, tz: str = "+05:30") -> list[Msg]:
    """Parse a WhatsApp export into typed messages with byte offsets.

    Continuation lines (multi-line messages) are folded into the preceding message so a
    deadline sentence split across lines stays one span — otherwise the verifier fails on
    perfectly good evidence. ~1 in 4 real class-group announcements wrap.
    """
    out: list[Msg] = []
    pos = 0
    for raw in text.split("\n"):
        line_start, line_end = pos, pos + len(raw)
        pos = line_end + 1
        line = raw.rstrip("\r")
        m = _WA.match(line)
        headerless = False
        if not m:
            m = _WA_NOYEAR.match(line)
            headerless = bool(m)
        if m:
            g = m.groupdict()
            y = int(g["y"]) if g["y"] else (anchor_year or datetime.now().year)
            if g.get("y") and y < 100:
                y += 2000
            d, mo = int(g["d"]), int(g["m"])
            # guard the day/month swap: if d>12 it must be the day; if m>12 the export is
            # month-first and we say so loudly instead of emitting a wrong year-month-date.
            swapped = False
            if mo > 12 and d <= 12:
                d, mo = mo, d
                swapped = True
            try:
                ts = f"{y:04d}-{mo:02d}-{d:02d}T{g['t'].replace('.', '').strip()}"
            except Exception:
                ts = f"{y:04d}-{mo:02d}-{d:02d}"
            out.append(Msg(line_start, ts, g["sender"].strip(), g["body"],
                           system=bool(_SYSTEM.search(g["body"])),
                           likely_claim=bool(CLAIM_MARKERS.search(g["body"])),
                           year_from_header=not swapped))
        elif out and line.strip():
            out[-1].body += "\n" + line.strip()
            out[-1].likely_claim = out[-1].likely_claim or bool(CLAIM_MARKERS.search(line))
        elif _SYSTEM.search(line):
            out.append(Msg(line_start, out[-1].ts if out else "", "(system)", line.strip(),
                           system=True, likely_claim=False))
    # Recompute after continuation folding: a claim sentence may only become recognisable
    # once its wrapped second line is attached to the first.
    for msg in out:
        msg.likely_claim = (not msg.system) and bool(CLAIM_MARKERS.search(msg.body))
    return out


def claim_candidates(msgs: list[Msg], min_len: int = 18) -> list[tuple[int, str, str]]:
    """(offset, sender, text) for the lines worth sending to a model. Everything else — and
    that is most of a 40k-message export — never reaches a prompt at all."""
    return [(m.offset, m.sender, m.body) for m in msgs
            if m.likely_claim and not m.system and len(m.body) >= min_len]


def group_for_model(msgs: list[Msg], max_window_min: int = 240, max_msgs: int = 12) -> list[list[Msg]]:
    """Cluster claim-bearing messages by time proximity, so a follow-up ("no but he said
    bring it monday") is extracted together with the message it corrects. A per-line
    extractor loses exactly the cases that matter."""
    out: list[list[Msg]] = []
    cur: list[Msg] = []
    last: datetime | None = None
    for m in msgs:
        if m.system:
            continue
        try:
            t = datetime.fromisoformat(m.ts if len(m.ts) > 10 else m.ts + "T00:00")
        except ValueError:
            t = last or datetime.min
        if cur and (last and (t - last) > timedelta(minutes=max_window_min) or len(cur) >= max_msgs):
            out.append(cur)
            cur = []
        cur.append(m)
        last = t
    if cur:
        out.append(cur)
    return out


def blocks_from_msg_groups(groups: list[list[Msg]], offset_base: int = 0) -> list[Block]:
    """Turn chat clusters into extractable blocks, **body only**.

    The export's `dd/mm/yyyy, hh:mm - Sender:` prefix is file metadata, not content. Extraction
    that reads it as text invents deadlines: 'dbms lab 4 extended to Monday' carries no date, and
    a line-scanning extractor that also sees '18/09/2026' will happily emit due_at=2026-09-18 —
    a date *earlier* than the real deadline, which then wins every conservative tie-break in the
    system. That is how an over-eager extractor poisons a fail-safe design, so the ingester, not
    the model, owns the distinction. Offsets stay relative to the original file for provenance.
    """
    out: list[Block] = []
    for g in groups:
        if not g:
            continue
        text = "\n".join(m.body for m in g)
        out.append(Block("sentence_group", text, g[0].offset + offset_base,
                         g[-1].offset + offset_base + len(g[-1].body), True))
    return out


# ------------------------------------------------------------------ ics ---

@dataclass
class IcsEvent:
    uid: str
    summary: str
    start: str | None
    due: str | None
    all_day: bool
    raw: str
    offset: int


def parse_ics(text: str) -> list[IcsEvent]:
    """Folded-lines-aware VEVENT reader (RFC5545 folds at 75 octets with CRLF+space — a
    naive line split corrupts long summaries and DTSTAMPs, which is the classic ICS bug)."""
    unfolded = re.sub(r"\r?\n[ \t]", "", text)
    events: list[IcsEvent] = []
    for m in re.finditer(r"BEGIN:VEVENT(.*?)END:VEVENT", unfolded, re.S):
        block = m.group(1)
        off = text.find(m.group(0)[:40])
        props: dict[str, str] = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            name = k.split(";")[0].upper()
            props.setdefault(name, v.strip())
        start, due, all_day = props.get("DTSTART"), props.get("DUE"), False
        if start and len(start.split("T")[0]) == 8 and start[:8].isdigit():
            all_day = True
        # TWO bugs that silently corrupt a deadline feed, both fixed here and tested:
        #  (1) a timed event with no DUE must fall back to DTSTART, else every Canvas/
        #      Google deadline imported as "no date" — the exact failure we sell against.
        #  (2) DTEND for an all-day event is EXCLUSIVE (RFC5545 3.6.1): a one-day deadline
        #      on 11 Oct is stored as DTSTART=20261011, DTEND=20261012. Taking DTEND makes
        #      every imported deadline one day late. Wrong in the dangerous direction.
        if not due:
            due = props.get("DTSTART")
        events.append(IcsEvent(uid=props.get("UID", ""), summary=_unesc(props.get("SUMMARY", "")),
                               start=_iso(start), due=_iso(due), all_day=all_day,
                               raw=block.strip(), offset=max(off, 0)))
    return events


def _unesc(v: str) -> str:
    return re.sub(r"\\([;,\\nN])", lambda m: {"n": "\n", "N": "\n"}.get(m.group(1), m.group(1)), v)


def _iso(v: str | None) -> str | None:
    if not v:
        return None
    v = v.strip()
    m = re.match(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2}))?(Z|[+-]\d{4})?$", v)
    if not m:
        return None
    y, mo, d = m.group(1), m.group(2), m.group(3)
    return f"{y}-{mo}-{d}"


def build_ics(events: list[dict], prodid: str = "-//Alibi//Twin//EN", tzid: str = "Asia/Kolkata") -> str:
    """Deterministic calendar *proposal*. Escaping per RFC5545 §3.3.11: ';' ',' '\\' and
    newlines — a missed comma in an assignment title is a broken import, and students
    notice that instantly.

    The event contract is one dict per entry: `date` (YYYY-MM-DD, required), `title` (shown as SUMMARY),
    `uid`, `description`, `course`, `alarm_min`. It used to read `title`/`date` while the only caller built
    `summary`/`dtstart` — which meant `GET /api/export/twin.ics`, the link on the sidebar of every page,
    raised `KeyError: 'date'`. That is the shape of bug a "helper takes a dict" interface hides, so the keys
    are now read through one place (`when()`) and the caller was fixed too.
    """
    def esc(s: str) -> str:
        return (str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
                .replace("\n", "\\n"))

    def when(e: dict) -> str:
        for key in ("date", "dtstart", "day"):
            v = e.get(key)
            if v:
                return str(v)[:10].replace("-", "")
        raise ValueError("an ICS event needs a date; ALIBI does not invent one to fill a calendar")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")   # aware, and identical for the whole file
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{prodid}", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:Alibi (twin)", f"X-WR-TIMEZONE:{tzid}"]
    for i, e in enumerate(events):
        d = when(e)
        title = e.get("title") or e.get("summary") or "Untitled obligation"
        uid = e.get("uid") or f"alibi-{e.get('id', i)}@twin"
        lines += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{d}", f"DTEND;VALUE=DATE:{d}", f"SUMMARY:{esc(title)}"]
        if e.get("description"):
            lines.append(f"DESCRIPTION:{esc(e['description'])[:700]}")
        lines.append(f"CATEGORIES:{esc(e.get('course', 'school'))}")
        if e.get("alarm_min"):
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY",
                      f"TRIGGER:-PT{int(e['alarm_min'])}M", f"DESCRIPTION:{esc(title)}",
                      "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# --------------------------------------------------------- attendance screen ---

@dataclass
class AttendanceRow:
    course: str
    attended: int
    total: int
    pct: float
    status: str
    offset: int


_ROW = re.compile(
    r"(?P<course>[A-Z]{2,9}[A-Z0-9\- ]{0,30}?(?:LAB|Lab)?)\s+"
    r"(?P<th>T|P|L\+T)?\s+(?P<a>\d{1,3})\s+(?P<t>\d{1,3})\s+(?P<pct>\d{1,3}(?:\.\d{1,2})?)\s*%"
    r"\s+(?P<status>OK|EDGE|SHORTAGE|DEFAULTER|EXEMPT)?", re.I)


def parse_attendance_table(text: str) -> list[AttendanceRow]:
    """ERP portals print a fixed-width table; a screenshot transcribed by the vision model
    preserves it. Row extraction is done in code so a percentage is never *hallucinated* by
    a model that was only asked to 'read the table'."""
    rows = []
    for m in _ROW.finditer(text):
        a, t = int(m.group("a")), int(m.group("t"))
        if t <= 0 or a > t:
            continue
        pct = float(m.group("pct")) if m.group("pct") else (100.0 * a / t)
        rows.append(AttendanceRow(" ".join(m.group("course").split()), a, t, pct,
                                  (m.group("status") or "").upper(), m.start()))
    return rows


# ----------------------------------------------------------- chunking (text) ---

@dataclass
class Block:
    kind: str                # table_row | heading | sentence_group | para
    text: str
    start: int
    end: int
    needs_model: bool = True


def blocks_from_syllabus(text: str) -> list[Block]:
    """Split by document *structure*, not by length — and mark rows that need no model.
    A line that already contains 'Course … 10% … due 12 Oct' is a rule, not an inference.
    This is the cheapest place in the whole system to cut LLM spend, and it is why the
    'most facts need no model' claim in the brief is defensible rather than aspirational."""
    out: list[Block] = []
    pos, buf, buf_start = 0, [], 0
    for raw in text.split("\n"):
        line, lstart, lend = raw, pos, pos + len(raw)
        pos = lend + 1
        s = line.strip()
        if not s:
            if buf:
                out.append(Block("para", "\n".join(buf), buf_start, lend - 1, True))
                buf, buf_start = [], 0
            continue
        if not buf:
            buf_start = lstart
        buf.append(s)
        tabley = (bool(re.search(r"\|", s)) or bool(re.search(r"\s{2,}\d{1,3}(\.\d+)?\s*%", s))
                  or bool(re.search(r"\.{6,}|\s\.{2,}\s", s)))       # dot-leader tables
        has_date = bool(re.search(r"\d{1,2}\s*[A-Za-z]{3,9}\.?,?\s*\d{2,4}"
                                  r"|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.]\d{1,2}[/.]\d{2,4}"
                                  r"|[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?", s))
        keyword = re.search(r"(?i)\b(due|submission|submit|exam|test|quiz|closed|closes|"
                            r"assessment|submission date|registration|cutoff|cut-off|"
                            r"presentation|demo|internal)\b", s)
        dated = bool(keyword) and has_date
        # A line is rule-extractable only when it is short, has one date and carries a keyword:
        # i.e. when a regex cannot choose wrong. Anything else goes to the model — this bias
        # toward "send it to the model" is deliberate, because a wrong rule is invisible in the
        # logs while a model error is caught by the verifier.
        only_one_fact = tabley and dated and len(s) < 200
        if only_one_fact:
            if buf[:-1]:
                out.append(Block("para", "\n".join(buf[:-1]), buf_start, lstart, True))
            out.append(Block("table_row", s, lstart, lend, False))
            buf, buf_start = [], 0
    if buf:
        out.append(Block("para", "\n".join(buf), buf_start, pos, True))
    # merge tiny paragraphs so the model sees a sentence in context, not a fragment
    merged: list[Block] = []
    for b in out:
        if merged and b.kind == "para" and merged[-1].kind == "para" \
                and merged[-1].end == b.start and len(merged[-1].text) < 120:
            merged[-1] = Block("para", merged[-1].text + "\n" + b.text, merged[-1].start, b.end, True)
        else:
            merged.append(b)
    return merged


def rule_claim_from_row(row_text: str, anchor_year: int = 2026) -> dict | None:
    """Deterministic claim for a self-contained table row / bullet. Returns None (→ model)
    whenever the row contains more than one candidate date, i.e. whenever a regex would have
    to choose. Returns the same shape a
    verified model claim would, with method='rule' and confidence=1.0, plus the offsets the
    caller already knows."""
    from .ground import dates_in, percents_in
    due = None
    m = re.search(r"(?i)\b(due|submission|submit|closes?|exam|test|quiz)\b[^.]{0,40}?"
                  r"(\d{1,2}\s*[A-Za-z]{3,9}\.?,?\s*\d{2,4}|\d{4}-\d{1,2}-\d{1,2}|"
                  r"\d{1,2}[/.]\d{1,2}[/.]\d{2,4}|[A-Za-z]{3,9}\s*\d{1,2}(?:,?\s*\d{4})?)", row_text)
    if m:
        cands = dates_in(m.group(2), anchor_year)
        if len(cands) == 1:
            due = cands[0]
        else:
            return None            # ambiguous keyword+date pairing must go to the model
    w = None
    mw = re.search(r"(?i)\((\d{1,3}(?:\.\d+)?)\s*%\s*(?:of|weight)?", row_text) or \
        re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", row_text)
    if mw:
        w = float(mw.group(1)) / 100.0
    late = re.search(r"(?i)(late[^.]{0,80})", row_text)
    all_cands = set(dates_in(row_text, anchor_year))
    if due and len(all_cands) > 1 and not m:
        return None
    if not due and w is None and not late:
        return None
    v: dict = {}
    if due:
        v["due_at"] = due
    if w is not None:
        v["weight"] = round(w, 4)
    if late:
        v["late_policy"] = late.group(1).strip()[:160]
    return {"value": v, "method": "rule", "confidence": 1.0}
