# HTTP API

`uvicorn alibi.server:app` (`make serve`). Everything is one process, one SQLite file, one template set.
There is no auth layer: read `docs/THREAT-MODEL.md` T10 before exposing this beyond localhost.

Two conventions worth stating before the table, because they are the reason this API is not a normal one:

- **Errors carry the reason, not a status name.** A 404 from `/api/plan/draft-extension` lists the subject ids
  that *do* have evidence; a 413 from `/api/ingest` quotes the configured limit and the variable that changes
  it; a 422 from `/me/wipe` says what to type. A user who can act on an error doesn't need a support thread.
- **Reads never mutate.** Every `GET` is safe to hit 500 times (`/readyz` is the healthcheck for exactly that
  reason). Ingestion happens only on `POST`; the counters `GET /metrics` prints are `COUNT`s over rows, so
  there is no in-memory counter to drift.

## Pages (GET, Jinja, no JavaScript) — 17 of them are user-facing screens; 20 GET pages exist in total

| path | shows | the thing to look at |
|---|---|---|
| `/` | cockpit: mode banner, counts, last sync, open risks | `sync.claims` equals the ledger count (measured 18/18) |
| `/twin` | every task with its due date, weight, source and trust state | rows with `verify_state != 'grounded'` are rendered as questions, not facts |
| `/timeline` | 14-day horizon: what fits, what does not | the infeasible day and its `slack_min` |
| `/feasibility` | solver output: status, core, priced remedies | `core=['no_miss']`, remedies flagged `cost_class=violates_your_sleep_floor` |
| `/risk` | proven vs predicted, split into two tables | a predicted row never sits in the proven table |
| `/claims` | filterable ledger (subject, predicate, state) | each row links to its receipt |
| `/sources` | every artifact, its kind, trust prior, purge state | injection blocks listed as quarantined |
| `/conflicts` | open disagreements with `explain_conflict()` prose | each links to the review item that answers it |
| `/queue` | human decisions outstanding, one per source per outage | `EXTRACTION_UNAVAILABLE` is not 12 copies of itself |
| `/actions` | drafts and pending approvals | `sending: HUMAN` on every email |
| `/lineage` | server-drawn SVG: source → claim → supersession edges | refused supersessions are drawn and labelled |
| `/audit` | append-only audit + `change_event` stream | who decided what, including "nothing was executed" |
| `/eval` | the four-variant table read from `EVAL_REPORT.json` | `rules_line` (the shipped floor), `naive_attribution` (the control) |
| `/settings` | resolved config, policy, provider stack | `redacted_view()` — key presence booleans only |
| `/privacy` | what is stored, for how long, and what left the machine | `cloud_bytes = 0`, `unverifiable` after a purge |
| `/metrics` | Prometheus-style counts | every line is a COUNT over rows |

## JSON / machine-readable (13 `/api` routes)

| method, path | params | response |
|---|---|---|
| `GET /healthz` | — | `{"ok": true}` — the process is up; **not** "the ledger is usable" |
| `GET /api/health` | — | `{state, components{…}, warnings[], mode, simulated_extraction, checked_at}` — eight probes (database, ledger, verifier, ai, solver, egress, extraction, queue), each `{state, detail, …}`. `degraded` and `empty` are real values. `no-store` |
| `GET /api/ai-check` | `dry=true\|false` | the AI runtime's verdict. `dry=true` reads only the health probe; `dry=false` sends one fixture line through the provider and returns `SUCCESS\|PARTIAL\|INVALID_OUTPUT\|UNAVAILABLE`, latency, `bytes_out`, the notes the transport added. Writes nothing: verification, not the model, decides what becomes a claim |
| `GET /readyz` | — | `ready`, `claims`, `db`, `uptime_s`, `mode`, `providers{local,local_model,cloud,cloud_available}`, `simulated_extraction`, `false_trust_count`; **503 until the first sync** (cold start is reported as not-ready, not as green) |
| `POST /api/sync` | `demo=true` (form) | `{"ms","run","sources","claims","rejected","reviews","changes","rejected_note","per_source":[…],"risks":{…}}` — the seed path the Sources button uses |
| `POST /api/ingest` | `file` (multipart), `kind=auto`, `label` | `IngestReport.as_dict()`: `source_id, kind, blocks, rule_claims, promoted, rejected, reviews_opened, changes, provider, model, outcome, egress, latency_ms, llm_involved, trace[], errors[]` |
| `GET /api/claims/{cid}/evidence` | — | the claim row plus its receipt; 404 if it does not exist |
| `GET /api/graph` | `task=SUBJECT_ID`, `type=task` | `{"nodes":[…],"edges":[…]}` — the exact rows the SVG was drawn from; no `task` → `{"error":"pass ?task=SUBJECT_ID"}` |
| `GET /api/export/ledger.json` | — | `{generated_at, policy, claims[], provenance[], changes[], conflicts[]}` — the whole evidence trail, portable |
| `GET /api/export/ledger.csv` | — | `id, subject_type, subject_id, predicate, value_json, verify_state, method, recorded_at, valid_from, valid_to, evidence_span`, RFC4180-quoted, formula-prefixed |
| `GET /api/export/twin.ics` | — | one VEVENT per dated deadline, weight in the summary, claim id in the description |
| `GET /api/plan/draft-extension` | `task`, `remedy_days=1` | see below |
| `POST /api/review/{rid}/resolve` | `decision`, `chosen`, `note`, `predicate=due_at` | `accept_safety` → `{"rule":"earliest_safe"}`; **`keep_own` → your sentence becomes a real claim** (`method='manual'`, its own receipt, `MANUAL_FACT_RECORDED` change event) and returns `{"recorded":true,"claim_id":…,"shape":"date"}`; re-runs `sync_conflicts()` |

