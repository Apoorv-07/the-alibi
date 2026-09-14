# Configuration

Everything the core will read from outside the repository is read in `alibi/config.py`, and the mode is
**detected, not trusted**: `resolve()` reports what the machine can actually do, and the UI prints that. A
`.env` claiming `local` while Ollama is down produces a cockpit that says "simulated", not one that quietly
lies about the provider. `.env.example` lists every variable with the exact default from the code.

---

## 1. Routing: `ALIBI_MODE` / `MODEL_PROVIDER`

| mode | path | what the ledger records | when to use |
|---|---|---|---|
| `local` + `LM_STUDIO_BASE_URL` | rules → **LM Studio** (`/v1/chat/completions`) | `method='llm+verified'`, provider `local_chat`, model id in the receipt | the intended everyday setup |
| `demo` | rules + deterministic simulator, no network | `method='rule'` or `method='model'` with `simulated=True` | judging, offline talks, CI |
| `local` | rules → local model (`OLLAMA_BASE_URL`) | `method='model'`, provider+model in every receipt | daily use on a laptop with 8 GB+ |
| `hybrid` | rules → local → **one** cloud escalation per source | receipts say `REDACTED_CLOUD`, egress counted | when a genuinely unreadable scan must be parsed |
| `auto` (default) | picks the best the machine can reach | as above, per source | first run |

### LM Studio (the supported AI runtime)

```bash
# 1. LM Studio → Install Model → e.g. `qwen2.5-7b-instruct` (any instruct model with JSON output works).
# 2. LM Studio → Developer → Start Server  (defaults to http://127.0.0.1:1234/v1)
curl -s http://127.0.0.1:1234/v1/models | head -c 200      # must list your model id
# 3. .env:  LM_STUDIO_BASE_URL=…  LM_STUDIO_MODEL=…  ALIBI_MODE=local
python3 -m alibi.cli sync
curl -s localhost:8000/api/ai-check?dry=false | python3 -m json.tool
```

`/api/ai-check` is a real round trip, not a config echo: it asks the runtime to read one fixture line and
reports the *outcome* (`SUCCESS` / `PARTIAL` / `INVALID_OUTPUT` / `UNAVAILABLE`). `GET /api/health` puts the
same verdict next to database, ledger, verifier, solver, egress and queue state — every chip computed,
because a hand-typed green dot is worse than no dot.

Six behaviours, all tested in `tests/test_providers_and_time.py` against a stand-in server:

| situation | what happens |
|---|---|
| server not running | `UNAVAILABLE` → the rules path keeps ingesting; one review per source, no crash |
| running, model not loaded | health says *"which is not loaded; loaded models: …"* and does **not** silently switch models |
| `http://` in the URL | honoured (plain HTTP on loopback). TLS is used only for non-loopback hosts |
| HTTP 500 / 429 / proxy HTML | `UNAVAILABLE` with the status and body snippet in `error`, never a traceback |
| prose instead of JSON | `INVALID_OUTPUT`, counted in `stats()["invalid_outputs"]` and shown on the cockpit |
| empty completion | `INVALID_OUTPUT`, *not* "the document had nothing" |

Privacy: `is_loopback(host)` decides whether a payload is redacted and budget-metered. `192.168.1.9:1234`
— your roommate's machine — is treated as cloud. `/api/health` prints the verdict so the claim is checkable.

Two rules that hold in every mode:

- **A model never supplies trust.** `confidence` is carried as *evidence* and recorded in the receipt; the
  verifier and the trust policy decide promotion. There is no code path where a model-supplied number becomes
  trust (`alibi/providers.py` header, rule 2).
- **`UNAVAILABLE` ≠ `nothing found`.** A dead model opens one `EXTRACTION_UNAVAILABLE` review per source per
  run; an unreadable document opens a review too. Only a *successful* read with no claims is silence. This is
  why every method returns a `ModelResult` value instead of raising (rule 1 in the same header).

Measured on the demo corpus in `demo` mode: 32 model calls, 32 local, **0 cloud, 0 bytes out**, 18 claims,
0 false trust, 2 injections quarantined (`ALIBI_DB=/tmp/dbg.db python3 -m alibi.cli sync`).

## 2. Trust knobs

