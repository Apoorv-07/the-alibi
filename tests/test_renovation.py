"""Tests for the renovation: the six destinations, and the language contract they are built on.

Why this file exists separately: `alibi/present.py` is the *only* place internal state may be translated for a
human. If it is wrong, every page is wrong at once — so it is tested directly (against a real ledger, not a
fixture of hand-written strings) and then again through the pages that consume it.

Every test here runs on a frozen clock. The product's own `_today()` is the real date — correct for a student,
useless for a test: "Was due 17 days ago" would quietly become "in six months" and the assertion would stop
proving anything about phrasing.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from alibi import twin as twin_mod
from alibi.present import Presentation, option_label, title_for, when_phrase
from alibi.server import NAV, NAV_ADVANCED, build_app

TODAY = "2026-09-14"


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(twin_mod, "_today", lambda: dt.date.fromisoformat(TODAY))


@pytest.fixture()
def pair(tmp_path, monkeypatch):
    """The app, warm: the demo corpus loaded through the *same* route the button on the page posts to.
    A fixture that hand-loads the DB would test a product nobody can reach."""
    monkeypatch.delenv("ALIBI_DB", raising=False)
    app, state = build_app(db_path=str(tmp_path / "renov.db"), seed=False)
    with TestClient(app) as c:
        r = c.post("/api/sync", data={"demo": "true"})
        assert r.status_code == 200, r.text
        yield c, state


@pytest.fixture()
def pres(pair) -> Presentation:
    return Presentation(pair[1].twin)


def norm(s: str) -> str:
    """Compare wording, not whitespace: a date phrase uses non-breaking spaces on purpose (a value that wraps
    in a narrow column is a value you misread), which would otherwise break every literal assertion."""
    return s.replace("\xa0", " ")


def text(html: str) -> str:
    # strip `<style>`/`<script>` *bodies* first: the inline layer-1 CSS contains `>` selectors, and a
    # tag regex that treats those as the end of a tag mangles the stylesheet before it ever reaches the text
    html = re.sub(r"<(style|script)\b.*?</\1>", " ", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


# ------------------------------------------------------------------- nav ---

def test_the_navigation_is_six_destinations_and_nothing_else():
    """The brief is a number, not a vibe: six primary items, each a destination the student can say back.
    Anything that grows here is the product drifting back towards a dashboard of panels."""
    keys = [k for k, _l, _h in NAV]
    assert keys == ["home", "calendar", "tasks", "review", "evidence", "settings"], keys
    for _k, label, href in NAV:
        assert label in ("Home", "Calendar", "Tasks", "Review", "Evidence", "Settings")
        assert href.startswith("/") and not href.endswith(".html")
    # the advanced area is *inside* the six, and it does not add a seventh top-level item
    flat = [(k, h) for _g, items in NAV_ADVANCED for k, _l, h in items]
    assert "/technical/cockpit" in [h for _k, h in flat]
    assert not [k for k, _l, h in NAV if h.startswith("/technical")]
    assert len(flat) >= 11, "the eleven technical pages must still be reachable somewhere"


@pytest.mark.parametrize("path", ["/", "/calendar", "/tasks", "/evidence", "/review", "/settings",
                                  "/technical", "/technical/cockpit"])
def test_every_destination_answers_without_jargon_or_orphans(pair, path):
    c, _ = pair
    r = c.get(path)
    assert r.status_code == 200, (path, r.status_code)
    body = text(r.text)
    # words a student never uses must not stand alone as UI text
    for word in ("Traceback", "NoneType", "TODO", "FIXME", "coming soon", "{{", "{%", "jinja2."):
        assert word not in body, f"{path} leaks {word!r}"
    # exactly one h1 per page: the header is a heading, everything under it is a section
    assert r.text.count("<h1") == 1, f"{path} has {r.text.count('<h1')} h1 elements"
    assert r.text.count("<main") == 1, f"{path} has more than one <main>"


# ------------------------------------------------------------- phrasing ---

def test_titles_read_the_way_a_student_writes_them():
    assert title_for("os-ia_2", []) == "IA 2"
    assert title_for("os-endsem", []) == "Endsem"
    assert title_for("csn-lab_4", []) == "Lab 4"
    # a title that is already prose keeps its own shape; only the coded tokens get re-cased
    assert title_for("csn-submit the lab 4 report", []) == "Submit the lab 4 report"
    # ...and a claim that states a title wins over any derivation, because the source outranks the key
    assert title_for("os-ia_2", [{"value": {"title": "Internal Assessment 2"}}]) == "Internal Assessment 2"


def test_when_phrase_never_shows_a_bare_iso_date(pres: Presentation):
    cards = pres.task_cards()
    assert cards, "the demo corpus must produce cards or this test proves nothing"
    for c in cards:
        assert not re.fullmatch(r"\d{4}-\d{2}-\d{2}", c["when"]), c
        assert not re.search(r"\d{4}-\d{2}-\d{2}", c["when"]), f"{c['when']} still carries a raw ISO date"
    today = dt.date.fromisoformat(TODAY)
    assert when_phrase(dt.date(2026, 8, 28), today).endswith("days ago")
    assert when_phrase(None, today) == "No date on file"
    assert "Sat" in when_phrase(dt.date(2026, 10, 17), today)


def test_a_label_column_holding_json_is_parsed_not_printed():
    """Review options used to render `{"date": "2026-10-30", "time": null}` as the button a human clicked."""
    lab = option_label({"label": '{"date": "2026-10-30", "time": null}', "value": {"date": "2026-10-30"}})
    assert "2026" in lab and "{" not in lab, lab
    assert "17 Oct" in lab or "16 Oct" in lab or "Oct" in lab, lab
    assert option_label({"label": "just some words"}) == "just some words"
    assert option_label("2026-10-30").startswith("Thu 2026-10-30".split()[0][:3]) or "Oct" in option_label(
        "2026-10-30")


def test_the_plan_is_words_before_it_is_a_status_code(pres: Presentation, pair):
    _c, state = pair
    planned = state.twin.run_pipeline()
    words = pres._plan_words(planned.get("feasibility") or {}, planned.get("summary") or {})
    assert words["state"] in ("feasible", "tight", "infeasible", "unknown")
    assert words["headline"], "a verdict with no sentence under it is a badge, not an answer"
    for fix in words["fixes"]:
        assert fix["label"] and not fix["label"].startswith("{"), fix
    empty = pres._plan_words({}, {})
    assert empty["state"] == "unknown" and empty["fixes"] == [], "no solver, no claim"


def test_absence_is_reported_as_absence_not_as_zero(tmp_path):
    """A cold ledger must not display numbers. This is the one behaviour the whole product's honesty rests on."""
    t = Twin(tmp_path)
    p = Presentation(t)
    assert t.db.one("SELECT COUNT(*) n FROM claim")["n"] == 0
    assert "does not fill this in" in p._trust_words()["line"]
    assert p.attention()["count"] == 0 and p.attention()["cards"] == []
    assert p.review()["count"] == 0
    home = p.home()
    assert home["attention"] == [] and home["this_week"] == [] and home["changed"] == []
    assert p.calendar()["events"] == 0