### `POST /api/review/{rid}/resolve` with `decision=keep_own` — the contract the UI promises

| your answer | response |
|---|---|
| `"The registrar confirmed 20 Oct 2026 by email this morning."` | `200 {"recorded": true, "claim_id": 20, "shape": "date"}` — claim written, review closed as `answered` |
| `"It is 17 Oct 2026, not 18 Oct 2026 as the group says."` | `200 {"ambiguous": true, "candidates": ["2026-10-17","2026-10-18"], "next": "edit your answer to name exactly one date…"}` — review **stays open**, nothing is chosen for you |
| `"17 Oct"` (shorter than the ledger's 12-char quote floor) | `422` naming the receipt rule, not "invalid input" |
| `""` | `422 "there is no answer to record…"` |
| `predicate=vibes` | `422 "not a storable predicate; allowed: …"` (the router's list, which is now also the schema's) |

Why this exists: `keep_own` used to write only `review_item.resolution`, so the button's sentence — "your
answer is recorded as the source of truth" — was false, and the plan went on using the value you had just
corrected. An answer is recorded when it becomes a row with its own verbatim quote; the coercion is
deliberately narrow (one unambiguous ISO/`11 Oct 2026` date, or a percentage), because asking a model to
interpret a vague human sentence reintroduces the exact failure the ledger exists to prevent.
| `POST /api/action/{aid}/decide` | `decision=approved\|rejected`, `note` | `{"ok","approval_id","action_id","decision","executed","result"}`; 409 on a replayed decision, 422 on any other value |
| `POST /me/wipe` | `confirm=WIPE` | `{"deleted": true, "remaining_rows": {claim, source, observation, change_event, action_request}}` — the counts the DB reports *after* the DELETEs |
| `GET /docs`, `GET /api/doc/{name}` | — | the repo's own markdown, filename-whitelisted (`.md`, ≤80 chars, `docs/` then repo root) |
| `GET /api/openapi.json`, `GET /api/docs` | — | FastAPI's own schema and Swagger UI, moved off `/openapi.json` and `/docs` so this app can use those two paths for its documentation page (a docs page that competes with the framework's is a docs page that gets shadowed) |

### `GET /api/plan/draft-extension` — the only externally-visible artefact

```json
{"action_id": 3, "decision": "APPROVAL_REQUIRED", "risk_class": "HIGH_IMPACT_WRITE",
 "reason": "external write: draft is rendered for a human, never sent",
 "supporting_claims": [15, 18],
 "draft": {"to": "faculty@college", "subject": "Extension request — CS",
           "body": ["Subject: Extension request — Cs-registration (2026-09-30)", "…",
                    "The ledger does not record a late-submission clause for this course, so I am not",
                    "assuming one — I am asking rather than relying on it.", "…"],
           "citations": "  · due_at ← claim 15\n  · due_at ← claim 18",
           "claims": [15, 18], "sending": "HUMAN",
           "remedy": {"kind": "extension_request", "days": 1, "cost_pct": null,
                      "penalty_stated": false, "core": ["no_miss"],
                      "minutes_left": 180, "shortfall_hours": 0.0,
                      "feasibility_status": "INFEASIBLE"}}}
```

Three properties, each fixing a bug this file had:

1. **No evidence, no draft.** A subject with no grounded `due_at`/`late_policy`/`weight` claim is a **404**
   ("a draft with no evidence is not a draft, it is a guess") plus the list of subject ids that do have
   evidence. A subject with a `due_at` row whose value holds no date is a **409**, because "could you
   extend X" with no X-date is a question to the professor that the ledger should have answered first.
2. **The penalty is cited or admitted missing.** `cost_pct` is computed from the `late_policy` claim's own
   text; when no source states one, `cost_pct` is `null`, `penalty_stated` is `false`, and the letter *says*
   it is not assuming one. `cost_pct = days * 10` — the developer's guess about a professor's policy — was
   the original behaviour. `late_policy_default.pct_per_day` is `0.0` for the same reason and is never used
   as a fallback here.
3. **The shortfall is the solver's.** `minutes_left` / `shortfall_hours` come from the same `per_prefix`
   window the plan page uses, for the deadline in question; when the date is outside the 14-day horizon the
   payload carries `solver_note` and the letter prints `?` rather than a number. An empty body of evidence
   was previously indistinguishable from a solved 0.

## Response codes you can rely on

| code | when |
|---|---|
| `404` | unknown claim id, unknown subject in the draft, doc not on the whitelist (including every traversal attempt) |
| `409` | action already decided (replay refused); a draft whose evidence carries no date |
| `413` | upload above `ALIBI_MAX_DOC_BYTES`; nothing is written |
| `422` | wipe without `confirm=WIPE`; a decision other than `approved`/`rejected` |
| `500` | never from a template: an empty database renders all 16 pages (`tests/test_api.py`) |

## Checking it yourself

```bash
uvicorn alibi.server:app --host 0.0.0.0 --port 8000      # binds 0.0.0.0 for the preview proxy
python3 scripts/smoke.py                                  # 16 pages, cold-503, seed, metrics, traversal, upload, draft
curl -s localhost:8000/metrics | grep -E "false_trust|cloud_bytes|injections"
curl -s "localhost:8000/api/plan/draft-extension?task=dbms-lab_4" | python3 -m json.tool
```

`run_id` is stored as TEXT on `change_event` and INTEGER on `run`; `sync_demo` passes `str(rid)`. That is
deliberate and load-bearing for the per-run counts above — do not "clean it up" to one type in only one of
the two queries.
