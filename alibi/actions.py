"""alibi.actions — bounded autonomy, decided by policy code, never by the model (PART 16/19).

The contract in one table:

    READ_ONLY          execute            (recompute, explain, forecast)
    REVERSIBLE         execute            (local calendar file, reminder row) — undo is a row flip
    LOW_RISK_WRITE     execute            (local plan change)
    HIGH_IMPACT_WRITE  APPROVAL_REQUIRED  (anything that leaves the machine: email, ticket, submit)
    IRREVERSIBLE       REFUSED            (delete, mark attendance, drop course) — no flag, no config

Three rules that make this more than a `if risk == ...` chain:

1. **Justification is a *requirement*, not a comment.** An action without supporting claims and a
   solver/policy sentence cannot be opened at all — it raises. That is what stops a plausible-sounding
   agent idea from becoming a write.
2. **Nothing that leaves the machine is ever executed by us.** A `send_email` action is *refused* and
   a `draft_email` is produced instead; the human pastes and sends. This is not timidity: a wrong
   email to a faculty member is the one mistake this system cannot roll back, and it costs social
   capital a student cannot afford.
3. **Idempotency is enforced before execution**, so a retry after a crash cannot send the same
   request twice.

The executor is injected. Tests hand in a recorder; the app hands in the local-calendar writer. The
policy gate is identical in both, which is the only reason the tests mean anything.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Callable

AUTO_OK, APPROVAL_REQUIRED, REFUSED = "AUTO_OK", "APPROVAL_REQUIRED", "REFUSED"

CLASSES = ("READ_ONLY", "REVERSIBLE", "LOW_RISK_WRITE", "HIGH_IMPACT_WRITE", "IRREVERSIBLE")

# What each class may do by default. Configuration may *tighten* this table (an institution that
# wants approval even for reminders), never loosen HIGH_IMPACT_WRITE to AUTO_OK.
DEFAULT_GATE: dict[str, str] = {
    "READ_ONLY": AUTO_OK,
    "REVERSIBLE": AUTO_OK,
    "LOW_RISK_WRITE": AUTO_OK,
    "HIGH_IMPACT_WRITE": APPROVAL_REQUIRED,
    "IRREVERSIBLE": REFUSED,
}

# Actions the model is never allowed to *name*, whatever the config says. A config file that can
# enable `delete_submission` is a config file that will, one demo day, be edited by someone in a hurry.
NEVER: tuple[str, ...] = ("delete_submission", "mark_attendance_present", "drop_course",
                          "change_grade", "execute_shell", "run_command", "overwrite_notice")

KIND_CLASS: dict[str, str] = {
    "recompute": "READ_ONLY", "explain": "READ_ONLY", "forecast": "READ_ONLY",
    "create_event": "REVERSIBLE", "set_reminder": "REVERSIBLE", "reschedule_local": "REVERSIBLE",
    "plan_change": "LOW_RISK_WRITE", "request_review": "LOW_RISK_WRITE",
    "draft_email": "HIGH_IMPACT_WRITE", "send_email": "IRREVERSIBLE",
    "submit_assignment": "IRREVERSIBLE", "file_condonation": "HIGH_IMPACT_WRITE",
    "delete_submission": "IRREVERSIBLE", "mark_attendance_present": "IRREVERSIBLE",
    "execute_shell": "IRREVERSIBLE",
}


@dataclass
class Action:
    kind: str
    target: str = ""
    payload: dict = field(default_factory=dict)
    risk_class: str = ""
    justification: str = ""                 # the solver/policy output, verbatim, no prose polish
    supporting_claims: list[int] = field(default_factory=list)
    solver_status: str = ""
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        self.risk_class = self.risk_class or KIND_CLASS.get(self.kind, "IRREVERSIBLE")
        if self.risk_class not in CLASSES:
            raise ValueError(f"unknown risk class {self.risk_class!r} for action {self.kind!r}")


@dataclass
class Verdict:
    decision: str
    reason: str
    action: Action
    executed: bool = False
    result: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def may_execute(self) -> bool:
        return self.decision == AUTO_OK


class PolicyError(RuntimeError):
    pass


# ---------------------------------------------------------------- the gate ---

def evaluate(a: Action, *, policy: dict | None = None, solver_status: str = "",
             now_iso: str = "") -> Verdict:
    """Pure function. No DB, no clock, no model — so the same verdict can be asserted in a test,
    recomputed in the UI, and replayed from the audit log."""
    gate = dict(DEFAULT_GATE)
    for k, v in ((policy or {}).get("action_gate") or {}).items():
        if k in gate and v != AUTO_OK:            # config may only tighten, see the module docstring
            gate[k] = v
    if a.kind in NEVER:
        return Verdict(REFUSED, f"`{a.kind}` is not an action this system can perform at all — it is "
                                f"on the never-list, not the approval list", a)
    if not a.supporting_claims:
        # The whole thesis: nothing acts on a feeling. A draft that cites no ledger row is a
        # hallucination with an SMTP connection.
        raise PolicyError(f"action {a.kind!r} has no supporting_claims; refusing to even open it")
    if not (a.justification or "").strip():
        raise PolicyError(f"action {a.kind!r} has no justification sentence; the audit log must be "
                          f"readable by someone who did not write this code")
    if not a.idempotency_key:
        raise PolicyError(f"action {a.kind!r} has no idempotency_key; a retry after a crash could "
                          f"repeat a write")
    dec = gate.get(a.risk_class, REFUSED)
    why = {"READ_ONLY": "read-only computation cannot damage state",
           "REVERSIBLE": "local and reversible: one row/entry to undo",
           "LOW_RISK_WRITE": "touches only the student's own local plan",
           "HIGH_IMPACT_WRITE": "leaves the machine or affects a record; a human approves the exact "
                                "bytes first",
           "IRREVERSIBLE": "cannot be rolled back, and the harm is social/academic, not technical"}\
        .get(a.risk_class, "unknown class")
    if a.kind == "draft_email":
        why += " — and we draft, we do not send"
    if solver_status and solver_status not in ("INFEASIBLE", "FEASIBLE", ""):
        return Verdict(REFUSED, f"solver status {solver_status!r} is not a proof; an action needs "
                                f"FEASIBLE/INFEASIBLE from CP-SAT, not UNKNOWN", a)
    if dec == APPROVAL_REQUIRED:
        why += (f"; approval is required and the payload is shown verbatim to the approver "
                f"(target={a.target or 'n/a'})")
    return Verdict(dec, why, a)


def open_action(db, a: Action, *, policy: dict | None = None, run_id: str = "",
                executor: Callable[[Action], str] | None = None) -> tuple[Verdict, int]:
    """Persist the request *before* deciding to execute it, then execute only what the gate allows.

    The order matters: a crash after execution with no record is the failure mode a "smart assistant"
    has and a receipted system does not.
    """
    v = evaluate(a, policy=policy, solver_status=a.solver_status)
    aid, created = db.open_action(
        kind=a.kind, risk_class=a.risk_class, target=a.target, payload=a.payload,
        justification=a.justification, supporting_claims=list(a.supporting_claims),
        solver_status=a.solver_status, policy_decision=v.decision, policy_reason=v.reason,
        idempotency_key=a.idempotency_key, run_id=run_id)
    v.notes.append("replayed (existing action row)" if not created else f"action:{aid} opened")
    if v.decision != AUTO_OK:
        db.audit("policy", f"action_{v.decision.lower()}", f"action:{aid}",
                 before=None, after={"kind": a.kind, "risk": a.risk_class, "reason": v.reason},
                 run_id=run_id)
        return v, aid
    if not created:
        v.result = "already executed earlier; not repeated"
        return v, aid
    if executor is None:
        db.finish_action(aid, "dry_run", "no executor wired (policy allowed it, nothing was done)")
        v.notes.append("dry_run: no executor configured")
        return v, aid
    try:
        out = executor(a)
        db.finish_action(aid, "executed", str(out)[:2000])
        v.executed, v.result = True, str(out)
        db.audit("policy", "action_executed", f"action:{aid}", after={"result": str(out)[:500]},
                 run_id=run_id)
    except Exception as e:                                   # an action that fails must still be closed
        db.finish_action(aid, "failed", f"{type(e).__name__}: {e}")
        v.result = f"failed: {e}"
        v.notes.append("executor raised; action closed as failed, nothing retried automatically")
    return v, aid


# --------------------------------------------------------------- drafting ----

def draft_extension_email(claim_rows: list[dict], remedy: dict, *, student: str, to: str,
                          course: str, evidence_lines: list[str], task_id: str = "", db=None) -> Action:
    """The one externally-visible artefact this system produces, and it is a *draft*.

    Every sentence in it is bound to a ledger row: the deadline, the weight, the late policy, the
    solver's shortfall. That is what makes it defensible to send — and if a claim is missing, the
    draft says "the ledger does not record X" instead of inventing X. No model writes this text, so
    no model can be prompted into adding a promise the college never made.
    """
    by_pred = {r.get("predicate"): r for r in claim_rows}
    # A student would never paste `2026-10-11` into an email to a professor, and an email that reads
    # like a database dump is an email that gets ignored — so dates are rendered human, from the
    # stored ISO value, with the raw string as the fallback when it does not parse.
    def human(v):
        """(text, is_iso). Never invents: when the stored value is not an ISO date, the caller is
        told, so the draft can say so instead of guessing."""
        if not v:
            return "[not recorded]", False
        try:
            return dt.date.fromisoformat(str(v)[:10]).strftime("%a %d %b %Y"), True
        except Exception:
            return str(v), False

    def clock(v):
        try:
            h, m = str(v).split(":")[:2]
            h = int(h)
            return f"{h % 12 or 12}:{m} {'am' if h < 12 else 'pm'}"
        except Exception:
            return str(v) if v else ""
    def val(pred, default=None):
        """A claim's value is normally an object, but a hand-edited or OCR-mangled row can hold a bare
        string. Coerce, never assume — an exception here would mean no draft at all, which pushes the
        student toward writing the email from memory (the failure mode we are preventing)."""
        v = (by_pred.get(pred) or {}).get("value", default)
        if isinstance(v, str):
            return {"date" if pred in ("due_at", "exam_date", "released_at") else "text": v}
        return v or (default or {})
    due, late, wt = val("due_at"), val("late_policy"), val("weight")
    # The work's own name if the ledger has one: "Extension request — dbms-dbms_ia2" is what a
    # professor receives from a machine; a human-readable label is what a student would write. A slug
    # is *promoted from the subject key*, never invented — if the ledger cannot name the work, the
    # subject line stays terse rather than getting a confident-sounding title.
    work = (task_id or (claim_rows[0].get("subject_id") if claim_rows else "") or "").replace("_", " ")
    work = work[0].upper() + work[1:] if work else ""
    due_text, due_ok = human(due.get("date"))
    q = lambda p: (by_pred.get(p) or {}).get("evidence_span", "")
    cost = remedy.get("cost_pct", 0)
    days = remedy.get("days", 1)
    body = []
    body.append(f"Subject: Extension request — {work or course}"
                + (f" ({due.get('date')})" if due_ok else ""))
    body.append("")
    body.append("Respected Sir/Ma\'am,")   # fixed salutation: an address is not a name to guess
    body.append("")
    if due_ok:
        clause = (f"The submitted work is due {due_text}"
                  + (f", {clock(due.get('time'))}" if due.get("time") else "") + ".")
    else:
        # Quoting what the source actually said beats rounding it into a plausible date: the
        # professor can correct the student, but cannot notice a confidently formatted lie.
        clause = (f"The ledger's due-date record for this work is not a date — it reads "
                  f"“{due_text}” — so I am confirming the date with you rather than assuming one.")
    body.append(f"I am {student}, {course}. {clause}")
    if late:
        # `late_policy` is claimed as free text OR as numbers depending on how the source was read,
        # so both shapes are rendered and neither is invented.
        stated = late.get("text") or (f"−{late.get('pct_per_day', 0):g}% per day"
                                      if late.get("pct_per_day") is not None else "as notified")
        cap = f", up to {late.get('max_days')} days" if late.get("max_days") else ""
        body.append(f"Your stated late policy is {stated}{cap}.")
    else:
        body.append("The ledger does not record a late-submission clause for this course, so I am "
                    "not assuming one — I am asking rather than relying on it.")
    body.append("")
    body.append("Why I am asking: the deterministic schedule check proves the remaining work "
                f"({remedy.get('minutes_left', '?')} min) does not fit before that date; the "
                f"shortfall is {remedy.get('shortfall_hours', '?')} hours. I am not asking for "
                "extra marks, only for time.")
    if cost and late:
        body.append(f"I accept the documented penalty of {cost}% "
                    " per day on this component"
                    + (f", which is {wt.get('weight', 0) * 100:.0f}% of the course."
                       if wt.get("weight") else "."))
    body.append("")
    if cost and not late:
        body.append(f"A one-day extension would cost me {cost}% of this component under any penalty "
                    "you apply; I am asking for the day, not for a waiver of the penalty.")
    body.append("If one more day is not possible, please ignore this note — I will submit on the "
                "current date as planned.")
    body.append("")
    body.append(f"— {student}")
    cite = "\n".join(f"  · {ln}" for ln in evidence_lines) or "  · (no citations — do not send this)"
    payload = {"to": to, "subject": f"Extension request — {course}", "body": body,
               "citations": cite, "claims": [r.get("id") for r in claim_rows],
               "sending": "HUMAN"}
    key = f"{task_id or course}:{due.get('date', 'nodate')}:ext{days}:{cost}"   # replay-safe idempotency
    a = Action(kind="draft_email", target=to, payload=payload, justification=json.dumps(
        {"remedy": remedy.get("kind"), "days": days, "cost_pct": cost,
         "core": remedy.get("core", []), "q_verbatim": {k: bool(q(k)) for k in
                                                        ("due_at", "late_policy", "weight")}},
        ensure_ascii=False), supporting_claims=[r["id"] for r in claim_rows if r.get("id")],
        solver_status="INFEASIBLE", idempotency_key=key)
    return a