| variable | default | raising it | lowering it |
|---|---|---|---|
| `ALIBI_CONFIDENCE_FLOOR` | 0.72 | more items to review, fewer promotions | more promotions on thin evidence — only touch with a real model |
| `ALIBI_SUPERSEDE_TRUST_FLOOR` | 0.5 | a class group can never retire a syllabus; conflicts pile up in /queue | **rumours overwrite receipts** — this is the setting that turns an assistant into a gossip machine |
| `ALIBI_ATTENDANCE_THRESHOLD` | 0.75 | fewer shortfalls reported | more, including ones the college will condone |
| `ALIBI_CONDONATION_FLOOR` | 0.65 | condonation refused for borderline cases | certificates accepted for students who may still be short |

`precedence` (which source type wins) is **not** an env var: it lives in the policy file, because changing it
changes the meaning of the ledger retroactively and should be a committed edit, not a container flag.

## 3. Institution policy

`load_institution_policy()` (used by `Twin` with `ALIBI_INSTITUTION_CONFIG`, else the built-in) returns:

```json
{"name": "tn-default", "attendance_threshold": 0.75, "condonation_floor": 0.65,
 "tie_break": "earliest_safe", "high_weight": 0.10, "supersede_trust_floor": 0.5,
 "precedence": ["lms_api","email_thread","notice_photo","chat_export","portal_pdf","syllabus_pdf"],
 "authority": {"lms_api": 0.9, "email_thread": 0.8, "professor_announcement": 0.75, "notice_photo": 0.6,
               "syllabus_pdf": 0.6, "calendar_event": 0.55, "manual_entry": 0.5, "erp_table": 0.5,
               "student_message": 0.35, "class_group": 0.4, "rumour": 0.2, "ocr_image": 0.45},
 "late_policy_default": {"pct_per_day": 0.0, "max_days": 0},
 "tz": "Asia/Kolkata", "term_anchor": "2026-09-22", "defaulter_freeze": "2026-10-30"}
```

Three things about this table that matter:

