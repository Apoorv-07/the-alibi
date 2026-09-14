# FINDINGS — what building the two load-bearing pieces changed about the plan

I implemented and ran the two components the pitch depends on (the deterministic verifier and
the feasibility/unsat-core engine) plus the SQLite schema, before writing the final design doc.
Five of my own design claims turned out to be wrong or incomplete. All five are now reflected in
`00-PROJECT-ARCHITECT.md` §31. Everything below is reproducible in this directory:
`python3 -m pytest -q` (30 passed, 0.59 s), `python3 run_poc.py`, `python3 checks/check_ground.py`,
`python3 checks/check_link.py`, `python3 evaluation/harness.py`.

---

## 1. "Minimal unsat core" was over-sold. It collapses to one line on real instances.

**What I claimed:** the engine returns a minimal unsatisfiable core like
`{DBMS eligibility floor, 11.5h fixed timetable, 6h sleep floor, 19.5h of ≥10%-weight work}` —
four human-readable lines.

**What actually happened:** with a greedy deletion filter, the minimal core was **`['no_miss']`** —
i.e. "the only way to satisfy everything is to skip a submission". Every other constraint was
redundant *relative to that escape*. Mathematically correct (removing `no_miss` alone makes the
horizon feasible), useless as an explanation, and if you demo it, a judge sees one token.

**Fix (implemented):** ship **two** artifacts and label them differently.
- **`per_prefix` binding arithmetic** — computed deterministically from due dates, fixed timetable
  and the daily cap, *without the solver*: `due 2026-10-11: 6.0h needed vs 4.0h legal → short 2.0h`.
  This is what the UI, the email and the slide show.
- **the solver-derived minimal core** — a *guarantee*, not the narrative: it proves the number of
  available levers is exactly one, and that the lever is a policy-permitted date change rather than
  effort. Shown on the architecture slide, not in the paragraph.

That is a strictly better story than what I originally promised: "here is the arithmetic; and here
is a proof that there is no other way out."

## 2. A constraint solver will "solve" overload by deleting your deadlines. It must be refused.

The first version let tasks be `missed` at a grade-weight penalty. The optimizer's answer to a
crunch week was: *drop DBMS Lab 4 and OS A3, objective improves, FEASIBLE.* A green bar on a plan
where the student doesn't submit is worse than a red bar — it is the same failure mode we are
selling against, wearing a solver.

**Fix (implemented):** `allow_miss=False` is the default; feasibility requires that no graded item
is silently forfeited. `test_a_plan_that_omits_graded_work_is_not_accepted_silently` proves the
model *does* cheat when the flag is on, so the flag is load-bearing and tested rather than assumed.

## 3. "No extension granted" must not be a baseline constraint, or the engine hides the problem

`ext_*` flags were in the default-active constraint set. The model therefore silently assumed the
professor had already said yes, reported FEASIBLE, and the demo's entire thesis evaporated.
Extensions are **remedies we are allowed to ask for**, not state.

**Fix (implemented):** `active_constraints()` deliberately omits `ext_*`; each remedy is evaluated
against a baseline where every extension is off. `test_granting_only_the_cited_extension_restores_feasibility`
asserts the exact shape of the product's claim:

> INFEASIBLE as things stand · FEASIBLE if, and only if, that one policy-permitted date moves.

Related and worth saying in the demo: `raise_work_cap` (+60 min/day = working 5h/day instead of 4h)
was **evaluated and shown not to fix this instance**, while +120 would. So the remedy list reads:
extension (policy-permitted, works) → work longer (violates your own sleep floor, works at +2h) →
*and the engine still ranks the extension first even when both work* (`cost_class`). The tool
refuses to solve overload by trading the one thing the evidence says not to trade
(≈−0.07 GPA/hour below ~6h; Okano 2019 / CMU 2023).

## 4. The ledger silently **discarded** the losing side of a disagreement — the precise bug this product exists to prevent

`upsert()` retired an existing claim *or* dropped the new one. With two sources disagreeing on the
same day (syllabus `11 Oct`, portal `13 Oct`) and the tie-break preferring the higher-trust source,
the portal claim was never stored. Result: **no conflict row, no alert, and a ledger that looks
reconciled.** Found in 4 lines of pytest, on the first run.

