# Architecture decisions

Every decision that shaped this codebase, with the alternative that was available and the price paid. Written
so a reviewer can disagree with a specific line rather than with a vibe. Where a decision came from a
measured failure in this repository, the failure is named with its number.

Conventions: **CHOSEN** / **REJECTED** / **COST**. `→ code` points at the file that enforces it, because a
decision that is not in code is a preference.

---

## A1. The model proposes; deterministic code proves

**Context.** The problem statement asks for an AI that manages a student's academic life. The naive build is
"LLM reads syllabus → LLM writes plan → LLM emails professor". Every failure mode in that chain (invented
date, invented policy, invented confidence) is invisible in a demo and catastrophic in use.

**CHOSEN.** The model has exactly one job: propose a *span* of text and a candidate predicate. Extraction
falls back to regexes. Verification, subject attribution, supersession, conflict detection, scheduling and
action gating are code. Nothing the model says is trusted because the model said it.

**REJECTED.** `function calling with a schema and a high temperature floor` — a schema makes malformed JSON
rare, not false; the value still has to be *found in the document*. Structured output was added (A-local
adapters) but as a parsing convenience, gated by `verify_claim`.

**COST.** Recall. The no-model path finds 10 of 14 date claims on the demo corpus (`rules_line`: 5/9
reconciled, 3/4 conflicts); the oracle finds 14. We buy the right to say "unsupported claims are prevented
from becoming trusted state", which is a stronger claim than any accuracy number.

→ `alibi/router.py` (routing), `alibi/ground.py:verify_claim` (the gate)

---

## A2. The ledger is rows, not prose

**CHOSEN.** SQLite, 19 tables, no ORM. Every fact is `claim(subject_type, subject_id, predicate, value_json,
source_id, evidence_span, char_start, char_end, verify_state, method, valid_from, valid_to, recorded_at,
run_id)`. The UI, the CLI, the solver and the exports all read the same rows through the same functions.

**REJECTED.** A JSON "memory" file per student (what an LLM-assistant build would do). It cannot answer "which
source said this, and what did it disagree with" without re-reading prose, and it cannot be re-verified.

**COST.** Migrations and constraint errors are my problem now. `INSERT OR IGNORE` turned a CHECK violation
into a silent empty ledger early in the build; `write_claim` now raises `ClaimWriteError` with the constraint
text and routes it to a review row.

→ `database/schema.sql`, `database/migrations/0002_twin.sql`, `alibi/db.py:write_claim`

---

## A3. A receipt, not a citation

**CHOSEN.** Every claim stores the verbatim `evidence_span` plus `(char_start, char_end)` and a
`claim_evidence` row naming the verifier, the checks that passed, the provider and model (if any) and the
model's confidence. `stats()["false_trust_count"]` re-verifies every trusted row against the source text on
demand and counts the ones that fail. That number is on the cockpit, the footer of /privacy, /metrics and
/readyz.

**REJECTED.** Citing a document name and page. A page number cannot be re-checked by a machine, which means
it cannot be *wrong* in a measurable way either — and unverifiable confidence is what this project exists to
remove.

**COST.** Retention pressure (raw text must survive long enough to re-verify; see the /privacy page and A24),
and a floor on quote length: a quote under 12 characters cannot distinguish "11 Oct" from "11 Oct 2027", so
thin quotes go to review instead of the ledger.

---

## A4. Newer does not mean stronger: bi-temporal supersession with a trust floor

**CHOSEN.** Two time axes: `recorded_at` (when we learned it) and `valid_from/valid_to` (when it was true).
Supersession is *within* a tier by recency, and *across* tiers only when the stronger source wins — with
`supersede_trust_floor = 0.5`. A class rumour cannot silently retire the syllabus; it opens a MEDIUM
escalation instead. Nothing is deleted: the retired row keeps its quote and gains `valid_to`.

**REJECTED.** (1) Recency alone: a WhatsApp message then overwrites a receipted LMS record, which is how
rumours become schedules. (2) Confidence ranking: the model's own confidence is not a trust signal — measured
1.0 on the simulator's outputs, i.e. useless.

**COST.** A rejected supersession still has to be visible, so /lineage draws both nodes and the edge, and the
UI has a word for "we considered and refused" (`why` on the edge).

→ `alibi/ledger.py`, `tests/test_twin_db.py::test_a_rumour_cannot_retire_the_syllabus`

---

## A5. Subject keys come from title shapes (`alibi/taskfacts.py`)

