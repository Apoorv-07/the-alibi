# 12-Hour Quickstart — plan §22, mapped to files that already exist

This document exists so that at hour 3 nobody is asking "where does the date check live?". Every row
gives a **command that proves the step is done**, because "I wrote it" is not a status at a hackathon.

Two assumptions baked in: person **A** does extraction/solver, person **B** does UI/infra. The gates
are shared. If you are alone, run A's column and cut B's UI to the two screens named in §22.

---

## 0. What this repo already gives you (skip these hours)

| Plan step | File(s) here | Status |
|---|---|---|
| sources + claims DDL | `database/schema.sql` (19 tables, 2 views, 2 triggers) | ✅ verified by `sqlite3` |
| deterministic preparse | `alibi/ingest.py` → `blocks_from_syllabus`, `rule_claim_from_row`, `parse_whatsapp` | ✅ runs on the corpus |
| ground-check (span, dates) | `alibi/ground.py` → `verify_claim`, `dates_in`, `instructional` | ✅ 28 tests + `checks/check_ground.py` |
| date→subject attribution | `alibi/link.py` (proximity, ties → review) | ✅ `checks/check_link.py` exits 0 |
| ledger + supersession + conflicts | `alibi/ledger.py` (`upsert`, `conflicts`, `safe_value`, trust floor) | ✅ tested |
| attendance / eligibility closed form | `alibi/ledger.attendance_state` | ✅ `N = ceil((0.75T−A)/0.25)` |
| CP-SAT + unsat core + priced remedies | `alibi/feasibility.py` (`per_prefix`, `solve`, `analyze`) | ✅ 5/5 planted overloads |
| local + cloud adapters, budget, redaction | `alibi/adapters.py` | ✅ request shape + failure paths only (no GPU here) |
| routing (rule → local → verify → cloud → promote/review) | `alibi/router.py` | ✅ with fake providers |
| eval harness + gold set + controls | `evaluation/harness.py`, `corpus/demo_corpus.py` | ✅ runs offline, no key |
| ChatGPT-substitution experiment | `evaluation/baselines.py` (`--plan-bot llm`) | ⚠️ wired, needs a provider |

**What you actually have to build in 12 hours:** the FastAPI/htmx surface over these functions, the
Telegram round-trip, the ICS read-back, and — if you want the win condition — real photos and a real
gold set instead of the synthetic ones.

```bash
pip install ortools pytest && python3 -m pytest -q     # 28 passed in ~0.55s
python3 run_poc.py                                      # the whole loop, printed
python3 evaluation/harness.py                           # EVAL_REPORT.md, no key needed
```

---

## 1. Pre-clock (do this yesterday)

| Do | Why it ends the night if skipped |
|---|---|
| `ollama pull qwen3.5:4b` + `nomic-embed-text`, then **run one real extraction** | 3.4 GB during the demo window; and if the 4 B can't follow the schema you need to know at hour 0, not hour 2 |
| Read Gemini model IDs from AI Studio **today** | they rotate; a stale ID in `alibi/adapters.py:Gemini(model=…)` fails every escalation |
| `python3 evaluation/harness.py --llm ollama --plan-bot llm` on your laptop | this is the 2-minute version of the experiment that wins the hackathon; run it **before** the clock so you know your own number |
| Create the Telegram bot, allowlist your chat id | 15 min, zero risk, unblocks B entirely |
| Collect **4 real artefacts from one real classmate**: 2 syllabus PDFs, 1 phone photo of the notice board, 1 WhatsApp `_chat.txt` | synthetic corpus proves the code; their corpus proves *the product*. Also your OCR reality check |
| Write the 60-row gold set as JSONL: `{id, course, due, weight, source_file, quote}` | `harness.py --emit-corpus eval/corpus.jsonl` gives you the template. **Every gold row must be quotable from a file** — that rule exists because 2 of my 8 planted conflicts had no sentence stating them, which made the metric meaningless |
| `git init`, `uv venv`, push to a private remote | graders read the log; and laptops die |

## 2. Hour 0:00 → 1:30 — ingestion, and one claim on screen

**A:** wrap `alibi/ingest.py` + `alibi/router.py` in a FastAPI route that returns `RouteResult` as
JSON. PDF→text via `pypdf` (their text layer is why 55% of facts need no model). Write `Source` rows
into SQLite with `database/schema.sql`.
**B:** htmx shell, `/ledger` table, drag-drop upload posting to that route, `/run/live` SSE skeleton.

Gate: `curl -F file=@real_syllabus.pdf localhost:8000/api/ingest` returns claims, and the dashboard
shows them with **the quote attached and the offset highlighting it**. If the highlight doesn't work,
the demo loses its single most persuasive visual — do it now, not at hour 9.

## 3. Hour 1:30 → 3:00 — local extraction and the human in the loop

**A:** point `alibi/adapters.Ollama` at the real daemon; run the router with `local=Ollama(...)` and
**no cloud**. Watch `fraction_needing_a_model`. If local accuracy is bad, do not tune the prompt past
30 minutes — the verifier's rejection log tells you whether it's the model or your span matching
(`checks/check_ground.py` isolates the latter).
**B:** Telegram approve/decline callback with a chat-id allowlist; review queue screen fed by
`Ledger.review`.