- **`syllabus_pdf` and `notice_photo` have the same authority (0.6)** on purpose: a photograph of a pinned
  notice can legitimately correct a two-month-old PDF. The *conflict* is what gets opened, and `precedence`
  decides which one is currently believed (`chat_export` sits **below** `notice_photo` and `email_thread`
  — that is A4's ranking, enforced in `alibi/ledger.py`).
- **`late_policy_default.pct_per_day = 0.0`.** A missing late-submission clause is not scored, because
  inventing a penalty is exactly the failure this project exists to prevent. Consequence: the draft extension
  letter refuses to quote a percentage unless a `late_policy` claim in the ledger states one (`alibi/server.py`,
  `api_draft` → `penalty_stated: false`, `cost_pct: null`).
- **`defaulter_freeze`** is the date after which condonation certificates stop being accepted, and the plan
  page refuses to schedule work past it.

Provide your own with `ALIBI_INSTITUTION_CONFIG=path/to.json` (JSON or YAML); `/readyz` prints the resolved
policy name so a wrong path is visible instead of silently falling back.

## 4. Egress and redaction

Cloud is off unless a key exists **and** `CLOUD_ESCALATION_ENABLED` is true. Then:

1. `Budget` caps `MAX_CLOUD_EGRESS` bytes/day, `ALIBI_MAX_CLOUD_CALLS` per artifact and
   `ALIBI_DAY_LIMIT_USD` per day, persisted in a `budget(day, calls, tokens)` table so a restart does not
   reset the day's allowance. Exhaustion degrades to `LOCAL_ONLY` and **says so** rather than failing or
   queueing a pile for tomorrow.
2. `RedactionPolicy` (in `alibi/providers.py`) minimises the *payload*, never the ledger — a policy change
   cannot rewrite history, only what the next request contains:
   - `drop_keys`: `attendance_pct`, `grade_pct`, `sleep_floor_min`, `journal`, `wellness` are removed whole;
   - `mask`: emails, Indian mobile numbers, `roll no`-prefixed identifiers, 6+ digit runs, alphanumeric roll
     numbers (needs both ≥2 digits and ≥2 letters, applied *before* the generic digit run or `[ID]` swallows
     half the token and leaves `CSE0417` looking anonymous), URLs;
   - `max_quote_chars = 220`, `max_items = 16` — a cloud request carries at most 16 claims with a capped quote
     each; `Gemini.redact` also replaces the student's own name with `[STUDENT]`.
3. `RulesOnly.egress = "NONE"`; the offline simulator is **`LOCAL`**, never `NONE` (A14), and
   `bytes_out = 0` is the number the privacy claim rests on. `/readyz`, `/metrics` and the footer all report it.

## 5. Paths, uploads, retention

| variable | effect |
|---|---|
| `ALIBI_DB` / `DATABASE_URL` | the ledger file. `Config` reads both, so `/privacy`, `/readyz`, the budget table and the wipe verification report on the same file the server opened (this was a real divergence inside the container: `ALIBI_DB=/data/alibi.db` while the default path was `/app/alibi.db`) |
| `ALIBI_UPLOAD_DIR` | stored originals (`doc_store.stored_path`); default `var/uploads` |
| `OLLAMA_BASE_URL` / `OLLAMA_HOST` | the local model server. `Config` reads `OLLAMA_BASE_URL` and passes it in; if it is empty the `Ollama` adapter falls back to `OLLAMA_HOST`, then to `127.0.0.1:11434`. Set one, not both, or the precedence will surprise you |
| `ALIBI_MAX_DOC_BYTES` | 12 MiB; `POST /api/ingest` refuses a larger upload with a 413 that quotes the configured limit, and writes nothing (this was a hardcoded `4_000_000` in the route, which made the documented knob dead) |
| `RETENTION_DAYS` | claims + receipts (365) |
| `ALIBI_RAW_RETENTION_DAYS` | raw source text (30). After it expires the claim's receipt still exists but cannot be re-verified, so `false_trust` reporting counts it as **`unverifiable`** instead of silently as verified |

## 6. Action safety

| variable | default | what changing it does |
|---|---|---|
| `REQUIRE_ACTION_APPROVAL` | `true` | every external action needs a human press approve; `draft_email` shows the exact bytes first |
| `DRY_RUN` | `true` | nothing is sent/submitted, a plan change is not pushed |
| `ALIBI_ALLOW_IRREVERSIBLE` | `false` | even when `true`, the never-list below still REFUSES |
| `ALIBI_NEVER_LIST` | `send_email, submit_assignment, delete_submission, drop_course, pay_fee` | a deny list that **outranks the policy gate** — set it to the empty string and `alibi/actions.py` still refuses them |

## 7. Failure modes, and what you get instead of a crash

| mistake | what happens | where you see it |
|---|---|---|
| `ALIBI_MODE=hybrid`, Ollama down | rules continue; simulator is **not** used when a real model was configured and merely unreachable → one `EXTRACTION_UNAVAILABLE` review per source | `/queue`, banner on `/`, `simulated=False` in `/readyz` |
| `ALIBI_MODE=demo` | deterministic simulator, everything marked | banner "numbers you are looking at were produced with no model", `simulated=True` |
| bad `ALIBI_INSTITUTION_CONFIG` path | built-in `tn-default` used, name printed | `/readyz` "policy: tn-default" |
| empty database | all 16 pages still render (they must, or a first run looks broken); `readyz` reports **503** because `state.ready` is only set once a sync has run | `/readyz`, `POST /api/sync` (the Sources page's button) |
| `ALIBI_MAX_DOC_BYTES` too small | 413 with the byte count in the message | the upload page, no partial claim written |
| non-numeric env value (`ALIBI_CONFIDENCE_FLOOR=high`) | falls back to the default silently *by design* (`_num`), logged at DEBUG | `/models` shows the effective number, not the intended one |
| a `meta` write that violates a CHECK | `ClaimWriteError` with the constraint text, row routed to a review | `/queue`, never a silent empty ledger |

## 8. CI and container

`make verify` = `pytest` (147 tests) + `checks/*.py` + `evaluation/harness.py` + `scripts/smoke.py`.
`.github/workflows/ci.yml` additionally boots the app with a throwaway DB and walks all 17 routes over HTTP,
asserting the false-trust metric is `0`, that a cold `/readyz` is a 503 rather than a green light on an empty ledger, and that `/api/doc/..%2F..%2Fetc%2Fpasswd` is a 404.

`docker compose up alibi` runs as uid 10001 with `/data` as the only writable path; `--profile seed` ingests
the demo corpus once; `--profile ollama` starts a local model and the container's mode becomes `local` if
`MODEL_PROVIDER`/`OLLAMA_BASE_URL` point at it. The healthcheck is `GET /readyz`, which returns 503 when the
ledger is empty — so a container that is up but has ingested nothing is *unhealthy on purpose*.