def Twin(tmp_path):                                            # noqa: N802 - a tiny local helper, not a class
    from alibi.twin import Twin as T
    return T.open(str(tmp_path / "cold.db"))


# ------------------------------------------------------------ the pages ---

def test_home_orders_attention_by_urgency_and_says_why(pres: Presentation):
    att = pres.attention()
    assert att["cards"], "the demo corpus has overdue or disputed items; an empty list here is a regression"
    flags = [bool(i["urgent"]) for i in att["cards"]]
    assert flags == sorted(flags, reverse=True), f"urgent items must lead: {flags}"
    assert att["reason"], "the header must state the count in words"
    for it in att["cards"]:
        assert it["line"] and it["href"], it


def test_a_card_with_two_dates_on_file_lets_the_page_say_so(pres: Presentation):
    """Silently picking the earlier of two dates is how a tool loses the right to be called a twin."""
    both = [c for c in pres.task_cards() if c["needs_choice"]]
    assert both, "the demo corpus has a disputed date; if none is marked, the card layer is hiding it"
    for c in both:
        assert c["due"] is None, "a disputed date must not be rendered as a single deadline"
        assert "choose" in c["when"].lower() or "disagree" in c["when"].lower(), c
        assert c["due_any"], "but the ordering still needs a date to sort by"


