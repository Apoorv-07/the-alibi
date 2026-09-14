"""Tests for `alibi.db` — the layer where every fact, receipt and decision is a row.

These exist because the schema is the product: an ingest that writes a claim without a receipt, a
re-sync that duplicates the ledger, or a provenance row that lies about whether the model was
involved, each look identical to success in the UI and are only visible here.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alibi.db import init_db, DB, KIND_MAP                                        # noqa: E402
from alibi.ledger import Ledger, Source                                        # noqa: E402


def _ids(r):
    """`register_source` answers (id, created); tests that do not care about the flag unpack here."""
    return r[0] if isinstance(r, tuple) else r


from alibi.config import load_institution_policy                          # noqa: E402

# The real production policy, extended — not a hand-written dict. A test policy that omits a key
# the reconciler reads (it is called `rationale`) proves nothing about the shipped behaviour and
# once did exactly that: it passed while `safe_value` would have raised in the app.
def policy(**over):
    p = load_institution_policy(None)
    p.update({"authority_challenge": "higher_trust_only",
              "source_trust": {"notice board photo": 4, "course portal": 3, "erp screen": 3,
                               "class group": 1}}, )
    p.update(over)
    return p


@pytest.fixture()
def db(tmp_path):
    d = init_db(tmp_path / "t.db", policy=policy())
    yield d
    d.close()


@pytest.fixture()
def text():
    return ("LAB 4 — Normalisation & ERD (10% of course grade)\n"
            "   Submission: Saturday 11 Oct 2026, 11:59 pm, portal.\n"
            "   Late policy: −10% per calendar day, maximum 3 days, after which no submission "
            "is accepted.\n")


# --------------------------------------------------------------- migrations --

def test_migrations_are_recorded_and_idempotent(tmp_path):
    """The applied list is derived from the files on disk, not hardcoded: a test that names the expected
    migrations by hand turns every new migration into an unrelated failure, which is how migration
    lists end up being "fixed" by deleting the assertion."""
    root = Path(__file__).resolve().parents[1] / "database"
    expected = ["schema.sql"] + sorted(f.name for f in (root / "migrations").glob("*.sql"))
    p = tmp_path / "m.db"
    d = init_db(p)
    first = sorted(d.applied())
    assert first == sorted(expected), f"applied={first}\nfiles ={sorted(expected)}"
    order = [r["name"] for r in d.q("SELECT name FROM applied_migrations ORDER BY rowid")]
    assert order == expected, f"migrations must apply in file order: {order}"
    sha = d.one("SELECT sha FROM applied_migrations WHERE name='schema.sql'")["sha"]
    assert len(sha) == 16, "a migration hash is how you notice someone edited history"
    d.close()
    d2 = init_db(p)                       # second open must not re-apply or crash
    assert sorted(d2.applied()) == first
    with pytest.raises(sqlite3.IntegrityError):
        # 0001's CHECK list is the contract: an unmapped kind must not be insertable at all,
        # otherwise a bad adapter silently poisons the trust policy that reads this column.
        d2.x("INSERT INTO source(kind,uri,sha256,captured_at,effective_at,trust_prior,"
             "parse_profile,status,note) VALUES ('carrier_pigeon','u','h','c','e',1,'','queued','')", ())
    d2.close()


def test_wal_and_pragmas_are_on(db):
    """Foreign keys OFF is the silent killer: every FK in 0002 becomes decoration and garbage rows
    write happily until a JOIN drops them out of the UI."""
    assert db.one("PRAGMA foreign_keys")["foreign_keys"] == 1
    assert str(db.q("PRAGMA journal_mode")[0]["journal_mode"]).lower() in ("wal", "memory")
    assert db.one("PRAGMA busy_timeout")["timeout"] >= 2000, \
        "a sync while the UI reads must wait, not crash with 'database is locked'"
    with pytest.raises(sqlite3.IntegrityError):
        db.x("INSERT INTO claim_evidence(claim_id,source_id,quote,verified,created_at) "
             "VALUES (9999,9999,'x',1,'now')", ())


# ------------------------------------------------------------------ sources --

def test_source_registration_is_content_addressed(db, text):
    a = _ids(db.register_source(kind="syllabus", label="Course portal", content=text, uri="https://x/1"))
    b = _ids(db.register_source(kind="syllabus", label="Course portal", content=text, uri="https://x/1"))
    assert a == b, "same bytes must not create a second source row"
    info = db.source_info(a)
    assert info["kind"] == "syllabus_pdf", "kind is the 0001 enum value"
    assert info["orig_kind"] == "syllabus" and info["label"] == "Course portal"


def test_adapter_kind_is_mapped_but_original_is_kept(db):
    sid = _ids(db.register_source(kind="whatsapp", label="class group", content="sir ct on 11/10"))
    info = db.source_info(sid)
    assert info["kind"] in set(KIND_MAP.values()), info
    meta = json.loads(info["source_meta"] or "{}")
    assert meta.get("orig_kind") == "whatsapp", meta
    assert "sir ct on 11/10" in (info.get("full_text") or ""), "the receipt must be re-readable"


def test_unknown_kind_is_rejected_not_silently_inserted(db):
    with pytest.raises(Exception):
        db.register_source(kind="carrier-pigeon", label="x", content="y")


# ------------------------------------------------------------------ claims --

def test_claim_round_trip_carries_a_verbatim_receipt(db, text):
    sid = _ids(db.register_source(kind="syllabus", label="Course portal", content=text))
    i = text.index("Saturday 11 Oct 2026")
    cid, created = db.write_claim(subject_type="task", subject_id="LAB-4", predicate="due_at",
                                 value={"date": "2026-10-11", "time": "23:59"}, source_id=sid,
                                 quote=text[i:i + 60], char_start=i, char_end=i + 60,
                                 method="rule", verified=True, checks=["date_in_text"])
    assert created
    row = db.claim(cid)                       # note: `claim()` parses value_json into `value`
    assert row["verify_state"] == "grounded" and row["evidence_span"].startswith("Saturday 11 Oct")
    assert row["value"]["date"] == "2026-10-11"
    assert row["evidence"][0]["checks_json"] == '["date_in_text"]'
    assert row["provenance"]["validation_status"] == "verified"


def test_re_ingest_does_not_duplicate_the_ledger(db, text):
    sid = _ids(db.register_source(kind="syllabus", label="Course portal", content=text))
    args = dict(subject_type="task", subject_id="LAB-4", predicate="due_at",
                value={"date": "2026-10-11"}, source_id=sid, quote=text[10:80],
                char_start=10, char_end=80, method="rule")
    c1, made1 = db.write_claim(**args)
    c2, made2 = db.write_claim(**args)
    assert made1 and not made2 and c1 == c2
    assert len(db.open_claims("task", "LAB-4")) == 1


def test_unverified_claim_never_enters_live_state(db, text):
    sid = _ids(db.register_source(kind="syllabus", label="Course portal", content=text))
    cid, _ = db.write_claim(subject_type="task", subject_id="GHOST", predicate="due_at",
                            value={"date": "2026-10-11"}, source_id=sid, quote=" " * 12,
                            char_start=-1, char_end=-1, verified=False, method="llm",
                            reject_reason="span not found in source", llm_involved=True)
    assert db.claim(cid)["verify_state"] == "rejected"
    # The 0001 `live_claims` view is the only definition of "currently believed", and it selects on
    # verify_state, not on valid_to — so an unverified claim must be absent from BOTH.
    assert db.one("SELECT COUNT(*) AS n FROM live_claims WHERE subject_id='GHOST'")["n"] == 0
    assert not db.derive_ledger().claims
    # A quote shorter than the schema's floor is refused loudly, not swallowed: silent rejection is
    # how a demo ends up with an empty ledger and a green header.
    with pytest.raises(Exception, match=r"length\(evidence_span\)"):
        db.write_claim(subject_type="task", subject_id="SHORT", predicate="due_at",
                       value={"date": "2026-10-11"}, source_id=sid, quote="too short",
                       char_start=-1, char_end=-1, method="llm", llm_involved=True)


def test_supersession_keeps_history_and_writes_lineage(db, text):
    old_id = _ids(db.register_source(kind="whatsapp", label="class group", content="ct on 18th"))
    new_id = _ids(db.register_source(kind="notice", label="notice board", content="ct moved to 12 Oct"))
    a, _ = db.write_claim(subject_type="task", subject_id="CT1", predicate="due_at",
                         value={"date": "2026-10-18"}, source_id=old_id, quote="ct on 18th!!",
                         char_start=0, char_end=10, method="rule")
    b, _ = db.write_claim(subject_type="task", subject_id="CT1", predicate="due_at",
                         value={"date": "2026-10-12"}, source_id=new_id, quote="ct moved to 12 Oct",
                         char_start=0, char_end=17, method="rule")
    db.retire_claim(a, b, why="higher authority source")
    open_ids = [c["id"] for c in db.open_claims("task", "CT1")]
    assert open_ids == [b], "retired claim must leave the live set but keep its row"
    lin = db.lineage("CT1")
    # `lineage()` normalises the edge vocabulary for the UI (`kind`, not the raw column `edge`):
    # asserting on the shipped shape is what catches a rename that blanks the graph panel.
    assert any(e["kind"] == "supersedes" for e in lin["edges"]), lin
    assert lin["claim_ids"] == [a, b] and len(lin["nodes"]) >= 3, lin["nodes"]
    assert db.claim(a)["valid_to"], "history must be closed, not deleted"


# ------------------------------------------------------------- provenance --

def test_provenance_row_names_the_model_and_the_checks(db, text):
    sid = _ids(db.register_source(kind="whatsapp", label="class group", content="ct is 12/10 says the rep"))
    iid = db.record_invocation(provider="gemma_local", model="gemma4:e4b", purpose="extract",
                              egress="LOCAL", outcome="SUCCESS", claims_returned=1,
                              claims_grounded=1, latency_ms=812, model_version="gemma4:e4b-q4")
    cid, _ = db.write_claim(subject_type="task", subject_id="CT1", predicate="due_at",
                            value={"date": "2026-10-12"}, source_id=sid,
                            quote="ct is 12/10 says the rep", char_start=0, char_end=23,
                            method="llm+verified", llm_involved=True,
                            provider="gemma_local", model="gemma4:e4b", observation_id=iid)
    p = db.one("SELECT * FROM claim_provenance WHERE claim_id=?", (cid,))
    assert p and p["llm_involved"] == 1 and p["model"] == "gemma4:e4b"
    assert p["checks_passed"], "the verifier's checks are part of the provenance"
    assert p["validation_status"] == "verified"
    # the row that answers "did an LLM write this?" must not need a JOIN to read
    assert p["source_label"] == "class group"


def test_false_trust_counter_starts_at_zero_and_moves_only_with_evidence(db):
    """FALSE TRUST RATE is the one number the whole architecture is judged on (PART 6/45), so the
    test asserts both directions: an honest LLM+verified row must not be flagged, and a trusted row
    whose receipt does not contain the date must be."""
    assert db.false_trust_count() == 0
    honest = "guys the ct is due 2026-10-11 confirmed by sir"
    hs = _ids(db.register_source(kind="whatsapp", label="class group", content=honest))
    db.write_claim(subject_type="task", subject_id="T", predicate="due_at",
                   value={"date": "2026-10-11"}, source_id=hs, quote=honest, char_start=0,
                   char_end=len(honest), method="llm+verified", verified=True, llm_involved=True,
                   checks=["quote_verbatim", "date_in_text"])
    assert db.false_trust_count() == 0, "a grounded LLM+verified claim is not false trust"

    liar = "she said maybe friday!!"
    ls = _ids(db.register_source(kind="whatsapp", label="class group", content=liar))
    db.write_claim(subject_type="task", subject_id="L", predicate="due_at",
                   value={"date": "2026-10-11"}, source_id=ls, quote=liar, char_start=0,
                   char_end=len(liar), method="llm", verified=True, llm_involved=True,
                   checks=["quote_verbatim"])
    assert db.false_trust_count() == 1, "the counter must move when a trusted receipt is unverifiable"
    st = db.stats()
    assert st["false_trust_count"] == 1 and st["false_trust_rate"] == 1 / st["claims_verified"], st


def test_purged_source_text_is_reported_as_unverifiable_not_verified(db):
    """The retention job deletes raw text. A trusted row whose receipt can no longer be re-read must
    show up as `unverifiable`, not silently vanish from the metrics — otherwise a green header is
    just an expired database."""
    txt = "the viva is on 2026-10-14 in lab 2"
    sid = _ids(db.register_source(kind="whatsapp", label="class group", content=txt))
    db.write_claim(subject_type="task", subject_id="V", predicate="due_at",
                   value={"date": "2026-10-14"}, source_id=sid, quote=txt, char_start=0,
                   char_end=len(txt), method="rule")
    assert db.stats()["unverifiable"] == 0
    db.x("UPDATE source_meta SET full_text='' WHERE source_id=?", (sid,))
    st = db.stats()
    assert st["unverifiable"] == 1, st
    assert st["false_trust_count"] == 0, "we cannot prove it was a lie; we must not claim it is fine"


def test_stats_and_run_counters_aggregate_from_rows_not_from_counters(db):
    rid = db.open_run("cli", note="test run")
    sid = _ids(db.register_source(kind="syllabus", label="s", content="c" * 30))
    db.record_invocation(provider="gemma_local", model="m", purpose="extract", egress="LOCAL",
                        outcome="SUCCESS", run_id=str(rid), claims_returned=2, claims_grounded=1)
    db.record_invocation(provider="gemini", model="g", purpose="draft", egress="REDACTED_CLOUD",
                        outcome="SUCCESS", run_id=str(rid))
    db.write_claim(subject_type="task", subject_id="A", predicate="due_at",
                   value={"date": "2026-10-11"}, source_id=sid, quote="c" * 15, char_start=0,
                   char_end=15, method="rule", run_id=str(rid))
    db.sync_run_counters(rid)
    st = db.run_stats(rid)
    assert st["llm_calls"] >= 1 and st["local_calls"] >= 1 and st["cloud_calls"] >= 1, st
    assert st["claims_in"] >= 1, st
    s = db.stats()
    for k in ("claims", "claims_verified", "conflicts", "false_trust_count", "cloud_calls",
              "llm_involved_claims"):
        assert k in s, s
    db.close_run(rid, "done")
    assert db.runs()[0]["state"] == "done"


# ------------------------------------------------------------- conflicts ---

def test_conflict_is_persisted_and_explainable(db):
    ws_text = "ct on 18th oct guys, confirmed"
    ws = _ids(db.register_source(kind="whatsapp", label="class group", content=ws_text))
    no_text = "CT1 due 12 Oct 2026, hard copy only"
    no = _ids(db.register_source(kind="notice_photo", label="notice board", content=no_text))
    db.write_claim(subject_type="task", subject_id="CT1", predicate="due_at",
                   value={"date": "2026-10-18"}, source_id=ws, quote=ws_text, char_start=0,
                   char_end=len(ws_text), method="rule")
    db.write_claim(subject_type="task", subject_id="CT1", predicate="due_at",
                   value={"date": "2026-10-12"}, source_id=no, quote=no_text, char_start=0,
                   char_end=len(no_text), method="rule")
    found = db.sync_conflicts()
    rows = db.conflicts()
    assert rows, found
    ex = db.explain_conflict(rows[0])
    # The explanation contract the UI renders, asserted by name so a rename cannot quietly turn a
    # panel into blanks: what disagrees, every side with its quote, who is stronger and why, what
    # the system plans against, and what the user should do.
    for k in ("subject_id", "predicate", "what", "values", "rows", "why", "plan_against", "rule",
              "uncertain", "do", "silent_deletion", "spread_days"):
        assert k in ex, ex
    assert len(ex["values"]) == 2 and len(ex["rows"]) == 2
    assert all(r["quote"] for r in ex["rows"]), "every side of a conflict must carry its receipt"
    assert ex["silent_deletion"] is False
    assert ex["spread_days"] == 6, ex["spread_days"]
    assert "authority" in ex["why"] and "never by the model" in ex["why"]
    assert rows[0]["severity"] in ("LOW", "MED", "HIGH"), "0001's CHECK list must be respected"


# ------------------------------------------------------ review / action ----

def test_review_items_dedupe_and_resolve_with_a_change_row(db):
    r1, made1 = db.open_review(kind="AMBIGUOUS_DATE", question="Which CT date is right?",
                              why="two sources differ", subject_id="CT1")
    r2, made2 = db.open_review(kind="AMBIGUOUS_DATE", question="Which CT date is right?",
                              why="again", subject_id="CT1")
    assert made1 and not made2 and r1 == r2, "re-syncing must not spawn a pile of identical questions"
    assert len(db.reviews("open")) == 1
    db.resolve_review(r1, decision="CHOOSE", chosen={"date": "2026-10-12"},
                     decided_by="user", consequence="CT1 due date set to 12 Oct")
    assert not db.reviews("open")
    kinds = [c["kind"] for c in db.changes(limit=20)]
    assert "REVIEW_RESOLVED" in kinds, kinds


def test_actions_are_gated_and_idempotent_by_key(db):
    a = db.open_action(kind="email_faculty", risk_class="C", target="prof@x",
                      payload={"subject": "extension"}, justification="ledger + solver",
                      supporting_claims=[], solver_status="INFEASIBLE",
                      policy_decision="APPROVAL_REQUIRED", policy_reason="irreversible send",
                      idempotency_key="k1")
    b = db.open_action(kind="email_faculty", risk_class="C", target="prof@x",
                      payload={"subject": "extension"}, justification="ledger + solver",
                      supporting_claims=[], solver_status="INFEASIBLE",
                      policy_decision="APPROVAL_REQUIRED", policy_reason="irreversible send",
                      idempotency_key="k1")
    assert a[0] == b[0] and b[1] is False, "a replayed key must not open a second action"
    ap = db.one("SELECT * FROM approval WHERE action_id=?", (a[0],))
    assert ap and ap["decision"] == "pending", "class C cannot proceed without a recorded decision"
    db.decide_approval(a[0], "approved", by="user", note="looks right")
    assert db.one("SELECT decision FROM approval WHERE action_id=?", (a[0],))["decision"] == "approved"
    db.finish_action(a[0], "succeeded", "draft queued")
    assert db.actions()[0]["status"] == "succeeded"


def test_forecast_rows_are_flagged_as_predictions(db):
    db.record_forecast(run_id="3", horizon_start="2026-09-12", horizon_end="2026-09-26",
                       subject_id="w38", probability=0.72, method="buffer_decay", status="AT_RISK",
                       basis={"ratio": 1.4}, recommendation="start today")
    f = db.forecasts()[0]
    assert f["is_prediction"] is True and f["status"] == "AT_RISK"
    assert f["basis"]["ratio"] == 1.4
    assert "FEASIBILITY_STATUS_CHANGED" in [c["kind"] for c in db.changes(limit=5)]


# ------------------------------------------------------------ derivation ----

def test_derive_ledger_matches_the_row_set(db, text):
    sid = _ids(db.register_source(kind="syllabus", label="Course portal", content=text))
    for pred, val in (("due_at", {"date": "2026-10-11"}), ("weight", {"weight": 0.10})):
        db.write_claim(subject_type="task", subject_id="LAB-4", predicate=pred, value=val,
                       source_id=sid, quote=text[5:70], char_start=5, char_end=70, method="rule")
    L = db.derive_ledger()
    assert isinstance(L, Ledger)
    live = {(c["subject_id"], c["predicate"]) for c in db.q("SELECT * FROM live_claims")}
    assert ("LAB-4", "due_at") in live and ("LAB-4", "weight") in live
    assert not L.conflicts(db.policy), "one source, two facts: not a conflict"
    assert len(L.sources) == 1 and isinstance(next(iter(L.sources.values())), Source)
    assert L.safe_value("task", "LAB-4", "due_at", db.policy)["date"] == "2026-10-11"
    # the derived ledger and the DB must agree on the *same* number of open facts,
    # or the API and the CLI are reconciling two different worlds
    assert len(L.open_claims("task", "LAB-4")) == 2


def test_audit_log_records_who_changed_what(db):
    db.audit("user", "approve_action", "action:7", before={"status": "pending"},
             after={"status": "approved"})
    row = db.audit_log()[0]
    assert row["actor"] == "user" and row["action"] == "approve_action"
    assert "approved" in json.dumps(row)


def test_the_schema_cannot_refuse_what_the_router_allows(tmp_path):
    """`router.ALLOWED_PREDICATES` and the CHECK list on `claim.predicate` are one contract written twice,
    in two languages, with nothing checking them against each other until now. They had drifted:
    `submitted` / `submitted_at` (the only evidence that retires a past-due obligation) were allowed by
    the router and refused by the database, so a submission receipt could not be stored at all.

    A *fresh* database from schema.sql alone is expected to refuse them: 0003 is what widens the list,
    and that asymmetry is asserted rather than hidden, because "the migration is optional" is the kind of
    claim a reviewer must be able to falsify in one command."""
    from alibi.router import ALLOWED_PREDICATES
    from alibi.twin import Twin

    mig = Twin.open(tmp_path / "m.db")                      # init_db runs every migration
    ddl = mig.db.q("SELECT sql FROM sqlite_master WHERE name='claim'")[0]["sql"]
    stored = set(re.findall(r"'([a-z_]+)'", ddl.split("predicate TEXT NOT NULL CHECK")[1].split("value_json")[0]))
    assert not (ALLOWED_PREDICATES - stored), f"the ledger rejects its own allowed predicates: {sorted(ALLOWED_PREDICATES - stored)}"
    methods = set(re.findall(r"'([a-z_+]+)'", ddl.split("method TEXT NOT NULL DEFAULT")[1].split("recorded_at")[0]))
    assert "manual" in methods, "a human answer must be recordable as a human answer: " + str(sorted(methods))

    # the fact-identity index, not the 0001 span key
    idx = mig.db.q("SELECT sql FROM sqlite_master WHERE name='ux_claim_fact'")
    assert idx and "value_json" in idx[0]["sql"] and "valid_to IS NULL" in idx[0]["sql"], idx
    # same value, same source, *different* span => one open row
    text = "LAB 9 due 01.11.2026 at five.   \nLAB 9 due 01.11.2026 at five. (repeat)"
    sid, _ = mig.db.register_source(kind="manual", label="dup probe", content=text)
    a, made_a = mig.db.write_claim(subject_type="task", subject_id="dbms-lab_9", predicate="due_at",
                                   value={"date": "2026-10-11"}, source_id=sid, quote=text[:22],
                                   char_start=0, char_end=22, method="rule", verified=True)
    b, made_b = mig.db.write_claim(subject_type="task", subject_id="dbms-lab_9", predicate="due_at",
                                   value={"date": "2026-10-11"}, source_id=sid, quote=text[33:55],
                                   char_start=33, char_end=55, method="rule", verified=True)
    assert made_a and not made_b and a == b, f"span-keyed duplicate survived: {a} vs {b}"
    # ...while the index stays out of *supersession*'s way: a retired row may coexist with a new open
    # row for the same fact (that is how history remains readable), and the dedupe branch must then
    # resolve the write to whichever row is currently open rather than erroring.
    mig.db.x("UPDATE claim SET valid_to=? WHERE id=?", ("2026-09-12T00:00:00+05:30", a))
    same_id, made_again = mig.db.write_claim(subject_type="task", subject_id="dbms-lab_9",
                                             predicate="due_at", value={"date": "2026-10-11"},
                                             source_id=sid, quote=text[33:55], char_start=33,
                                             char_end=55, method="rule", verified=True)
    # A re-asserted fact whose only match is retired *should* become a new open row: retirement means
    # "we stopped believing this", and the same source saying it again is a fresh assertion, not a
    # duplicate. The dedupe branch only short-circuits against OPEN rows, which is what makes the two
    # behaviours agree instead of fighting.
    assert made_again and same_id != a, (made_again, same_id, a)
    assert mig.db.one("SELECT COUNT(*) n FROM claim WHERE subject_id='dbms-lab_9' AND predicate='due_at'"
                      " AND valid_to IS NULL")["n"] == 1, "exactly one open row for one fact"
    assert mig.db.one("SELECT valid_to FROM claim WHERE id=?", (a,))["valid_to"], "retirement preserved"
    # and a *different* value from the same source is a new open row, i.e. a conflict, not a duplicate
    d_id, made_d = mig.db.write_claim(subject_type="task", subject_id="dbms-lab_9", predicate="due_at",
                                      value={"date": "2026-10-12"}, source_id=sid, quote=text[33:55],
                                      char_start=33, char_end=55, method="rule", verified=True)
    assert made_d and d_id != a, "disagreeing values must coexist so the conflict engine can see them"
    mig.db.close()

    # Behaviour, not text-matching: a schema.sql-only database must REFUSE the fact the migration
    # exists to allow. (Grepping the DDL for a word does not work — the comment written beside the
    # 0001 constraint mentions `submitted`, and a test that passes because of a comment tells you
    # nothing.)
    fresh = DB(str(tmp_path / "f.db"))
    fresh.conn.executescript((Path(__file__).resolve().parents[1] / "database" / "schema.sql").read_text())
    fresh.x("INSERT INTO source(id,kind,uri,sha256,captured_at,effective_at) "
             "VALUES (9,'user_note','u','h','2026-09-01','2026-09-01')")
    with pytest.raises(sqlite3.IntegrityError):
        fresh.x("INSERT INTO claim(subject_type,subject_id,predicate,value_json,source_id,evidence_span,"
                 "recorded_at,valid_from,verify_state,method) VALUES "
                 "('task','dbms-lab_9','submitted','{}',9,'a quote long enough','2026-09-01',"
                 "'2026-09-01','grounded','manual')", ())
    fresh.close()


def test_every_view_in_the_schema_is_selectable(tmp_path):
    """`claim_lineage_graph` was valid-looking SQL that referenced two columns which do not exist, and
    nothing selected from it, so it stayed broken across every test run. A view that is never queried is
    not a feature; it is documentation that happens to be invalid."""
    from alibi.twin import Twin
    import corpus.demo_corpus as C
    t = Twin.open(tmp_path / "v.db")
    t.ingest_text(C.SYLLABUS_DBMS, kind="syllabus_text", label="DBMS syllabus p.1")
    views = [r["name"] for r in t.db.q("SELECT name FROM sqlite_master WHERE type='view'")]
    assert "claim_lineage_graph" in views, sorted(views)
    for v in views:
        rows = t.db.q(f"SELECT * FROM {v} LIMIT 5")            # noqa: S608 - names come from sqlite_master
        assert isinstance(rows, list)
    g = t.db.q("SELECT claim_id, source_label, quote, verify_state FROM claim_lineage_graph LIMIT 5")
    assert g and all(r["source_label"] for r in g), "a lineage graph without its source label is not a receipt"
    assert all("verified" not in r for r in g)


def test_a_free_text_date_does_not_crash_conflict_bookkeeping(tmp_path):
    """`Ledger._severity` computed a day-spread over every open `due_at` row with `str(value.get("date"))`,
    so a due_at whose value is prose — a human answer we refused to coerce, an OCR fragment — produced
    `str(None)` and `date.fromisoformat('None'): ValueError`. The crash was in the code that runs *because*
    evidence disagrees, which is the worst place for one: a conflict becomes an exception page instead of a
    question."""
    from alibi.twin import Twin
    t = Twin.open(tmp_path / "s.db")
    sid, _ = t.db.register_source(kind="manual", label="prose", content="the due date is whenever, honestly")
    t.db.write_claim(subject_type="task", subject_id="dbms-lab_9", predicate="due_at",
                     value={"text": "whenever, honestly", "stated_by": "human"}, source_id=sid,
                     quote="the due date is whenever, honestly", char_start=0, char_end=32,
                     method="manual", verified=True)
    t.db.write_claim(subject_type="task", subject_id="dbms-lab_9", predicate="due_at",
                     value={"date": "2026-10-11"}, source_id=sid, quote="the due date is whenever, honestly",
                     char_start=0, char_end=32, method="rule", verified=True)
    conf = t.db.sync_conflicts()
    assert conf is None or isinstance(conf, (int, list, dict))
    rows = t.db.q("select severity from conflict where subject_id='dbms-lab_9'")
    assert rows, "the disagreement must still be recorded"
    assert t.run_pipeline()["summary"], "and the pipeline must survive it"
    t.db.close()


def test_0003_collapses_span_duplicates_without_losing_history(tmp_path):
    """The interesting half of a constraint-widening migration is not the new constraint, it is the old
    data that violates it. A pre-0003 database holds one fact several times (once per span); the migration
    must keep exactly one open row, keep retired rows as history, drop receipts/edges that pointed at the
    collapsed ones, and leave `false_trust_count` at 0 rather than at 'whatever the leftovers imply'."""
    import datetime as dt
    root = Path(__file__).resolve().parents[1]
    db = str(tmp_path / "dup.db")
    c = sqlite3.connect(db)
    c.executescript((root / "database" / "schema.sql").read_text())
    c.executescript((root / "database" / "migrations" / "0002_twin.sql").read_text())
    from alibi.db import now, sha256
    for name, rel in (("schema.sql", "database/schema.sql"),
                      ("0002_twin.sql", "database/migrations/0002_twin.sql")):
        c.execute("INSERT INTO applied_migrations VALUES (?,?,?)",
                  (name, now(), sha256((root / rel).read_text())[:16]))
    q = "Submission: Saturday 11 Oct 2026"
    c.execute("INSERT INTO source(id,kind,uri,sha256,captured_at,effective_at) "
              "VALUES (1,'user_note','u','h','2026-09-01','2026-09-01')")
    c.execute("INSERT INTO source_meta(source_id,orig_kind,label,chars,captured_at,full_text) "
              "VALUES (1,'manual','note',?,'2026-09-01',?)", (len(q), q))
    for cid, cs, ce, vt in ((1, 0, 31, None), (2, 11, 33, None), (3, 13, 35, None), (4, 2, 33, "X")):
        c.execute(f"""INSERT INTO claim(id,subject_type,subject_id,predicate,value_json,source_id,
                        evidence_span,char_start,char_end,verify_state,method,recorded_at,valid_from
                        {',valid_to' if vt else ''})
                       VALUES ({cid},'task','dbms-lab_9','due_at','{{"date":"2026-10-11"}}',1,?,{cs},{ce},
                        'grounded','rule','2026-09-01','2026-09-01'{",'2026-09-02'" if vt else ''})""", (q,))
    for cid in (1, 2, 4):
        c.execute("INSERT INTO claim_evidence(claim_id,source_id,quote,offset_start,offset_end,verified,"
                  "created_at) VALUES (?,1,?,?,?,1,'2026-09-01')", (cid, q, 0, len(q)))
    c.execute("INSERT INTO claim_lineage(from_claim,to_claim,edge,why,created_at) "
              "VALUES (1,2,'conflicts_with','x','2026-09-01')")
    c.execute("INSERT INTO claim_lineage(from_claim,to_claim,edge,why,created_at) "
              "VALUES (2,4,'supersedes','y','2026-09-01')")
    c.execute("INSERT INTO conflict(subject_type,subject_id,predicate,claim_ids,kind,severity,rule,opened_at)"
              " VALUES ('task','dbms-lab_9','due_at','[1,2]','date','LOW','earliest_safe','2026-09-01')")
    c.commit(); c.close()

    d = DB(db)
    assert "0003_predicates_and_view.sql" in d.applied() or d.migrate()
    open_rows = d.q("SELECT id FROM claim WHERE valid_to IS NULL")
    assert [r["id"] for r in open_rows] == [1], open_rows
    assert [r["id"] for r in d.q("SELECT id FROM claim WHERE valid_to IS NOT NULL")] == [4], \
        "retired rows are history, not duplicates to delete"
    assert [r["claim_id"] for r in d.q("SELECT claim_id FROM claim_evidence ORDER BY claim_id")] == [1, 4]
    assert d.q("SELECT * FROM claim_lineage") == [], "an edge between two collapsed rows must not survive"
    assert d.one("SELECT COUNT(*) n FROM conflict")["n"] == 1
    st = d.stats()
    assert st["false_trust_count"] == 0 and st["unverifiable"] == 0, st
    assert not d.conn.execute("PRAGMA foreign_key_check").fetchall()
    assert d.migrate() == [], "and it must be re-runnable/idempotent"
    d.close()