**Context.** The demo ran for a long time with `os-submitted_work_3_cpu` (syllabus) and
`dbms-dbms_says_12_oct` (the chat contradicting it). Every row was individually verified. Conflicts: **0**.
Two true statements about two different keys do not disagree, so the whole contradiction engine was silently
dead. The first fix attempt — "attribute every date to every task in the document" — is the
`naive_attribution` variant in the eval report: 25 claims, verifier pass 1.0, gold dates correct 77.8%, 2/9
reconciled. Verification passing and the answer being wrong is the central measured fact of this project.

**CHOSEN.** `task_key()` accepts only `<numbered kind> <ordinal>` (`LAB 4`, `IA2`, `Internal Assessment 2`) or
a bare unnumbered kind (`registration`, `term project`). Facts are claimed only when title, date and
deadline wording sit in one window: the line, widened by at most one neighbour, never onto a line naming a
different task. Relative dates are never resolved by rules. Two candidate dates in a line ⇒ no claim, one
review note.

**REJECTED.** (1) Slug-from-prose (`subject_id(block[:120])`) — the original bug. (2) Embedding the titles and
matching by cosine similarity: defensible for merge candidates, indefensible as the *only* key, because the
ledger would then hold a similarity score where a reviewer expects a title. (3) Letting the model name the
subject: it invents plausible tasks ("Submitted work 3 CPU"), and those rows verify.

**COST.** Recall on phrasings outside the shapes (a task named only as "the report" is not claimed), and one
honest note per refusal so the UI can distinguish "quiet" from "blind".

---

## A6. A grounded fact the model cannot attribute is a question, not a row

**CHOSEN.** When the LLM path produces a claim whose quote names no task, it becomes
`review_item(kind='UNATTRIBUTED_FACT')` and `rep.rejected`. It is never promoted under a block-derived slug.

**REJECTED.** "Promote with low confidence and let supersession sort it out." Confidence is not evidence: the
row would enter `earliest_safe` selection and corrupt the plan it disagreed with.

---

## A7. A numeric date must carry a year

**CHOSEN.** `12.10.2026`, `2026-10-12`, `18.09.2026` parse. `10.00`, `12.00`, `09:00–12:00` do not. Measured:
the permissive version produced a phantom `2026-12-00` from a reshuffled-class time range, and because the
extractor refuses ambiguous lines, the phantom *deleted a real deadline*.

**REJECTED.** Two-digit-year inference for bare `24.09`. In an intranet form it is a real convention; guessing
the year from the term context to gain one claim, when a missing date costs a review row and a wrong date
costs a plan, is the trade this project refuses.

---

## A8. `11 Oct` is a date; `20, no` is not

**CHOSEN.** Month-name patterns end in `(?![a-z])`, and `Oct 20` inside `12 Oct 2026` is refused by
`(?![\d:a-z])`. The shared abbreviation list contains `no` (November), so `\b` was satisfied by
"with your reg no" — measured as a fabricated 2 November in the DAA syllabus, which again killed the true
16 October by ambiguity.

**COST.** A scanned "12 Oct," followed by a comma-and-newline is fine; a genuine `12 Oct.` at end of line is
fine; a date glued to a punctuation mark the pattern does not allow would be missed. Every one of those
misses becomes a note, which is the point.

---

## A9. A grading summary is a table, not a sentence

**CHOSEN.** `IA1 15% · IA2 15% (Fri 16 Oct 2026) · Lab record 20%` is split on `·` and **each piece must name
its own task**. Whole-line matching runs first and wins when the line is not a table.

**REJECTED.** (1) Whole-line-only: `IA1` inherits `IA2`'s exam date (this happened, twice, via two different
"obvious" rules). (2) Split-always: orphan pieces ("Lab record 20%") inherit a stranger's task from the block
scope, and good single-clause lines like `Submission: Saturday 11 Oct 2026` lose their date. Both were
measured.

---

## A10. Every open conflict opens a review item

**CHOSEN.** `db.sync_conflicts()` inserts a `CONFLICTING_STATEMENTS` review per conflict, deduped on the
question text, and `explain_conflict()` returns its `review_id` so the /conflicts page can link straight to
the answer box.

**REJECTED.** A badge with no question behind it. That is the failure mode of every dashboard assistant:
"the system noticed" and nobody was asked, so nothing changed.

---

## A11. De-duplicate facts per (subject, predicate, source, value) — not per span

**CHOSEN.** `_promote` refuses a second identical value from the same source even when the character span
differs. A source that says the same deadline twice is not twice as true.