def test_evidence_page_links_a_claim_to_its_quote(pair):
    c, state = pair
    row = state.twin.db.open_claims()[0]
    body = text(c.get(f"/evidence?subject={row['subject_id']}").text)
    assert (row["evidence_span"] or "")[:32] in body, "the verbatim receipt has to be on the page, not fetched"
    assert f"claim {row['id']}" in body


def test_the_ask_panel_answers_from_the_ledger_alone(pair):
    """No model on this path: an answer is a sentence plus the rows it came from, or an admission."""
    c, _ = pair
    body = text(c.get("/?ask=what is due this week").text)
    assert "You asked" in body and "no model call" in body, body[:400]
    body2 = text(c.get("/?ask=what is my hostel mess fee").text)
    assert "no rows" in body2, "an unanswerable question must be refused, not answered"


def test_defer_is_not_recorded_as_an_answer(pair):
    """'I'll decide later' must leave the item out of the answered pile and out of the open queue, with the
    reason attached. A status CHECK that 500s on an unknown decision is the same bug wearing a different hat."""
    c, state = pair
    rid = state.twin.db.one("SELECT id FROM review_item WHERE status='open' ORDER BY id")["id"]
    r = c.post(f"/api/review/{rid}/resolve", data={"decision": "later", "note": "waiting for the portal"})
    assert r.status_code == 200, r.text
    row = state.twin.db.one("SELECT status, consequence FROM review_item WHERE id=?", (rid,))
    assert row["status"] == "expired", row
    assert "portal" in row["consequence"] or "postponed" in row["consequence"], row
    assert state.twin.db.one("SELECT COUNT(*) n FROM review_item WHERE status='answered'")["n"] == 0


def test_an_unknown_decision_word_cannot_break_the_ledger(pair):
    """The CHECK constraint is the last line of defence for data integrity; a UI verb must never reach it."""
    c, state = pair
    rid = state.twin.db.one("SELECT id FROM review_item WHERE status='open' ORDER BY id")["id"]
    r = c.post(f"/api/review/{rid}/resolve", data={"decision": "snooze_until_friday"})
    assert r.status_code in (200, 400, 422), r.text
    assert state.twin.db.one("SELECT status FROM review_item WHERE id=?", (rid,))["status"] in (
        "open", "answered", "dismissed", "expired")


def test_choosing_a_candidate_from_the_calm_page_writes_a_claim(pair):
    """The Review sheet is a different template over the same route; the two must not diverge in effect."""
    c, state = pair
    item = next((i for i in Presentation(state.twin).review()["cards"] if i["options"]), None)
    assert item, "the demo corpus needs at least one two-date item for this to mean anything"
    rid = item["id"]
    before = state.twin.db.stats()["claims"]
    r = c.post(f"/api/review/{rid}/resolve",
               data={"decision": "use", "chosen_index": "1", "note": "portal is authoritative"})
    assert r.status_code == 200, r.text
    st = state.twin.db.one("SELECT status, resolution FROM review_item WHERE id=?", (rid,))
    assert st["status"] == "answered" and '"' in st["resolution"], st
    assert state.twin.db.stats()["claims"] >= before
    assert "portal" in (state.twin.db.one("SELECT consequence FROM review_item WHERE id=?", (rid,))
                        ["consequence"] or "")


