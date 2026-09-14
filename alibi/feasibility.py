"""Alibi — feasibility engine (loop step 5) + minimal-unsat-core explainer (step 6).

MODEL
  A horizon of days. Available minutes on a day = [day_start, day_end] minus fixed
  timetable sessions. A task's minutes are split across three modes:
      on_time  → days strictly before the safe due date
      extended → days inside the extension window permitted by the *cited* late policy
      missed   → only if allow_miss is set (default OFF)
  Default OFF is deliberate: a "solution" where the student simply doesn't submit is not
  a plan, it is a lie with a nicer objective value. The plan's real decision variables are
  "which deadline must move" and "which session can I not skip", so the model is asked to
  prove impossibility, not to optimise away responsibility.
  A per-day self-directed work cap encodes the humane/sleep guardrail (≥6h floor is
  literature-derived, never learned from the user — see the docstring on `explain`).

SOUNDNESS, STATED HONESTLY
  Preemptive, capacity-per-day relaxation. INFEASIBLE here ⇒ impossible for real (no
  block decomposition repairs a negative capacity). FEASIBLE here is necessary but not
  sufficient; the block-legal plan is emitted separately and re-checked at block
  granularity. This is the right trade because the failure we are protecting against is
  *booking a plan that cannot be executed* — the relaxation errs in exactly that direction.

WHY NOT AN LLM
  ExtractBench (arXiv 2602.12247) shows frontier models emitting schema-valid but
  content-wrong structured output (90% valid vs 12.5% pass on one domain); arithmetic-
  heavy feasibility is where a fluent wrong answer is the catastrophic mode this product
  exists to remove. The model writes prose; the solver owns the decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from ortools.sat.python import cp_model

MISS_MINUTES = 45  # 1% of grade weight ≈ this many minutes of "pain"; one objective unit


@dataclass
class Session:
    id: str
    course: str
    day: date
    minutes: int
    must_attend: bool = False              # forced by the eligibility floor, not by the solver
    kind: str = "class"
    note: str = ""


@dataclass
class Task:
    id: str
    title: str
    course: str
    minutes: int
    safe_due: date                         # from Ledger.safe_value()
    weight: float = 0.0                    # fraction of final grade
    ext_days: int = 0                      # permitted by the *cited* late policy
    ext_pct_per_day: float = 0.0           # e.g. 10 (%/day)
    review_blocks: int = 0                 # spaced-retrieval cards before safe_due
    review_gap_days: int = 2
    requires: tuple[str, ...] = ()
    released: date | None = None            # work cannot precede release (portal opens, data set)


@dataclass
class Horizon:
    days: list[date]
    sessions: list[Session] = field(default_factory=list)
    day_start_min: int = 9 * 60
    day_end_min: int = 22 * 60 + 30
    work_cap_min: int = 240                # 4h/day of self-directed work


@dataclass
class Result:
    status: str                            # FEASIBLE | INFEASIBLE | UNKNOWN
    core: list[str] = field(default_factory=list)
    why: list[str] = field(default_factory=list)
    remedies: list[dict] = field(default_factory=list)
    per_prefix: dict[str, dict] = field(default_factory=dict)
    plan: dict = field(default_factory=dict)
    objective: float | None = None
    solves: int = 0

    @property
    def feasible(self) -> bool:
        return self.status == "FEASIBLE"

    @property
    def shortfall_min(self) -> int:
        vals = [v["slack_min"] for v in self.per_prefix.values()] or [0]
        return min(0, min(vals))


# --------------------------------------------------------------------------- #
def _availability(hz: Horizon) -> dict[date, int]:
    free = {d: hz.day_end_min - hz.day_start_min for d in hz.days}
    for s in hz.sessions:
        if s.day in free:
            free[s.day] -= s.minutes
    return {d: max(v, 0) for d, v in free.items()}


def per_prefix(hz: Horizon, tasks: list[Task]) -> dict[str, dict]:
    """required vs available minutes for every window ending on a due date."""
    free = _availability(hz)
    out: dict[str, dict] = {}
    for dl in sorted({t.safe_due for t in tasks}):
        days = [d for d in hz.days if d < dl]
        # A deadline at or before the window's first day has *zero* legal time left in this horizon.
        # Keep that as a first-class case: `days == []` used to make the explanation below raise IndexError,
        # which the caller reported as "solver did not run" — a crash dressed up as a missing forecast.
        if not days:
            out[dl.isoformat()] = {"days": [], "required_min": sum(t.minutes for t in tasks
                                                                   if t.safe_due <= dl),
                                   "free_min": 0, "legal_min": 0, "fixed_session_min": 0,
                                   "work_cap_min": 0, "slack_min": -sum(t.minutes for t in tasks
                                                                         if t.safe_due <= dl),
                                   "empty_window": True, "deadline_before_horizon": True}
            continue
        need = sum(t.minutes for t in tasks if t.safe_due <= dl)
        fixed = sum(s.minutes for s in hz.sessions if s.day in days)
        gross = sum(free[d] for d in days)
        legal = min(gross, hz.work_cap_min * len(days)) if days else 0
        out[dl.isoformat()] = {
            "days": [d.isoformat() for d in days],
            "required_min": need, "free_min": gross, "legal_min": legal,
            "fixed_session_min": fixed, "work_cap_min": hz.work_cap_min * len(days),
            "slack_min": legal - need,
        }
    return out


# --------------------------------------------------------------------------- #
def _build(hz: Horizon, tasks: list[Task], on: dict[str, bool], *,
            allow_miss: bool = False):
    m = cp_model.CpModel()
    free = _availability(hz)
    ot, ex, ms, used = {}, {}, {}, {d: [] for d in hz.days}

    for t in tasks:
        ot[t.id], ex[t.id] = {}, {}
        for d in hz.days:
            if t.released and d < t.released and on.get(f"released_{t.id}", True):
                continue
            if on.get(f"due_{t.id}", True) and d < t.safe_due:
                v = m.NewIntVar(0, t.minutes, f"on_{t.id}_{d}")
                ot[t.id][d] = v
                used[d].append(v)
            if t.ext_days and on.get(f"ext_{t.id}", True):
                hi = t.safe_due + timedelta(days=t.ext_days)
                if t.safe_due <= d <= hi:
                    v = m.NewIntVar(0, t.minutes, f"ex_{t.id}_{d}")
                    ex[t.id][d] = v
                    used[d].append(v)

    state = {}
    for t in tasks:
        o = m.NewIntVar(0, t.minutes, f"O_{t.id}")
        e = m.NewIntVar(0, t.minutes, f"E_{t.id}")
        b = m.NewIntVar(0, t.minutes, f"B_{t.id}")
        m.Add(o == sum(ot[t.id].values()))
        m.Add(e == sum(ex[t.id].values()))
        m.Add(o + e + b == t.minutes)
        if not allow_miss and on.get("no_miss", True):
            m.Add(b == 0)
        state[t.id] = (o, e, b)

    # Precedence at day granularity (coarser than the rest = conservative: it can only
    # ever report MORE infeasibility, never less, so the explanation stays trustworthy).
    if on.get("precedence", True):
        for t in tasks:
            for r in t.requires:
                if r not in ot or t.id not in ot:
                    continue
                for d, v in list(ot[t.id].items()) + list(ex[t.id].items()):
                    prior = [x for dd, x in ot[r].items() if dd < d]
                    if prior:
                        w = m.NewBoolVar(f"wk_{t.id}_{r}_{d}")
                        m.Add(v >= 1).OnlyEnforceIf(w)
                        m.Add(v == 0).OnlyEnforceIf(w.Not())
                        m.Add(sum(prior) >= t.minutes).OnlyEnforceIf(w)
    # humane cap per day
    for d in hz.days:
        if used[d] and on.get(f"cap_{d.isoformat()}", True):
            m.Add(sum(used[d]) <= hz.work_cap_min)

    # spaced review cards: 30 min each, ≥ review_gap_days before the due date.
    # Fixed-interval on purpose: Latimier et al. 2021 found expanding == uniform
    # (g = 0.03, n.s.); Cepeda's 10-20% rule sets the gap. No Anki, no SRS algorithm.
    for t in tasks:
        if not t.review_blocks or not on.get(f"review_{t.id}", True):
            continue
        ok = [d for d in hz.days
              if d < t.safe_due and (t.safe_due - d).days >= max(1, t.review_gap_days)]
        cards = []
        for d in ok:
            c = m.NewBoolVar(f"card_{t.id}_{d}")
            cards.append(c)
            used[d].append(30 * c)
        if cards:
            m.Add(sum(cards) >= min(t.review_blocks, len(cards)))

    # objective: minimise lost grade (missed work) then extensions asked for
    pen = []
    for t in tasks:
        o, e, b = state[t.id]
        wpen = max(1, int(round(t.weight * 100))) * MISS_MINUTES
        if allow_miss:
            pen.append(wpen * b)
        if t.ext_days:
            pen.append(int(round(t.ext_days * max(t.ext_pct_per_day, 1.0))) * MISS_MINUTES // 10 * e
                       + wpen * e)
    if pen:
        m.Minimize(sum(pen))
    return m, dict(ot=ot, ex=ex, used=used, free=free, state=state)


def solve(hz: Horizon, tasks: list[Task], on: dict[str, bool] | None = None, *,
          max_seconds: float = 6.0, allow_miss: bool = False) -> tuple[str, dict]:
    on = on or {}
    m, parts = _build(hz, tasks, on, allow_miss=allow_miss)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max_seconds
    solver.parameters.num_search_workers = 4
    st = solver.Solve(m)
    name = solver.StatusName(st)
    if name in ("FEASIBLE", "OPTIMAL"):
        plan = {}
        for t in tasks:
            o, e, b = parts["state"][t.id]
            vo, ve, vb = (int(solver.Value(x)) for x in (o, e, b))
            slots = [(d.isoformat(), int(solver.Value(v)))
                     for d, v in parts["ot"][t.id].items() if solver.Value(v) > 0]
            slots += [(d.isoformat(), int(solver.Value(v)))
                      for d, v in parts["ex"][t.id].items() if solver.Value(v) > 0]
            per_day: dict[str, int] = {}
            for d, mn in slots:
                per_day[d] = per_day.get(d, 0) + mn
            stt = "at_risk" if vb else "extended" if ve else "on_time"
            plan[t.id] = {"state": stt, "on_time_min": vo, "ext_min": ve, "missed_min": vb,
                          "slots": sorted(slots), "per_day": per_day}
        return "FEASIBLE", {"plan": plan, "objective": solver.ObjectiveValue()}
    if name == "MODEL_INVALID":
        raise RuntimeError("CP-SAT MODEL_INVALID — constraint construction bug")
    return ("INFEASIBLE" if name == "INFEASIBLE" else "UNKNOWN"), {}


# --------------------------------------------------------------------------- #
def active_constraints(hz: Horizon, tasks: list[Task], *, no_miss: bool = True) -> list[str]:
    # NOTE: ext_* are deliberately NOT baseline constraints. "No extension granted" is the
    # starting state; each extension is a *remedy we are allowed to ask for*. Putting them in
    # the baseline would let the model silently assume the favour and hide the whole problem.
    c = [f"due_{t.id}" for t in tasks]
    c += [f"released_{t.id}" for t in tasks if t.released]
    c += [f"cap_{d.isoformat()}" for d in hz.days]
    c += [f"review_{t.id}" for t in tasks if t.review_blocks]
    c += [f"precedence"]
    if no_miss:
        c.append("no_miss")
    return list(dict.fromkeys(c))


def _on_for(cset: list[str], tasks: list[Task]) -> dict[str, bool]:
    on = {c: True for c in cset}
    for t in tasks:
        on.setdefault(f"ext_{t.id}", False)   # "no extension granted" is the default state
    return on


def analyze(hz: Horizon, tasks: list[Task], *, allow_miss: bool = False,
            max_seconds: float = 4.0, log=None) -> Result:
    """Solve; if infeasible, compute a minimal unsat core + priced, policy-bounded remedies."""
    solves = 0
    cset = active_constraints(hz, tasks, no_miss=not allow_miss)
    st, _ = solve(hz, tasks, _on_for(cset, tasks), max_seconds=max_seconds, allow_miss=allow_miss)
    solves += 1
    pp = per_prefix(hz, tasks)

    if st == "FEASIBLE":
        _, payload = solve(hz, tasks, _on_for(cset, tasks), max_seconds=max_seconds,
                           allow_miss=allow_miss)
        return Result("FEASIBLE", per_prefix=pp, plan=payload["plan"],
                      objective=payload.get("objective"), solves=solves)

    # ---- greedy deletion filter → minimal unsat core ----
    core = list(cset)
    for c in list(core):
        trial = [x for x in core if x != c]
        allow = allow_miss or c == "no_miss"
        st2, _ = solve(hz, tasks, _on_for(trial, tasks), max_seconds=max_seconds, allow_miss=allow)
        solves += 1
        if st2 == "INFEASIBLE":
            core = trial
            if log:
                log(f"drop  {c:<22} → still impossible")
        elif log:
            log(f"keep  {c:<22} → removing THIS one makes the week possible")

    # ---- remedies: only relaxations the *cited policy* actually permits are offered ----
    remedies = []
    # "no extension is granted" is the honest baseline for testing a remedy: every task
    # must finish on time, or it is missed.  A remedy is only worth drafting if it moves
    # THIS one fact and nothing else.
    full = {**{c: False for c in cset if c.startswith("ext_")}, **{c: True for c in cset}}
    full.update({f"ext_{t.id}": False for t in tasks if t.ext_days})
    full["no_miss"] = True
    base_ok = solve(hz, tasks, full, max_seconds=max_seconds, allow_miss=False)[0] != "INFEASIBLE"
    for t in tasks:
        if not t.ext_days:
            continue
        ok = solve(hz, tasks, {**full, f"ext_{t.id}": True},
                   max_seconds=max_seconds, allow_miss=allow_miss)[0] != "INFEASIBLE"
        solves += 1
        remedies.append({"kind": "extension_request", "task_id": t.id, "title": t.title,
                         "also_fixes_without_extension": base_ok,
                         "course": t.course, "days": t.ext_days,
                         "cost_pct": round(t.ext_days * t.ext_pct_per_day, 1),
                         "makes_feasible": ok,
                         "justification": "cited late-policy clause + ledger evidence of progress"})
    for extra in (60, 120):
        hz2 = replace(hz, work_cap_min=hz.work_cap_min + extra)
        ok = solve(hz2, tasks, full, max_seconds=max_seconds,
                   allow_miss=allow_miss)[0] != "INFEASIBLE"
        solves += 1
        remedies.append({"kind": "raise_work_cap", "extra_min": extra, "makes_feasible": ok,
                         "cost_pct": 999.0,
                         "cost": "encroaches the sleep floor (≈−0.07 GPA/hour lost; Okano 2019, CMU 2023)",
                         "title": f"work {(hz.work_cap_min + extra) // 60}h/day instead of {hz.work_cap_min // 60}h/day"})
    # A remedy that violates the student's *own* guardrail (sleep cap) ranks below one the
    # institution's policy actually permits, even when both work. The tool should not offer
    # to solve an overload by taking the one thing the literature says not to trade away.
    def rank(r):
        return (not r["makes_feasible"],
                 0 if r["kind"] == "extension_request" else 1 if r["kind"] == "raise_work_cap" else 2,
                 r.get("cost_pct", 999))
    for r in remedies:
        r["cost_class"] = ("policy_permitted" if r["kind"] == "extension_request"
                           else "violates_your_sleep_floor" if r["kind"] == "raise_work_cap"
                           else "your_choice")
    remedies.sort(key=rank)
    binding = [c for c in cset if c not in core] + core
    return Result("INFEASIBLE", core=core, why=explain(cset, hz, tasks, pp,
                                                        no_evidence=any(
                                                            t.safe_due < min(hz.days) for t in tasks)),
                  remedies=remedies, per_prefix=pp, solves=solves,
                  plan={"minimal_core": core, "binding_facts": binding})


def explain(core: list[str], hz: Horizon, tasks: list[Task], pp: dict[str, dict],
            no_evidence: bool = False) -> list[str]:
    lines: list[str] = []
    worst = sorted(pp.items(), key=lambda kv: kv[1]["slack_min"])
    for k, w in worst[:2]:
        if w.get("deadline_before_horizon"):
            past = ("the ledger holds no submission for it, so it is at best already past its date"
                    if no_evidence else "the obligation is recorded as due before this horizon begins")
            lines.append(f"Deadline {k} is at or before the start of this horizon: {w['required_min']/60:.1f}h"
                         f" of work is attributed to it and {past}. No arrangement of the remaining days can"
                         " make this feasible, so it is reported rather than solved — and 'no submission on"
                         " file' is an absence of evidence, not proof that it was missed: confirm it or add"
                         " the receipt.")
            continue
        if w["slack_min"] < 0:
            lines.append(
                f"Window {w['days'][0]}→{k} ({len(w['days'])} days): "
                f"{w['required_min']/60:.1f}h of graded work vs {w['legal_min']/60:.1f}h of legal time "
                f"[{w['free_min']/60:.1f}h free − {w['fixed_session_min']/60:.1f}h fixed timetable, "
                f"capped {w['work_cap_min']/60:.1f}h/day].  Shortfall {(-w['slack_min'])/60:.1f}h.")
    elig = sorted({c.split("eligibility_floor_")[-1] for c in core if c.startswith("eligibility_floor_")})
    if elig:
        lines.append("Sessions that cannot be skipped because of the exam-eligibility floor: "
                     + ", ".join(elig) + ".")
    caps = [c for c in core if c.startswith("cap_")]
    if caps:
        lines.append(f"Daily self-directed work cap of {hz.work_cap_min//60}h on {len(caps)} days "
                     f"(the sleep guardrail; not learned from the user).")
    dues = [c for c in core if c.startswith("due_")]
    if dues:
        lines.append(f"{len(dues)} deadline windows that cannot be reused after the due date.")
    if "no_miss" in core:
        lines.append("No submission may be skipped (all items are graded or mandatory).")
    if "precedence" in core:
        lines.append("A prerequisite order the student cannot reorder.")
    stuck = [f"{t.title} ({t.course}, {t.weight:.0%})" for t in tasks if not t.ext_days]
    day = hz.work_cap_min
    too_big = [f"{t.title}: needs {t.minutes/60:.1f}h but a legal day holds {day/60:.1f}h "
               f"and only {(t.safe_due - (t.released or hz.days[0])).days} day(s) sit between "
               f"release and the deadline"
               for t in tasks if not t.ext_days and t.minutes > day * max(
                   1, (t.safe_due - (t.released or hz.days[0])).days)]
    if too_big:
        lines.append("Individually impossible: " + "; ".join(too_big[:3]) + ".")
    if stuck:
        lines.append("No policy-permitted extension exists for: " + "; ".join(stuck[:4]) + ".")
    return lines