**Fix (implemented):** supersession only when *strictly later recorded*; otherwise both claims stay
open and `conflicts()` surfaces it. `test_disagreeing_claims_coexist_rather_than_being_dropped`
guards it. Corollary design note: supersession keys on **recorded_at (when we learned it)**, never
on the asserted date — a re-uploaded stale PDF that mentions a later date must not override a fresh
correction (`test_a_later_observation_supersedes_and_keeps_the_receipt`).

## 5. A nullable column made the idempotency key vacuous — the schema was smoke-tested, not read

`UNIQUE (subject_type, subject_id, predicate, source_id, char_range)` with `char_range TEXT NULL`
never fires in SQLite (NULL ≠ NULL). Re-ingesting the same 6 documents on Tuesday would have
duplicated the entire ledger and made `per_prefix` report phantom overload.

**Fix (implemented):** `char_start`/`char_end INTEGER NOT NULL DEFAULT -1` in the key; plus
`CHECK (length(evidence_span) >= 12)`, so a 5-character "receipt" is rejected **by the database**,
not by a prompt. Also: `capacity_signal.no_export CHECK (no_export = 1)` means an exportable
health row is unrepresentable — the privacy boundary is structural.

## 6. Relative-date handling: reject-vs-confirm is a real behavioural choice

`"bring it Monday"` has three defensible referents from a 22 Sep anchor. Two wrong answers are
possible: silently accepting (hallucination laundering) and silently rejecting (the deadline
vanishes — the *omission* failure ExtractBench shows is more common than fabrication). The verifier
now returns `relative_date_needs_confirmation`, which routes to the human queue with the candidate
set attached: one tap for the student, and `→ confirm` visible in the trace.

## 7. Verified soundness statement for the planner (say this if challenged)

Preemptive, capacity-per-day relaxation. **INFEASIBLE here ⇒ impossible for real.** FEASIBLE here is
necessary, not sufficient — the block-legal plan is emitted separately and re-checked. The model
errs in the one direction that matters: it can never bless a plan you cannot execute. Precedence is
modelled at day granularity, which is *also* conservative. This is a trade, not a claim of exactness;
stating it precisely is what makes the "why not just ask the LLM" answer land.

## 8. The verifier's two guards did not stop injection. A third, uglier guard does.

**What I believed:** predicate allowlist + verbatim quote = an injection payload cannot become
trusted state, because (a) it has no legal predicate to name and (b) a fabricated quote fails.

**What the router run showed:** the payload quoted the *real* chat line and named
`schedule_change`, an allowed predicate. It was **PROMOTED**. Both guards passed, because grounding
answers "did the model copy the source?", not "is this text safe to act on?" — and those are
different questions. A claim that is harmless inside the ledger is harmful one hop later, when a
drafting model reads the ledger as context.

**Fix (implemented):** screen the *value content*, not just the predicate name —
`alibi/ground.instructional()` rejects imperative-shaped or "this supersedes instructions" text
under any predicate (`instructional_value_refused`), and the router quarantines it
(`injection_suspect`) instead of promoting. Real corrections still pass: `"no lecture in 101, moved
to 105"` is grounded because it is a statement about the world, not a command to a reader. Four
cases are now a regression test and a harness section, and the answer to "what about prompt
injection?" is a table of measured outcomes instead of a design intent.

**Honest limit:** this is a belt, not a wall. The wall is architectural: the drafting prompt receives
typed claim objects with no free-text directive slot, and Tier-1 actions need a human tap. If a judge
asks what happens when the screen is defeated — "the text reaches a draft I approve, it never reaches
a portal" is the true answer, and it is why the UI shows the quote.

## 9. Three date-parsing bugs, all invisible to unit tests, all caught by the eval corpus

1. `MONTHS.get("09")` → numeric months (`18/09/2026`, `26.09.2026`) returned **no date at all**.
   That is the single most common written form on Indian portals and ERP screens.
