"""tests/test_api.py — the HTTP surface, tested as the thing a user actually touches.

These exist because most of this project's bugs were never in a unit: they were in the gap between a
correct core and a route that rendered it wrongly (an empty cell with a 200, a hardcoded size cap, a
draft quoting an invented penalty). The core can be green while the product lies.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient                      # noqa: E402

from alibi.server import build_app                              # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """An app on an *empty* database, seeded through the same POST the Sources page makes."""
    monkeypatch.delenv("ALIBI_DB", raising=False)
    app, state = build_app(db_path=str(tmp_path / "api.db"), seed=False)
    with TestClient(app) as c:
        yield c, state


# ------------------------------------------------------------------ cold start ---

def test_an_empty_database_reports_not_ready_instead_of_all_green(client):
    """A container that is up but has ingested nothing must not look healthy."""
    c, _ = client
    r = c.get("/readyz")
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["claims"] == 0 and body["ready"] is False
    assert "false_trust_count" in body, "the readiness probe is also the honest-mode banner"


def test_every_page_renders_on_an_empty_database(client):
    c, _ = client
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from smoke import PAGES as pages                      # the same list the harness walks
    bad = [p for p in pages if c.get(p).status_code != 200]
    assert not bad, f"empty-DB 500s: {bad}"
    assert c.get("/healthz").status_code == 200
    assert c.get("/readyz").status_code == 503, "ops endpoints are not pages; cold readiness must be 503"


# ------------------------------------------------------------------- uploads ---

def test_upload_size_limit_is_the_configured_one_not_a_hardcoded_literal(client, monkeypatch):
    """ALIBI_MAX_DOC_BYTES was read into Config and used nowhere: the route carried its own `4_000_000`,
    so the documented knob did nothing. A config value with no effect is worse than no config value."""
    c, state = client
    monkeypatch.setattr(state.twin.cfg, "max_doc_bytes", 2048)
    big = ("LAB 1 due 12.10.2026 " + "x" * 4000).encode()
    r = c.post("/api/ingest", files={"file": ("big.txt", big, "text/plain")}, data={"kind": "auto"})
    assert r.status_code == 413, r.text
    detail = r.json()["detail"]
    assert "2,048" in detail, f"the message must quote the limit the operator set: {detail}"
    assert "ALIBI_MAX_DOC_BYTES" in detail, "and the variable that changes it"
    assert state.twin.db.one("SELECT COUNT(*) n FROM source")["n"] == 0, \
        "a refused upload must leave nothing behind, not a partial source"

    small = "LAB 2 due 13.10.2026, 09:00, submitted at the lab counter.".encode()
    r = c.post("/api/ingest", files={"file": ("ok.txt", small, "text/plain")}, data={"kind": "auto"})
    assert r.status_code == 200, r.text


def test_docs_route_cannot_be_turned_into_a_file_reader(client):
    """The obvious `FileResponse(ROOT / "docs" / name)` is a file-read primitive wearing a docs page."""
    c, _ = client
    for evil in ["..%2F..%2Fetc%2Fpasswd", "../../etc/passwd", "README.md%00.png", "a" * 200 + ".md"]:
        assert c.get(f"/api/doc/{evil}").status_code in (404, 422), evil
    assert c.get("/api/doc/README.md").status_code == 200, "the whitelist must still serve real docs"


# ---------------------------------------------------------------- draft email ---

def test_a_draft_needs_evidence_and_says_which_subjects_have_it(client):
    c, state = client
    c.post("/api/sync", data={"demo": "true"})
    r = c.get("/api/plan/draft-extension", params={"task": "does_not_exist"})
    assert r.status_code == 404
    msg = r.json()["detail"]
    assert "not a draft, it is a guess" in msg
    assert "dbms" in msg, f"the retry hint must list real subject ids: {msg}"


def test_the_penalty_in_a_draft_comes_from_a_clause_or_is_admitted_as_absent(client):
    """The one number in a letter a student signs must not be the developer's guess."""
    c, state = client
    c.post("/api/sync", data={"demo": "true"})
    tasks = [s for s in state.twin.db.derive_ledger().open_claims("task") if s.predicate == "due_at"]
    assert tasks, "the demo corpus must yield at least one dated task"
    by_subject = {}
    for s in tasks:
        by_subject.setdefault(s.subject_id, []).append(s)
    body = c.get("/api/plan/draft-extension", params={"task": sorted(by_subject)[0]}).json()
    remedy = body["draft"]["remedy"] if "remedy" in body["draft"] else body["draft"]
    assert body["decision"] == "APPROVAL_REQUIRED", "a draft is shown, never sent"
    text = str(body["draft"])
    if not remedy.get("penalty_stated", True):
        assert remedy["cost_pct"] is None
        assert "does not record" in text, "an absent policy must be stated, not defaulted to 0% or 10%"
    else:
        assert "%" in text