def test_onboard_is_the_only_way_in_and_the_sample_is_one_click(tmp_path, monkeypatch):
    monkeypatch.delenv("ALIBI_DB", raising=False)
    app, _state = build_app(db_path=str(tmp_path / "cold-web.db"), seed=False)
    with TestClient(app) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/onboard"
        body = c.get("/onboard").text
        assert 'action="/api/ingest"' in body and 'name="file"' in body, "the one action must be a real upload"
        assert "sample corpus" in text(body).lower(), "the demo must be an explicit, labelled choice"
        assert c.get("/?demo=1").status_code == 200, "?demo=1 lets a seeded server answer directly"


# ----------------------------------------------------------------- chrome ---

def test_calm_pages_carry_their_own_layer_and_no_cinematic_one(pair):
    c, _ = pair
    html = c.get("/tasks").text
    assert "/static/calm.css?v=" in html and "/static/calm.js?v=" in html
    for asset in ("atmosphere.js", "scene.js", "motion.js"):
        assert asset not in html, f"{asset} belongs to the technical pages only"
    assert "data-calm" in html, "the theme hook must be on <html>, where the inline layer can see it"
    assert "fluid-cursor" not in html, "the custom cursor is not even in a calm page's DOM"
    assert html.count('<a class="navi') == 6, "the rail is the product's shape: six items, always"


def test_the_attention_budget_is_honest_about_its_size(pair):
    """The brief says 80 % obligations, 15 % attention, 5 % system status. On a quiet day the 15 % must
    genuinely vanish — a 'nothing needs you' panel that occupies the fold is the dashboard this replaced."""
    c, _ = pair
    body = text(c.get("/").text)
    # the header sentence is `present.attention()["reason"]`, phrased for the count it found
    assert re.search(r"need(s)? you", body), "Home must open with what needs the student, in words"
    assert "Read my sources again" in body, "the one write action on the home page must be a re-read"
    assert "model " in body, "which engine ran is stated on the page, not hidden behind a status API"


def test_a_calm_page_degrades_to_the_inline_layer(pair):
    """With no stylesheet and no script the six pages must still be laid out, navigable and readable: the
    inline block in base.html is layer 1 and carries the same geometry on purpose."""
    c, _ = pair
    html = c.get("/").text
    assert "html[data-calm]" in html, "the inline calm layer is missing from <head>"
    for needle in ("min-height:100vh", "grid-template-columns:250px", "prefers-reduced-motion"):
        assert needle in html, f"layer 1 lost {needle}"
    assert ".fluid-cursor" not in html and "atmosphere" not in html, "the calm pages must not even carry the hooks"

# ------------------------------------------------------ provenance & filters ---

def test_a_card_names_the_document_it_came_from(pair):
    """`claim` carries `source_id`; the label the human typed on upload is the only thing that says *which*
    syllabus. A card that printed "A source on file" for all six sources threw that away."""
    c, state = pair
    body = text(c.get("/tasks?state_f=past").text)
    assert "Why does ALIBI say this?" in body
    assert re.search(r"Sources (?:[A-Z][^·]{3,})(?: · [A-Z][^·]{3,})*", body), body[body.index("Sources"):][:160]
    assert "A source on file" not in body, "the fallback phrase must only appear for a genuinely unnamed source"
    card = Presentation(state.twin).task_cards()[0]
    assert card["sources"], card


def test_the_tasks_filter_is_a_real_filter_not_a_re_render(pair):
    """Chips that change their own border colour and nothing else are the definition of a dead control."""
    c, _ = pair
    everything = c.get("/tasks").text.count('<article class="task')
    past = c.get("/tasks?state_f=past")
    assert past.status_code == 200
    assert 0 < past.text.count('<article class="task') < everything, "the past filter must show fewer cards"
    assert "Past its date — ALIBI will not guess whether you submitted" in text(past.text)
    done = c.get("/tasks?state_f=done").text
    assert "Nothing matches this filter." in text(done) or '<article class="task' in done
    os_only = c.get("/tasks?course=OS").text
    assert os_only.count('<article class="task') < everything
    assert "OS only" in text(os_only), "the header must say which filter it is describing"


