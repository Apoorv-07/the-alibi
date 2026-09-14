"""alibi.risk — forecast layer, kept architecturally separate from the solver (PART 17).

Three states, and the separation is a type, not a style preference:

    PROVEN_INFEASIBLE  CP-SAT found no schedule under the *current* facts. Mathematical, reproducible,
                       no probability anywhere in the sentence: "impossible", not "probably bad".
    AT_RISK            the current plan is feasible but a *forecast* says it will break unless you
                       act now (attendance floor, buffer decay, dependency depth). Probabilistic,
                       named model, with its inputs printed.
    PENDING            the model needs data it does not have. This state exists so that the honest
                       answer to "will I make it?" on day one is "I have 2 weeks of observations",
                       not a number invented to fill a gauge.

The bug this design prevents: once a forecast can say INFEASIBLE, the whole system's credibility
becomes hostage to a probability nobody can audit. So `Risk.__post_init__` physically rejects a
probabilistic model claiming proof, and `assess()` refuses to let a forecast upgrade a solver result.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

PROVEN, AT_RISK, PENDING, ON_TRACK = ("PROVEN_INFEASIBLE", "AT_RISK", "PENDING", "ON_TRACK")


def _d(s) -> dt.date | None:
    if isinstance(s, dt.date):
        return s
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


@dataclass
class Risk:
    category: str                       # PROVEN_INFEASIBLE | AT_RISK | PENDING | ON_TRACK
    subject_type: str                   # week | attendance | task
    subject_id: str
    horizon_start: str = ""
    horizon_end: str = ""
    probability: float | None = None    # None unless a *forecasting* model produced it
    expected_harm: str = ""             # concrete: "4.0h short on Wed", "2 sessions below floor"
    model: str = ""
    inputs: dict = field(default_factory=dict)
    evidence: str = ""
    recommendation: str = ""
    confidence_label: str = ""          # NEVER a fake decimal: "closed form", "3 of 5 similar", ...
    calibration: dict | None = None     # {"hits":5,"n":8} measured from our own past forecasts

    def __post_init__(self) -> None:
        if self.probability is not None and not (0.0 <= self.probability <= 1.0):
            raise ValueError(f"probability must be a fraction, got {self.probability}")
        # A probability attached to a proof is a category error, so it is rejected here rather than
        # "handled" downstream where somebody will format it into a sentence like "definitely at risk".
        if self.category == PROVEN and self.probability is not None:
            raise ValueError("PROVEN_INFEASIBLE must not carry a probability: the solver proves, "
                             "it does not estimate")
        if self.category in (AT_RISK, PENDING) and self.probability is None and not self.model:
            raise ValueError("a forecast needs the model that produced it, or it is an opinion")

    @property
    def needs_action(self) -> bool:
        return self.category in (PROVEN, AT_RISK)

    def sentence(self) -> str:
        """The whole point of the module in one line, and it never says "probably impossible"."""
        if self.category == PROVEN:
            return f"PROVEN impossible for {self.subject_id}: {self.expected_harm} ({self.model})"
        if self.category == AT_RISK:
            p = "" if self.probability is None else f"p≈{self.probability:.2f} "
            cal = ""
            if self.calibration and self.calibration.get("n", 0) >= 5:
                cal = f", historically {self.calibration['hits']}/{self.calibration['n']} of these were right"
            return (f"AT RISK {self.subject_id}: {p}{self.expected_harm} "
                    f"({self.model or 'forecast'}{cal})")
        if self.category == PENDING:
            return f"cannot forecast {self.subject_id} yet: {self.expected_harm}"
        return f"{self.subject_id} on track: {self.expected_harm}"

    def as_forecast(self) -> dict:
        return {"horizon_start": self.horizon_start or self.horizon_end,
                "horizon_end": self.horizon_end, "subject_id": self.subject_id,
                "subject_type": self.subject_type,
                "probability": 0.0 if self.probability is None else round(self.probability, 4),
                "method": self.model or "none", "status": self.category, "basis": self.inputs,
                "evidence": self.evidence, "recommendation": self.recommendation or self.expected_harm,
                "is_prediction": self.category in (AT_RISK, PENDING)}
        # `is_prediction` is the honesty flag the whole spec turns on: a solver result is a *result*,
        # a forecast is a prediction, and the DB (plus the UI badge) must be able to tell them apart
        # forever, without reading this file to find out.


# ------------------------------------------------------- attendance forecast --

def attendance_forecast(current_rate: float, sessions_left: int, floor_pct: float, *,
                        total_sessions: int | None = None, attended: int | None = None,
                        today: str = "") -> Risk:
    """Closed form, no solver, no model. Given the rate you are at *now* (including the sessions
    still to come, as ERP screens report it), how many of the remaining ones can you still miss?

        attended + (left - m)          current_rate/100 * total + (left - m)
        ----------------------- >= f    ⟺   --------------------------------  >= f
              total                             total

    Reported as a whole number of sessions, because "you can miss 1.7 classes" is fake precision a
    student cannot act on. Denominator is fixed: a cancelled or extra class is a *recompute*, which
    is precisely the kind of change the twin layer is there to catch.
    """
    inputs: dict = {"current_rate_pct": current_rate, "sessions_left": sessions_left,
                    "floor_pct": floor_pct,
                    "formula": "(attended + left - missed)/total >= floor"}
    if total_sessions is None and attended is None:
        # Without the denominator the question is unanswerable: 78% of 50 sessions and 78% of 300
        # sessions are two different futures. Say so instead of assuming a term length.
        return Risk(PENDING, "attendance", "overall", horizon_end=today,
                    expected_harm="attendance denominator unknown (sessions held so far), so the "
                                  "eligibility line cannot be projected",
                    model="attendance_closed_form", inputs=inputs,
                    recommendation="open the ERP attendance detail page and re-sync — the count, not "
                                   "the percentage, is what this needs")
    total = int(total_sessions) if total_sessions else int(round(attended + sessions_left))
    att = int(attended) if attended is not None else int(round(current_rate / 100.0 * total))
    att = max(0, min(att, total))
    ceiling = (att + sessions_left) / total * 100.0 if total else 0.0
    missable = 0
    for m in range(sessions_left + 1):
        if (att + sessions_left - m) / total * 100.0 < floor_pct:
            break
        missable = m
    inputs.update({"total_sessions": total, "attended": att, "max_reachable_pct": round(ceiling, 1),
                   "missable": missable})
    if ceiling < floor_pct:
        return Risk(PROVEN, "attendance", "overall", horizon_end=today, probability=None,
                    expected_harm=f"even attending all {sessions_left} remaining sessions only reaches "
                                  f"{ceiling:.1f}% against a {floor_pct:g}% floor",
                    model="attendance_closed_form", inputs=inputs,
                    evidence="closed form on the ERP-reported counts",
                    recommendation="this is arithmetic, not a forecast: apply for medical condonation "
                                   "now, and get the receipt recorded",
                    confidence_label="closed form (deterministic)")
    if missable <= 1:
        return Risk(AT_RISK, "attendance", "overall", horizon_end=today, probability=None,
                    expected_harm=f"you can miss {missable} of {sessions_left} remaining sessions "
                                  f"before dropping under {floor_pct:g}% (attending all reaches "
                                  f"{ceiling:.1f}%)",
                    model="attendance_closed_form", inputs=inputs,
                    recommendation="treat the next sessions as mandatory; a skip needs a receipt",
                    confidence_label="closed form (deterministic)")
    return Risk(ON_TRACK, "attendance", "overall", horizon_end=today, probability=None,
                expected_harm=f"{missable} of {sessions_left} remaining sessions can be missed before "
                              f"the {floor_pct:g}% floor (attending all reaches {ceiling:.1f}%)",
                model="attendance_closed_form", inputs=inputs,
                confidence_label="closed form (deterministic)")


# ------------------------------------------------------------- buffer decay --

def buffer_decay(tasks: list[dict], *, today: str, work_hours_per_day: float = 3.0) -> list[Risk]:
    """Every open task has a *burn rate*: work left ÷ days left. When that ratio crosses your
    realistic capacity the plan is not impossible yet — it becomes impossible on a date you can
    point at. Forecasting that date is the useful part; asserting the breach is the solver's job.

    `tasks`: [{id, title, minutes_remaining, due, started_minutes, requires:[ids]}]
    """
    out: list[Risk] = []
    t0 = _d(today)
    if not t0:
        return [Risk(PENDING, "week", "unknown", expected_harm="no reference date given",
                     model="buffer_decay")]
    byid = {t["id"]: t for t in tasks}
    for t in tasks:
        due = _d(t.get("due"))
        if not due:
            out.append(Risk(PENDING, "task", t["id"], horizon_end=today,
                            expected_harm="no verified due date on record, so no decay curve exists",
                            model="buffer_decay", recommendation="ask the faculty / re-sync the portal"))
            continue
        days_left = (due - t0).days
        rem = max(0, int(t.get("minutes_remaining", t.get("minutes", 0))))
        need_days = rem / max(15.0, work_hours_per_day * 60.0)
        # Dependency depth: your own work is not the only schedule you depend on.
        chain, seen = 0, set()
        cur = t
        while cur and cur.get("requires") and len(seen) < 12:
            nxt = next((byid.get(r) for r in cur["requires"] if r not in seen), None)
            if not nxt:
                break
            seen.add(nxt["id"])
            chain += 1
            cur = nxt
        if days_left < 0:
            out.append(Risk(AT_RISK, "task", t["id"], horizon_start=today, horizon_end=str(due),
                            probability=1.0, expected_harm=f"already {abs(days_left)}d past its due "
                            f"date with {rem} min of work left", model="buffer_decay",
                            inputs={"minutes_remaining": rem, "days_left": days_left},
                            recommendation="request the policy-permitted extension or accept the "
                                           "documented penalty — do not silently let it rot",
                            confidence_label="date arithmetic"))
            continue
        ratio = need_days / max(1, days_left)
        prob = max(0.0, min(0.97, 0.5 + 0.5 * (ratio - 1.0))) if ratio > 0.8 else max(
            0.02, 0.5 * ratio)
        basis = {"minutes_remaining": rem, "days_left": days_left, "need_days": round(need_days, 2),
                 "capacity_hours_per_day": work_hours_per_day, "ratio": round(ratio, 2),
                 "dependency_depth": chain}
        breach = t0 + dt.timedelta(days=max(0, int(need_days * (1.0 / max(1e-6, ratio)) if ratio else 0)))
        if ratio >= 1.0 or chain >= 3:
            out.append(Risk(AT_RISK, "task", t["id"], horizon_start=today, horizon_end=str(due),
                            probability=prob,
                            expected_harm=(f"needs {need_days:.1f} work-day(s) in {days_left} calendar "
                                           f"day(s) at {work_hours_per_day:g}h/day"
                                           + (f"; {chain} releases you are waiting on" if chain else "")),
                            model="buffer_decay", inputs=basis,
                            recommendation="start today, or shrink scope to the parts the rubric "
                                           "actually weights",
                            confidence_label=f"ratio {ratio:.2f}, depth {chain}"))
        else:
            out.append(Risk(ON_TRACK, "task", t["id"], horizon_start=today, horizon_end=str(due),
                            probability=prob, expected_harm=f"{ratio * 100:.0f}% of capacity used",
                            model="buffer_decay", inputs=basis, confidence_label="ratio"))
    return out


# ----------------------------------------------------------- calibration ----

def calibrate(past_forecasts: list[dict], *, subject_prefix: str = "", min_n: int = 5) -> dict:
    """How often this kind of forecast was right, measured on our own records (PART 17's
    "and we were right x of y times"). `past_forecasts` rows from `db.forecasts()`; an outcome is
    known when a later change_event/claim proves the horizon passed.

    Returns {"n":..,"hits":..,"rate":..,"ready":bool}. `ready` is False with fewer than `min_n`
    resolved samples, and callers must then *say so* rather than quote the meaningless rate.
    """
    res = [r for r in past_forecasts
           if not subject_prefix or str(r.get("subject_id", "")).startswith(subject_prefix)]
    scored = [r for r in res if r.get("basis") and "actual" in json.dumps(r.get("basis") or {})]
    hits = sum(1 for r in scored if _agrees(r))
    n = len(scored)
    return {"n": n, "total": len(res), "hits": hits,
            "rate": (hits / n) if n else None, "ready": n >= min_n,
            "note": (f"{n} resolved sample(s) of {len(res)}; need {min_n} before a historical rate means "
                     f"anything")}


def _agrees(row: dict) -> bool:
    b = row.get("basis") or {}
    act, pred = b.get("actual"), b.get("predicted")
    if act is None or pred is None:
        return False
    try:
        return abs(float(act) - float(pred)) <= 0.25
    except Exception:
        return bool(act) == bool(pred)


# ------------------------------------------------------------- assessment --

def assess(*, feasibility: dict | None, attendance: Risk | None, decay: list[Risk],
           today: str, horizon_days: int = 14, calibration: dict | None = None) -> list[Risk]:
    """Merge the three sources into one ranked list. Rule: a solver result always outranks a
    forecast about the same subject, and a forecast never converts a PENDING into a PROVEN."""
    end = (_d(today) + dt.timedelta(days=horizon_days)) if _d(today) else ""
    out: list[Risk] = []
    if feasibility:
        status = feasibility.get("status", "UNKNOWN")
        if status == "INFEASIBLE":
            core = feasibility.get("core") or []
            out.append(Risk(PROVEN, "week", feasibility.get("week", "current"),
                            horizon_start=today, horizon_end=str(end),
                            expected_harm=(feasibility.get("head_line") or
                                           f"no schedule exists; unsat core {', '.join(core[:4])}"),
                            model="cp_sat", inputs={"core": core,
                                                    "shortfalls": feasibility.get("per_prefix", {}),
                                                    "remedies": [r.get("kind") for r in
                                                                 feasibility.get("remedies") or []]},
                            evidence=feasibility.get("why", [""])[0] if feasibility.get("why") else "",
                            recommendation=(f"apply remedy: {feasibility['remedies'][0]['kind']}"
                                            f"({feasibility['remedies'][0].get('cost_pct')}% grade)"
                                            if feasibility.get("remedies") else
                                            "reduce scope — no policy-permitted remedy resolves the core"),
                            confidence_label="proof (solver)"))
        elif status == "FEASIBLE":
            out.append(Risk(ON_TRACK, "week", feasibility.get("week", "current"), horizon_start=today,
                            horizon_end=str(end),
                            expected_harm=f"schedule exists; slack {feasibility.get('slack_hours', '?')}h",
                            model="cp_sat", inputs={"objective": feasibility.get("objective")},
                            confidence_label="proof (solver)"))
        else:
            out.append(Risk(PENDING, "week", feasibility.get("week", "current"),
                            expected_harm=feasibility.get("reason", "solver did not run"),
                            model="cp_sat"))
    if attendance:
        out.append(attendance)
    out.extend(decay)
    # Forecasts may raise a *rank*, never change a category. Kept as an explicit step because the
    # tempting shortcut — "attendance is at risk, therefore the week is infeasible" — is exactly
    # the fake precision the design forbids.
    for r in out:
        if r.category == AT_RISK and calibration is not None:
            r.calibration = calibration if calibration.get("ready") else None
            if not calibration.get("ready"):
                r.inputs = {**(r.inputs or {}), "calibration": calibration.get("note", "")}
    sev = {PROVEN: 0, AT_RISK: 1, PENDING: 2, ON_TRACK: 3}
    out.sort(key=lambda r: (sev.get(r.category, 9), -(r.probability or 0.0)))
    return out


def summarise(risks: list[Risk]) -> dict:
    from collections import Counter
    c = Counter(r.category for r in risks)
    return {"counts": dict(c), "proven": c.get(PROVEN, 0), "at_risk": c.get(AT_RISK, 0),
            "pending": c.get(PENDING, 0),
            "predicted_rows": sum(1 for r in risks if r.as_forecast()["is_prediction"]),
            "head_line": ("nothing provably broken" if not c.get(PROVEN) else
                          f"{c[PROVEN]} proven infeasible (solver, not forecast)") + "; " +
                         f"{c.get(AT_RISK, 0)} at risk, {c.get(PENDING, 0)} pending data",
            "sentences": [r.sentence() for r in risks]}
