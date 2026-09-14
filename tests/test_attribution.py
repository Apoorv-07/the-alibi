"""Tests for `alibi.taskfacts` — deciding *which obligation* a line is about.

Everything else in this repo verifies that a claim's quote is real. These tests verify the thing the
verifier cannot see: whether the claim was pinned to the right task. That distinction is not academic —
the demo corpus ran for days with `os-submitted_work_3_cpu` (syllabus) and `dbms-dbms_says_12_oct` (the
chat contradicting it) as two verified, undisputed rows, and the conflict count was reported as zero
because two true statements about different keys never disagree.

Each test below is the regression test for a specific mis-attribution that was actually measured.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alibi.taskfacts import (DUE_LINE, EXPLICIT_DATE, clause_dates, facts,  # noqa: E402
                             message_body, task_key)

YEAR = 2026


def due(text, course_hint="", **kw):
    fs, _, _n = facts(text, anchor_year=YEAR, course_hint=course_hint, **kw)
    return {(f["subject_id"], f["predicate"]): f for f in fs}


# ------------------------------------------------------- the title gate ----
def test_a_prose_mention_of_lab_is_not_a_task():
    """`75% theory AND lab counted separately` is a rule about attendance. It became `os-lab`, a task with
    a genuine quote and no meaning — the single worst kind of ledger row."""
    assert task_key("75% theory AND lab counted separately. Below 75%: no exam hall.", "os") is None
    assert due("75% theory AND lab counted separately. Below 75%: no exam hall.", "OS") == {}


def test_numbered_kind_needs_its_ordinal_but_project_does_not():
    assert task_key("LAB 4 — Normalisation & ERD", "dbms") == "dbms-lab_4"
    assert task_key("Internal Assessment 2 ...... 15%   (Thursday, 15 Oct 2026)", "os") == "os-ia_2"
    assert task_key("IA2 15% (Fri 16 Oct 2026)", "dbms") == "dbms-ia_2"
    # A leftmost numbered title wins over a bare kind: "Lab 2" here is a venue, and the *fact* is refused
    # by the due-word gate below — so the key choice is harmless. What would not be harmless is asserting
    # a deadline for a line that only names a room.
    assert task_key("Term project demo: 30 Oct 2026, 09:00, Lab 2.", "dbms") == "dbms-lab_2"
    assert due("Term project demo: 30 Oct 2026, 09:00, Lab 2. Bring the schema.", "DBMS") == {}
    assert task_key("Assignment 3 due", "os") == "os-assignment_3"
    assert task_key("nothing about work here", "os") is None


def test_the_kind_alternation_must_match_multiword_names():
    """A broken character class inside the alternation (an escaped `[\\s\\-]+`) silently stopped matching
    "internal assessment" — the whole DAA syllabus went unclaimed for a session. This pins the shape."""
    assert task_key("internal assessment 1 was on 28 Aug 2026") == "misc-ia_1"
    assert task_key("end-semester 50%", "os") == "os-end_sem"


def test_chat_and_syllabus_reach_the_same_key():
    """The whole bug, in one assertion. Different documents, one obligation, one key."""
    assert task_key("LAB 4 — Normalisation & ERD (10% of course grade)", "dbms") == \
        task_key("guys dbms lab 4 extended to Monday", "") == "dbms-lab_4"
    assert task_key("IA2 15% (Fri 16 Oct 2026)", "dbms") == \
        task_key("DBMS IA2 moved to 17 Oct apparently", "") == "dbms-ia_2"


# --------------------------------------------------- the date proximity ----
def test_a_date_belongs_to_the_clause_that_carries_it_not_to_the_first_task():
    """`Grading: IA1 15% · IA2 15% (Fri 16 Oct 2026) · Lab record 20%` — IA1 must not inherit IA2's exam
    date. This exact mis-attribution was produced twice in this repo, by two different "obvious" rules."""
    line = "Grading: IA1 15% · IA2 15% (Fri 16 Oct 2026) · Lab record 20% · Term project 10% · End-sem 40%"
    got = due(line, "DBMS")
    assert ("dbms-ia_2", "due_at") in got
    assert got[("dbms-ia_2", "due_at")]["value"]["date"] == "2026-10-16"
    assert ("dbms-ia_1", "due_at") not in got
    # weights ride with their own label, so both rows are still recorded
    assert got[("dbms-ia_1", "weight")]["value"]["weight"] == 0.15


def test_a_heading_owns_the_lines_under_it_in_the_same_block_only():
    block = ("LAB 4 — Normalisation & ERD (10% of course grade)\n"
             "   Submission: Saturday 11 Oct 2026, 11:59 pm, portal.\n"
             "   Hard copy to the lab instructor in the next session.")
    got = due(block, "DBMS")
    assert got[("dbms-lab_4", "due_at")]["value"]["date"] == "2026-10-11"
    assert got[("dbms-lab_4", "due_at")]["scoped"] is True
    # the quote is the *widened* span (heading + submission line) and it is a real substring: a scoped
    # fact quotes the structure it came from instead of pretending to be a single sentence
    f = got[("dbms-lab_4", "due_at")]
    assert "Submission: Saturday 11 Oct 2026" in f["quote"]
    assert f["quote"] in block


def test_untitled_lines_never_inherit_scope_in_chat():
    """Two messages, one export. `allow_context=False` is the difference between a timeline and a rumour
    machine."""
    chat = ("18/09/2026, 21:45 - Divya S: OS assignment 3 due date is 13 Oct 2026 on the LMS\n"
            "18/09/2026, 21:46 - Someone: submission also for the lab record 20%")
    got = due(chat, "", allow_context=False)
    assert set(got) == {("os-assignment_3", "due_at")}


def test_two_dates_in_one_line_are_escalated_not_averaged():
    line = "DBMS Lab 4 is due 11 Oct 2026 and the hard copy 13 Oct"
    got, notes, _ = facts(line, anchor_year=YEAR, course_hint="DBMS")
    assert got == []
    assert any("2 candidate dates" in n for n in notes)


# ------------------------------------------------------- due-word gate ----
def test_a_venue_is_not_a_deadline():
    assert not DUE_LINE.search("Bring the seed data to Lab 2.")
    assert due("Term project demo: 30 Oct 2026, 09:00, Lab 2. Bring the schema.", "DBMS") == {}


def test_the_exam_sitting_shape_counts_as_a_deadline():
    """`(conducted 28 Aug 2026)` is when you must be in a hall. Refusing it would drop a real deadline; the
    rule is a shape (percentage + date in the same clause), not a keyword list grown until it matches
    everything."""
    assert due("Internal Assessment 1 ...... 15%   (conducted 28 Aug 2026)", "OS")[
        ("os-ia_1", "due_at")]["value"]["date"] == "2026-08-28"
    assert due("Internal Assessment 2 ...... 15%   (Thursday, 15 Oct 2026, 09:00, Hall C)", "OS")[
        ("os-ia_2", "due_at")]["value"]["date"] == "2026-10-15"


# ------------------------------------------------- fabricated-date traps ----
@pytest.mark.parametrize("s", [
    "Submit on the portal. Printout of code with your reg no. 10% penalty/day after.",
    "RESHUFFLED to Saturday 26.09.2026, 09:00–12:00",
    "Late submissions: not accepted.",
    "DBMS IA2 moved to 17 Oct apparently",       # one real date only
])
def test_no_ghost_dates_from_times_and_prose(s):
    """`20, no` reads as 2 November; `12:00` reads as 12.00 → 2026-12-00; `Oct 20` inside `12 Oct 2026`
    reads as a second date. Every one of these was produced by a plausible regex in this repo and each one
    *destroyed a real deadline*, because the extractor refuses ambiguous lines. A missing date is a review
    item; a fabricated one is a plan built on it."""
    if "17 Oct" in s:
        assert clause_dates(s, YEAR) == ["2026-10-17"]
    else:
        assert clause_dates(s, YEAR) in ([], ["2026-09-26"]), clause_dates(s, YEAR)


def test_message_timestamp_is_metadata_not_evidence():
    line = "18/09/2026, 21:41 - Aravind Kumar: guys dbms lab 4 extended to Monday"
    body, off = message_body(line)
    assert off == len(line) - len(body)
    assert clause_dates(body, YEAR) == []           # the stamp is stripped before matching
    assert due(line, "") == {}                      # "extended to Monday" is not a fact we assert


# -------------------------------------------------- the real corpus, e2e ----
DEMO_CONFLICTS = {("os-assignment_3", "2026-10-12", "2026-10-13"),
                  ("dbms-ia_2", "2026-10-16", "2026-10-17"),
                  ("cs-registration", "2026-09-30", "2026-09-24")}


def _sync(tmp_path):
    from alibi.twin import Twin
    import corpus.demo_corpus as C
    t = Twin.open(tmp_path / "twin.db")
    for text, kind, label in ((C.SYLLABUS_DBMS, "syllabus_text", "DBMS syllabus p.1"),
                              (C.SYLLABUS_OS, "syllabus_text", "OS syllabus p.1"),
                              (C.WHATSAPP_CHAT, "whatsapp", "class group export"),
                              (C.NOTICE_BOARD_PHOTO, "notice_photo", "notice board photo")):
        t.ingest_text(text, kind=kind, label=label)
    return t


@pytest.fixture()
def synced(tmp_path):
    return _sync(tmp_path)


def test_the_syllabus_and_the_chat_land_on_the_same_tasks(synced):
    L = synced.db.derive_ledger()
    subs = {c.subject_id for c in L.open_claims("task")}
    assert {"dbms-lab_4", "dbms-ia_2", "os-assignment_3", "cs-registration"} <= subs
    junk = [s for s in subs if s.startswith(("os-submitted_work", "dbms-dbms_says", "os-os_", "misc-"))]
    assert not junk, f"slug-shaped subjects came back: {junk}"


def test_the_planted_contradictions_are_found_with_their_dates(synced):
    """Not a count-equals assertion, on purpose: the demo also carries rumours the rules path may or may
    not promote depending on the provider, so pinning the total would let the test pass by *deleting a true
    claim*. What is demanded is that each planted pair is open with both values on file."""
    synced.db.sync_conflicts()
    open_pairs = set()
    for c in synced.db.conflicts():
        ex = synced.db.explain_conflict(c)
        ds = tuple(sorted(str(r["value"].get("date", "")) for r in ex.get("rows") or []))
        open_pairs.add((c["subject_id"], ds))
    for want in DEMO_CONFLICTS:
        assert (want[0], tuple(sorted((want[1], want[2])))) in open_pairs, open_pairs


def test_an_open_conflict_is_also_an_open_question(synced):
    """/conflicts must never show a badge without something in /queue to answer it."""
    synced.db.sync_conflicts()
    for c in synced.db.conflicts():
        q = synced.db.q("SELECT * FROM review_item WHERE subject_id=? AND kind='CONFLICTING_STATEMENTS'",
                        (c["subject_id"],))
        assert q, f"{c['subject_id']} has a conflict and no review item"


def test_no_claim_is_promoted_without_its_own_quote_and_date(synced):
    """The floor this product is judged on: re-verify every trusted row against its source text."""
    from alibi.ground import verify_claim
    rows = synced.db.q("""SELECT c.*, m.full_text FROM claim c
                          JOIN source s ON s.id=c.source_id
                          JOIN source_meta m ON m.source_id = s.id
                          WHERE c.valid_to IS NULL""")
    assert rows
    for r in rows:
        v = verify_claim({"predicate": r["predicate"], "value": json.loads(r["value_json"]),
                          "evidence_span": r["evidence_span"]}, r["full_text"],
                         anchor_date=date(2026, 9, 22))
        assert v.ok, f"claim {r['id']} ({r['predicate']}) fails its own receipt: {v.reason}"
    assert synced.db.stats()["false_trust_count"] == 0


def test_weight_is_never_a_penalty(syllabus=None):
    """`10% of internal marks` is a weight; `−10% per calendar day` is a penalty. Reading the second as the
    first would make the solver think an extension is free."""
    block = ("LAB 4 — Normalisation & ERD (10% of course grade)\n"
             "   Late policy: −10% per calendar day, maximum 3 days, after which no submission is\n"
             "   accepted.")
    got = due(block, "DBMS")
    assert got[("dbms-lab_4", "weight")]["value"]["weight"] == 0.1
    weights = [v for (s, p), v in got.items() if p == "weight"]
    assert len(weights) == 1, f"a penalty became a second weight: {weights}"


def test_re_ingest_is_still_idempotent_after_attribution_changes(tmp_path):
    """Attribution keys are stable, so a second read of the same bytes adds nothing (the schema's dedupe
    key is subject+predicate+source+span, which would otherwise be perturbed by a changed key)."""
    t = _sync(tmp_path)
    before = t.db.stats()["claims"]
    import corpus.demo_corpus as C
    t.ingest_text(C.SYLLABUS_DBMS, kind="syllabus_text", label="DBMS syllabus p.1")
    assert t.db.stats()["claims"] == before


def test_no_evidence_of_submission_is_reported_and_not_asserted(tmp_path):
    """`os-ia_1` is due 2026-08-28 and today is later: the horizon has no legal days left for it.

    Two wrong answers were tried. (a) Drop past-due tasks from planning: the alarm disappears exactly when
    it is most needed. (b) Say "you missed it": an invented fact extracted from silence, the one error this
    project exists to prevent. The shipped behaviour is a third thing — report the gap, name it as a gap,
    and retire the obligation only when a submission claim is actually on file.
    """
    t = _sync(tmp_path)
    L = t.db.derive_ledger()
    before = t._feasibility(L, "2026-09-12")
    assert before["status"] == "INFEASIBLE", before
    assert "no_miss" in before["core"]
    assert before["slack_hours"] is not None and before["slack_hours"] < 0
    head = before["head_line"]
    assert "absence of evidence, not proof" in head, head
    assert "missed" not in head.split("not proof that it was")[0]     # no verdict before the sentence

    assert "os-ia_1" in {x.id for x in t._tasks_from_ledger(L)}, "a past-due task must stay in the horizon"

    receipt = ("Manual note, 2026-09-12: I submitted OS IA1 on 2026-08-27 before the "
               "deadline and the lab-incharge gave me a signed receipt.")
    sid, created = t.db.register_source(kind="user_note", label="manual note", content=receipt)
    assert created
    q_start = receipt.index("I submitted OS IA1")
    cid, made = t.db.write_claim(subject_type="task", subject_id="os-ia_1", predicate="submitted",
                                 value={"text": "submitted 2026-08-27, receipt held"}, source_id=sid,
                                 quote=receipt[q_start:q_start + 60], char_start=q_start,
                                 char_end=q_start + 60, method="manual", verified=True)
    assert made and cid > 0

    L2 = t.db.derive_ledger()
    assert "os-ia_1" not in {x.id for x in t._tasks_from_ledger(L2)}, \
        "a recorded submission retires the obligation for planning"
    after = t._feasibility(L2, "2026-09-12")
    assert "absence of evidence, not proof" not in after.get("head_line", ""), after.get("head_line")
    st = t.db.stats()
    assert st["false_trust_count"] == 0, "a manually entered receipt must still pass its own quote check"