def test_the_header_counts_describe_the_ledger_not_the_filter(pair):
    """Two numbers, two meanings, stated separately: '10 open' is the ledger; '1 shown' is the filter."""
    c, _ = pair
    body = text(c.get("/tasks?state_f=past").text)
    assert "10 open · 1 past its date · 0 done" in body, body[:400]
    assert "1 shown" in body


def test_the_nav_badge_says_what_the_number_means(pair):
    c, _ = pair
    html = c.get("/tasks").text
    m = re.search(r'<span class="navi__n" aria-label="([^"]+)">', html)
    assert m, "a bare 3 in the rail is a number a screen reader has to interpret out of context"
    assert "waiting for you" in m.group(1), m.group(1)


def test_the_evidence_trail_phrases_every_value(pair):
    """The trail used to print `{"date": "2026-08-28", "precision": "day"}` and `from syllabus_pdf`, i.e. the
    two columns a human is reading to decide whether to trust a deadline. `present.py` phrases them; the
    template may not reach for `|tojson` or a raw column."""
    c, _ = pair
    html = c.get("/evidence?subject=os-ia_1").text
    body = norm(text(html))
    assert "Where IA 1 came from" in body, "the heading is the task's name, not its primary key"
    assert "Fri 28 Aug 2026" in body and "date only, no time" in body, body[:600]
    assert "15% of the grade" in body
    for leak in ("syllabus_pdf", "due_at", "precision", '"date"', "&quot;date"):
        assert leak not in html, f"raw column `{leak}` reached the page"
    assert "chars 0–" in body, "the offsets are the part that makes a quote checkable"
    tmpl = pathlib.Path(__file__).resolve().parents[1] / "web/templates/evidence.html"
    src = pathlib.Path(tmpl).read_text()
    assert "|tojson" not in src, "a template must not phrase a value by dumping JSON"


# ------------------------------------------------------ the answered recap ---

def test_the_recap_phrases_an_answer_and_never_fakes_one(pair):
    """Three verbs, three honest lines. `resolution` is stored as the option JSON, so the recap used to print
    `{"date": "2026-10-13", "precision": "day"}` under a heading that promised the answer — and it listed a
    postponement with no distinguishing word, which is the ledger claiming something the student never did."""
    c, state = pair
    rids = [r["id"] for r in state.twin.db.q("SELECT id FROM review_item WHERE status='open' ORDER BY id")]
    assert len(rids) >= 3, "the demo corpus needs three open items to exercise the three verbs"
    c.post(f"/api/review/{rids[0]}/resolve",
           data={"decision": "use", "chosen_index": "1", "note": "portal is authoritative"})
    c.post(f"/api/review/{rids[1]}/resolve", data={"decision": "later", "note": "waiting for the HOD"})
    c.post(f"/api/review/{rids[2]}/resolve", data={"decision": "dismiss"})

    body = text(c.get("/review").text)
    assert "Recently answered" in body
    assert re.search(r"\d{2} \w{3} \d{4}.*answered", body), body[body.index("Recently answered"):][:400]
    assert "postponed" in body and "skipped" in body, "the two non-answers must be labelled as such"
    assert "portal is authoritative" in body, "the note belongs with the decision"
    html = c.get("/review").text
    for leak in ('"date"', "precision", "&quot;date"):
        assert leak not in html, f"raw resolution JSON ({leak}) reached the recap"
    recent = Presentation(state.twin).review()["recent"]
    assert len({r["id"] for r in recent}) == len(recent), "one decision shown twice is a double-booked receipt"