# ------------------------------------------------------------------- exports ---

def test_ledger_export_carries_the_receipts_and_the_policy(client):
    c, _ = client
    c.post("/api/sync", data={"demo": "true"})
    j = c.get("/api/export/ledger.json").json()
    assert j["claims"], "an empty export is how a working ingest looks from the outside"
    assert {k for k in ("policy", "provenance", "changes", "conflicts")} <= set(j)
    every = j["claims"][0]
    for col in ("evidence_span", "char_start", "char_end", "verify_state", "method"):
        assert col in every, f"a claim without its {col} is not re-verifiable"
    assert all(c["evidence_span"] for c in j["claims"]), "a grounded row with no quote is a bug"


def test_csv_export_is_a_table_and_not_a_report(client):
    """Parsed with a real CSV reader, and re-joined: evidence quotes contain commas, so a naive
    `split(",")` on this file produces a spreadsheet where a deadline's own text is a new column."""
    import csv as _csv
    c, state = client
    c.post("/api/sync", data={"demo": "true"})
    text = c.get("/api/export/ledger.csv").text
    rows = list(_csv.reader(text.splitlines()))
    head, body = rows[0], rows[1:]
    assert len(body) == state.twin.db.one("SELECT COUNT(*) n FROM claim")["n"], "a row eaten by quoting"
    assert {len(r) for r in rows} == {len(head)}, "ragged CSV: quoting bug"
    assert "evidence_span" in head and all(len(r) == len(head) for r in body)
    i = head.index("evidence_span")
    assert any("," in r[i] for r in body), "no comma to test with: the assertion would be vacuous"
    assert all(not r[0].startswith(("=", "+", "-", "@")) for r in body), \
        "a cell Excel evaluates as a formula is an injection point in a 'download my data' file"


# ------------------------------------------------------------------ decisions ---

def _first_draft_action(c, state):
    """Create a draft action the way the UI does, and return its action id."""
    subs = sorted({s.subject_id for s in state.twin.db.derive_ledger().open_claims("task")
                   if s.predicate == "due_at"})
    assert subs, "the demo corpus must yield at least one dated task"
    body = c.get("/api/plan/draft-extension", params={"task": subs[0]}).json()
    return body["action_id"]