Gate: a photo of a notice board produces one claim, one Telegram prompt, one tap, one calendar write
(or a `DRY_RUN` line saying it would have). Plus: **an injection payload must appear in the review
queue as text, not in the ledger.** That is `instructional_value_refused`; demo it deliberately — the
integrity question is going to be asked about "AI that manages your academic life", and answering it
unprompted converts suspicion into approval.

## 4. Hour 3:00 → 4:30 — escalation, conflicts, ICS

**A:** enable `cloud=GEMINI` behind `alibi/adapters.Budget` (day cap + per-artefact cap); the router
already escalates **only** what the verifier refused or scored below the confidence floor.
**B:** `/conflict/:id` — three columns (each source, its quote, its `captured_at`) and the policy line
from `safe_value["_why"]`. ICS fetch + `parse_ics`.

Gate: three sources disagree → conflict row appears **without** anyone overwriting anyone. Test the one
that matters most: `test_a_rumour_cannot_retire_the_syllabus` — a class-group message must not retire
the syllabus, because recency is what a naive sync trusts and a student's newest artefact is always
the least authoritative one.

## 5. Hour 4:30 → 5:30 — eligibility math. 🚩 HARD GATE at 05:30

`alibi/ledger.attendance_state` is done; wire it to the ERP table (`parse_attendance_table`) and the
UI strip. **At 05:30 cut everything not listed in §22's "must survive" line.** No "30 more minutes".

## 6. Hour 5:30 → 7:30 — the solver, and *the slide*

`analyze()` already returns: status, minimal core, `per_prefix` binding arithmetic, remedies ranked by
`cost_class`. Two UI elements only: the red bar (per-day shortfall in hours) and the 4-line explanation.

Say this out loud in the demo, because it is the whole technical argument: **`INFEASIBLE` here means
provably impossible for real** (the model is a preemptive capacity relaxation, so it never blesses an
unexecutable plan), and it refused to "fix" the week by deleting your submissions or by quietly
assuming the professor grants an extension (`allow_miss=False`; `ext_*` are remedies, not baseline
constraints). If a judge asks "why not just ask an LLM to schedule it": the answer is on screen — a
minimal unsat core is a *proof*, and a plan with an unaddressed −2.0 h shortfall is a *vibe*.

## 7. Hour 7:30 → 9:00 — act, then audit

Tiering + executor (ICS/`.ics` write, gmail draft) with `idempotency_key`; then **self-audit**: re-read
the calendar feed, diff against intent, `MISMATCH` → keep a re-check job, and plan the fallback that
works *without* the favour. `run_poc.py` step 7 is that exact trace.

Gate: the loop closes on demo data — a promised action is only marked handled when an external feed
confirms it, and the "what if the extension never happened" fallback is computed up front.

## 8. Hour 9:00 → 10:30 — the experiment that is the win condition

```bash
python3 evaluation/harness.py --emit-corpus eval/corpus.jsonl     # your 60-row gold set goes here
python3 evaluation/harness.py --llm ollama --plan-bot llm         # real numbers, labelled real
```

Three things to print on one slide, in this order:

1. **Extraction:** local vs cloud accuracy on *their* artefacts (the `--llm` rows).
2. **The substitution test:** 5 planted overload weeks, ask ChatGPT/Claude/Gemini for a plan, grade
   each with `plan_is_executable()` — the product's own arithmetic, not a model's opinion. Expect
   confident plans that allocate more hours to a day than legal time allows. **The number that wins is
   "0/5 vs 5/5"**, i.e. how many impossible plans the baseline shipped and how many our engine refused
   to produce. Run it on real transcripts; if the baseline scores well, that is a *finding*, and the
   right response is to say what we still add (provenance, ledger, follow-through), not to hide it.
3. **Cost:** `fraction_needing_a_model`, tokens, cloud $/day from `Budget.snapshot()` — the claim
   "most facts need no model", measured rather than asserted.

## 9. Hour 10:30 → 11:30 — fix the three ugliest things the eval exposed. Freeze at 11:00.

Expect to spend it on: subject attribution (which task a date belongs to), OCR garbage on one bad
photo, and empty states. Do **not** spend it on a second solver objective.

## 10. Hour 11:30 → 12:00 — rehearsal

Two consecutive successful runs with **no cloud** (unplug the network and confirm the ingest still
works — local extractor + review queue, no crash: `Budget.snapshot()["cloud_available"]` goes False
and the router degrades to review-only by design), a 3-minute backup video, tag `demo-ready`. A/B each
take one half of the pitch: A on "why deterministic", B on "what it does on Tuesday at 06:50".
*(There is no `OFFLINE_MODE` flag in this repo yet — plan §22 lists it as a 10:30 build item. The
behaviour is already correct; add the flag to make it one keystroke.)*

---

## Commands you will actually re-run all day

```bash
python3 -m pytest -q              # the guarantees. Never demo with red tests.
python3 checks/check_ground.py    # verifier vs real corpus text
python3 checks/check_link.py      # no date attributed to the wrong obligation (exit 1 = fail)
python3 run_poc.py                # the full loop as prose — your demo script, basically
python3 evaluation/harness.py     # the numbers, with provenance labels
python3 evaluation/harness.py --plan-bot llm --llm ollama   # needs a provider
```

## The two things that must survive every cut

`alibi/ground.py` (+ `alibi/link.py`) and the self-audit. Together ~400 lines, and they *are* the
difference between "a ChatGPT wrapper with a calendar" and "a system that can be held accountable".
If you are out of time at 10:00, cut the Gantt chart before you cut either.