def test_the_recap_carries_no_invented_value_for_a_deferral(pair):
    """An empty value column is a claim of absence; the word `defer` echoed back from `resolution` would read
    as if the student had answered with it."""
    c, state = pair
    rid = state.twin.db.one("SELECT id FROM review_item WHERE status='open' ORDER BY id")["id"]
    c.post(f"/api/review/{rid}/resolve", data={"decision": "later", "note": "after the meeting"})
    it = next(i for i in Presentation(state.twin).review()["recent"] if i["id"] == rid)
    assert it["chosen"] == "", it
    assert it["status"] == "postponed", it
    assert "after the meeting" in it["note"], it


def test_a_review_card_says_something_new_in_every_line(pair):
    """The card used to print the question, then print it again underneath as `body` — the visual signature of
    a template that had nothing to add. Every line must earn its place: heading (what kind of decision),
    question (which item), body (why there is a disagreement at all), why (the rule), options (the values)."""
    c, state = pair
    items = Presentation(state.twin).review()["cards"]
    withopts = [i for i in items if i["options"]]
    assert withopts, "the demo corpus needs a two-date conflict for this to mean anything"
    for it in withopts:
        assert it["body"] and it["why"], it
        assert it["body"] != it["question"], "body must not restate the question"
        assert "state different values" in it["body"], it["body"]
        for leak in ("an unnamed source", "None", "{", "}"):
            assert leak not in it["body"], (leak, it["body"])
        # the two named sources are named, because that is the whole reason a human is being asked
        assert " and " in it["body"], it["body"]


def test_evidence_rows_keep_the_value_and_its_quote_together(pair):
    """The evidence groups used to be a four-column table, which forced a choice between a date wrapped over
    three lines and a verbatim quote printed over its neighbour — on a page whose entire purpose is the quote.
    So: one row per claim, the receipt inside the same element as the value it proves, and no sibling table
    column for the quote to escape into. (The pixel half of this claim — nothing clipped, date on one line —
    is measured at three viewports by `make browser-check`.)"""
    c, _ = pair
    html = c.get("/evidence").text
    assert '<ol class="claims"' in html, "the evidence group must be a claim list, not a squeezed table"
    assert 'class="tbl"' not in html, "a four-column table cannot hold a 60-char quote and a date"
    row = html[html.index('<li class="claim">'):html.index('<li class="claim">') + 3000]
    assert 'class="claim__value"' in row and 'class="quote' in row, "value and receipt belong to one row"
    assert "chars " in row, "the offsets are what make a quote checkable against the document"
    body = norm(text(html))
    assert "Wed 30 Sep 2026 date only, no time" in body, body[:400]
    for leak in ("{&quot;", '"date"', "precision", "syllabus_pdf"):
        assert leak not in html, f"raw column {leak} reached the page"


# ---------------------------------------------------------------- settings ---
# Settings is the sixth destination and it was the one still rendering in the *fluid* system. Two bugs came
# from that and both are silent: the page loaded the other stylesheet, and its "Reading preferences" block —
# the only control that turns the fluid layer off — was gated on `calm`, so it never rendered at all. The
# README told people to click a control that did not exist. Hence: assert the layer, and assert the control.

def test_settings_is_a_calm_destination_with_a_reachable_toggle(pair):
    c, _ = pair
    html = c.get("/settings").text
    assert "/static/calm.css?v=" in html and "/static/calm.js?v=" in html
    assert 'href="/static/alibi.css' not in html          # the link, not the explanatory comment
    for fluid in ("atmosphere.js", "scene.js", "motion.js", "<canvas"):
        assert fluid not in html, f"/settings still carries the fluid layer: {fluid}"
    assert 'id="fluid-toggle"' in html, "the fluid-layer control must render, not sit behind a false condition"
    assert "Reading preferences" in html
    assert html.count("<h1") == 1


def _visible(html: str) -> str:
    """Page text *outside* the raw disclosure: identifiers belong in `policy | tojson` and nowhere else, so
    judging the prose means excluding the one place they are legitimately the thing being configured."""
    stripped = re.sub(r'<details class="deep".*?</details>', " ", html, flags=re.S)
    return text(stripped)