**REJECTED.** The schema's original key `(subject, predicate, source, char_start, char_end)`, which allowed
"13 Oct … not 12 Oct as the syllabus says" to create two claims for one chat message and quietly double the
weight of the loudest source. Different *values* still coexist as an open conflict — that is a different
mechanism (A4), not a duplicate.

**COST.** Two genuinely separate mentions of one deadline produce one row, so /lineage shows one receipt for
them; the trace records the suppression so it is not mistaken for a missing fact.

---

## A12. Re-ingest must not fall through to the model

**CHOSEN.** `facts()` returns a third value: how many facts were *read* before de-duplication. The ingest path
returns on `tf_raw`, not on `n_created`.

**REJECTED.** `if n: return`. On the second sync every fact is already on file, so `n == 0`, which read as
"the rules found nothing" and handed the block to the model. Measured on re-ingest of the demo: **16 claims →
24**, including eight junk keys (`os-submitted_work_3_cpu` etc.). A test now pins idempotency
(`test_re_ingest_is_still_idempotent_after_attribution_changes`).

---

## A13. One review per source per outage, not per block

**CHOSEN.** `EXTRACTION_UNAVAILABLE` is opened once per source per run (`rep.unavailable_flagged`).

**REJECTED.** One per unread block: 12 blocks of one dead Ollama became 12 identical questions, which is how a
review inbox stops being read at all.

---

## A14. The offline path is `LOCAL`, never `NONE`

**CHOSEN.** `DeterministicSimulator.egress = "LOCAL"`. `NONE` is reserved for calls refused before a payload
existed, and `bytes_out = 0` is what carries the privacy claim.

**REJECTED.** `egress = "NONE"` for the simulator — it made "local calls: 0" and "model calls: 32" disagree on
the same screen, i.e. a truthful dashboard showing a contradiction.

---

## A15. The external-action gate is a deny list that outranks the policy

**CHOSEN.** `send_email`, `submit_assignment`, `delete_submission`, `drop_course`, `pay_fee` are REFUSED even
when `policy.action_gate` says `AUTO_OK`. `forecast/create_event/plan_change` are AUTO_OK; `draft_email`
requires approval and shows the exact bytes. The app drafts; the human sends.

**REJECTED.** "Confidence-graded autonomy" — a model with 0.95 confidence on a forged portal PDF is the attack,
not the exception.

→ `alibi/actions.py`, `tests/test_core.py` (gate matrix), measured `draft → APPROVAL_REQUIRED`

---

## A16. The remedy's price comes from the cited clause

