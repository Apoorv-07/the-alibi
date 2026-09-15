"""present — ALIBI's rows, said in the student's language.

This module exists because the product's sophistication was reaching the screen untranslated. A student does
not read `verify_state='grounded'`, `CONFLICTING_STATEMENTS`, `PROVEN_INFEASIBLE` or `subject_id=os-ia_1`;
they read "Confirmed from your syllabus", "two sources disagree", "this doesn't fit before Friday" and
"Operating Systems · Internal Assessment 1". The intelligence stays exactly where it is — the ledger, the
verifier, the solver, the audit trail — and this file decides *how it is phrased*, in one place, so a
translation cannot drift from one screen to another.

Three rules, and they are the reason this is a module rather than template conditionals:

1. **Never invent.** A title comes from the subject key or from a claim; if neither holds one, the key is
   shown as it is. A truncated guess looks exactly like a real deadline, and a fabricated one is the failure
   this product exists to prevent.
2. **Layer the truth, do not remove it.** "Confirmed from your syllabus" is the first line; the source, the
   quote and the claim id are one click away and are always reachable. Calm on the surface, auditable under.
3. **Silence over noise.** No count, badge or alert is manufactured here; a section that would be empty says
   so, in words, and offers the one action that could fill it.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Iterable

# ─────────────────────────────── vocabulary ────────────────────────────────
# One mapping, used by every screen. Anything not listed here must not be translated in a template.

#: internal claim state → the word a student reads
VERIFY_LABEL = {
    "grounded": "Confirmed",
    "grounded_relative_ambiguous": "Confirmed, wording is loose",
    "review": "Needs your confirmation",
    "rejected": "Not used — it did not check out",
    "unverified": "Not verified yet",
}

#: internal predicate → a human noun phrase
PREDICATE_LABEL = {
    "due_at": "Due date",
    "released_at": "Released",
    "weight": "Worth",
    "late_policy": "Late policy",
    "submitted": "Handed in",
    "submitted_at": "Handed in",
    "exam_at": "Exam",
    "class_session": "Class",
    "attendance_pct": "Attendance",
    "venue": "Room",
    "injection_suspect": "Suspicious instructions in a document",
}

#: internal review kind → what is actually being asked of the student
REVIEW_LABEL = {
    "CONFLICTING_STATEMENTS": "Two sources disagree",
    "LOW_CONFIDENCE": "Needs your confirmation",
    "AMBIGUOUS_DATE": "Which date is right?",
    "MISSING_FIELD": "Something the system needs is missing",
    "RETIRED_SOURCE": "A source has been updated",
}

#: solver / risk verdicts → plain speech, with the reassurance that no number was fudged
STATUS_LABEL = {
    "PROVEN_INFEASIBLE": "Can't fit your schedule",
    "INFEASIBLE": "Can't fit your schedule",
    "AT_RISK": "At risk",
    "PENDING": "Not started",
    "ON_TRACK": "On track",
    "SOLVED": "Fits, with room to spare",
    "DONE": "Done",
}

#: source kind → what the student would call it
SOURCE_LABEL = {
    "syllabus": "Course syllabus",
    "notice": "Notice board",
    "chat": "Message thread",
    "lms": "Course portal",
    "calendar": "Academic calendar",
    "manual": "Something you told ALIBI",
    "timetable": "Your timetable",
    "email": "An email",
    "other": "A document",
}


def verify_word(state: str | None) -> str:
    return VERIFY_LABEL.get(state or "", "Checked, details below")


def predicate_word(pred: str) -> str:
    return PREDICATE_LABEL.get(pred, pred.replace("_", " ").capitalize())


def review_word(kind: str) -> str:
    return REVIEW_LABEL.get(kind, "Needs your judgment")


def status_word(status: str) -> str:
    return STATUS_LABEL.get(status, status.replace("_", " ").lower().capitalize())


def source_word(kind: str | None, label: str | None = None) -> str:
    """A source is named by what it is, not by its row id."""
    if label and label.strip():
        return label.strip()
    # an unnamed source is still a *kind* of source, and "A source on file" reads like the app shrugging.
    # The aliases are what the ingest path actually stores (`syllabus_text`, `whatsapp`, `erp_table`), so a
    # document with no label still gets a name a student recognises instead of a fallback.
    if kind:
        k = str(kind).lower()
        for alias, word in (("syllabus", "Course syllabus"), ("whatsapp", "Message thread"),
                            ("chat", "Message thread"), ("notice", "Notice board"), ("photo", "A document"),
                            ("erp", "Course portal"), ("lms", "Course portal"), ("timetable", "Your timetable"),
                            ("calendar", "Academic calendar"), ("email", "An email"), ("manual", "Something you told ALIBI")):
            if alias in k:
                return word
    return "an unnamed source"


def when_phrase(day: dt.date | None, today: dt.date) -> str:
    """The phrase a student acts on — and never a false one. `None` is 'no date recorded', not 'today'."""
    if day is None:
        # "Date not recorded yet" reads like a form field. The student's fact is: nothing states it.
        return "No date on file"
    n = (day - today).days
    short = f"{day:%a %d %b}" if day.year == today.year else f"{day:%a %d %b %Y}"
    if n == 0:
        return "Due today"
    if n == 1:
        return f"Due tomorrow ({short})"
    if n == -1:
        return f"Was due yesterday ({short})"
    if n < 0:
        return f"Was due {short} · {-n} days ago"
    if n <= 5:
        return f"In {n} days ({short})"
    return f"Due {short}"


def effort_phrase(minutes: int | None, assumed: bool = False) -> str:
    """Minutes come from a claim when one exists, otherwise from the planner's documented default — and the
    word 'assumed' says which of the two it is. ALIBI never guesses a number silently."""
    if not minutes:
        return "Effort not recorded"
    hours = minutes / 60
    text = f"~{hours:g}h" if hours >= 1 else f"~{minutes} min"
    return f"{text} (assumed)" if assumed else text


def title_for(subject_id: str, claims: Iterable[dict] = ()) -> str:
    """`dbms-lab_4` → `Lab 4`; `os-attendance_75_theory_lab` → `Attendance 75 theory lab`.

    Promoted from the key, exactly as `Twin._label` does, so the list and the graph never disagree about a
    name. A key that is a whole sentence is returned intact: a shortened guess would read like a real title.
    """
    for c in claims:                                    # a claim that carries a title wins
        t = (c.get("value") or {}).get("title")
        if isinstance(t, str) and t.strip():
            return t.strip()
    tail = subject_id.split("-", 1)[1] if "-" in subject_id else subject_id
    words = tail.replace("_", " ").strip()
    if not words or len(words) > 60:
        return subject_id
    words = words[0].upper() + words[1:]
    # A whole sentence ("submit the lab 4 report") is *already* the words the source used: re-casing tokens
    # inside it invents a title that appears on no document. Code-shaped titles are one or two tokens, and
    # that is exactly where normalising an abbreviation pays — `ia 2` is how a syllabus writes Internal
    # Assessment, and printing "Ia 2" is a small lie with a large capacity to confuse. Short codes go upper,
    # real words stay title case, and nothing is re-cased unless a number follows it.
    if len(words.split()) <= 2:
        words = re.sub(r"\b(?:i[ae]s?|quiz|midsem|endsem|proj|vn)(?=\s\d)",
                       lambda m: m.group(0).upper() if len(m.group(0)) <= 3 else m.group(0).capitalize(),
                       words, flags=re.IGNORECASE)     # the first word is already capitalised
        words = re.sub(r"\blab(?=\s\d)", "Lab", words, flags=re.IGNORECASE)
    return words


def value_parts(v: Any) -> tuple[str, str]:
    """(the value itself, the qualifier that explains it) — `("Wed 30 Sep 2026", "date only, no time")`.

    Two fields because one string is what a *sentence* needs and two is what a *table cell* needs: a
    date in a narrow Value column wrapped to four lines, and the "(date only, no time)" note — the single
    most important caveat on the row — got buried mid-sentence. `value_phrase()` is the join of these, so
    prose and tables can never disagree about the value.
    """
    if isinstance(v, str):
        s = v.strip()
        if s[:1] in "{[":
            try:
                v = json.loads(s)
            except ValueError:
                return s, ""
        else:
            try:
                d = dt.date.fromisoformat(s[:10])
            except ValueError:
                return s, ""
            return (f"{d:%a %d %b %Y}", "") if len(s) == 10 else (s, "")
    if isinstance(v, dict):
        d = _date_of(v)
        if d:
            # non-breaking spaces inside the date: a value that wraps is a value you misread
            main = f"{d:%a\xa0%d\xa0%b\xa0%Y}"
            return main, ("date only, no time" if v.get("precision") == "day" else "")
        if "weight" in v:
            try:
                return f"{float(v['weight']) * 100:g}%", "of the grade"
            except (TypeError, ValueError):
                pass
        if "text" in v:
            return str(v["text"])[:160], ""
        return ", ".join(f"{k.replace('_', ' ')} {val}" for k, val in list(v.items())[:3])[:160], ""
    if isinstance(v, list):
        return " · ".join(value_phrase(x) for x in v[:3]), ""
    return str(v)[:160], ""


def value_phrase(v: Any) -> str:
    """`{"date": "2026-10-12", "precision": "day"}` → `Wed 12 Oct 2026 date only, no time`.

    One line of phrasing, built by `value_parts()`. An option's `label` column in this database contains the
    raw value JSON (written by the extractor, not a copywriter), so rendering that column verbatim is how a
    review card reads `"date": "2026-10-12", "precision"` to a student. Read the value, phrase it, and fall
    back to the raw text only when it is genuinely unstructured.
    """
    main, qual = value_parts(v)
    if not qual:
        return main
    # a weight reads as prose ("15% of the grade"); a caveat reads as a caveat ("… (date only, no time)")
    return f"{main} {qual}" if qual.split(" ", 1)[0] in ("of", "in", "on", "per") else f"{main} ({qual})"


def option_label(o: Any) -> str:
    """A review option, phrased. Never the JSON that happens to sit in a text column."""
    if isinstance(o, dict):
        label = str(o.get("label") or "").strip()
        value = o.get("value")
        phrase = value_phrase(value if value is not None else (label or o))
        # the stored label is usually the value again; prefer the phrase, keep any human suffix
        extra = "" if label.replace(" ", "").strip('{}":,').startswith(phrase[:10].replace(" ", "")) else ""
        return phrase + extra
    return value_phrase(o)


def trust_words(st: dict) -> dict:
    """The trust summary, phrased — the one definition, shared by the six pages, the cockpit and the CLI.

    `alibi.cli status` and the web UI used to compute this twice, and they had already diverged: the CLI
    printed `FALSE TRUST: 0 / 0 grounded = 0.00%` on a cold ledger, i.e. a percentage over nothing, which is
    exactly the kind of figure this product is supposed to refuse. One function, so the two surfaces cannot
    tell two different stories about the same rows.
    """
    ft = st.get("false_trust") or {}
    n, den = ft.get("count"), ft.get("denominator") or 0
    pct = (100.0 * n / den) if den else None
    got, tot = st.get("claims_verified", 0), st.get("claims", 0)
    # "0 of 0 facts re-checked" is technically true and practically noise: a number that cannot be
    # divided is not a statistic. Say what is absent instead, in the same flat voice.
    line = ("Nothing has been read yet, so there is nothing to verify — ALIBI does not fill this in."
            if not tot else
            f"{got} of {tot} facts re-checked on read"
            + (f" · {n} wrongly trusted" if n else " · nothing wrongly trusted"))
    return {"grounded": got, "claims": tot, "false_trust": n, "denominator": den,
            "pct": pct, "line": line, "unverifiable": st.get("unverifiable", 0)}


def course_for(subject_id: str) -> str:
    head = subject_id.split("-", 1)[0] if "-" in subject_id else ""
    return head.upper() if head else "Unassigned course"


def _date_of(value: dict) -> dt.date | None:
    raw = (value or {}).get("date")
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _claims_by_subject(rows: list[dict]) -> dict[str, dict[str, list[dict]]]:
    out: dict[str, dict[str, list[dict]]] = {}
    for r in rows:
        out.setdefault(r["subject_id"], {}).setdefault(r["predicate"], []).append(r)
    return out


def _decode(row: dict) -> dict:
    if "value" not in row:
        try:
            row["value"] = json.loads(row.get("value_json") or "{}")
        except (TypeError, ValueError):
            row["value"] = {}
    return row


# ──────────────────────────────── the work ───────────────────────────────────

class Presentation:
    """Row-level reads for the six human-facing destinations.

    `twin` is used for what only it can compute (the derived ledger, the pipeline verdicts); `db` for
    everything else. Nothing here writes: a view that mutates state makes the audit trail lie about who did
    what, and half these screens are rendered on a GET.
    """

    #: the planner's documented default (see `feasibility.Horizon`); surfaced *as an assumption*, never as a fact
    ASSUMED_MINUTES = 180

    def __init__(self, twin) -> None:
        self.twin = twin
        self.db = twin.db

    # ------------------------------------------------------------- shared ----
    def _today(self) -> dt.date:
        return dt.date.fromisoformat(self.twin._today_str())

    def live(self) -> tuple[list[dict], dict[str, dict[str, list[dict]]]]:
        rows = [_decode(r) for r in self.db.open_claims("task")]
        # `claim` stores `source_id`, not the label, and every card used to print "A source on file" for all
        # of them — which is the one piece of provenance a trust product has to get right. One join here,
        # cached for the request, so the card can name the syllabus or the chat export it came from.
        labels, kinds = self.source_labels()
        for r in rows:
            r["source_label"] = labels.get(r.get("source_id")) or ""
        return rows, _claims_by_subject(rows)

    def source_labels(self) -> tuple[dict[int, str], dict[int, str]]:
        """(source_id → label, source_id → kind) for every source on file, read once per Presentation.

        Two maps because a *label* is what the human named the document and a *kind* is what the ingester
        guessed; the phrase prefers the first and falls back to the second. One query per request beats a
        join per claim row, and a card that could only say "a source on file" is a card that threw away the
        provenance it was handed.
        """
        cache = getattr(self, "_source_labels", None)
        if cache is None:
            rows = self.db.q("""SELECT s.id AS id, s.kind AS kind, COALESCE(m.label, '') AS label
                                FROM source s LEFT JOIN source_meta m ON m.source_id = s.id""")
            cache = self._source_labels = ({r["id"]: r["label"] for r in rows},
                                           {r["id"]: r["kind"] for r in rows})
        return cache[0], cache[1]

    def task_cards(self, *, include_done: bool = True) -> list[dict]:
        """One card per obligation the ledger knows about, in the order a student should read them:
        past-due first, then by date. No invented effort, no invented confidence."""
        rows, by_subj = self.live()
        _labels, kinds = self.source_labels()        # source_id → kind, for cards with no typed label
        today = self._today()
        risk = {r["subject_id"]: r for r in self.db.q(
            "SELECT * FROM risk_forecast WHERE subject_type='task' "
            "ORDER BY computed_at DESC, id DESC")}
        done_ids = {r["subject_id"] for r in rows if r["predicate"] in ("submitted", "submitted_at")}
        cards: list[dict] = []
        for sid, preds in by_subj.items():
            if not include_done and sid in done_ids:
                continue
            dates = sorted({d for c in preds.get("due_at", []) if (d := _date_of(c.get("value") or {}))})
            due = dates[0] if dates else None
            conflicting = len(dates) > 1
            weight = next((c for c in preds.get("weight", [])), None)
            est = next((c for c in preds.get("est_minutes", [])), None)
            minutes = None
            assumed = False
            if est and isinstance((est.get("value") or {}).get("minutes"), (int, float)):
                minutes = int(est["value"]["minutes"])
            else:
                minutes, assumed = self.ASSUMED_MINUTES, True
            # name the document, not the row: the label the human typed on upload is the one thing that
            # tells them *which* syllabus said this, and it beats the generic kind every time.
            srcs = sorted({source_word(kinds.get(c.get("source_id")), c.get("source_label"))
                           for pred in preds.values() for c in pred})
            verdict = risk.get(sid)
            cards.append({
                "id": sid,
                "title": title_for(sid, [c for p in preds.values() for c in p]),
                "course": course_for(sid),
                # ISO, deliberately: it is the raw row inside "why", not the sentence a student reads. The
                # phrased date lives in `when`; a card that phrases both loses the value you would quote.
                "due": (dates[0].isoformat() if len(dates) == 1 else None),
                "due_any": dates[0].isoformat() if dates else None,
                "needs_choice": len(dates) > 1,
                "due_dates": [d.isoformat() for d in dates],
                "when": ("Two dates on file — you choose" if len(dates) > 1 else when_phrase(due, today)),
                "days": (due - today).days if due else None,
                "minutes": minutes,
                "minutes_assumed": assumed,
                "effort": effort_phrase(minutes, assumed),
                "weight": (weight.get("value") or {}).get("weight") if weight else None,
                "state": "done" if sid in done_ids else (
                    "past" if due and due < today else "today" if due == today else "open"),
                "conflicting": conflicting,
                "confidence": max((c.get("confidence") or 0) for p in preds.values() for c in p) or None,
                "verify": verify_word(max((c.get("verify_state") or "" for p in preds.values() for c in p),
                                          key=lambda s: 0 if s == "grounded" else 1)),
                "sources": srcs,
                "claim_ids": sorted({c["id"] for p in preds.values() for c in p}),
                "risk": (status_word(verdict["status"]) if verdict else None),
                "risk_note": (verdict or {}).get("recommendation") or None,
                "prediction": bool((verdict or {}).get("is_prediction")),
                "n_claims": sum(len(v) for v in preds.values()),
                "overdue": bool(due and due < today and sid not in done_ids),
                "released": (lambda d: d.isoformat() if d else None)(
                    _date_of((next((c for c in preds.get("released_at", [])), {}) or {}).get("value") or {})),
                "done_at": (lambda d: d.isoformat() if d else None)(
                    _date_of((next((c for c in (preds.get("submitted_at") or preds.get("submitted") or [])),
                                   {}) or {}).get("value") or {})),
            })
        cards.sort(key=lambda c: (c["state"] != "past", c["state"] == "done",
                                  c["due"] or "9999-12-31", c["course"], c["title"]))
        return cards

    # --------------------------------------------------------------- home ----
    def home(self) -> dict:
        """"What do I need to know right now", and nothing else.

        The order is the argument: attention first, then this week, then what is next, then what changed.
        Every figure comes from the same DB the technical pages read; the trust number is recomputed on this
        request rather than remembered.
        """
        today = self._today()
        cards = self.task_cards()
        open_cards = [c for c in cards if c["state"] != "done"]
        week_end = today + dt.timedelta(days=6)
        attention = self.attention()
        # record=False — Home is a read, and it renders the change band, so a render that wrote a change row
        # would feed itself: the page about "what changed" growing because the page was opened.
        planned = self.twin.run_pipeline(record=False) if open_cards else {}
        feas = planned.get("feasibility") or {}
        changes = self.db.changes(limit=6)
        return {
            "greeting": self._greeting(),
            "today_long": f"{today:%A %d %B %Y}",
            "attention": attention["cards"],
            "attention_count": attention["count"],
            "attention_reason": attention["reason"],
            "today_items": [c for c in open_cards if c["due"] == today.isoformat()],
            "this_week": [c for c in open_cards if c["due"] and today.isoformat() < c["due"] <= week_end.isoformat()],
            "later": [c for c in open_cards if c["due"] and c["due"] > week_end.isoformat()][:4],
            "no_date": [c for c in open_cards if not c["due"]][:3],
            "changed": [self._change_phrase(r) for r in changes],
            "plan": self._plan_words(feas, planned.get("summary") or {}),
            "trust": self._trust_words(),
            "connected": self._connected(),
            "empty": not open_cards and not changes,
        }

    def _greeting(self) -> str:
        hour = dt.datetime.now(dt.timezone.utc).astimezone().hour
        part = ("Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening")
        return f"{part}. Here's what matters today."

    def attention(self) -> dict:
        """Everything that genuinely needs a human, merged and de-duplicated, with a reason attached.

        An empty list is the honest answer on a quiet day and the template must show it that way — a
        manufactured alert is worse than none, because the student learns to ignore the panel.
        """
        today = self._today()
        items: list[dict] = []
        seen: set[tuple] = set()

        def push(key: tuple, **kw: Any) -> None:
            if key in seen:
                return
            seen.add(key)
            items.append(dict(kw))

        for c in self.task_cards():
            if c["state"] == "done":
                continue
            if c["overdue"]:
                push(("past", c["id"]), kind="past", subject=c["id"], title=c["title"], course=c["course"],
                     line=f"{c['title']} passed its date ({c['when'].replace('Was due ', '')})"
                          " with no submission on file",
                     why="ALIBI has a due date and no receipt; it will not assume either way",
                     action="Mark it handed in", href=f"/evidence?subject={c['id']}",
                     urgent=True)
            elif c["conflicting"]:
                opts = " or ".join(value_phrase({"date": d}) for d in c["due_dates"])
                push(("conflict", c["id"]), kind="conflict", subject=c["id"], title=c["title"],
                     course=c["course"], line=f"Two sources disagree about {c['title']}'s date ({opts})",
                     why="A date is only used once both sources agree; otherwise the plan is built on a guess",
                     action="Choose the date", href=f"/review", urgent=True)
            elif c["due"] and (today + dt.timedelta(days=1)).isoformat() >= c["due"] >= today.isoformat():
                push(("due", c["id"]), kind="due", subject=c["id"], title=c["title"], course=c["course"],
                     line=f"{c['title']} · {c['when']}",
                     why=f"{c['effort']} of work, {c['verify'].lower() if c['verify'] else 'from your sources'}",
                     action="View task", href=f"/tasks#{c['id']}", urgent=c["overdue"])
            elif c["risk"] == "At risk" and c["prediction"]:
                push(("risk", c["id"]), kind="risk", subject=c["id"], title=c["title"], course=c["course"],
                     line=f"{c['title']} is tight: {c['risk_note'] or 'the remaining days are short'}",
                     why="Measured against your free time and the deadline, not a mood",
                     action="See the plan", href="/tasks", urgent=False)

        for r in self.db.reviews("open", 12):
            sid = r.get("subject_id") or ""
            push(("review", r["id"]), kind="review", subject=sid, id=r["id"],
                 course=course_for(sid) if sid else "General",
                 title=title_for(sid) if sid else (r.get("question") or "")[:60],
                 line=self._review_line(r), why=review_word(r.get("kind") or ""),
                 action="Resolve", href="/review", urgent=(r.get("kind") == "CONFLICTING_STATEMENTS"))

        items.sort(key=lambda i: (not i.get("urgent"), i.get("kind") or ""))
        count = len(items)
        reason = "Nothing needs you today." if not count else (
            "1 thing needs you." if count == 1 else f"{count} things need you.")
        # the key is `cards`, not `items`: a Jinja `dict.items` lookup returns the *method* (attribute wins
        # over `__getitem__`), and a `{% for %}` over it dies with "builtin_function_or_method is not iterable"
        # on any page that renders the attention list. Names in a template contract are load-bearing.
        return {"cards": items[:8], "count": count, "reason": reason}

    def _review_body(self, r: dict, built: list[dict]) -> str:
        """One sentence of substance under the question: which two sources disagree, and the rule that will
        apply if no one answers. `authority` figures are the policy's, and saying them is the difference
        between 'a machine compared two documents' and a student understanding what is at stake."""
        evid = r.get("evidence") or []
        if isinstance(evid, str):
            try:
                evid = json.loads(evid)
            except ValueError:
                evid = []
        names = []
        for e in evid[:4]:
            # the evidence rows a conflict carries are keyed `source` (the human's own name for the
            # document), not `kind`/`label`: the first version of this line read those absent keys and every
            # card said "an unnamed source", which is the exact shrug this sentence exists to avoid.
            nm = str(e.get("source") or e.get("label") or "").strip()
            if not nm:
                nm = source_word(e.get("kind"), "")
            if nm and nm not in names:
                names.append(nm)
        if len(names) >= 2:
            return (f"{names[0]} and {names[1]} state different values, and both match their documents "
                    "verbatim, so neither is a transcription error.")
        if names:
            return f"One source ({names[0]}) is not enough on its own for a date this consequential."
        return "ALIBI has two readings of your sources and no basis to prefer one."

    def _review_line(self, r: dict) -> str:
        """The question, in the student's words. The stored `question` is a key-and-predicate sentence;
        the human one is rebuilt from the row's own fields, and falls back to it verbatim if there is less."""
        sid = r.get("subject_id") or ""
        opts = r.get("options") or []
        if isinstance(opts, str):
            try:
                opts = json.loads(opts)
            except ValueError:
                opts = []
        if sid and opts:
            choices = " or ".join(option_label(o) for o in opts[:3])
            return f"{title_for(sid)}: {review_word(r.get('kind') or '').lower()} — {choices}"
        if sid:
            return f"{title_for(sid)}: {review_word(r.get('kind') or '').lower()}"
        return r.get("question") or "A source needs your judgment"

    def _change_phrase(self, row: dict) -> dict:
        sid = row.get("subject_id") or ""
        kind = row.get("kind") or ""
        verb = {"NEW_REQUIREMENT": "new obligation", "SUPERSEDED": "updated", "CONFLICT_OPENED": "disagreement",
                "REVIEW_RESOLVED": "your answer", "STATE_CHANGED": "change"}.get(kind, kind.lower().replace("_", " "))
        return {"raw": kind, "subject": sid, "course": course_for(sid) if sid else "",
                "line": f"{verb} · {title_for(sid)}" if sid else verb,
                "detail": (row.get("effect") or row.get("why") or "")[:120],
                "at": (row.get("ts") or "")[5:16].replace("T", " ")}

    def _plan_words(self, feas: dict, summary: dict) -> dict:
        """The solver's verdict as a sentence a student can act on. The numbers are the solver's own."""
        status = feas.get("status") or ""
        core = ", ".join(feas.get("core") or [])
        short = feas.get("slack_hours")
        remedies = feas.get("remedies") or []
        if not feas:
            return {"state": "unknown", "headline": "No plan computed yet",
                    "body": "ALIBI has not run the schedule check on these obligations.", "fixes": []}
        if status in ("INFEASIBLE", "PROVEN_INFEASIBLE"):
            head = "Your current workload doesn't fit before the horizon."
            body = ("There is " + (f"{abs(short):g}h more work than free time" if short else "more work than free time")
                    + (f" (the binding constraint is {core})" if core else "") + ".")
            fixes = [{"label": self._remedy_words(r), "helps": bool(r.get("makes_feasible")),
                      "cost": str(r.get("cost") or ""), "cost_class": str(r.get("cost_class") or "")}
                     for r in remedies[:3]]
            return {"state": "infeasible", "headline": head, "body": body, "fixes": fixes,
                    "counts": summary.get("counts") or {}}
        # "fits" and "fits by a hair" are different decisions to make. The threshold is the planner's own
        # (a day's work of slack): under it, one slipped source empties the buffer, and the student should
        # know that *now* rather than in the week it happens.
        if status and short is not None and 0 <= float(short) < 6:
            return {"state": "tight", "headline": "It fits, but there is almost nothing to spare.",
                    "body": f"About {float(short):g}h of slack across the horizon — one shifted date, "
                            "or one assignment that takes longer than the estimate, and it stops fitting.",
                    "fixes": [], "counts": summary.get("counts") or {}}
        return {"state": "feasible", "headline": "Everything fits, with room to spare.",
                "body": ("The plan needs no changes; ALIBI will tell you if a source shifts a date."
                         if status else "No plan computed yet — add a due date and it will be checked."),
                "fixes": [], "counts": summary.get("counts") or {}}

    @staticmethod
    def _remedy_words(r: dict) -> str:
        # the solver already phrases its own remedies (`title`), and it phrases them better than a template
        # can: "work 5h/day instead of 4h/day" carries the before and after. Re-inventing that sentence here
        # would drop the number it is warning about. The `kind` fallbacks exist only for a remedy the solver
        # invented without a title — a KeyError or an empty bullet on a student's plan is not an option.
        title = str(r.get("title") or "").strip()
        if title:
            return title[0].upper() + title[1:]
        kind = str(r.get("kind") or "")
        if kind == "move_buffer":
            return "Move revision into the weekend"
        if kind == "drop_lowest_weight":
            return "Drop the lowest-weight item from this horizon"
        if kind == "raise_work_cap":
            return "Work longer days"
        return kind.replace("_", " ").strip() or "Adjust the plan"

    def _trust_words(self) -> dict:
        return trust_words(self.db.stats())

    def _connected(self) -> dict:
        rows = self.db.q("""SELECT s.id, s.kind, m.label, m.chars, s.captured_at
                             FROM source s LEFT JOIN source_meta m ON m.source_id = s.id
                             ORDER BY s.id DESC LIMIT 6""")
        return {"n": len(rows) and self.db.one("SELECT COUNT(*) n FROM source")["n"] or 0,
                "latest": [{"label": source_word(r["kind"], r["label"]), "at": (r["captured_at"] or "")[:10]}
                           for r in rows[:3]]}

    # ----------------------------------------------------------- calendar ----
    def calendar(self, *, days: int = 42, anchor: dt.date | None = None) -> dict:
        """Deadlines, releases and updates on a real grid. Days with nothing are *left empty* — a calendar
        that decorates itself with class events it has never seen is exactly the fabrication this product is
        a rebuttal to."""
        today = anchor or self._today()
        start = today - dt.timedelta(days=((today.weekday()) % 7))          # Monday of this week
        cells = []
        by_day: dict[str, list[dict]] = {}
        for c in self.task_cards():
            for key in ("due", "released"):
                d = c.get(key)
                if d:
                    by_day.setdefault(d, []).append(
                        {"subject": c["id"], "title": c["title"], "course": c["course"],
                         "kind": "deadline" if key == "due" else "released",
                         "state": c["state"], "conflicting": c["conflicting"]})
        for r in self.db.q("SELECT * FROM review_item WHERE status='open' ORDER BY id"):
            sid = r.get("subject_id") or ""
            if not sid:
                continue
            by_day.setdefault((r.get("created_at") or today.isoformat())[:10], []).append(
                {"subject": sid, "title": title_for(sid), "course": course_for(sid),
                 "kind": "question", "state": "open", "conflicting": False})
        for i in range(days):
            d = start + dt.timedelta(days=i)
            ev = by_day.get(d.isoformat(), [])
            cells.append({"date": d.isoformat(), "day": d.day, "weekday": f"{d:%a}",
                          "month": f"{d:%B}" if d.day == 1 or i == 0 else "",
                          "is_today": d == today, "is_weekend": d.weekday() >= 5,
                          "in_month": d.month == today.month, "events": ev,
                          "has_event": bool(ev), "n": len(ev)})
        weeks = [cells[i:i + 7] for i in range(0, len(cells), 7)]
        agenda = [c for c in cells if c["has_event"]]
        return {"cells": cells, "weeks": weeks, "agenda": agenda, "today": today.isoformat(),
                "events": sum(len(c["events"]) for c in cells),
                "weeks_label": f"{start:%d %b} – {(start + dt.timedelta(days=days - 1)):%d %b}",
                "timetable_note": ("No timetable source is connected, so class sessions are not shown. "
                                   "Add one and this grid fills up — ALIBI does not draw classes it has not been told about.")}

    def day(self, date: str) -> dict:
        """Everything the ledger says about one date, for the click-through panel."""
        cards = [c for c in self.task_cards() if date in (c.get("due_dates") or ([c["due"]] if c["due"] else []))]
        return {"date": date, "cards": cards, "count": len(cards),
                "phrase": when_phrase(dt.date.fromisoformat(date), self._today()) if date else ""}

    # ------------------------------------------------------------- review ----
    def review(self) -> dict:
        """Only what needs the student's judgment, one decision at a time, with the receipts beside it."""
        out = []
        for r in self.db.reviews("open", 50):
            sid = r.get("subject_id") or ""
            opts = r.get("options") or []
            if isinstance(opts, str):
                try:
                    opts = json.loads(opts)
                except ValueError:
                    opts = []
            evid = r.get("evidence") or []
            if isinstance(evid, str):
                try:
                    evid = json.loads(evid)
                except ValueError:
                    evid = []
            claims = [_decode(c) for c in self.db.q("SELECT * FROM claim WHERE subject_id=? ORDER BY id", (sid,))] \
                if sid else []
            out.append({
                "id": r["id"], "subject": sid, "kind": r.get("kind") or "",
                "heading": review_word(r.get("kind") or ""),
                "question": title_for(sid) if sid else (r.get("question") or "A question for you"),
                "course": course_for(sid) if sid else "",
                # the question line already names the item and both dates, so `body` must not repeat it —
                # a card that says the same sentence twice reads like a template bug (it was). What the student
                # needs here is *why* there is a disagreement at all: which two sources, and what decides it.
                "body": self._review_body(r, out),
                "why": r.get("why") or "",
                "consequence": r.get("consequence") or "",
                "created": (r.get("created_at") or "")[:10],
                "options": [{"i": i, "label": option_label(o),
                             "consequence": (o.get("consequence") if isinstance(o, dict) else ""),
                             "value": (o.get("value") if isinstance(o, dict) else o)}
                            for i, o in enumerate(opts)],
                "evidence": [{"source": str(e.get("source") or e.get("label") or "").strip()
                                        or source_word(e.get("kind"), ""),
                              "quote": e.get("quote") or e.get("span") or "",
                              "at": (e.get("captured_at") or "")[:10]} for e in evid[:4]],
                "n_claims": len(claims),
                "recommended": r.get("recommended"),
                "claims": claims,
            })
        out.sort(key=lambda x: (x["kind"] != "CONFLICTING_STATEMENTS", x["created"]))
        return {"cards": out, "count": len(out),
                "quiet": not out,
                "recent": self._answered_recent()}

    def _answered_recent(self) -> list[dict]:
        """The last few closed items, newest first, one entry per item.

        Two status queries concatenated can repeat a row (a resolved conflict is re-opened and answered again
        by the next sync), and a list headed "recently answered" that shows the same decision twice is a
        receipt book that double-books — so it is keyed here, at the only place that phrases it.
        """
        seen, rows = set(), []
        for status in ("answered", "expired", "dismissed"):
            for r in self.db.reviews(status, 6):
                if r["id"] in seen:
                    continue
                seen.add(r["id"])
                rows.append(self._answered(r))
        rows.sort(key=lambda x: x["at"], reverse=True)
        return rows[:8]

    def _answered(self, r: dict) -> dict:
        """A closed item, phrased the way the open one was — and with an honest status verb.

        `resolution` holds the option *as stored*: sometimes a dict, often the JSON string the review flow
        wrote. Rendering it directly is how "recently answered" printed
        `{"date": "2026-10-13", "precision": "day"}` under a heading that promised the answer, so the same
        `option_label()` that phrases the button is used to phrase the record — one fact, one phrase, in both
        the question and the receipt. `deferred` is said, not hidden: an item postponed under a heading reading
        "recently answered" would be the ledger claiming something the student never did.
        """
        raw = r.get("resolution")
        if isinstance(raw, str) and raw.strip()[:1] in "{[":
            try:
                raw = json.loads(raw)
            except ValueError:
                pass
        status_word = str(r.get("status") or "answered")
        if status_word in ("expired", "dismissed"):
            # no value was chosen, and `resolution` holds the *verb* the form sent. Echoing "defer" back as if
            # it were an answer would be the ledger overstating what happened: the tag says it, the value column
            # stays empty.
            chosen = ""
        else:
            chosen = option_label(raw) if raw not in (None, "") else ""
            if chosen.strip() in ("", "None", "{}") or chosen.strip().lower() in ("keep_own", "manual", "approve"):
                chosen = "acknowledged, no value chosen"
        # the status is a ledger word (`expired` covers a postponement); the student's word is different
        status = {"answered": "answered", "expired": "postponed", "dismissed": "skipped",
                  "open": "still waiting"}.get(status_word, "answered")
        # the recap's subject line: a short label, because `chosen` already carries the value. Repeating the
        # whole question ("Assignment 3: two sources disagree — Mon 12 Oct … or Tue 13 Oct …") next to the answer
        # ("Tue 13 Oct") reads like a copy-paste bug, which is exactly what it was.
        sid = r.get("subject_id") or ""
        short = (title_for(sid) + " · " + review_word(r.get("kind") or "").lower()) if sid \
            else (r.get("question") or "A question you answered")
        return {"id": r["id"], "subject": sid, "line": short,
                "chosen": chosen, "status": status,
                "note": (r.get("consequence") or "")[:160], "at": (r.get("resolved_at") or "")[:10]}

    # ----------------------------------------------------------- evidence ----
    def evidence(self, *, q: str = "", subject: str = "", limit: int = 60) -> dict:
        """The claim list, in human order, plus the receipt trail behind each one.

        This is the 5% of the product that stays technical — reached from a card, never as a home screen.
        """
        rows = [_decode(r) for r in self.db.claims_view("all", q)[:limit]]
        if subject:
            rows = [_decode(r) for r in self.db.q(
                "SELECT c.*, m.label AS source_label FROM claim c "
                "LEFT JOIN source_meta m ON m.source_id=c.source_id "
                "WHERE c.subject_id=? ORDER BY c.id", (subject,))]
        groups: dict[str, list[dict]] = {}
        for r in rows:
            r["verify_word"] = verify_word(r.get("verify_state"))
            r["predicate_word"] = predicate_word(r["predicate"])
            r["value_text"], r["value_qual"] = self._value_parts(r)
            r["source_word"] = source_word(r.get("orig_kind"), r.get("source_label"))
            r["quote"] = (r.get("evidence_span") or "").strip()
            groups.setdefault(r["subject_id"], []).append(r)
        labels, kinds = self.source_labels()
        trail = None
        if subject:
            # the trail the detail page shows: the same rows, phrased, plus the supersession edges under them
            lin = self.db.lineage(subject)
            for c in (lin.get("claims") or []):
                _decode(c)
                c["value_text"], c["value_qual"] = self._value_parts(c)
                c["predicate_word"] = predicate_word(c.get("predicate") or "")
                c["verify_word"] = verify_word(c.get("verify_state"))
                c["source_word"] = source_word(kinds.get(c.get("source_id")),
                                                labels.get(c.get("source_id")))
                c["quote"] = (c.get("evidence_span") or "").strip()
            trail = {"subject": subject, "title": title_for(subject, lin.get("claims") or []),
                     "course": course_for(subject), "claims": lin.get("claims") or [],
                     "edges": lin.get("edges") or []}
        cards = [{"subject": sid, "title": title_for(sid, claims), "course": course_for(sid),
                  "claims": claims, "n": len(claims),
                  "verified": sum(1 for c in claims if c.get("verify_state") == "grounded")}
                 for sid, claims in groups.items()]
        cards.sort(key=lambda c: (c["course"], c["title"]))
        return {"cards": cards, "count": len(rows), "q": q, "subject": subject, "trail": trail,
                "filtered": bool(q or subject)}

    @staticmethod
    def _value_parts(row: dict) -> tuple[str, str]:
        return value_parts(row.get("value") or {})

    @staticmethod
    def _value_text(row: dict) -> str:
        """A claim's value, phrased. `value_phrase()` is the same function a review option uses, so a date
        reads identically in the evidence trail, in the card and in the button a student clicks — three
        renderings of one fact, and any drift between them is read as the tool lying."""
        v = row.get("value") or {}
        if not v:
            return "no value recorded"          # a raw `{}` on screen looks like a bug, not an absence
        return value_phrase(v)

    # ---------------------------------------------------------------- ask ----
    ASK_PATTERNS = (
        ("today", re.compile(r"\b(what.*(today|now)|should i (work|do)|today'?s)\b", re.I)),
        ("deadline", re.compile(r"\b(when|deadline|due|date)\b.*\b(is|are|for)\b|\bwhen is\b", re.I)),
        ("why", re.compile(r"\b(why|how do you know|where.*(did|does).*from|proof|receipt|source)\b", re.I)),
        ("fit", re.compile(r"\b(can i|finish|fit|feasib|schedule|plan)\b", re.I)),
        ("risk", re.compile(r"\b(risk|miss|late|fall behind|at risk)\b", re.I)),
        ("changed", re.compile(r"\b(what changed|changed|updated|new)\b", re.I)),
        ("attention", re.compile(r"\b(attention|need me|urgent|priority|next)\b", re.I)),
    )

    def ask(self, text: str) -> dict:
        """Answered from the ledger, with the rows it came from. No model call: every sentence below is
        produced by reading rows this process can re-read and prove. If a model is configured, the *phrasing*
        may later be improved by it — never the facts, and never silently."""
        q = (text or "").strip()
        if not q:
            return {"ok": False, "answer": "Ask something specific: a date, a course, what to do today.",
                    "receipts": [], "intent": "empty"}
        intent = next((name for name, pat in self.ASK_PATTERNS if pat.search(q)), "lookup")
        today = self._today()
        cards = self.task_cards()
        answer, receipts = "", []

        if intent == "today":
            due = [c for c in cards if c["state"] != "done" and c["due"] and c["due"] <= (today + dt.timedelta(days=1)).isoformat()]
            att = self.attention()["cards"]
            answer = (f"{len(due)} obligation(s) are due by tomorrow." if due else
                      "Nothing is due by tomorrow.") + (f" {att[0]['line']}." if att else "")
            receipts = [{"kind": "task", "subject": c["id"], "label": f"{c['course']} · {c['title']}",
                         "detail": f"{c['when']} · {c['effort']} · {c['verify'].lower()}",
                         "claims": c["claim_ids"]} for c in due[:4]]
        elif intent == "deadline":
            key = self._subject_in(q, cards)
            hits = [c for c in cards if (key and c["id"] == key) or
                    (not key and c["due"] and any(w in c["title"].lower() for w in re.findall(r"[a-z]{4,}", q.lower())))]
            if not hits:
                answer = ("I could not match that to a task in the ledger, so I am not going to guess a date."
                          " Name the course or the assignment as your sources spell it.")
            else:
                c = hits[0]
                answer = (f"{c['course']} · {c['title']}: "
                          + ("two dates on file — " + " / ".join(c["due_dates"]) + ". Pick one in Review."
                             if c["conflicting"] else f"{c['when']}"))
                receipts = [{"kind": "claims", "subject": c["id"], "label": "the claims behind it",
                             "detail": "; ".join(sorted(c["sources"])), "claims": c["claim_ids"]}]
        elif intent == "why":
            key = self._subject_in(q, cards)
            if not key:
                answer = "Name the thing you want the receipt for — a task, a date, a course."
            else:
                lin = self.db.lineage(key)
                claims = lin.get("claims") or []
                answer = (f"{title_for(key)} has {len(claims)} claim(s) on file"
                          + (f", {sum(1 for c in claims if c.get('verify_state') == 'grounded')} verified against "
                             "their quotes." if claims else "."))
                receipts = [{"kind": "lineage", "subject": key, "label": "open the trail",
                             "detail": f"{len(claims)} claims, {len(lin.get('edges') or [])} supersessions/links",
                             "claims": [c["id"] for c in claims[:6]]}]
        elif intent == "fit":
            p = self._plan_words(self.twin.run_pipeline(record=False).get("feasibility") or {}, {})
            answer = p["headline"] + " " + p["body"]
            receipts = [{"kind": "plan", "subject": "solver", "label": "the solver's own numbers",
                         "detail": "run on the current horizon", "claims": []}] if p["state"] != "unknown" else []
        elif intent == "risk":
            risky = [c for c in cards if c["risk"] == "At risk"]
            answer = (f"{len(risky)} obligation(s) are tight." if risky else "Nothing in the ledger is flagged at risk.")
            if risky:
                answer += " " + risky[0]["line" if "line" in risky[0] else "title"] + \
                          (f": {risky[0]['risk_note']}" if risky[0].get("risk_note") else "")
            receipts = [{"kind": "task", "subject": c["id"], "label": f"{c['course']} · {c['title']}",
                         "detail": c["when"], "claims": c["claim_ids"]} for c in risky[:4]]
        elif intent == "changed":
            ch = self.db.changes(limit=4)
            answer = (f"{len(ch)} recent change(s) in the ledger." if ch else "Nothing has changed on file yet.")
            receipts = [{"kind": "change", "subject": c.get("subject_id") or "",
                         "label": self._change_phrase(c)["line"], "detail": self._change_phrase(c)["detail"],
                         "claims": []} for c in ch]
        else:
            # No pattern matched. The tempting shortcut is to answer with "here is what needs you", which
            # reads like a helpful response to *any* question and teaches the student that Ask is a chat box.
            # So: if no word of the question touches a row in the ledger, say plainly that there is nothing
            # to answer with — an empty `receipts` list is what the template renders as "found no rows".
            low = q.lower()
            touched = any(w in low for c in cards for w in (c["title"].lower(), c["course"].lower())
                          if len(w) > 3)
            if not touched:
                answer = (f"ALIBI found no rows in your ledger that match “{q[:80]}”, so it has no answer for "
                          "that — and it will not make one up. Ask about a deadline, a plan, a risk, or where "
                          "a fact came from.")
                receipts = []
            else:
                att = self.attention()
                answer = att["reason"] + " Ask about a deadline, a plan, or where a fact came from."
                receipts = [{"kind": "review", "subject": i.get("subject") or "", "label": i["line"],
                             "detail": i.get("why") or "", "claims": []} for i in att["cards"][:3]]
        return {"ok": True, "intent": intent, "question": q, "answer": answer, "receipts": receipts,
                "suggested": self._suggestions()}

    def _subject_in(self, q: str, cards: list[dict]) -> str | None:
        low = q.lower()
        for c in cards:
            if c["id"].lower() in low or c["title"].lower() in low:
                return c["id"]
            if c["title"].lower() and all(w in low for w in c["title"].lower().split()[:2]):
                return c["id"]
        return None

    @staticmethod
    def _suggestions() -> list[str]:
        return ["What should I work on today?", "When is my OS assignment due?",
                "Why did my exam date change?", "Can I finish everything before Friday?",
                "What am I most at risk of missing?"]