def test_settings_phrases_every_date_and_code(pair):
    c, _ = pair
    body = norm(_visible(c.get("/settings").text))
    assert not re.search(r"\d{4}-\d{2}-\d{2}", body), "a raw ISO date reached the prose, not the receipt"
    for code in ("earliest_safe", "rules_only", "attendance_threshold", "deterministic_simulator", "None"):
        assert code not in body, f"{code!r} is internal state, not something a student should have to read"
    assert "Fri 30 Oct 2026" in body                      # the seeded policy's defaulter freeze, phrased
    assert "75% per subject" in body


def test_the_raw_layer_stays_one_click_below(pair):
    """Plain language on top, the actual bytes underneath: paraphrasing config without an escape hatch is how
    an admin page becomes something you have to trust."""
    c, _ = pair
    html = c.get("/settings").text
    assert html.count('<details class="deep">') >= 3
    for needle in ("attendance_threshold", "LM_STUDIO_API_KEY", "max_cloud_egress_bytes_per_day"):
        assert needle in html, f"{needle} vanished from the page entirely"
    # a substring search for "sk-" would match `for="ask-q"` in the Ask form — which is exactly what it did.
    # The real question is narrower: is any key rendered as a *value*? The redacted view stores booleans, so
    # a quoted string next to a secret's name is the failure, and that is what is asserted here.
    raw = re.search(r'<details class="deep">.*?</details>', html, re.S).group(0)
    assert '"secrets_present"' in raw
    assert not re.search(r'"(?:LM_STUDIO_API_KEY|OPENAI_API_KEY|GEMINI_API_KEY|GOOGLE_GEMINI_API_KEY|'
                         r'GROQ_API_KEY|CEREBRAS_API_KEY|OPENROUTER_API_KEY|TELEGRAM_BOT_TOKEN)"\s*:\s*"',
                         raw), "a secret is rendered as something other than presence"


class _OnlyPolicy:
    """Stand-in twin: `policy_words` reads the policy and the frozen date, nothing else. A thin policy file
    is a real installation state — a colleague edits one key out to see what happens — so the fallbacks are
    tested as behaviour, not as a template's idea of what `or` does."""

    def __init__(self, policy):
        self.policy = policy
        self.db = None        # Presentation's constructor takes the handle; policy_words never uses it


def test_policy_words_reports_absence_instead_of_inventing_it():
    pw = Presentation(_OnlyPolicy({})).policy_words()          # type: ignore[arg-type]
    assert pw["name"] == "no policy file" and pw["empty"] is True and pw["authority"] == []
    for label, value in pw["rows"]:
        assert value.strip(), f"{label!r} rendered nothing at all"
        assert "None" not in value and "undefined" not in value and "{" not in value
    tie = dict(pw["rows"])["If two sources give different dates"]
    assert "earliest" in tie, "with no tie_break set the ledger does use the earliest — say what it does"


def test_policy_words_formats_a_real_policy(pres: Presentation):
    body = dict(pres.policy_words()["rows"])
    assert body["Attendance needed to sit an exam"] == "75% per subject"
    assert "Fri 30 Oct 2026" in body["Reduced on a certificate"]
    assert pres.date_words("2026-09-22") == "Tue 22 Sep 2026"
    assert pres.date_words("sometime in October") == "sometime in October"   # echoed, never reformatted
    assert pres.date_words(None) == "not set in this policy"


def test_date_words_is_not_the_deadline_voice(pres: Presentation):
    """`when_phrase` says "Due", which is right for a task and wrong for a freeze date nobody must act on."""
    from alibi.present import when_phrase as deadline_voice
    phrased = pres.date_words("2026-09-22")
    assert phrased == "Tue 22 Sep 2026"
    assert "Due" not in phrased and "Due" in deadline_voice(dt.date(2026, 9, 22), dt.date(2026, 9, 14))