**CHOSEN.** The draft extension email's "−10% per day" is read out of the stored `late_policy` value, and when
the policy is absent the letter *says* it is absent ("the ledger does not record a late-submission clause … I
am asking rather than relying on it"). `cost_pct` is `null` rather than a default.

**REJECTED.** `cost_pct = days * 10`, i.e. the developer's guess about the professor's penalty, printed in a
letter a student signs their name to.

---

## A17. A past due date with no submission is not a missed deadline

**CHOSEN.** `submitted`/`submitted_at` are ledger predicates; a task with one leaves the planning horizon.
A task without one stays, and the explanation reads: "'no submission on file' is an absence of evidence, not
proof that it was missed: confirm it or add the receipt."

**REJECTED.** Excluding past-due tasks (the alarm disappears when it is most needed) and asserting "you missed
this" (the assistant inventing a fact from silence — the exact thing A1 exists to prevent).

**COST.** One extra predicate in the allowlist, and a solver window that can be provably empty (see A18).

---

## A18. An empty planning window is a first-class result

**CHOSEN.** `per_prefix` emits `{"days": [], "deadline_before_horizon": true, "slack_min": -need}` instead of
an empty list. `explain()` has a branch for it.

**REJECTED.** Letting it raise: `IndexError: list index out of range` inside the solver was reported to the
user as "solver did not run", which is a crash wearing the costume of a missing forecast. Both the message and
the counters are now correct, and the case is a normal part of the demo (the Aug IA1 deadline).

---

## A19. The eval report is rendered from disk, never restated

**CHOSEN.** `/eval` reads `EVAL_REPORT.json` produced by `evaluation/harness.py` on this repository. If the
file is missing the page says so and shows nothing. `gold_plan_correct: None` prints as "not measured".

**REJECTED.** Copying numbers into a template. A dashboard that can display an unbacked figure is the failure
mode the whole project is a reaction to — including in its own pitch.

---

## A20. The no-model floor is measured as its own variant

**CHOSEN.** `harness.py` runs four extractors through the same scoring path: `oracle`, `rules_only` (table-row
regexes), `rules_line` (the shipped `taskfacts` path, no model, no oracle) and `naive_attribution` (the
control). Measured: `rules_line` 10 claims, verifier pass 1.00, 5/9 reconciled, 3/4 conflicts found.

**REJECTED.** Quoting "rules_only = 2 claims" as the product's floor. `rules_only` measures the *old* row
parser; claiming it as the shipped behaviour would understate the system, and claiming the oracle number
would overstate it. Both are lies a reviewer can catch in one command.

---

## A21. UI: two layers, one truth — server-rendered first, enhanced second

**Context.** The first version of this decision said "no JavaScript", and defended it as robustness. It was
half a principle and half a rationalisation: the same constraint was why every `POST` left the user staring
at a JSON blob, and why a rejected answer looked identical to a successful one.

**CHOSEN.** Layer 1: Jinja, one inline `<style>` block, `_graph_svg()` drawn server-side as SVG text, the
footer printing a number measured on that request (`{{RENDER_MS}}`). Everything that *constitutes the truth* —
counts, receipts, filters, forms, the health strip — lives there. Layer 2: `web/static/alibi.css` and
`web/static/alibi.js`, served by the same process, adding depth, motion, toasts that surface the API's own
`detail` sentences, a palette, busy-states. `test_the_page_works_with_and_without_the_enhancement_layer`
asserts the split is real: the page is correct without layer 2, no asset URL is absolute, and the client
layer uses no `.innerHTML =` / `document.write`.

**REJECTED.** A React/Vite front end (a build step, a node_modules tree, an app that cannot be demoed
offline); mermaid/d3 for the lineage graph (a CDN dependency in the one environment where the network is the
risk); and, in the other direction, "keep it JS-free" as a reason to leave error states unstyled.

**COST.** Two files to keep in sync with the templates, and a rule that must be tested, not admired: a layer-2
component that becomes load-bearing is a regression. The review workflow is still "read the receipt, then
decide" — no drag targets, no client-side data fetching.

---

## A22. Render helpers are Python globals, not Jinja macros

**CHOSEN.** `badge/pill/stat` are functions in `server.py` registered on `templates.env.globals`.

**REJECTED.** Macros in `base.html`: a macro must be imported by every template that uses it, and a forgotten
import renders a *blank cell* on a trust dashboard. Measured while building this: four pages rendered empty
tables with HTTP 200 and no error.

---

## A23. Per-page context is injected in one place

**CHOSEN.** `page()` adds `stats`, `live_tasks` and `active` for every route. A route that forgets a key still
renders.

**REJECTED.** Per-route context dicts for shared keys — the first version of this file 500'd on 13 of 16
pages with `UndefinedError: 'stats' is undefined` because each route re-declared the shell variables.

---

## A24. Retention drops raw text, never receipts; wipe is a verified DELETE

**CHOSEN.** `raw_content_retention_days` (default 30) purges `source_meta.full_text` and marks `purged_at`.
Such claims report `unverifiable` in the false-trust panel rather than being counted as verified. `/me/wipe`
requires typing `WIPE`, then returns the row counts the database *now reports*.

**REJECTED.** "Delete my data" implemented as `UPDATE is_deleted = 1` (a lie to a regulator), and deleting the
claims with the text (which would silently turn a re-verifiable ledger into an unbackable one).

---

## A25. Docs are served by filename whitelist

**CHOSEN.** `/api/doc/{name}` takes `Path(name).name`, requires `.md`, caps length at 80, and tries only two
known roots. `/docs` lists repo-relative paths only.

**REJECTED.** `FileResponse(ROOT / "docs" / name)`, the obvious implementation, which turns a documentation
page into a file-read primitive (`?name=../../etc/passwd`). The smoke test asserts 404 on that exact request.

---

## A26. Routing has a `demo` mode with no provider at all

**CHOSEN.** `ALIBI_MODE=demo` runs rules + `regex-extractor-v1`, reports `simulated=True` in `/readyz`, on
every promoted row, and in a banner on the cockpit. A configured-but-unreachable local model falls back to the
simulator and says so; a healthy local model is never replaced by it.

**REJECTED.** Failing to start without Ollama (the demo then only works on the developer's laptop) and
silently running the fallback (a judge would see simulated numbers without knowing). Both are unproducible;
the honest third option costs one flag.

---

## A27. Counters are computed from rows on every read

**CHOSEN.** `db.stats()` is a set of COUNTs over `claim`/`claim_evidence`/`conflict`/`change_event`; run
counters are re-synced from rows (`sync_run_counters`).

**REJECTED.** Incremental counters kept in memory or in a `meta` table. In-process counters reset on restart
and drift under re-ingest; a UI that prints a drifting number is worse than one that prints nothing, because
the number is the argument.

---

## A28. The AI runtime is LM Studio, on loopback, and that is a *transport* decision

**CHOSEN.** One OpenAI-compatible client (`LocalChat`) configured by `LM_STUDIO_BASE_URL` /
`LM_STUDIO_MODEL` / `LM_STUDIO_API_KEY` (aliases of the generic `OPENAI_*` variables; `config.py` records
which one spoke). TLS is derived from the address, so the URL copied out of LM Studio's UI works. No key is
required on loopback, one is required elsewhere. Nothing else in the product knows or cares which runtime is
configured: extraction, verification and scheduling are unchanged.

**REJECTED.** Bundling a model runtime (LM Studio's job); requiring a cloud provider; refusing to start when
no model answers; and treating `OPENAI_BASE_URL` as inherently cloud, which is what would have made a LAN
laptop look "local".

**COST.** Two transports with one payload shape means the redaction path is exercised less often in the local
case — mitigated by `is_loopback()` being one tested function, and by `/api/health` printing the verdict.

→ `alibi/providers.py:LocalChat`, `alibi/adapters.py:wants_plain_http`, `tests/test_providers_and_time.py`

## A29. Graceful AI degradation is a *state*, not a fallback to a lie

**CHOSEN.** Every provider method returns a `ModelResult` (`SUCCESS | PARTIAL | UNAVAILABLE | INVALID_OUTPUT`)
and never raises. A down runtime: rules keep ingesting, one `EXTRACTION_UNAVAILABLE` review per source per run,
`ai: degraded` on `/api/health`. A server up but without the named model: the reason lists the ids that *are*
loaded, and the app does not silently substitute one. Prose instead of JSON: `INVALID_OUTPUT`, counted in
`stats()["invalid_outputs"]`, visible on the cockpit. Empty completion: `INVALID_OUTPUT`, never "the document
had nothing". A source-level outage notice is raised even when the rules covered every block, using a sticky
`rep.ai_degraded` — because `rep.provider` is overwritten per block and "no failure" was the default reading
of a mixed run.

**REJECTED.** Falling back to the deterministic simulator silently when a real model was configured (it would
print model-shaped numbers with no model); and failing the ingest, which turns an unavailable laptop service
into a broken product.

**COST.** A review row per source during an outage, which is the point — but it means the queue grows while
LM Studio is closed, and the queue is the page users skim. Deduped per source per run for exactly that reason.

## A30. Migrations are one file per transaction, and a rebuild rebuilds the children

**CHOSEN.** `DB.migrate()` runs each file inside `BEGIN…COMMIT`, records it only on success, refuses
`executescript` (which implicitly commits first, making any rollback around it decorative) and asserts
`PRAGMA foreign_key_check` afterwards. Because `ALTER TABLE … RENAME TO` rewrites **inbound** FK clauses,
0003 rebuilds `claim_evidence`, `task` and `conflict` too, and collapses pre-existing duplicates before
creating the new partial unique index.

**REJECTED.** "It'll be fine, SQLite is forgiving" — the first attempt left the repo's own database holding
`claim__pre0003` with the migration *recorded*: unrecoverable, and re-corrupting on every start.

**COST.** Rebuilding child tables means writing their column lists twice, and a shadow-column design (the
`source_meta.orig_kind` pattern this repo already uses) would have avoided the whole problem for a constraint
widening. **If you are adding a CHECK, read A29's lesson first: prefer an unconstrained companion field plus
Python-side validation over a table rebuild.** Both were available; the rebuild was chosen for the stronger
guarantee, and the cost is documented rather than hidden.

→ `alibi/db.py:migrate`, `database/migrations/0003_predicates_and_view.sql`,
`tests/test_twin_db.py::test_0003_collapses_span_duplicates_without_losing_history`

## A31. Fact identity belongs in the schema, not in the caller

**CHOSEN.** `ux_claim_fact`: `UNIQUE(subject_type, subject_id, predicate, source_id, value_json) WHERE
valid_to IS NULL`. Re-ingest is idempotent *at the database*, whatever path wrote the row.

**REJECTED.** `db._promote`'s Python-side SELECT-before-INSERT, which is what had been "the fix": it only
covered callers that went through it, so `write_claim` — the door most of the code uses — still duplicated
whenever a document's layout shifted by a character (the 0001 key was `(…, char_start, char_end)`: *span*
identity). A guard in one path is a bug in the other.

**COST.** Free-text `due_at` values must be excluded from severity math (`str(None)` fed to `fromisoformat`
raised `ValueError` inside conflict bookkeeping — the code that runs *because* evidence disagrees), and a
re-asserted fact whose only match is retired legitimately becomes a new open row, which took a test to pin
down rather than an intuition.

## A32. Health is one function, called by the page and the endpoint

**CHOSEN.** `AppState.health()` computes eight components from COUNTs, a live provider probe and a solver
call; `GET /api/health` serialises it and the cockpit renders it. `degraded`/`empty` are real values, and
`/readyz` stays 503 until a sync has run, so "up" never implies "usable".

**REJECTED.** `{"ok": true}` constants (a green dot typed by hand is worse than no dot: it trains the operator
to ignore the one screen that would have told them the truth), and client-side fetching of health so that the
page looks empty for the first 200 ms.

**COST.** Each cockpit render re-probes the provider (~2 ms cached in `demo`; real per-request latency with
Ollama/LM Studio configured, since `routing()` deliberately re-probes). Accepted: the alternative is a health
page that is stale, which is the failure this component exists to prevent.

---

## A33. A read path computes; it does not commit

**CHOSEN.** `Twin.run_pipeline(record=False)` is what every GET handler calls (Home, Tasks, Feasibility,
Risk, Ask, `alibi plan`); `record=True` stays with the write paths that own a `run` row — `alibi sync`,
`alibi ingest`, the demo seed, the upload route. `run_pipeline` is not a query: it writes run counters and one
`change_event` per forecast, and its docstring used to say "it reads only rows", which is how it ended up
inside page handlers. Separately, `db.record_forecast` now compares the incoming status with the newest stored
row and appends a change event **only on a difference**, and `db.forecasts()` selects the newest row per
subject (the table is append-only history by design, and `INSERT OR REPLACE` cannot collapse it because there
is no UNIQUE key on the subject — deliberately, so a run's forecast survives an audit).

**WHY IT MATTERS.** Twenty-seven page views appended 48 `change_event` rows. The band that answers "what
changed in my obligations" was filling with rows produced by *looking at the page*, `alibi_changes` in
`/metrics` climbed on every refresh, and an idempotent re-sync (the exact thing the UI's re-read button is)
doubled the log while changing nothing. The duplicate rows also leaked twice more: `/risk` listed every
obligation twice after two syncs, and `risk.assess(calibration=db.forecasts())` calibrated over repeats as if
they were independent observations. A ledger artifact must be written by the event that changed the ledger.

**REJECTED.** Caching the pipeline output to hide the cost of re-solving on read (the write would still
happen on a cache miss, and a stale plan is a wrong plan); letting `change_event` record repeats and telling
users to ignore the growth (the count is the product's claim, not a log to be skimmed); adding a UNIQUE index
on `risk_forecast` to force de-duplication at write time (it would delete the forecast history the audit
depends on, and the read is the correct place to pick "current").

**PINNED BY.** `tests/test_api.py::test_a_page_view_writes_nothing_at_all` (every walk-list page plus the ops
endpoints, twice: zero rows added in nine tables) and `::test_an_unchanged_resync_records_no_change_but_still_records_a_run`
(change log flat at the seeded 30, while `run`, `audit` and `model_invocation` all grow, and
`db.forecasts()` stays one row per subject).

---

## Open / unresolved (recorded, not hidden)

1. **Title matching is a shape, not a parser.** "the report due before Diwali" is not claimed. The right fix is
   a merge-candidate review queue, not a cleverer regex.
2. **Chat follow-ups.** A second message that corrects the first ("not 12 Oct, 13 Oct") is read as two
   statements about one task only when *both* name the task. Per-message scope is why. The `os_ia2`
   "not official yet" rumour is currently refused as an ambiguous line instead of becoming an open conflict.
3. **`minutes_remaining` is 180 by default** for every task. The solver's shortfalls are therefore honest about
   *capacity* and uninformative about *effort*. Fixing it needs a "remaining work" signal the corpus does not
   contain; the alternative (guessing effort from weight) is the kind of confident invention this ledger
   rejects.
4. **Supersession across sources is authority-ordered, not recency-ordered**, so a portal correction issued
   after the syllabus still loses unless a human confirms. Deliberate for now; `/queue` is where that changes.