2. `d-m-y` read the hour as a year: `"9 Oct, 10:00"` produced **2010-10-09**, and the 2-digit year
   folder happily accepted it. A fabricated *far-past* date is the worst possible payload for a
   conservative ledger, because it beats every real date under `earliest_safe`.
3. `\s+` matched across newlines, so `"…says 30th."` + the next line's `"form says 24.09"` fused
   into a third date in a chat export.

Fixes: a `_mo()` resolver accepting words *or* digits; a year token that is 4 digits or 2 digits not
glued to `:`/`.`/digits; `_WS1` (horizontal space only) in every multi-part pattern; a plausibility
window (anchor ±4 years) in `_safe()`; and `dates_in` now raises `TypeError` if handed a `date`
where a year belongs — because `f"{date:04d}"` is legal Python that renders the literal `'04d'`,
which is what had been poisoning every candidate in my own probe.

**Lesson for the doc's claims:** "the verifier is deterministic" is not the same as "the verifier is
correct". Determinism without a corpus is just a reproducible bug. Every one of these survived
22 passing unit tests, because every fixture used `2026-10-11T23:59` — the one format the fast path
handled.

## 10. Supersession-by-recency let a class rumour retire the syllabus

My ledger retired any older claim whose `recorded_at` was earlier. In a student's life that is
exactly backwards: the authoritative artefact (the syllabus, read once at enrolment) is *permanently*
the oldest record, and the newest is a class-group "prof said maybe Monday". Recency alone silently
overwrote the document with the rumour, and — because the plan is built from the ledger — nobody saw it.

This surfaced as a collapse, not a crash: reconciliation fell from 9/9 to 5/9 the moment chat claims
were attributed correctly. Fix: recency decides **within** a trust tier, and a newer claim may only
overwrite when `trust_prior ≥ supersede_trust_floor` (default 0.5, i.e. email-thread or better).
Below it, both claims stay live as a receipted conflict and `earliest_safe` plans against the earlier
date. The event is recorded (`conflicts_found`), and a conflict whose weaker source *attempted* an
overwrite escalates severity LOW → MEDIUM: someone is already acting on the rumour, which is precisely
when a nudge prevents a missed deadline. A strong later source (HOD email) still supersedes, or the
rule would just be "never update".

## 11. Over-attribution is not noise, it is corruption — and I had it in my own harness

Attributing every date on a line to every task on that line emitted **25** claims against **14**
correctly-linked ones, and reconciled **2/9** instead of **9/9**. The intuition "more recall is
forgivable, the reconciler will sort it out" fails here for a specific reason: the fail-safe policy
picks the *earliest* date, so an extra wrong-early date always wins. A conservative policy is
corrupted by exactly the class of error that over-extraction produces.

Two consequences now written into the code:
* `alibi/link.py` is a first-class stage, not a nicety: pair by proximity **within one document**,
  and refuse to guess when two obligations tie (the tie becomes a review item, and the corpus has a
  real one: `Internal Assessment 2 … 15 Oct` in a merged three-PDF view).
* attribution scope is **per-message for chat, per-block for documents**. Clustering chat for the
  model is right ("bring it monday" is meaningless alone); clustering for attribution is wrong —
  a 12-message cluster puts every date beyond the link radius and silently drops the claim, which
  is how my harness lost the demo's central contradiction.

## 12. My own control experiment was invalid, and the fix strengthens the claim

I wrote "the same workloads are FEASIBLE once the safety constraints are dropped (0/5)" while the
control disabled *everything including the daily work cap* — so the comparison measured nothing and
contradicted the sentence above it. Rewritten properly: keep `due_*`, `cap_*`, `released_*`,
`precedence`; drop only the student-waivable protections (`no_miss`, `review_*`). Now **5/5** flip
from INFEASIBLE to FEASIBLE, i.e. the planted weeks are infeasible *because of the protections*, which
is what lets the message be "your attendance rule makes this week impossible" rather than "you have too
much to do" — different advice, different remedy, and the reason the unsat core is reported at all.

## 13. The corpus and the gold set disagreed; the corpus was wrong

