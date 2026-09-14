"""alibi.changes — typed change detection. The ledger is time, not state (PART 10).

Why "typed" rather than a diff string: a user can act on "due date moved 6 days earlier" and cannot
act on `due_at: {…} -> {…}`. And a `kind` is what makes the change searchable in `change_event`
later — "show me every date that moved earlier this month" is a question people actually ask when
their schedule collapses.

Severity is computed from the effect on the student's buffer, not from the field name. A weight
change from 10% to 30% is bigger than a due date that moves a week in a subject you are passing.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

KINDS = ("NEW_REQUIREMENT", "REMOVED_REQUIREMENT", "DATE_CHANGED", "WEIGHT_CHANGED",
         "SCOPE_CHANGED", "LOCATION_CHANGED", "MODE_CHANGED", "RELEASED", "CORRECTED")

# What the user is shown for each changed field, and which typed kind it implies.
FIELD_MAP: dict[str, tuple[str, str]] = {
    "date":     ("due date", "DATE_CHANGED"),
    "time":     ("due time", "DATE_CHANGED"),
    "days":     ("days until due", "DATE_CHANGED"),
    "value":    ("value", "DATE_CHANGED"),
    "weight":   ("weight", "WEIGHT_CHANGED"),
    "text":     ("scope", "SCOPE_CHANGED"),
    "items":    ("scope", "SCOPE_CHANGED"),
    "count":    ("scope", "SCOPE_CHANGED"),
    "unit":     ("scope", "SCOPE_CHANGED"),
    "start":    ("coverage window", "DATE_CHANGED"),
    "until":    ("coverage window", "DATE_CHANGED"),
    "venue":    ("place", "LOCATION_CHANGED"),
    "seat":     ("place", "LOCATION_CHANGED"),
    "channel":  ("where you submit", "MODE_CHANGED"),
    "mode":     ("how it is submitted", "MODE_CHANGED"),
    "kind":     ("how it is submitted", "MODE_CHANGED"),
    "url":      ("link", "MODE_CHANGED"),
    "source":   ("release", "RELEASED"),
    "released": ("release", "RELEASED"),
    "rate":     ("capacity", "SCOPE_CHANGED"),
    "available": ("capacity window", "DATE_CHANGED"),
    "note":     ("remark", "CORRECTED"),
}


@dataclass
class Change:
    kind: str
    subject_type: str
    subject_id: str
    predicate: str
    old: object
    new: object
    field_name: str = ""
    label: str = ""
    detail: str = ""
    direction: str = "NEUTRAL"          # EARLIER|LATER|TIGHTER|LOOSENED|NEUTRAL
    severity: str = "LOW"               # LOW|MED|HIGH
    buffer_delta_hours: float = 0.0
    is_correction: bool = False         # a promise that came from elsewhere and was fixed
    detected_by: str = "deterministic_diff"
    old_source_label: str = ""
    new_source_label: str = ""
    evidence_span: str = ""

    def as_row(self, run_id: int | None = None, occurred_at: str = "", why: str = "") -> dict:
        """Shape for `db.record_change(**row)`. `delta_json` carries what has no column of its own —
        direction, which source overruled which, and the verbatim span — so a change is reproducible
        from the database alone, not only from the process that detected it."""
        delta = json.dumps({"direction": self.direction, "severity": self.severity,
                            "label": self.label, "detail": self.detail, "field": self.field_name,
                            "buffer_delta_hours": round(self.buffer_delta_hours, 2),
                            "is_correction": bool(self.is_correction),
                            "old_source": self.old_source_label, "new_source": self.new_source_label,
                            "detected_by": self.detected_by,
                            "evidence_span": self.evidence_span[:300]}, ensure_ascii=False)
        j = lambda v: None if v is None else (json.dumps(v, ensure_ascii=False)
                                             if isinstance(v, (dict, list)) else str(v))
        # `field_name` is deliberately not a column: it lives inside `delta_json` as "field",
        # so `Change.as_row()` can be splatted straight into `db.record_change(**row)`.
        return {"kind": self.kind, "subject_type": self.subject_type, "subject_id": self.subject_id,
                "predicate": self.predicate,
                "old_value": j(self.old), "new_value": j(self.new), "delta_json": delta,
                "buffer_delta_hours": self.buffer_delta_hours, "detected_by": self.detected_by,
                "severity": self.severity, "why": why or self.detail, "effect": self.direction,
                "run_id": run_id, "occurred_at": occurred_at or None}


# --------------------------------------------------------------- primitives --

def _date(v) -> dt.date | None:
    if isinstance(v, dict):
        v = v.get("date")
    if isinstance(v, str):
        try:
            return dt.date.fromisoformat(v[:10])
        except Exception:
            return None
    return v if isinstance(v, dt.date) else None


def _norm(value) -> dict:
    v = value.get("value") if isinstance(value, dict) and "value" in value else value
    return dict(v) if isinstance(v, dict) else {"value": v}


def _label_of(predicate: str, value: dict) -> str:
    """Human label for what a predicate asserts, independent of which field moved."""
    return {"due_at": "the deadline", "released_at": "the material release",
            "weight": "its share of the grade", "submission_channel": "where you submit it",
            "submission_time": "the submission window", "venue": "where it happens",
            "attendance_pct": "your attendance", "exam_date": "the exam date",
            "review_gap": "the revision gap", "capacity_rate": "the resource capacity",
            "clause": "the policy text", "grade_pct": "your grade",
            "late_policy": "the late-submission rule", "sleep_floor_min": "your sleep floor"}\
        .get(predicate, predicate.replace("_", " "))


def _fields(old: dict, new: dict) -> list[tuple[str, object, object]]:
    keys = [k for k in old if k not in ("precision", "relative", "as_of", "student")]
    return [(k, old[k], new[k]) for k in keys if k in new and old[k] != new[k]]


def classify(subject_type: str, subject_id: str, predicate: str, old_value, new_value, *,
             old_source: str = "", new_source: str = "", evidence: str = "",
             recorded_at: str = "") -> list[Change]:
    """Every field that differs becomes one typed Change. Multiple fields can move at once
    ("submitted on WhatsApp instead of the portal, and it is now due Thursday") — collapsing them
    into one row would hide the second half of the sentence."""
    o, n = _norm(old_value), _norm(new_value)
    out: list[Change] = []
    for fname, a, b in _fields(o, n):
        label, kind = FIELD_MAP.get(fname, (fname.replace("_", " "), "CORRECTED"))
        ch = Change(kind=kind, subject_type=subject_type, subject_id=subject_id, predicate=predicate,
                    old=a, new=b, field_name=fname, label=label,
                    detail=f"{label} for {_label_of(predicate, o)}: {a!s} → {b!s}",
                    old_source_label=old_source, new_source_label=new_source,
                    evidence_span=evidence,
                    is_correction=bool(old_source and new_source and old_source != new_source))
        _orient(ch, o, n, recorded_at)
        out.append(ch)
    return out


def _orient(ch: Change, old: dict, new: dict, recorded_at: str) -> None:
    """Direction + severity. Severity is buffer arithmetic, so it is explainable in one sentence
    and can never be a vibes number."""
    if ch.field_name in ("date", "value", "days"):
        d0, d1 = _date(old), _date(new)
        if d0 and d1:
            delta = (d1 - d0).days
            # buffer = time you have left. Moving a deadline EARLIER destroys buffer, so the
            # sign is positive-when-later throughout; `summarise` negates it into "days lost".
            ch.buffer_delta_hours = delta * 24.0
            ch.direction = "EARLIER" if delta < 0 else ("LATER" if delta > 0 else "NEUTRAL")
            if abs(delta) >= 5:
                ch.severity = "HIGH" if delta < 0 else "MED"
            elif abs(delta) >= 2:
                ch.severity = "MED" if delta < 0 else "LOW"
            elif delta < 0:
                ch.severity = "MED"
    elif ch.field_name == "weight":
        try:
            w0, w1 = float(old.get("weight", 0)), float(new.get("weight", 0))
        except Exception:
            w0 = w1 = 0.0
        if w1 > w0:
            ch.direction, ch.severity = "TIGHTER", ("HIGH" if w1 - w0 >= 0.15 else "MED")
        elif w0 > w1:
            ch.direction, ch.severity = "LOOSENED", "LOW"
        ch.buffer_delta_hours = 0.0
    elif ch.field_name in ("time",):
        ch.direction, ch.severity = "TIGHTER", "MED"
    elif ch.kind == "SCOPE_CHANGED":
        ch.direction, ch.severity = ("TIGHTER", "MED") if len(str(ch.new)) >= len(str(ch.old)) \
            else ("LOOSENED", "LOW")
    else:
        ch.direction = "NEUTRAL"
    # A change that lands inside 48h of work you already scheduled is worse than the same change
    # weeks out, because there is no slack to absorb it.
    if ch.direction == "EARLIER" and recorded_at:
        try:
            days_until = (_date(old) - dt.date.fromisoformat(recorded_at[:10])).days
            if days_until <= 3:
                ch.severity = "HIGH"
        except Exception:
            pass


def added(subject_type: str, subject_id: str, predicate: str, new_value, *, new_source: str = "",
          evidence: str = "", today: str = "") -> Change:
    """A fact the ledger did not hold before. Severity is *how little time it leaves you*, computed
    from the record's own `recorded_at`, so re-syncing last week's export reproduces last week's
    answer instead of drifting with the wall clock (which would make the UI disagree with the log)."""
    n = _norm(new_value)
    ch = Change(kind="NEW_REQUIREMENT", subject_type=subject_type, subject_id=subject_id,
                predicate=predicate, old=None, new=n.get("date") or n.get("value") or n,
                label=_label_of(predicate, n),
                detail=f"new commitment: {_label_of(predicate, n)} ({n.get('date') or n})",
                direction="TIGHTER", severity="MED", new_source_label=new_source,
                evidence_span=evidence)
    d = _date(n)
    if d:
        anchor = _date({"date": today}) or dt.date.today()
        days_left = (d - anchor).days
        ch.severity = "HIGH" if days_left <= 3 else ("MED" if days_left <= 10 else "LOW")
        # Buffer lost is *time you no longer have*. A commitment 60 days out has destroyed none, so
        # the only case that subtracts buffer is one already past its date: charging work-hours for
        # every new row would make the header "days lost" meaningless within a week of term.
        ch.buffer_delta_hours = min(0.0, float(days_left)) * 24.0
        if days_left < 0:
            # A commitment dated in the past is the most urgent shape it can have, not the least:
            # the `<= 3` chain would call it HIGH anyway, but the marker is what makes the sentence
            # ("already 2d past") match the user's reality instead of reading like a bug.
            ch.direction = "OVERDUE"
            ch.detail += f" — already {abs(days_left)}d past"
    return ch


def removed(subject_type: str, subject_id: str, predicate: str, old_value, *, old_source: str = "",
            reason: str = "REPLACED") -> Change:
    o = _norm(old_value)
    sev = "LOW" if reason == "REPLACED" else "MED"
    return Change(kind="REMOVED_REQUIREMENT", subject_type=subject_type, subject_id=subject_id,
                  predicate=predicate, old=o.get("date") or o, new=None,
                  label=_label_of(predicate, o),
                  detail=f"{_label_of(predicate, o)} is gone ({reason.lower()})",
                  direction="LOOSENED", severity=sev, old_source_label=old_source,
                  detected_by=f"deterministic_diff:{reason.lower()}")


# ------------------------------------------------------------------ diffing --

def diff_ledger(prev: list[dict], cur: list[dict], *, trust: dict[str, int] | None = None) -> list[Change]:
    """Compare two snapshots of open claims (rows with subject_type/subject_id/predicate/value_json…).

    Deliberately not a per-row timestamp comparison: what matters is *what the system now believes
    about each fact*, which is a set of (subject, predicate) → value. That is also the only
    comparison that stays honest when a re-ingest rewrote three claims at once.
    """
    trust = trust or {}

    def key(r):
        return (r["subject_type"], r["subject_id"], r["predicate"])

    def payload(r):
        import json
        try:
            return json.loads(r.get("value_json") or "{}")
        except Exception:
            return {}

    old = {key(r): r for r in prev}
    new = {key(r): r for r in cur}
    out: list[Change] = []
    for k, r in new.items():
        src = r.get("source_label", "")
        if k not in old:
            # `recorded_at`, not "today": a re-sync of last week's export must produce the same
            # change severity it produced last week, or the UI disagrees with the audit log.
            out.append(added(*k, payload(r), new_source=src, evidence=r.get("evidence_span", ""),
                             today=r.get("recorded_at", "")))
            continue
        o = old[k]
        out.extend(classify(*k, payload(o), payload(r), old_source=o.get("source_label", ""),
                           new_source=src, evidence=r.get("evidence_span", ""),
                           recorded_at=r.get("recorded_at", "")))
    for k, r in old.items():
        if k not in new:
            out.append(removed(*k, payload(r), old_source=r.get("source_label", ""),
                               reason=str(r.get("retire_reason") or "REPLACED")))
    for ch in out:
        # An authoritative source that overrides a group chat is news; a group chat that overrides
        # a notice board is a conflict, and the conflict path owns it (PART 10).
        if ch.is_correction and trust.get(ch.new_source_label, 0) >= trust.get(ch.old_source_label, 0):
            ch.severity = "HIGH" if ch.severity != "LOW" else "MED"
        elif ch.is_correction:
            ch.kind = "CORRECTED"
    return out


def summarise(changes: list[Change]) -> dict:
    import collections
    sev = collections.Counter(c.severity for c in changes)
    kinds = collections.Counter(c.kind for c in changes)
    earliest = [c for c in changes if c.direction == "EARLIER"]
    hours = sum(-c.buffer_delta_hours for c in earliest) / 24.0
    return {"total": len(changes), "by_kind": dict(kinds), "severity": dict(sev),
            "dates_moved_earlier": len(earliest),
            "days_lost": round(hours, 1),
            "corrections": sum(1 for c in changes if c.is_correction),
            "head_line": _head_line(changes, kinds, sev)}


def _head_line(changes: list[Change], kinds, sev) -> str:
    if not changes:
        return "nothing changed since the last sync"
    hi = [c for c in changes if c.severity == "HIGH"]
    if hi:
        return f"{len(changes)} change(s); {len(hi)} severe — {hi[0].detail}"
    return f"{len(changes)} change(s): " + ", ".join(
        f"{n}×{k.replace('_', ' ').lower()}" for k, n in kinds.most_common(3))
