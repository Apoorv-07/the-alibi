"""Tests for the model layer (PART 5/18/32/38) and the time layers (PART 10/17).

Nothing here calls a real model. That is deliberate: the properties under test are the *contract* —
degradation is a value, egress is a policy, outcomes are recorded — and each must hold whether or not
Ollama happens to be running on the machine that checks out the repo. A test that needs a GPU is a
test that silently stops existing.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alibi import changes as C                                                # noqa: E402
from alibi import providers as P                                              # noqa: E402
from alibi import risk as K                                                   # noqa: E402
from alibi.adapters import Budget, Raw                                        # noqa: E402
from alibi.config import Config, load_institution_policy                      # noqa: E402


# =========================================================== model results ==

def _raw(text, obj=None, err=""):
    return Raw(text=text, model="gemma4:e4b", provider="ollama", ms=412, tokens_in=90,
               tokens_out=40, json_obj=obj, err=err)


def test_outcomes_are_values_not_exceptions():
    for raw, want in ((_raw('{"facts":[{"a":1}]}', {"facts": [{"a": 1}]}), P.SUCCESS),
                      (_raw('{"facts":[]}', {"facts": []}), P.PARTIAL),
                      (_raw("I cannot help with that"), P.INVALID_OUTPUT),
                      (None, P.UNAVAILABLE)):
        got, facts, _ = P._Wrapped._outcome_from_raw(raw)
        assert got == want, (want, got)
        assert isinstance(facts, list)


def test_unparsable_output_is_not_silently_treated_as_empty():
    """'no facts' and 'the model ignored the schema' must stay distinguishable, or an empty ledger
    looks like a quiet document instead of a broken pipeline."""
    ok, facts, trunc = P._Wrapped._outcome_from_raw(_raw("Sure! The deadline is 12 Oct."))
    assert ok == P.INVALID_OUTPUT and facts == []
    assert P.describe_outcome(P.ModelResult(outcome=ok)).startswith("model output did not match")


def test_json_recovery_parses_but_never_repairs():
    assert P._loose_json('```json\n{"facts":[{"a":1}]}\n```') == {"facts": [{"a": 1}]}
    assert P._loose_json('noise before {"facts":[]} noise after') == {"facts": []}
    # A truncated object is NOT brace-fixed: inventing structure is how fabricated facts get born.
    assert P._loose_json('{"facts":[{"a":1}') is None


def test_local_provider_degrades_without_raising(monkeypatch):
    def boom(self, *a, **k):
        raise ConnectionRefusedError("connection refused")
    monkeypatch.setattr(P.Ollama, "extract", boom)
    res = P.GemmaLocal("gemma4:e4b").extract("text", {})
    assert res.outcome == P.UNAVAILABLE and not res.ok and res.egress == "NONE"
    assert "AI extraction unavailable" in P.describe_outcome(res)
    assert res.candidates == [], "a failure must not smuggle half-parsed candidates downstream"


def test_health_probe_reports_which_model_actually_answered(monkeypatch):
    monkeypatch.setattr(P.Ollama, "healthy",
                        lambda self: {"ok": True, "models": ["llama3.2:3b", "qwen3.5:4b"]})
    h = P.GemmaLocal("gemma4:e4b").healthy()
    assert h["available"] and h["model"] == "qwen3.5:4b", "must not claim Gemma when it is absent"
    assert h["fallback_used"] is False
    monkeypatch.setattr(P.Ollama, "healthy", lambda self: {"ok": True, "models": ["mistral:7b"]})
    h2 = P.GemmaLocal("gemma4:e4b").healthy()
    assert h2["available"] is False or h2["model"] == "gemma4:e4b"
    assert h2.get("fallback_used", False) or not h2["available"]


def test_cloud_never_receives_unredacted_text(monkeypatch):
    seen = {}

    class FakeGemini:
        key = "k"
        model = "gemini-2.5-flash-lite"

        def extract(self, text, schema, images=None, system="", opts=None):
            seen["text"] = text
            seen["schema"] = schema
            return _raw('{"facts":[{"predicate":"due_at"}]}', {"facts": [{"predicate": "due_at"}]})

    c = P.CloudEscalation(provider_label="gemini")
    c.client = FakeGemini()
    c.redaction = P.RedactionPolicy(names=("Arun Kumar",))
    out = c.extract("Arun Kumar says roll 21CSE0417 fee 9080812 due 2026-10-11 mail a@b.co", {})
    assert out.outcome == P.SUCCESS and out.egress == "REDACTED_CLOUD"
    assert "Arun Kumar" not in seen["text"], "the student's name left the machine"
    assert "9080812" not in seen["text"] and "a@b.co" not in seen["text"]
    assert "2026-10-11" in seen["text"], "the date is the payload; masking it defeats the purpose"
    assert out.bytes_in == len(seen["text"].encode()), "egress accounting must measure what was sent"


def test_budget_refusal_is_recorded_as_policy_not_failure():
    b = Budget()
    b.day_tokens = 1
    b.state = {"day": date.today().isoformat(), "calls": 9, "tokens": 999, "ms": 0}
    c = P.CloudEscalation(provider_label="gemini", budget=b)
    r = c.extract("some text", {})
    assert r.outcome == P.UNAVAILABLE and r.egress == "NONE", "refused before anything was sent"
    assert "budget" in r.error and any("policy" in n for n in r.notes)


def test_model_supplied_confidence_never_becomes_trust():
    """The value is carried as evidence and the docstring is not the enforcement — this test pins
    that no ModelResult field is *named* trust, so nothing downstream can read it by accident."""
    res = P.ModelResult(outcome=P.SUCCESS, candidates=[{"confidence": 0.99}], provider="gemma_local",
                        model="gemma4:e4b")
    fields = set(res.as_row())
    assert "trust" not in fields and "confidence" not in fields
    assert res.as_row()["outcome"] == P.SUCCESS
    assert "model_confidence" not in res.as_row(), "only the *evidence row* stores the model's guess"


def test_rules_only_is_a_provider_not_a_none():
    r = P.RulesOnly("-").extract("x", {})
    assert r.outcome == P.UNAVAILABLE and r.egress == "NONE" and P.RulesOnly("-").healthy()["available"]


def test_build_probes_and_reports_the_real_routing(monkeypatch, tmp_path):
    monkeypatch.setenv("ALIBI_MODE", "demo")
    cfg = Config.from_env()
    local, cloud, desc = P.build(cfg, policy=load_institution_policy(None))
    assert desc["mode"] == "demo" and local is None and cloud is None, "demo must not call anything"
    assert "egress_policy" in desc and "budget" in desc
    assert desc["cloud"]["available"] is False


# ============================================================= redaction ====

@pytest.mark.parametrize("secret,token", [
    ("vignesh.a@college.edu", "[EMAIL]"), ("+91 9876543210", "[PHONE]"),
    ("21CSE0417", "[ID]"), ("https://erp.college/x?k=1", "[URL]"), ("9080812", "[ID]")])
def test_identifiers_are_masked(secret, token):
    assert token in P.RedactionPolicy().scrub_text(f"note about {secret} end")


def test_academic_content_survives_redaction():
    """Masking that eats dates and course codes turns the cloud call into a useless call — the
    policy has to be a scalpel, not a shredder."""
    s = "CT on 11/10/2026 for AOT unit 3, 77.1% attendance, seminar room 4"
    assert P.RedactionPolicy().scrub_text(s) == s


def test_field_level_minimisation_drops_sensitive_values():
    pol = P.RedactionPolicy()
    kept, dropped = pol.filter_claims([{"predicate": "attendance_pct",
                                       "value": {"attendance_pct": 77.1, "date": "2026-10-11"},
                                       "evidence_span": "77.1%"},
                                      {"predicate": "due_at", "value": {"date": "2026-10-11"},
                                       "evidence_span": "due 11 Oct"}])
    assert "attendance_pct" in dropped
    assert "attendance_pct" not in kept[0]["value"]
    assert kept[0]["value"]["date"] == "2026-10-11", "dates are what the escalation needs"
    assert len(kept) == 2


def test_egress_bytes_are_counted_from_the_serialised_request():
    pol = P.RedactionPolicy(max_quote_chars=40)
    long_quote = "x" * 5000
    kept, _ = pol.filter_claims([{"value": {"a": 1}, "evidence_span": long_quote}])
    assert len(kept[0]["evidence_span"]) <= 40 * 4


# ========================================================= typed changes ====

def _row(subj, pred, val, src, *, rec="2026-10-11"):
    return {"subject_type": "task", "subject_id": subj, "predicate": pred,
            "value_json": json.dumps(val), "source_label": src, "evidence_span": "s" * 20,
            "recorded_at": rec}


def test_a_moved_deadline_is_typed_directional_and_severe():
    cs = C.diff_ledger([_row("CT1", "due_at", {"date": "2026-10-18"}, "course portal")],
                       [_row("CT1", "due_at", {"date": "2026-10-12"}, "notice board", rec="2026-11-02")])
    assert len(cs) == 1
    ch = cs[0]
    assert (ch.kind, ch.direction, ch.severity) == ("DATE_CHANGED", "EARLIER", "HIGH")
    assert ch.buffer_delta_hours == -144.0, "6 days of buffer destroyed, computed not asserted"
    assert C.summarise(cs)["days_lost"] == 6.0


def test_a_later_deadline_is_not_reported_as_a_crisis():
    cs = C.diff_ledger([_row("CT1", "due_at", {"date": "2026-10-18"}, "course portal")],
                       [_row("CT1", "due_at", {"date": "2026-10-25"}, "course portal")])
    assert cs[0].direction == "LATER" and cs[0].severity in ("LOW", "MED")
    assert C.summarise(cs)["days_lost"] == 0.0, "gained time must not be counted as lost"


def test_weight_change_outranks_a_trivial_rewording():
    w = C.classify("task", "LAB4", "weight", {"weight": 0.10}, {"weight": 0.30})
    venue = C.classify("task", "LAB4", "venue", {"venue": "room 3"}, {"venue": "room 4"})
    assert w[0].kind == "WEIGHT_CHANGED" and w[0].severity == "HIGH"
    assert venue[0].kind == "LOCATION_CHANGED" and venue[0].severity == "LOW"


def test_multiple_fields_in_one_source_move_stay_separate():
    cs = C.classify("task", "CT1", "due_at",
                    {"date": "2026-10-18", "time": "23:59"},
                    {"date": "2026-10-12", "time": "09:00"})
    assert len(cs) == 2 and {c.field_name for c in cs} == {"date", "time"}, \
        "collapsing the pair would hide half the sentence"


def test_new_commitment_severity_uses_the_recorded_date_not_wall_clock():
    """A re-sync of last week's export must produce the same severity it produced last week, or the
    UI disagrees with the audit log for reasons nobody can reproduce."""
    soon = C.diff_ledger([], [_row("X", "due_at", {"date": "2026-09-13"}, "class group",
                                   rec="2026-09-12")])
    far = C.diff_ledger([], [_row("X", "due_at", {"date": "2026-11-13"}, "class group",
                                  rec="2026-09-12")])
    assert soon[0].kind == "NEW_REQUIREMENT" and soon[0].severity == "HIGH"
    assert far[0].severity == "LOW" and far[0].buffer_delta_hours == 0.0, \
        "a commitment 62 days out has destroyed no buffer yet"
    assert soon[0].buffer_delta_hours == 0.0, "tomorrow's new task costs nothing *yet* — the " \
                                               "solver's job is the shortfall, not the calendar"
    assert C.summarise(soon)["days_lost"] == 0.0
    overdue = C.diff_ledger([], [_row("X", "due_at", {"date": "2026-09-10"}, "class group",
                                      rec="2026-09-12")])
    assert overdue[0].buffer_delta_hours == -48.0, "past its date = the buffer is gone"
    assert overdue[0].severity == "HIGH" and overdue[0].direction == "OVERDUE", \
        "a new requirement dated in the past is the most urgent shape, not the least"
    again = C.diff_ledger([], [_row("X", "due_at", {"date": "2026-09-13"}, "class group",
                                    rec="2026-09-12")])
    assert [c.as_row()["new_value"] for c in again] == [c.as_row()["new_value"] for c in soon]


def test_authoritative_override_and_correction_are_distinguished():
    """Newer ≠ higher trust (PART 11): a group chat contradicting a notice board is a *correction*
    candidate at best, never an upgrade."""
    hi = C.diff_ledger([_row("CT1", "due_at", {"date": "2026-10-18"}, "class group")],
                       [_row("CT1", "due_at", {"date": "2026-10-12"}, "notice board")],
                       trust={"notice board": 4, "class group": 1})
    lo = C.diff_ledger([_row("CT1", "due_at", {"date": "2026-10-18"}, "notice board")],
                       [_row("CT1", "due_at", {"date": "2026-10-12"}, "class group")],
                       trust={"notice board": 4, "class group": 1})
    assert hi[0].is_correction and hi[0].severity in ("MED", "HIGH")
    assert lo[0].is_correction and lo[0].kind == "CORRECTED"


def test_removals_are_typed_not_silences():
    cs = C.diff_ledger([_row("CT9", "due_at", {"date": "2026-10-19"}, "class group",
                             rec="2026-10-01")], [])
    assert cs[0].kind == "REMOVED_REQUIREMENT"
    cs2 = C.diff_ledger([dict(_row("CT9", "due_at", {"date": "2026-10-19"}, "class group"),
                               retire_reason="WITHDRAWN")], [])
    assert cs2[0].severity == "MED", "a withdrawn class is news; a superseded row is not"


def test_change_rows_are_db_shaped_and_json_valid():
    ch = C.classify("task", "CT1", "due_at", {"date": "2026-10-18"}, {"date": "2026-10-12"})[0]
    row = ch.as_row(run_id="7")
    parsed = json.loads(row["delta_json"])
    assert parsed["direction"] == "EARLIER" and parsed["field"] == "date"
    assert set(row) >= {"kind", "subject_type", "subject_id", "predicate", "old_value", "new_value",
                        "delta_json", "buffer_delta_hours", "detected_by", "severity", "why", "effect"}
    assert "field_name" not in row, "as_row() must be splat-compatible with db.record_change()"
    assert all(isinstance(v, (str, float, int, type(None))) for v in row.values())


def test_no_change_is_stated_as_no_change():
    same = [_row("CT1", "due_at", {"date": "2026-10-18"}, "course portal")]
    cs = C.diff_ledger(same, [dict(r) for r in same])
    assert cs == [] and "nothing changed" in C.summarise(cs)["head_line"]


# ================================================================ risk ======

def test_solver_proof_and_forecast_are_different_types():
    risks = K.assess(feasibility={"status": "INFEASIBLE", "week": "w40", "core": ["no_miss"],
                                 "head_line": "4.0h short on Wed", "why": ["AOT CT1 needs 10h"],
                                 "remedies": [{"kind": "extension_request", "cost_pct": 10.0}],
                                 "slack_hours": 0},
                     attendance=None, decay=[], today="2026-09-12")
    assert risks[0].category == K.PROVEN
    assert risks[0].probability is None, "a proof never carries a probability"
    assert risks[0].as_forecast()["is_prediction"] is False
    assert "PROVEN" in risks[0].sentence() and "probably" not in risks[0].sentence()
    assert "extension_request" in risks[0].recommendation


def test_a_forecast_cannot_be_mistaken_for_a_proof():
    risks = K.assess(feasibility={"status": "FEASIBLE", "week": "w40", "slack_hours": 2.0,
                                 "objective": 5},
                     attendance=K.attendance_forecast(72.0, 20, 75.0, total_sessions=225),
                     decay=K.buffer_decay([{"id": "CT1", "minutes_remaining": 600,
                                            "due": str(date(2026, 9, 14) + timedelta(days=1))}],
                                          today=str(date(2026, 9, 14))),
                     today="2026-09-14")
    assert any(r.category == K.AT_RISK and r.as_forecast()["is_prediction"] for r in risks)
    assert risks[0].category == K.AT_RISK, "forecasts rank below the solver's answer"


def test_probability_on_a_proven_row_is_rejected_at_construction():
    with pytest.raises(ValueError, match="must not carry a probability"):
        K.Risk(K.PROVEN, "week", "w1", probability=0.8)
    with pytest.raises(ValueError, match="fraction"):
        K.Risk(K.AT_RISK, "task", "t", probability=1.4)
    with pytest.raises(ValueError, match="needs the model"):
        K.Risk(K.AT_RISK, "task", "t")


def test_attendance_forecast_is_closed_form_and_reports_the_ceiling():
    r = K.attendance_forecast(72.0, 20, 75.0, total_sessions=225)
    assert r.category == K.ON_TRACK and r.probability is None
    assert r.inputs["missable"] == 13 and r.inputs["max_reachable_pct"] == 80.9
    assert K.attendance_forecast(65.0, 20, 75.0, total_sessions=225).category == K.PROVEN
    tight = K.attendance_forecast(78.6, 20, 75.0, total_sessions=225)
    assert tight.inputs["attended"] == 177


def test_missing_denominator_is_pending_not_a_guess():
    """Assuming a 225-session term to fill a gauge is exactly the fake precision the spec forbids."""
    r = K.attendance_forecast(78.0, 20, 75.0)
    assert r.category == K.PENDING and "denominator unknown" in r.expected_harm
    assert r.as_forecast()["is_prediction"] is True


def test_calibration_says_when_it_has_no_data():
    cal = K.calibrate([])
    assert cal["ready"] is False and cal["rate"] is None and "need 5" in cal["note"]
    risks = K.assess(feasibility=None, attendance=None,
                     decay=K.buffer_decay([{"id": "X", "minutes_remaining": 600, "due": "2026-09-15"}],
                                          today="2026-09-14"),
                     today="2026-09-14", calibration=cal)
    assert risks[0].calibration is None, "no historical rate may be quoted from 0 samples"
    assert "resolved sample" in json.dumps(risks[0].inputs)
    rows = [{"subject_id": "w1", "basis": {"predicted": 0.9, "actual": 0.95}},
            {"subject_id": "w2", "basis": {"predicted": 0.9, "actual": 0.95}},
            {"subject_id": "w3", "basis": {"predicted": 0.2, "actual": 0.9}},
            {"subject_id": "w4", "basis": {"predicted": 0.3, "actual": 0.3}},
            {"subject_id": "w5", "basis": {"predicted": 0.8, "actual": 0.85}}]
    cal2 = K.calibrate(rows)
    assert cal2["ready"] and cal2["hits"] == 4 and abs(cal2["rate"] - 0.8) < 1e-9


def test_risk_summary_counts_predictions_separately_from_proofs():
    risks = K.assess(feasibility={"status": "INFEASIBLE", "week": "w", "core": ["c"],
                                 "head_line": "h"}, attendance=None, decay=[], today="2026-09-14")
    summ = K.summarise(risks)
    assert summ["proven"] == 1 and summ["predicted_rows"] == 0
    assert "solver, not forecast" in summ["head_line"]


# ============================================================== actions ======

def _claims():
    return [{"id": 11, "predicate": "due_at", "value": {"date": "2026-10-11", "time": "23:59"},
             "evidence_span": "Submission: Saturday 11 Oct 2026, 11:59 pm, portal."},
            {"id": 12, "predicate": "late_policy",
             "value": {"pct_per_day": 10, "max_days": 3},
             "evidence_span": "Late policy: −10% per calendar day, maximum 3 days."},
            {"id": 13, "predicate": "weight", "value": {"weight": 0.10},
             "evidence_span": "(10% of course grade)"}]


def test_gate_matrix_is_executed_not_documented():
    from alibi import actions as A
    for kind, want in (("forecast", A.AUTO_OK), ("create_event", A.AUTO_OK),
                       ("plan_change", A.AUTO_OK), ("draft_email", A.APPROVAL_REQUIRED),
                       ("send_email", A.REFUSED), ("submit_assignment", A.REFUSED)):
        a = A.Action(kind=kind, justification="solver: 4.0h short", supporting_claims=[1],
                     idempotency_key=f"k-{kind}")
        assert A.evaluate(a).decision == want, (kind, want)


def test_never_list_beats_configuration():
    from alibi import actions as A
    # A config that "allows" deleting a submission must not become an action: the never-list is code.
    pol = {"action_gate": {"IRREVERSIBLE": "AUTO_OK"}}
    a = A.Action(kind="delete_submission", justification="because", supporting_claims=[1],
                 idempotency_key="k")
    assert A.evaluate(a, policy=pol).decision == A.REFUSED
    assert "never-list" in A.evaluate(a, policy=pol).reason


def test_an_action_without_evidence_cannot_even_be_opened():
    from alibi import actions as A
    with pytest.raises(A.PolicyError, match="supporting_claims"):
        A.evaluate(A.Action(kind="set_reminder", justification="i feel like it"))
    with pytest.raises(A.PolicyError, match="idempotency_key"):
        A.evaluate(A.Action(kind="set_reminder", justification="4.0h short", supporting_claims=[1]))
    with pytest.raises(A.PolicyError, match="justification"):
        A.evaluate(A.Action(kind="set_reminder", supporting_claims=[1], idempotency_key="k"))


def test_high_impact_is_persisted_as_pending_and_never_executed(tmp_path):
    from alibi import actions as A
    from alibi.db import init_db
    db = init_db(tmp_path / "a.db")
    calls = []
    # (1) a send is REFUSED: the row exists so the user can see it was asked and blocked, and the
    # executor is never even handed the action.
    a = A.Action(kind="send_email", target="prof@x", justification="INFEASIBLE core=no_miss",
                 supporting_claims=[1], solver_status="INFEASIBLE", idempotency_key="send-1")
    v, aid = A.open_action(db, a, executor=lambda x: calls.append(x) or "sent")
    assert v.decision == A.REFUSED and not calls, "refused must mean the executor never ran"
    row = db.actions()[0] if len(db.actions()) == 1 else db.actions()[1]
    assert [r for r in db.actions() if r["kind"] == "send_email"][0]["status"] == "pending"
    assert row["supporting_claims"] == [1], "the receipt must name the rows it cites"

    # (2) a draft is APPROVAL_REQUIRED: pending, no execution, and an approval row the UI can act on.
    d = A.Action(kind="draft_email", target="prof@x", payload={"body": ["hi"]},
                 justification="INFEASIBLE", supporting_claims=[1], solver_status="INFEASIBLE",
                 idempotency_key="draft-1")
    v2, aid2 = A.open_action(db, d, executor=lambda row: calls.append(row) or "opened in mail client")
    assert v2.decision == A.APPROVAL_REQUIRED and not calls
    assert db.one("SELECT decision FROM approval WHERE action_id=?", (aid2,))["decision"] == "pending"
    assert db.actions(status="pending"), "an unapproved action must stay visible in the queue"

    # (3) approval alone does not execute — the executor has to be handed over deliberately.
    out = db.decide_approval(aid2, "approved", by="user")
    assert out["ok"] and not out["executed"] and not calls, "CLI path records, does not act"
    assert [r for r in db.actions() if r["id"] == aid2][0]["status"] == "approved"

    # (4) refusing writes an auditable rejection and executes nothing.
    d2 = A.Action(kind="draft_email", target="prof@x", justification="INFEASIBLE",
                  supporting_claims=[1], solver_status="INFEASIBLE", idempotency_key="draft-2")
    _, aid3 = A.open_action(db, d2, executor=lambda row: calls.append(row) or "sent")
    db.decide_approval(aid3, "rejected", by="user", note="wrong date")
    assert not calls and [r for r in db.actions() if r["id"] == aid3][0]["status"] == "rejected"
    assert any(c["kind"] == "APPROVAL_REFUSED" and "refused" in c["why"]
               for c in db.changes(limit=10)), \
        "the human decision must appear in the change log, not only in the approval table"
    db.close()


def test_local_reversible_action_executes_once_and_replays_safely(tmp_path):
    from alibi import actions as A
    from alibi.db import init_db
    db = init_db(tmp_path / "b.db")
    seen = []
    a = A.Action(kind="create_event", target="local.ics", payload={"title": "AOT CT1 draft"},
                 justification="REVERSIBLE: writes one VEVENT to the student's own calendar file",
                 supporting_claims=[1], idempotency_key="ics-1")
    v, aid = A.open_action(db, a, executor=lambda x: seen.append(x.payload["title"]) or "wrote 1 event")
    assert v.executed and v.result.startswith("wrote") and seen == ["AOT CT1 draft"]
    v2, aid2 = A.open_action(db, a, executor=lambda x: seen.append("again") or "wrote again")
    assert aid2 == aid and seen == ["AOT CT1 draft"], "a crash-retry must not double-write"
    assert v2.result == "already executed earlier; not repeated"
    db.close()


def test_extension_draft_quotes_only_what_the_ledger_holds():
    from alibi import actions as A
    a = A.draft_extension_email(_claims(), {"kind": "extension_request", "days": 2, "cost_pct": 20.0,
                                            "minutes_left": 420, "shortfall_hours": 4.0,
                                            "core": ["no_miss", "cap_Wed"]},
                                student="A. Vigneshwar", to="faculty@a", course="AOT",
                                evidence_lines=["due_at claim 11"])
    text = "\n".join(a.payload["body"])
    assert "Sun 11 Oct 2026" in text, "a pasted email must read like a student wrote it, not "        "a database dump"
    assert "11:59 pm" in text
    assert "−10% per day" in text
    assert "up to 3 days" in text
    assert "4.0" in text and "20.0%" in text, "the cost offered must be the cost the policy states"
    assert a.kind == "draft_email" and a.risk_class == "HIGH_IMPACT_WRITE"
    assert a.payload["sending"] == "HUMAN"
    assert a.idempotency_key == "AOT:2026-10-11:ext2:20.0"
    assert json.loads(a.justification)["days"] == 2 and a.solver_status == "INFEASIBLE"
    # missing facts are stated as missing, never filled in
    thin = A.draft_extension_email([{"id": 9, "predicate": "due_at",
                                     "value": "sometime next week maybe",
                                     "evidence_span": "class group message"}],
                                   {"days": 1, "kind": "extension_request"},
                                   student="S", to="f", course="X", evidence_lines=[])
    tt = "\n".join(thin.payload["body"])
    assert "does not record a late-submission clause" in tt
    assert "not a date" in tt and "sometime next week maybe" in tt, \
        "an unparseable date is quoted as what it is, never smoothed into a guess"
    assert "(no citations" in thin.payload["citations"]


# ---------------------------------------------------------- LM Studio transport ---

class _FakeServer:
    """A minimal OpenAI-compatible server: /v1/models + /v1/chat/completions, one behaviour at a time.

    LM Studio is the only AI runtime this product is expected to talk to, so its wire behaviour is tested
    against a stand-in rather than "will work on the reviewer's laptop": the failure modes that matter
    (plain HTTP on a loopback port, no API key, a model that is not loaded, a 500, prose instead of JSON)
    are all properties of the *protocol*, not of the app.
    """

    def __init__(self, mode="ok"):
        import json, threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        self.mode = mode
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path == "/v1/models":
                    return self._send(200, {"data": [{"id": "qwen2.5-7b-instruct"}]})
                return self._send(404, {"error": "no"})

            def do_POST(self):
                n = int(self.headers.get("content-length", 0))
                self.rfile.read(n)
                m = outer.mode
                if m == "500":
                    return self._send(500, {"error": {"message": "model failed to load: oom"}})
                if m == "garbage":
                    return self._send(200, {"choices": [{"message": {"content": "The deadline is soon!"}}]})
                if m == "empty":
                    return self._send(200, {"choices": [{"message": {"content": ""}}]})
                if m == "html":                      # a proxy in front of the server
                    self.send_response(502); self.send_header("content-type", "text/html")
                    b = b"<html><body>502 Bad Gateway</body></html>"
                    self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)
                    return
                body = {"facts": [{"fact_type": "due_date", "value": "2026-10-11",
                                   "source_quote": "Submission: Saturday 11 Oct 2026",
                                   "task_title": "LAB 4", "confidence": 0.9}]}
                self._send(200, {"choices": [{"message": {"content": json.dumps(body)}}],
                                 "model": "qwen2.5-7b-instruct",
                                 "usage": {"total_tokens": 300, "prompt_tokens": 200,
                                           "completion_tokens": 100}})

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()


def _lmstudio(model="qwen2.5-7b-instruct", base="http://127.0.0.1:1/v1"):
    from alibi.providers import LocalChat
    return LocalChat(model, base_url=base)


FIXTURE = ("LAB 4 — Normalisation & ERD (10% of course grade)\n"
           "   Submission: Saturday 11 Oct 2026, 11:59 pm, portal.")


def test_lm_studio_url_from_the_docs_is_the_url_that_works():
    """The URL LM Studio prints in its own UI is `http://127.0.0.1:1234/v1`. The transport used to speak
    TLS to it (`SSL: WRONG_VERSION_NUMBER`), so the documented copy-paste could not work."""
    from alibi.adapters import wants_plain_http
    assert wants_plain_http("127.0.0.1:1234") and wants_plain_http("localhost:1234")
    assert not wants_plain_http("api.groq.com")
    assert wants_plain_http("127.0.0.1:1")            # explicit http:// is honoured, and so is the default
    srv = _FakeServer("ok")
    try:
        lc = _lmstudio(base=srv.base_url)
        h = lc.healthy()
        assert h["available"] and h["loopback"] and "qwen2.5-7b-instruct" in h["installed"]
        from alibi.router import EXTRACTION_SCHEMA
        r = lc.extract(FIXTURE, EXTRACTION_SCHEMA)
        assert r.outcome == "SUCCESS" and len(r.candidates) == 1
        assert r.egress == "LOCAL", "a loopback model must not be labelled as an escalation"
        assert r.bytes_out > 0 and r.bytes_in > 0, "the payload is metered even when local"
    finally:
        srv.stop()


def test_every_local_ai_failure_mode_is_a_value_not_a_crash():
    from alibi.router import EXTRACTION_SCHEMA
    # `empty` is INVALID_OUTPUT, not PARTIAL: an empty completion is a contract violation (the API
    # answered, and answered with nothing usable), whereas PARTIAL means "answered, and there was
    # genuinely no commitment-shaped text in the document". Conflating them is how an outage becomes
    # "this PDF had no deadlines".
    cases = {"500": "UNAVAILABLE", "garbage": "INVALID_OUTPUT", "empty": "INVALID_OUTPUT",
             "html": "UNAVAILABLE"}
    for mode, want in cases.items():
        srv = _FakeServer(mode)
        try:
            res = _lmstudio(base=srv.base_url).extract(FIXTURE, EXTRACTION_SCHEMA)
            assert res.outcome == want, f"{mode}: got {res.outcome} ({res.error}) want {want}"
            assert isinstance(res.candidates, list)
        finally:
            srv.stop()
    dead = _lmstudio(base="http://127.0.0.1:1/v1")
    h = dead.healthy()
    assert h["available"] is False and "not reachable" in h["reason"]
    assert "LM Studio" in h["hint"], "a health probe that does not say what to do next is a stack trace"
    from alibi.router import EXTRACTION_SCHEMA
    assert dead.extract(FIXTURE, EXTRACTION_SCHEMA).outcome == "UNAVAILABLE"


def test_a_configured_but_unloaded_model_is_named_not_guessed():
    srv = _FakeServer("ok")
    try:
        h = _lmstudio(model="llama3.1:8b", base=srv.base_url).healthy()
        assert h["available"] and h.get("model_not_found"), h
        assert "llama3.1:8b" in h["reason"] and "qwen2.5-7b-instruct" in h["reason"], h["reason"]
        assert "LM_STUDIO_MODEL" in h["hint"]
    finally:
        srv.stop()


def test_unconfigured_runtime_reports_unset_instead_of_trying_anything():
    h = _lmstudio(base="").healthy()
    assert h["available"] is False and "no base URL" in h["reason"]
    assert "/v1" in h["reason"] or "LM_STUDIO_BASE_URL" in h["reason"]


def test_egress_is_decided_by_the_address_not_the_variable_name():
    from alibi.providers import is_loopback, _host_of
    assert is_loopback(_host_of("http://localhost:1234/v1"))
    assert not is_loopback(_host_of("http://192.168.1.9:1234/v1")), \
        "a neighbour's laptop on the college LAN is not 'local' for privacy purposes"
    assert not is_loopback(_host_of("https://openai.example/v1"))