Two planted contradictions had no sentence in any artefact that actually stated them
(`dbms_lab4` had no "12 Oct" message; `sih_reg`'s chat line was mid-continuation-fold). An eval set
where the gold answer is not in the input measures nothing. Fixed by adding the two chat lines
(`ok the portal for DBMS says 12 Oct now for lab 4…`, `OS Assignment 3 due date is 13 Oct 2026 on the
LMS, syllabus is old`) and by re-deriving `expect_for` from the planted values rather than assuming the
corpus. Rule for the 12-hour build: **every gold row must be quotable from an artefact, and a test must
say so.**

---

## Measured headline (this sandbox, no model calls)

| metric | oracle (with `alibi.link`) | naive attribution | no-model rules only |
|---|---|---|---|
| claims emitted | 14 | 25 | 2 |
| verifier pass | 1.0 | 1.0 | 1.0 |
| reconciled 9 gold tasks | **9/9** | 2/9 | 0/9 |
| planted conflicts found | **4/4** | 4/4 | 0/4 |
| infeasible weeks flagged | **5/5** | — | — |
| …of which safety-binding | **5/5** | — | — |
| …with one honest remedy | 2/5 (3/5 correctly "no remedy") | — | — |
| injection payloads promoted | **0/4** | — | — |

---

## 14. What is still unproven here (and what it will cost)

| Unknown | How to settle it | Budget |
|---|---|---|
| `qwen3.5:4b` Q4 extraction accuracy vs cloud on photographed syllabi | run `evaluation/harness.py` three ways on your 60-item gold set; ship Q5_K_M if Δ > 3 pp | 90 min |
| OCR quality on a real hostel-lamp photo | 10 real photos from one classmate | 15 min |
| 0/5 vs 5/5 "ChatGPT returns a confident impossible plan" | plant 5 overloads, ask 3 assistants for a plan, diff against solver ground truth | **60 min — highest-value hour of the day** |
| ICS read-back as a zero-OAuth audit path | paste a real Google Calendar secret-ICS link, parse, diff | 30 min |
| Live Telegram approve round-trip | one bot, one chat-id allowlist | 20 min |

---

# Second pass (after the UI, the web app, and the infra existed)

Everything above was learned while building the core. The nine findings below were learned while wiring that
core to something a human clicks on — and every one of them is a bug **the unit tests could not see**.

## 15. The ledger's own UI was the one component that could lie about the ledger.

`POST /api/sync` reported `claims: 34` on a first sync of the demo corpus while the database held **18** rows,
because the counter summed per-report `rule_claims + promoted` — a rule claim is *also* promoted, and
de-duplication happens below both numbers. On a re-sync the same code reported `claims: 0` (idempotency
masked it). So: **every figure rendered from a run must be a query over the rows that run wrote**
(`… WHERE run_id=?`), never an accumulation of what the components said they did.
`tests/test_api.py::test_the_sync_banner_reports_what_the_database_actually_holds` pins 18 = 18 and 0 = 0.

## 16. "De-duplicated to nothing" is not "found nothing" — and the difference invented 8 claims.

`_ingest_block` returned early on `if n:` where `n` was the count of claims *created*. On the second read of
identical bytes `n == 0`, which read as "the rules found nothing, hand it to the model". The model path then
re-invented the block: 16 → 24 claims with keys like `os-submitted_work_3_cpu`, all `method='rule'`, all
`char_start=0`, all real. Fix: `taskfacts.facts()` returns a third value — how many facts were **read before**
de-duplication — and callers branch on that. Lesson kept: never let a caller infer "nothing" from the length
of a filtered list.

## 17. A normalised field that stays caller-visible makes a second read behave differently.

The adapter maps `whatsapp|telegram|discord → chat_export` in `db.KIND_MAP`, but `IngestReport.kind` kept the
caller's raw string. Anything downstream branching on `rep.kind` therefore saw `whatsapp` the first time and
`chat_export` the second — the actual trigger for #16's cascade. `ingest_text` now re-reads the canonical
kind from `db.source_info(sid)`. Two rules: canonicalise at the boundary, and if a normalised value is
visible to callers, make it the normalised one.

## 18. Verification passing is not the answer being right (re-measured, and now a permanent control row).

The `naive_attribution` control — "attribute every date in a document to every task in it" — emits 25 claims,
passes the verifier at **1.00**, has 77.8 % correct gold dates, and reconciles **2/9** tasks. This is the
whole argument of the project in one row: a pipeline where every part is individually checked can still be
systematically wrong, because *attribution* was never part of the check.

## 19. A crash wearing a status message is worse than a crash.

`explain()` indexed `w["days"][0]`; a deadline at or before the horizon's first day (`os-ia_1`, due
2026-08-28, today 2026-09-12) has no legal days, so `IndexError` → the UI said "solver did not run" → the
`UNKNOWN` branch printed a forecast gap that did not exist. `per_prefix` now emits a first-class
`{"days": [], "deadline_before_horizon": true, "slack_min": -need}` entry and `explain()` has a branch for it.
Related trap: `analyze()`'s `except` returns `{"reason": f"solver did not run: {e}"}` — so *any* exception
inside the solver is reported to the user as a missing forecast. If a bug you add is invisible, put it in
that handler.

