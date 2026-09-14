# Interface

The UI exists to answer one question without leaving the page: **"on what evidence did you decide this?"**
Every design choice below serves that, including the ones that look like limitations.

## Constraints the interface runs under

1. **No build step, no CDN, no framework — and a page that is *correct* without any of layer 2.**
   Layer 1 is 16 templates plus one `<style>` block inline in `web/templates/base.html` (85 lines): colours,
   layout, tables, pills, the server-drawn SVG graph from `_graph_svg()`. Layer 2 is two files served by this
   same process (`web/static/alibi.css` ≈ 150 lines, `web/static/alibi.js` ≈ 230) adding depth, motion,
   toasts, a command palette and busy-states. Nothing depends on layer 2 to render the truth: if
   `/static/*` 404s, the numbers, receipts, filters and forms are all still in the HTML.

   That split is the design decision, not an optimisation. A trust dashboard whose layout needs a JS bundle
   is a dashboard that shows nothing exactly when something has gone wrong — and a demo that needs
   `npm install` is a demo that fails on someone else's laptop.
2. **It must render on an empty database.** A fresh install is a legitimate state, not an error page. Pinned
   by `tests/test_api.py::test_every_page_renders_on_an_empty_database` (17 walked pages render; the cold
   `/readyz` is a deliberate 503, because "up" and "usable" are different claims). The walk list itself is
   derived from the router by `test_the_walk_list_matches_the_router` — `/twin` had been missing from it while
   sitting in the nav bar, which is exactly how a page goes untested.
3. **Numbers are rendered, never restated, and never fetched.** Templates read `twin.db.stats()` and the solver's dict. `/eval`
   reads `EVAL_REPORT.json` from disk; if the file is missing the page says so and shows nothing rather than
   quoting last run's figures from a template string.
4. **Anything the system could not verify is displayed as a question**, with the same visual weight as a
   fact — because a greyed-out "unverified" note is a note nobody reads.

## Page map

```
                    ┌──────────┐
        ingest ───▶ │  sources │  artifact, kind, trust prior, purge state, quarantined blocks
                    └────┬─────┘
                         ▼  (verify → promote → supersede → conflict)
   ┌─────────┐    ┌──────────┐    ┌───────────┐    ┌─────────┐
   │  queue  │◀───│  claims  │────│ conflicts │───▶│ lineage │
   └────┬────┘    └────┬─────┘    └─────┬─────┘    └─────────┘
        │ answers       │ receipts       │ open questions
        ▼               ▼                ▼
   ┌─────────┐    ┌──────────┐    ┌───────────┐    ┌─────────┐
   │ actions │───▶│timeline  │───▶│feasibility│───▶│  risk   │
   └─────────┘    └──────────┘    └───────────┘    └─────────┘
        human-approves only, never sends            proven vs predicted, split tables
```

`/` (cockpit) is the only page that shows mode; `/settings`, `/readyz` and `/privacy` repeat it, because the
mode is what licenses every number on the other pages.

`/docs` is the repo's own documentation page, and `create_app(docs_url="/api/docs",
openapi_url="/api/openapi.json")` is what makes that possible: left alone, FastAPI would own `/docs` for its
Swagger UI and the app's page would be shadowed by a framework default. A test asserts
`app.routes["/docs"].name == "docs"`, because the shadowing failure is silent — both pages would render 200.

## The five states a row can be in, and how each looks

| ledger state | UI treatment | why |
|---|---|---|
| `grounded` | green pill + "claim #id → source #id" link | the link is the point; a colour without a target is decoration |
| `grounded_relative_ambiguous` | amber pill, "22 Sep, no year stated" text | the system knows the date *shape* is fine and the *value* is not |
| `rejected` | red pill, kept visible with its quote | a rejected claim is evidence about the source, not noise |
| superseded (`valid_to` set) | struck through, next to its replacement, both drawn | history must be readable or "current" is unfalsifiable |
| unverifiable (raw text purged) | grey pill, `unverifiable` count on /privacy and /metrics | purging text must not silently convert a checked fact into an unchecked one |

## Two widgets carry the whole argument

- **`badge(kind, text, tone)` / `pill(state)` / `stat(label, value, note)`** are Python functions registered
  on `templates.env.globals`, not Jinja macros. A macro must be imported in every template; a forgotten import
  renders a *blank cell* with HTTP 200 — measured during this build on 4 pages of a trust dashboard. A global
  raises or prints, and printing is the failure mode you can actually see in review.
- **`{{RENDER_MS}}`** is substituted after rendering, in `page()`, so the footer's number is measured on the
  request you are reading (14–40 ms in practice). A footer that says "fast" is marketing; a footer that says
  18 ms is a claim you can falsify with `curl -w`.

## Per-page notes worth defending

- **Cockpit.** The first line is the *mode*, not the greeting: `ALIBI · mode=rules_only · cloud=off ·
  simulated=True`. If a number below it came from a deterministic simulator, you know before you believe it.
  `sync.claims` is `COUNT(*)` for this run, which is why a re-sync correctly reports `claims: 0` — a banner
  that says "34 new claims" every time is a banner nobody trusts twice.
- **Claims.** A filter form of GET selects (no POST, no JS) whose options come from `seen_kinds`, so the page
  can only ever offer predicates the ledger actually contains.
- **Conflicts.** Each card shows both quotes side by side, the precedence rule that decided it, and the
  review item that answers it (`review_id` from `explain_conflict()`). The number that used to sit here alone
  was a badge; a badge with no question behind it is the "system noticed and told nobody" failure.
- **Queue.** One item per source per outage, never one per block (12 copies of the same question is how an
  inbox stops being read). Every card states what the human's answer changes downstream.
- **Feasibility.** The solver's core (`no_miss`) is printed *before* any remedy, and a remedy whose
  cost is `violates_your_sleep_floor` is labelled with that rather than being silently dropped. The point of
  the page is not "here is a plan", it is "here is why this week cannot work".
- **Risk.** `PROVEN_INFEASIBLE` and `AT_RISK` are separate tables with separate headers, because
  "p≈1.00 already 15d past its due date" is a *prediction* with a calibrated number attached and a solver
  result is not. Mixing them is how a dashboard convinces you of something it did not prove.
- **Lineage.** The SVG is generated from the same rows `/api/graph` returns, so the picture and the data can be
  diffed by anyone. Refused supersessions are drawn with their `why`, because a system that hides its
  rejections hides half its behaviour.
- **Eval.** Four variants, one control, and the shipped no-model floor (`rules_line`) rather than the oracle
  number. `gold_plan_correct: null` prints as "not measured", not as 0% or 100%.

## What is deliberately absent

Charts of "productivity", streaks, confetti, an assistant chat box, and any number a model produced about the
student's own diligence. The last one especially: a chat box is where an unverified sentence enters a plan
without passing through the verifier, and this UI's whole claim is that nothing does that.

## How to check the claims on this page

```bash
python3 scripts/smoke.py            # 17 pages cold, 17 seeded, empty cells fail the run, /static/* must serve
python3 -m pytest tests/test_api.py # cold 503, empty-DB rendering, docs whitelist, CSV shape, draft contracts
grep -c "{% macro" web/templates/*.html   # 0 — the macros/imports decision above
grep -rn "https\?://" web/templates/ web/static/ | grep -v "example\|#"   # empty: no origin-bound asset, no CDN
grep -c "\.innerHTML\s*=" web/static/alibi.js   # 0 — asserted by a test, not by eye (comments excluded)
```