def test_an_approved_draft_is_rendered_not_sent_and_cannot_be_decided_twice(client):
    c, state = client
    c.post("/api/sync", data={"demo": "true"})
    aid = _first_draft_action(c, state)
    r = c.post(f"/api/action/{aid}/decide", data={"decision": "approved", "note": "send it myself"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["executed"] is True, "an approved action that silently did nothing is its own bug"
    assert "never sends" in out["result"], f"the executor must be a renderer: {out['result']}"
    row = state.twin.db.one("SELECT status, executor_result FROM action_request WHERE id=?", (aid,))
    assert row["status"] == "executed" and "never sends" in row["executor_result"]
    # the receipt says what happened; the mail never left the machine
    assert state.twin.db.one("SELECT COUNT(*) n FROM outbox")["n"] == 0 if \
        state.twin.db.one("SELECT name FROM sqlite_master WHERE name='outbox'") else True

    r2 = c.post(f"/api/action/{aid}/decide", data={"decision": "approved"})
    assert r2.status_code == 409, f"a replayed decision must not re-execute: {r2.text}"
    assert "no pending approval" in r2.json()["detail"]
    assert c.post(f"/api/action/{aid}/decide", data={"decision": "maybe"}).status_code == 422


def test_wipe_requires_the_keyword_and_reports_the_real_counts(client):
    c, state = client
    c.post("/api/sync", data={"demo": "true"})
    assert c.post("/me/wipe", data={"confirm": "yes"}).status_code == 422
    before = state.twin.db.stats()["claims"]
    assert before > 0, "a wipe of nothing proves nothing"
    out = c.post("/me/wipe", data={"confirm": "WIPE"}).json()
    assert out["deleted"] is True
    assert out["remaining_rows"]["claim"] == 0, "a wipe that reports success while rows remain is the worst bug here"
    assert out["remaining_rows"]["source"] == 0
    assert state.twin.db.stats()["false_trust_count"] == 0


# ------------------------------------------------------------------- counters ---

def test_the_sync_banner_reports_what_the_database_actually_holds(client):
    """Measured and fixed: the first sync of the demo corpus reported `claims: 34` because the counter
    summed per-source `rule_claims + promoted` (a rule claim is *also* promoted, and de-duplication
    happens below it). The ledger held 18. The banner now asks the database."""
    c, state = client
    first = c.post("/api/sync", data={"demo": "true"}).json()
    rows = state.twin.db.one("SELECT COUNT(*) n FROM claim")["n"]
    assert first["claims"] == rows == 18, f"banner said {first['claims']}, ledger says {rows}"
    assert first["changes"] <= rows * 3
    assert first["per_source"] and first["per_source"][0]["egress"] in ("NONE", "LOCAL")

    # The ledger's own counters, not the per-source line sums. `injections_quarantined` is a COUNT over
    # change_event rows, so it cannot drift from what happened; its exact value depends on how the corpus
    # is split into blocks (the forged paragraph appears in a notice *and* in two chat paragraphs), so the
    # invariant pinned here is "at least one, and never the payload's fact" — not a lucky integer.
    q = state.twin.db.q("SELECT new_value FROM change_event WHERE kind='INJECTION_QUARANTINED'")
    assert len(q) >= 1, "an injection must leave a visible event, not just an absent row"
    assert any("ignore" in (r["new_value"] or "").lower() for r in q), q
    assert state.twin.db.one("SELECT COUNT(*) n FROM claim WHERE value_json LIKE '%2026-01-01%'")["n"] == 0, \
        "the payload's invented date must not be in the ledger under any path"

    second = c.post("/api/sync", data={"demo": "true"}).json()
    assert second["claims"] == 0, "a re-sync of identical bytes must add nothing to the ledger"
    assert state.twin.db.one("SELECT COUNT(*) n FROM claim")["n"] == 18
    assert second["rejected"] >= 1, "the injection is re-detected: quarantine is not idempotent work"


def test_a_draft_without_a_date_is_refused_not_written(client):
    """/api/plan/draft-extension for a subject whose grounded claims carry a weight but no date must
    refuse: the letter would ask to extend something whose deadline it does not know."""
    c, state = client
    c.post("/api/sync", data={"demo": "true"})
    L = state.twin.db.derive_ledger()
    no_date = with_date = None
    for s in sorted({x.subject_id for x in L.open_claims("task")}):
        has_due = bool(list(L.open_claims("task", s, "due_at")))
        has_any = bool([p_ for p_ in ("due_at", "late_policy", "weight")
                         if list(L.open_claims("task", s, p_))])
        if has_any and not has_due and no_date is None:
            no_date = s                      # grounded evidence exists, but it never names a deadline
        if has_due and with_date is None:
            with_date = s
    assert no_date is not None, "the demo corpus must contain a subject with evidence but no due date"
    r = c.get("/api/plan/draft-extension", params={"task": no_date})
    assert r.status_code == 409, r.text
    assert "nothing to move" in r.json()["detail"]
    assert c.get("/api/plan/draft-extension", params={"task": with_date}).status_code == 200


def test_the_walk_list_matches_the_router():
    """`scripts/smoke.py` walks a hand-written list of pages. If a route is added and the list is not
    updated, the harness keeps reporting "17/17 pages render" about a *smaller* app than exists. Derive
    the expectation from the router so the omission becomes a test failure."""
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from smoke import PAGES as walked, OPS as ops
    from alibi.server import create_app
    from alibi.twin import Twin

    app, _st = create_app(Twin.open(":memory:"))
    get_pages = sorted(
        r.path for r in app.routes
        if "GET" in (getattr(r, "methods", set()) or set())
        and "{" not in r.path and not r.path.startswith(("/static", "/openapi", "/docs/", "/redoc", "/docs"))
        and not r.path.startswith("/api/"))
    covered = set(walked) | set(ops)
    missing = [p_ for p_ in get_pages if p_ not in covered]
    assert not missing, f"GET pages the smoke harness never renders: {missing}"
    # `/docs` is the only path FastAPI owns by default (its Swagger UI) that this app *replaces* with its
    # own documentation page, and the app's route wins. So the derived list legitimately lacks it: assert
    # the replacement explicitly instead of loosening the check for everything.
    assert covered - set(get_pages) <= {"/docs"}, f"harness walks non-existent pages: {covered - set(get_pages)}"
    routes = {r.path: r for r in app.routes if r.path == "/docs"}
    assert getattr(routes["/docs"], "name", "") != "swagger_ui_html", \
        "/docs must be the repo's own page, not FastAPI's Swagger UI"


# ------------------------------------------------------------------ retention ---

def test_retention_drops_text_and_reports_the_loss_rather_than_hiding_it(client):
    """The /privacy page has promised a retention job since the first draft. Promised is not built:
    this pins that it exists, that it deletes only the raw text, and that the resulting claims are
    reported as `unverifiable` instead of being counted as still-verified."""
    c, state = client
    c.post("/api/sync", data={"demo": "true"})
    before = state.twin.db.one("SELECT COUNT(*) n FROM claim")["n"]
    state.twin.db.x("UPDATE source_meta SET captured_at='2024-01-01T09:00:00+05:30'", ())

    out = state.twin.run_retention(days=30, now="2026-09-12")
    assert out["cutoff"] == "2026-08-13", "the window must be measured back from the given date"
    assert out["sources_purged"] >= 1
    assert state.twin.db.one("SELECT COUNT(*) n FROM source_meta WHERE full_text<>''")["n"] == 0
    st = state.twin.db.stats()
    assert st["claims"] == before, "retention removes text, never the ledger"
    assert state.twin.db.one("SELECT COUNT(*) n FROM claim WHERE evidence_span<>''")["n"] == before, \
        "the verbatim quote must survive the purge of its document"
    assert st["unverifiable"] == before, "a claim nobody can re-check is not a checked claim"
    assert st["false_trust_count"] == 0
    assert state.twin.db.one("SELECT COUNT(*) n FROM change_event WHERE kind='RETENTION_RUN'")["n"] == 1, \
        "the deletion itself is recorded"
    # and the app still renders after it, with the loss visible
    assert c.get("/privacy").status_code == 200
    assert c.get("/claims").status_code == 200


def test_a_task_label_is_promoted_from_the_key_not_truncated_from_it(client):
    c, state = client
    c.post("/api/sync", data={"demo": "true"})
    L = state.twin.db.derive_ledger()
    tasks = {t.id: (t.title, t.course) for t in state.twin._tasks_from_ledger(L)}
    assert tasks, "the corpus must yield tasks"
    for tid, (title, course) in tasks.items():
        assert not title.startswith(tuple(f"{c2}-" for c2 in ("dbms", "os", "daa", "cs"))) or " " in title, \
            f"{tid}: {title!r} still reads like a database key"
        assert title == title.strip() and "_" not in title, f"{tid}: {title!r}"
        assert course.isupper() and len(course) <= 4, f"{tid}: course {course!r} (keys use '-', slugs '_')"
    assert tasks["dbms-lab_4"][0] == "Lab 4"


# ------------------------------------------------- the human answer round-trip ---

def _first_open_review(state):
    return state.twin.db.one("SELECT id, subject_id FROM review_item WHERE status='open' ORDER BY id")


def _loads(s):
    import json
    return json.loads(s or "{}")


def test_use_my_answer_becomes_a_claim_not_only_a_closed_inbox_item(client):
    """The button has said "your answer is recorded as the source of truth" since the first draft of this
    UI, while `resolve_review` only wrote `review_item.resolution`: the plan kept using the value the
    reviewer had just corrected. An answer is recorded when it becomes a row with its own receipt."""
    c, state = client
    c.post("/api/sync", data={"demo": "true"})
    rid = _first_open_review(state)["id"]
    before = state.twin.db.stats()["claims"]
    r = c.post(f"/api/review/{rid}/resolve", data={
        "decision": "keep_own", "predicate": "due_at",
        "chosen": "The professor confirmed 17 Oct 2026 in class today."})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["recorded"] and j["shape"] == "date", j
    st = state.twin.db.stats()
    assert st["claims"] == before + 1, "the answer must reach the ledger"
    row = state.twin.db.one("SELECT * FROM claim WHERE id=?", (j["claim_id"],))
    assert row["method"] == "manual" and row["verify_state"] == "grounded"
    assert _loads(row["value_json"])["date"] == "2026-10-17"
    full = state.twin.db.source_info(row["source_id"])["full_text"]
    assert row["evidence_span"] in full, "a manual claim still carries a verbatim receipt, from a source that exists"
    n_ev = state.twin.db.one("SELECT COUNT(*) n FROM claim_evidence WHERE claim_id=?", (j["claim_id"],))["n"]
    assert n_ev == 1, "no receipt, no trust — including for facts a human asserted"
    assert state.twin.db.one("SELECT COUNT(*) n FROM change_event WHERE kind='MANUAL_FACT_RECORDED'")["n"] == 1
    q = c.get("/queue").text
    assert "Recently answered" in q and "manual" in q
    assert st["false_trust_count"] == 0, "an answer is not a licence to break the receipt rule"


def test_an_ambiguous_answer_is_asked_again_instead_of_resolved_by_sorting(client):
    c, state = client
    c.post("/api/sync", data={"demo": "true"})
    rid = _first_open_review(state)["id"]
    claims_before = state.twin.db.stats()["claims"]
    r = c.post(f"/api/review/{rid}/resolve", data={
        "decision": "keep_own", "predicate": "due_at",
        "chosen": "It is 17 Oct 2026, not 18 Oct 2026 as the group says."})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ambiguous"] and j["candidates"] == ["2026-10-17", "2026-10-18"], j
    assert "one date" in j["next"], j
    open_after = state.twin.db.one("SELECT status FROM review_item WHERE id=?", (rid,))["status"]
    assert open_after == "open", "the question stays where the reviewer can answer it again"
    assert state.twin.db.stats()["claims"] == claims_before, "nothing was invented from an ambiguous sentence"
    assert state.twin.db.one("SELECT COUNT(*) n FROM change_event WHERE kind='MANUAL_ANSWER_AMBIGUOUS'")["n"] == 1


def test_an_answer_too_thin_to_be_a_receipt_is_refused_with_the_rule(client):
    c, state = client
    c.post("/api/sync", data={"demo": "true"})
    rid = _first_open_review(state)["id"]
    before = state.twin.db.stats()["claims"]
    for payload in ({"decision": "keep_own", "chosen": "17 Oct"},
                    {"decision": "keep_own", "chosen": ""},
                    {"decision": "keep_own", "chosen": "anything at all", "predicate": "vibes"}):
        r = c.post(f"/api/review/{rid}/resolve", data=payload)
        assert r.status_code == 422, (payload, r.text)
        detail = r.json()["detail"]
        assert ("receipt" in detail) or ("no answer" in detail) or ("storable" in detail), detail
    assert state.twin.db.one("SELECT status FROM review_item WHERE id=?", (rid,))["status"] == "open"
    assert state.twin.db.stats()["claims"] == before


def test_readyz_names_the_ai_runtime_and_says_when_it_is_only_the_simulator(client):
    c, state = client
    j = c.get("/readyz").json()
    assert "ai" in j and j["ai"]["local_provider"], j
    assert j["simulated_extraction"] is True, "no model is installed here; the probe must not imply one"
    k = c.get("/api/ai-check").json()
    assert k["verdict"] == "simulated", k
    assert "deterministic" in k["verdict_note"] or "no model" in k["verdict_note"], k
    live = c.get("/api/ai-check", params={"dry": "false"}).json()
    assert live["verdict"] in ("SUCCESS", "PARTIAL"), live
    assert live["provider"], live


# ---------------------------------------------------------------- enhancement ---

def test_the_page_works_with_and_without_the_enhancement_layer(client):
    """Layer 2 (CSS/JS) is served from this process, never a CDN, and the page must be *correct*
    without it: a campus network that blocks an asset must degrade the look, not the truth. The test
    asserts both halves — that the link exists when the files exist, and that nothing in the rendered
    HTML depends on them."""
    c, state = client
    html = c.get("/").text
    assert "/static/alibi.css?v=" in html and "/static/alibi.js?v=" in html
    assert c.get("/static/alibi.css").status_code == 200
    assert c.get("/static/alibi.js").status_code == 200
    # the inline layer still carries the design tokens, so a 404 on the file is a plain page, not a broken one
    assert "--panel" in html and "class=\"shell\"" in html
    # no external origin anywhere: the only asset URLs are relative
    import re
    ext = [u for u in re.findall(r'(?:href|src)="([^"]+)"', html) if u.startswith(("http:", "https:", "//"))]
    assert not ext, f"external asset on a trust dashboard: {ext}"
    # and every page's numbers are in the HTML, not fetched by JS
    body = re.sub(r"<[^>]+>", " ", html)
    assert "false trust" in body and "grounded claims" in body, \
        "the two numbers the product is judged on must be in the HTML, not fetched by JS"
    js = c.get("/static/alibi.js").text
    for banned in ("innerHTML =", "eval(", "document.write", "insertAdjacentHTML"):
        assert banned not in js, f"{banned} in the client layer — an XSS surface for no benefit"
    assert "fetch(" in js, "the toast layer should be the thing that reads API errors"


def test_ai_health_is_reported_as_a_component_not_a_green_dot(client):
    c, state = client
    j = c.get("/api/health").json()
    comps = {k: v["state"] for k, v in j["components"].items()}
    assert comps["database"] == "healthy", j
    # "no model, deterministic extractor instead" is *degraded*, not healthy and not unavailable: the
    # app reads documents fine, and the product must not hide which engine did it.
    assert comps["ai"] == "degraded" and j["simulated_extraction"] is True, j
    assert "simulator" in j["components"]["ai"]["detail"], j
    assert comps["ledger"] == "empty", "a fresh database must not look populated"
    assert comps["egress"] == "closed" and comps["verifier"] == "healthy", j
    assert j["state"] in ("degraded", "unavailable"), j
    assert any("empty" in w or "simulator" in w for w in j["warnings"]), j
    # and the paths it prints are the paths actually open, not the config default
    assert j["components"]["database"]["detail"].endswith("api.db") or "api.db" in j["components"]["database"]["detail"], j
    # the cockpit shows the same verdicts the API reports, and says where they came from
    page = c.get("/").text
    assert "System health" in page and "AI: degraded" in page, page[:200]
    assert "/api/health" in page, "a dashboard chip you cannot inspect is decoration"


def test_the_compose_file_parses_and_sets_only_variables_the_code_reads():
    """A compose file is code. The first version of this one set `OLLAMA_URL` and `UPLOAD_DIR`, neither of
    which any line of Python reads, and the YAML additionally had a mis-indented block that a reviewer's
    `docker compose up` would have hit before ever seeing a page. Both classes of failure are cheap to
    prevent and expensive to demo."""
    import re
    try:
        import yaml
    except ImportError:
        pytest.skip("pyyaml is not a runtime dependency; install it to lint the compose file")
    root = ROOT
    doc = yaml.safe_load((root / "docker-compose.yml").read_text())
    assert "services" in doc and "alibi" in doc["services"], sorted(doc)
    svc = doc["services"]["alibi"]
    assert svc["healthcheck"]["test"][0] == "CMD", svc["healthcheck"]
    assert "/readyz" in (svc["healthcheck"]["test"][-1] + "".join(svc["healthcheck"]["test"])), \
        "the container must health-check readiness, not liveness"
    declared = set(svc.get("environment") or {})
    code = "\n".join(p.read_text() for p in (root / "alibi").glob("*.py"))
    unread = sorted(k for k in declared if k not in code)
    assert not unread, f"compose sets variables no code reads: {unread}"
    # every ALIBI_*/LM_STUDIO_* the code reads should be documented in .env.example or compose
    read = set(re.findall(r'environ\.get\("((?:ALIBI|LM_STUDIO|OLLAMA|LOCAL|MODEL)_[A-Z_]+)"', code))
    # `LM_STUDIO_API_KEY_ENV` is a *pointer to* a variable name, not a setting a user would configure by
    # value; its target is documented, which is what matters.
    read.discard("LM_STUDIO_API_KEY_ENV")
    documented = set((root / ".env.example").read_text()) and set(
        re.findall(r"^#?\s*([A-Z][A-Z_0-9]+)=", (root / ".env.example").read_text(), re.M))
    documented |= set(declared)
    missing = sorted(k for k in read if k not in documented)
    assert not missing, f"the code reads these but neither .env.example nor compose mentions them: {missing}"


# ------------------------------------------------------------------- logging ---

def test_the_logging_knobs_in_config_are_the_logging_knobs_in_the_handler(client):
    """`LOG_LEVEL` / `ALIBI_LOG_JSON` / `DEBUG` were read into `Config` and used by nothing — a knob that
    looks configured and does nothing (the same class as `ALIBI_MAX_DOC_BYTES`, `OLLAMA_URL`). This pins
    that the app actually installs a handler from them."""
    import logging
    from alibi.log import JsonFormatter, configure, get

    root = configure(client[1].twin.cfg, force=True)
    assert root.handlers and root.level == logging.INFO, (root.level, root.handlers)
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    log = get("probe")
    # Double-printing guard: `propagate` is False on the *named root* ("alibi"), which is what stops a
    # record from also reaching the uvicorn/root handlers. Child loggers keep propagate=True by design —
    # they must still reach "alibi"'s handler — so asserting on the child is the wrong check.
    assert logging.getLogger("alibi").propagate is False

    # a structured line contains exactly the fields the call site attached
    stream = __import__("io").StringIO()
    h = logging.StreamHandler(stream)
    h.setFormatter(JsonFormatter())
    log.addHandler(h)
    log.info("ingest", extra={"run_id": "9", "claims": 18, "api_key": "sk-live",
                             "text": "y" * 2000})
    log.removeHandler(h)
    import json as _json
    rec = _json.loads(stream.getvalue().strip())
    assert rec["msg"] == "ingest" and rec["claims"] == 18 and rec["run_id"] == "9"
    assert rec["api_key"] == "[set]", "a secret must not reach stdout because it was passed as a field"
    assert len(rec["text"]) < 900, "long payloads are capped, not spooled"
    for noise in ("relativeCreated", "processName", "args", "lineno"):
        assert noise not in rec, f"LogRecord internals leaking into every line: {noise}"

    # and a level change is observed
    root.setLevel(logging.ERROR)
    stream.truncate(0); stream.seek(0)
    log.addHandler(h)
    log.info("should-be-suppressed")
    log.removeHandler(h)
    assert stream.getvalue() == "", "LOG_LEVEL=ERROR must actually suppress INFO"
    root.setLevel(logging.INFO)