## 20. Absence of evidence must not become an assertion — in either direction.

A task whose due date has passed with no submission on file: excluding it from the horizon makes the alarm
disappear exactly when it is most needed; asserting "you missed it" is the assistant inventing a fact out of
silence. Both are wrong. Now: `submitted` / `submitted_at` are first-class predicates in
`router.ALLOWED_PREDICATES`, a recorded submission retires the obligation from planning, and without one the
head-line reads *"…and 'no submission on file' is an absence of evidence, not proof that it was missed:
confirm it or add the receipt."*

## 21. A draft letter quoted my guess as the student's agreement.

`cost_pct = round(remedy_days * 10, 1)` — 10 % per day, my invention, printed in the one artefact a student
signs their name to. It is now read out of the stored `late_policy` claim's own text; when no source states a
penalty, `cost_pct` is `null`, `penalty_stated: false`, and the letter says it is not assuming one. The same
route then printed *"the remaining work (? min) … shortfall is ? hours"* because it never passed the solver's
numbers to the template — a `?` in a letter to a professor is the assistant admitting in writing that it did
not check. The request now resolves `minutes_left`/`shortfall_hours` from the same `per_prefix` window the
plan page uses, and refuses outright (409) when the grounded claims carry no date to move. Note that
`late_policy_default.pct_per_day = 0.0` exists in the policy and is deliberately **not** a fallback here:
"no penalty" is as invented as "10 %".

## 22. `/privacy` had been promising a retention job that did not exist.

The page text said raw text is dropped after 30 days and the schema comment said *"purged by the retention job
(see alibi_retention)"*. There was no `alibi_retention`, no CLI command, nothing. Writing `Twin.run_retention()`
immediately found a second bug: my `now and date or (today - days)` expression treated the caller's *today* as
the *cutoff*, so a replay with `--now` purged everything while printing a plausible window. Two rules:
a retention mechanism is not a documented intention, and any job that deletes must be reported in the ledger
(`change_event kind=RETENTION_RUN`) — plus `stats()["unverifiable"]` must count what can no longer be
re-checked, so a purge can never turn into a clean bill of health.

## 23. A hand-written test list quietly shrinks the thing being tested.

`scripts/smoke.py` walked 16 pages while the app had 17 GET pages: `/twin` was in the nav bar and never
rendered by the harness — so "16/16 pages render" was a true statement about a smaller app. The walk list is
now derived from the router by `tests/test_api.py::test_the_walk_list_matches_the_router` (with `/docs`
handled explicitly, since the app's page shadows FastAPI's Swagger default at that path). Same logic applies
to any list of "everything": if the list is maintained by hand, the coverage claim is about the list.

## 24. A config key the code ignores is worse than a missing key.

Three found in one pass: `ALIBI_MAX_DOC_BYTES` was read into `Config` and used nowhere (`/api/ingest`
hardcoded `4_000_000`); `docker-compose.yml` set `OLLAMA_URL`, which no code reads (`OLLAMA_BASE_URL`, or the
adapter's `OLLAMA_HOST` fallback); and `Config` derived its DB path only from `DATABASE_URL` while the CLI and
server used `ALIBI_DB` — so inside the container `/privacy`, the budget table and the wipe verification
reported on `/app/alibi.db` while the ledger lived in `/data/alibi.db`. Every one of those is a dashboard
describing a different system than the one the user is using. All three are fixed, and
`.env.example` was rewritten to list only variables the code actually reads.

## Measured headline, second pass

`python3 -m alibi.cli sync` on a fresh database (this sandbox, no model, no network):

```
ALIBI · mode=rules_only local=regex-extractor-v1 cloud=off
  sources 6 · claims 18 · grounded 18 · in review 0 · superseded 0
  conflicts open 3 · reviews 3 · actions pending 0
  model calls 32 (local 32, cloud 0, 0 bytes out) · invalid outputs 0
  FALSE TRUST: 0 / 18 grounded = 0.00%   (unverifiable: 0)
  tasks tracked 10 · 31 change(s): 18×new requirement, 8×feasibility status changed, 3×date changed
```

Re-running the same sync: 18 claims, `promoted=0` everywhere, `rejected=2` (the injection blocks re-detected
and re-quarantined, no new rows). `python3 -m pytest tests/ -q` → 125 passed. `python3 scripts/smoke.py` →
17/17 pages render cold and seeded, `/readyz` 503 before seeding and 200 after, `false_trust_count=0`,
`cloud_bytes=0`, `/api/doc/..%2F..%2Fetc%2Fpasswd` → 404.

Four-variant table (`EVAL_REPORT.md`, `evaluation/harness.py` — the shipped no-model floor is `rules_line`,
*not* `rules_only`, which measures the old row parser):

| variant | claims | verifier pass | reconciled | conflicts | recall | ms |
|---|---|---|---|---|---|---|
| `oracle` (gold spans, no model) | 14 | 1.00 | **9/9** | 4/4 | 0.93 | 9 |
| `rules_line` (**what this repo does with no model**) | 10 | 1.00 | 5/9 | 3/4 | 0.71 | 10 |
| `rules_only` (old table-row regexes) | 2 | 1.00 | 0/9 | 0/4 | 0.00 | 1 |
| `naive_attribution` (the control) | 25 | 1.00 | 2/9 | 4/4 | 1.00 | 4 |

The gap between `rules_line` and `oracle` is the honest price of refusing to guess: 4 claims, 4 gold
obligations, 1 planted conflict. The gap between `oracle` and `naive_attribution` is what refusing to guess
buys: 9/9 against 2/9 on the same verifier with the same pass rate.

---

# Third pass (LM Studio, the client layer, and a database I broke myself)

The brief changed shape: the AI runtime must be **LM Studio**, configured by environment variables, degrading
gracefully; and the UI must not read like a CLI wearing a stylesheet. Both were implemented. Eight more
findings, one of them about a corrupted database.

## 25. The documented LM Studio URL could not work.

`https_connection()` was named for the cloud case and always built an `HTTPSConnection`, so
`http://127.0.0.1:1234/v1` — the exact string LM Studio prints in its own UI — failed with
`SSL: WRONG_VERSION_NUMBER`, which surfaced as `ModelUnavailable` and looked like "the server is down".
Fixed by deriving TLS from the address and honouring an explicit scheme (`wants_plain_http`). Second bug in the
same file, found at the same time: `OpenAICompat.extract` refused to run without an API key — right for Groq,
wrong for a loopback server — and its message was the literal `no api key in ${'KEY'}` instead of the variable
name. `require_key` is now tied to loopback-ness. **Rule learned: a "local" integration must be tested against
a fake local server**, because the failure modes are transport-level and no unit test of my own code would
ever see them. `tests/test_providers_and_time.py` now runs a real `HTTPServer` on an ephemeral port and asserts
five of its behaviours.

## 26. `_classify` returned the exception instead of raising it.

Its docstring said "raises ProviderError/QuotaExceeded on failure"; the code did `return ProviderError(...)`,
so `st, data, raw = _classify(*_post(...))` died with `TypeError: cannot unpack non-sequence ProviderError`.
Every server-side error (LM Studio OOM, a 502 from a proxy) would have presented itself as a crash in my
transport rather than as what it was. Found in the same lines: `_post`/`_get` return `(status, headers, body)`
while `_classify(status, raw, hdrs)` reads `(status, body, headers)` and every caller unpacks with `*` — a
silent positional swap that only surfaces when the body is not JSON (an HTML error page). Fixed by raising,
and by normalising on *type* rather than position, which a future caller cannot re-break.

## 27. A config value the transport ignores is a privacy bug, not a UX bug.

`is_loopback(host)` — not a provider label, not a variable name — decides whether a payload is redacted and
budget-metered, so `http://192.168.1.9:1234/v1` (a roommate's laptop on the college LAN) is treated as cloud:
`REDACTED_CLOUD`, counted, capped. Pinned by
`test_egress_is_decided_by_the_address_not_the_variable_name`. Two related bugs: `Budget.load()` raised
`no such table: budget` on an in-memory database, because `os.path.exists(":memory:")` is not the guard it
looks like; and a read-only volume would have turned a successful extraction into a 500 while persisting
nothing. Both now degrade and say so in `/api/health` instead.

## 28. Half of my own status page was a constant, and the other half described a different database.

`/api/health` started life as a hand-written dict with `True` values — the thing this project exists to
refuse, in the place you go to check for it. It is now `AppState.health()`: eight probes, each a COUNT, a
live provider query or a solver call, and **the cockpit renders that same dict**, so a chip and the endpoint
cannot disagree. Writing it exposed two more bugs: it printed `twin.cfg.db_path.name`, which was `alibi.db`
while the open file was `/tmp/ci.db` — `Twin.open(path)` never propagated the path into `Config`, so the
**budget table persisted to the wrong file too**, meaning a restart reset the day's cloud spend; and I wrote
`stats()["db_readable"]`, a key that does not exist, which a reviewer would have found in one command. Fixed:
one path in one object, and the DB probe is a real `SELECT 1`.

## 29. `keep_own` was a promise with no code behind it, and a status value made a row invisible.

The queue's "use my answer" wrote `review_item.resolution` and nothing else, under the sentence "your answer
is recorded as the source of truth" — the plan went on using the value you had just corrected. It now writes a
claim (`method='manual'`, its own verbatim quote from a `user_note` source, a `MANUAL_FACT_RECORDED` change
event), refuses an answer shorter than the ledger's own 12-character receipt floor, and **does not resolve an
ambiguity by sorting**: "it is 17 Oct, not 18 Oct" returns both candidates and leaves the question open.
Writing that test exposed a second bug: `resolve_review` set `status='manual'`, which no view filtered on, so
an answered review vanished from the open queue *and* never appeared under "recently answered". `status` is
now one of the four values the schema's CHECK allows, with the verdict in `resolution`.

## 30. I corrupted the repository's own database, and the next migration would have shipped broken.

To widen two CHECK constraints I wrote a migration that renamed `claim` aside. Two things went wrong. (a)
`DB.migrate()` ran `executescript` **outside** a transaction and recorded `applied_migrations` only on success,
so a file that failed half-way left the database holding `claim__pre0003` with the migration *recorded*:
unopenable, unrecoverable, and re-corrupting on every start. (b) `ALTER TABLE … RENAME` rewrites every
**inbound** foreign-key clause too, so `claim_evidence`, `task` and `conflict` ended up pointing at the
dropped name — invisible while `PRAGMA foreign_keys` is off at runtime (which it is, by pysqlite default), and
fatal to the next rebuild. Both are fixed properly now: one migration per transaction with rollback, a
`PRAGMA foreign_key_check` after each, a statement splitter that avoids `executescript` (it implicitly commits
first, which makes any BEGIN/ROLLBACK around it a lie), and a migration that rebuilds the children as well as
the parent, collapsing the pre-existing span-duplicates the new index forbids
(`test_0003_collapses_span_duplicates_without_losing_history`).

**The honest part:** recovering my dev database was `rm alibi.db && make seed`, and that is survivable *here*
only because the ledger is derived from a corpus inside the repository. In a user's installation it would be a
data-loss event. A schema change whose only undo is deleting data should have been a shadow column — this
repository already has that pattern (`source_meta.orig_kind` beside `source.kind`'s CHECK list) and I did not
reach for it. Recorded so the next person widening a constraint starts from the right answer.

## 31. The "no JavaScript" claim was a limitation wearing a principle.

Layer 1 (inline CSS, server-rendered HTML, `{{RENDER_MS}}` measured on the request) was justified as
offline-robustness. It was also, unexamined, a reason to ship a UI where a rejected answer and a successful
one looked identical, because every form posted and left you with a JSON blob. The fix is not a framework, it
is *layering*: `web/static/alibi.css` (~150 lines: depth, motion, state styles, responsive collapse, print)
and `web/static/alibi.js` (~230 lines: toasts that read the API's own `detail` sentences, a command palette
whose entries are scraped from the nav bar so it cannot drift from the app, busy-states on submit). Both are
served by this process; there is no CDN and no build. What keeps the principle intact is the test:
`test_the_page_works_with_and_without_the_enhancement_layer` asserts the numbers, receipts, filters, forms and
the health strip are in the HTML, that no asset URL is absolute, and that the client layer contains no
`.innerHTML =` or `document.write` — two rules enforced by CI rather than by eye.

## 32. A hand-written test list had already shrunk the product it claimed to cover.

The harness walked 16 pages while the app had 17 (`/twin` was in the nav bar and never rendered), and it asked
`/api/graph?task=os_3` — a subject id from *before* the attribution work — which returned an empty 200 that
the old assertion (`"edges" in json()`) accepted. Both are now structurally impossible:
`test_the_walk_list_matches_the_router` derives the expected pages from the router, and the graph check asserts
`nodes` **and** `claim_ids` are non-empty for a subject the ledger actually holds.

## 33. `make checks` did nothing, successfully.

There is a directory named `checks/`, so `make checks` reported "up to date" and exited 0 for the rest of the
session — CI would have been green while running no probes. Every phony-looking target is now declared
`.PHONY`. A Makefile target whose name matches a path is a target that can silently stop existing.

## Measured headline, third pass

`ALIBI_DB=/tmp/verify.db python3 -m alibi.cli sync` → 6 sources · **18 claims** · 18 grounded · 3 open
conflicts · 3 reviews · 32 model calls (all local, **0 cloud, 0 bytes out**) · **false trust 0 / 18**.

`python3 -m pytest tests/ -q` → **147 passed**. `python3 scripts/smoke.py` → **SMOKE: PASS** (17 pages cold,
17 seeded, `/static/*` served, `/api/graph` non-empty, manual answer reaches the ledger, traversal → 404).
`make checks` → verifier probes 0 failures; attribution 43 blocks → 14 linked claims, 9/9 correct, 0
over-attributed; calibration unchanged. `make eval` → unchanged in substance: `oracle` 14 claims / 9/9,
`rules_line` (the shipped no-model floor) 10 claims / pass 1.00 / 5-of-9 reconciled / 3-of-4 conflicts,
`naive_attribution` 25 claims / pass 1.00 / 2-of-9.

With LM Studio emulated on loopback: `ALIBI_MODE=local` → `/api/health` says `ai: healthy`, promoted claims
carry `provider=local_chat` plus the model id in their receipt, `cloud_bytes` still 0. Kill the server →
`ai: degraded`, one `EXTRACTION_UNAVAILABLE` review per source naming the reason and the fix, rules-only
claims still promoted, `false_trust_count` still 0, every page still 200.

(§34–§35 measured, after the injection-screen rewrite and the conflict-dedupe migration: `pytest` **146
passed**, `checks/*` 0 failures + attribution 9/9 + calibration unchanged, `scripts/smoke.py` **SMOKE: PASS**
with 17/17 pages cold and seeded, `make eval` unchanged in substance, and three consecutive syncs of a fresh
database reporting an identical ledger — 18 claims, 3 conflicts, 1 quarantined source, 0 false trust.)
