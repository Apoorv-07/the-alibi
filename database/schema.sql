-- ALIBI — claim ledger. 5 tables + 2 views + 1 FTS index. That is the whole store.
-- Deliberately relational (not a vector store): deadlines/attendance/policy are STATE with
-- time, provenance and contradiction semantics, which is exactly what semantic search over
-- chunks cannot express. (The Zep/Graphiti "temporal knowledge graph" idea, shrunk to what
-- a 12-hour team can actually operate.)
--
-- sqlite> PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;

PRAGMA user_version = 1;

CREATE TABLE institution (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  config_path TEXT NOT NULL,          -- ../config/institutions/*.yaml (thresholds, tz, precedence)
  policy_doc_sha TEXT                 -- which handbook version this row was validated against
);

CREATE TABLE course (
  id INTEGER PRIMARY KEY,
  institution_id INTEGER NOT NULL REFERENCES institution(id),
  code TEXT NOT NULL,                 -- CS8586
  title TEXT NOT NULL,
  attendance_threshold REAL NOT NULL DEFAULT 0.75,   -- per course: lab may differ (0.85)
  condonation_floor REAL NOT NULL DEFAULT 0.65,
  lms_site TEXT, lms_course_id TEXT, ics_url_hash TEXT,   -- never store the raw token here
  UNIQUE (institution_id, code)
);

-- One row per document/message/feed-snapshot we ever looked at. `captured_at` vs
-- `effective_at` is the bi-temporal pair: supersession keys on captured_at (what we learned
-- later retires what we learned earlier); semantic validity keys on effective_at.
CREATE TABLE source (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN
    ('syllabus_pdf','syllabus_photo','notice_photo','chat_export','portal_pdf','attendance_screen',
     'ics_feed','lms_api','email_thread','user_note','journal','policy_pdf')),
  uri TEXT,
  sha256 TEXT NOT NULL,
  course_id INTEGER REFERENCES course(id),
  captured_at TEXT NOT NULL,          -- ISO8601 +05:30
  effective_at TEXT NOT NULL,         -- when the document asserted it
  trust_prior REAL NOT NULL DEFAULT 0.6,   -- re-estimated per source from correction history
  parse_profile TEXT,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','parsing','parsed','degraded','failed')),
  note TEXT,
  UNIQUE (sha256, kind)
);

-- THE PRODUCT.  A claim is (predicate, value, evidence, time, trust).  Nothing becomes
-- `verified` without the deterministic ground-check in alibi/ground.py; nothing unverified
-- is ever deleted — it lands in status='review' and is counted in the UI header.
CREATE TABLE claim (
  id INTEGER PRIMARY KEY,
  subject_type TEXT NOT NULL CHECK (subject_type IN
    ('task','session','attendance','grade','policy','capacity','wellness')),
  subject_id TEXT NOT NULL,
  -- NB: this 0001 list is deliberately NOT the whole contract. Predicates that arrived later
  -- (`submitted`, `submitted_at`, `schedule_change`) and the `manual` method are added by
  -- database/migrations/0003_predicates_and_view.sql, which rebuilds this table. schema.sql holds the
  -- *baseline*; the migration holds the *change*; tests/test_twin_db.py asserts a migrated database
  -- accepts everything `router.ALLOWED_PREDICATES` can produce. Two files carrying the same list drift.
  predicate TEXT NOT NULL CHECK (predicate IN
    ('due_at','released_at','weight','late_policy','submission_channel','submission_time',
     'venue','attendance_pct','grade_pct','exam_date','review_gap','clause','capacity_rate',
     'sleep_floor_min')),
  value_json TEXT NOT NULL,           -- validated against the per-predicate JSON Schema in ai/schemas.py
  source_id INTEGER NOT NULL REFERENCES source(id),
  evidence_span TEXT NOT NULL CHECK (length(evidence_span) >= 12),        -- VERBATIM. The receipt. Exported with the ledger.
  char_start INTEGER NOT NULL DEFAULT -1,       -- into source text, for the highlight UI
  char_end   INTEGER NOT NULL DEFAULT -1,       -- -1 when the span could not be located exactly
  confidence REAL NOT NULL DEFAULT 0.5,
  verify_state TEXT NOT NULL DEFAULT 'unverified'
    CHECK (verify_state IN ('grounded','grounded_relative_ambiguous','review','rejected')),
  method TEXT NOT NULL DEFAULT 'llm' CHECK (method IN ('rule','ics','lms_api','llm','llm+verified')),
  -- 0003 widens this with 'manual': a human answer to a review question is neither a rule nor a model.
  recorded_at TEXT NOT NULL,          -- = source.captured_at at write time; the supersession axis
  valid_from TEXT NOT NULL,
  valid_to TEXT,                      -- NULL = currently believed. Never deleted on change.
  supersedes INTEGER REFERENCES claim(id),
  superseded_by INTEGER REFERENCES claim(id),
  -- idempotent re-ingest: same document, same fact, same span => no new row
  -- NB: every column in this key is NOT NULL on purpose. A nullable column makes the UNIQUE
  -- constraint vacuous in SQLite (NULL != NULL), and re-ingestion silently duplicates the
  -- whole ledger — which I found by smoke-testing this schema, not by reading it.
  UNIQUE (subject_type, subject_id, predicate, source_id, char_start, char_end)
);
CREATE INDEX ix_claim_open ON claim(subject_type, subject_id, predicate) WHERE valid_to IS NULL;
CREATE INDEX ix_claim_src  ON claim(source_id);

-- Health-derived capacity is stored in a SEPARATE table that the action layer cannot read
-- (see threat model). Only the *numeric floor* crosses into the solver; the reason never
-- appears in any outbound artifact. `no_export` is enforced by a test, not a convention.
CREATE TABLE capacity_signal (
  user_id TEXT NOT NULL,
  night_date TEXT NOT NULL,
  minutes INTEGER NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('manual','wearable_import')),
  no_export INTEGER NOT NULL DEFAULT 1 CHECK (no_export = 1)
);

CREATE TABLE task (
  id TEXT PRIMARY KEY,
  course_id INTEGER REFERENCES course(id),
  title TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'assignment',
  weight REAL,
  est_minutes INTEGER, actual_minutes INTEGER,
  status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open','in_progress','done','missed','not_found','withdrawn')),
  due_claim_id INTEGER REFERENCES claim(id)   -- the *safe* value chosen by reconciliation
);

CREATE TABLE conflict (
  id INTEGER PRIMARY KEY,
  subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, predicate TEXT NOT NULL,
  claim_ids TEXT NOT NULL,             -- JSON array
  kind TEXT NOT NULL CHECK (kind IN ('date','weight','schedule','existence','missing_date','venue')),
  severity TEXT NOT NULL CHECK (severity IN ('LOW','MED','HIGH')),
  state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','accepted_risk','resolved','dismissed')),
  rule TEXT NOT NULL,                  -- 'earliest_safe' | 'latest_asserted' | 'human'
  resolution_claim_id INTEGER REFERENCES claim(id),
  opened_at TEXT NOT NULL, resolved_at TEXT
);

-- Every action carries a justification that MUST reference a conflict or an unsat core.
-- The executor refuses when it is empty. Tier 2 is a code-level refusal, tested in CI.
CREATE TABLE action (
  id INTEGER PRIMARY KEY,
  run_id INTEGER,
  tier TEXT NOT NULL CHECK (tier IN ('tier0_auto','tier1_approval','tier2_refused')),
  kind TEXT NOT NULL CHECK (kind IN
    ('ledger_update','calendar_propose','calendar_write','gmail_draft','telegram_broadcast',
     'report_generate','extension_request','shortage_application','review_schedule')),
  payload_json TEXT NOT NULL,
  justification TEXT,                  -- JSON: {"conflict_id":7,"core":["due_os_assign3",...]}
  status TEXT NOT NULL DEFAULT 'proposed'
    CHECK (status IN ('proposed','approved','edited','rejected','executing','auditing',
                      'ok','mismatch','failed','expired')),
  approved_by TEXT, approved_at TEXT, executed_at TEXT,
  audit_evidence TEXT,                 -- raw ICS/portal response proving the world changed
  audit_checked_at TEXT,
  idempotency_key TEXT UNIQUE,
  refuse_reason TEXT                   -- populated for tier2_refused; never silently swallowed
);

-- Observability + the cost meter. This table alone lets you answer "what did the agent
-- actually do, how much did it cost, and which model decided what" — i.e. the eval.
CREATE TABLE run (
  id INTEGER PRIMARY KEY,
  trigger TEXT NOT NULL,               -- artifact|feed_delta|cron_morning|cron_nightly|user_intent|approval
  started_at TEXT NOT NULL, finished_at TEXT,
  state TEXT NOT NULL DEFAULT 'perceiving',
  llm_calls INTEGER NOT NULL DEFAULT 0,
  local_calls INTEGER NOT NULL DEFAULT 0,
  cloud_calls INTEGER NOT NULL DEFAULT 0,
  tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
  gpu_ms INTEGER NOT NULL DEFAULT 0, cost_cents INTEGER NOT NULL DEFAULT 0,
  claims_in INTEGER, claims_grounded INTEGER, claims_rejected INTEGER, claims_review INTEGER,
  conflicts_found INTEGER, solve_ms INTEGER, solver_status TEXT,
  error TEXT
);

-- Durable job queue: a crash mid-run must not lose the job (demo beat: kill -9 the worker).
CREATE TABLE job (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL, payload TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'ready'
    CHECK (state IN ('ready','running','done','retry','failed','parked_budget')),
  attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
  next_retry_at TEXT, claimed_at TEXT, claimed_by TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')), last_error TEXT
);
CREATE INDEX ix_job_ready ON job(state, next_retry_at);

CREATE TABLE source_trust (               -- learned reliability: the network-effect asset
  source_kind TEXT NOT NULL, course_id INTEGER, predicate TEXT NOT NULL,
  agreed INTEGER NOT NULL DEFAULT 0, disagreed INTEGER NOT NULL DEFAULT 0,
  last_updated TEXT,
  PRIMARY KEY (source_kind, course_id, predicate)
);

-- The ONLY view the rest of the app reads for deadlines. Enforces the safety policy in one
-- place so no caller can accidentally plan against a single unverified source.
CREATE VIEW task_safe AS
SELECT t.id, t.title, t.course_id, t.weight,
       min(json_extract(c.value_json, '$.date')) AS safe_due,
       count(DISTINCT c.source_id)               AS n_sources,
       group_concat(c.source_id)                 AS provenance,
       max(c.confidence)                         AS best_confidence
FROM task t
JOIN claim c ON c.subject_type='task' AND c.subject_id=t.id
            AND c.predicate='due_at' AND c.valid_to IS NULL
            AND c.verify_state IN ('grounded','grounded_relative_ambiguous')
GROUP BY t.id;

CREATE VIEW live_claims AS
SELECT * FROM claim WHERE valid_to IS NULL AND verify_state LIKE 'grounded%';

-- Institutional policy corpus: clause-aware chunks, verbatim-only answers, quote or it
-- didn't happen. (RAG is justified here and ONLY here — see ARCHITECTURE §13.)
CREATE TABLE policy_chunk (
  id INTEGER PRIMARY KEY,
  institution_id INTEGER NOT NULL REFERENCES institution(id),
  doc TEXT NOT NULL, clause_path TEXT,           -- "4.2(b)"
  page INTEGER, version TEXT, effective_from TEXT, effective_to TEXT,
  text TEXT NOT NULL, sha256 TEXT NOT NULL,
  UNIQUE (institution_id, doc, clause_path, sha256)
);
CREATE VIRTUAL TABLE policy_fts USING fts5(text, clause_path UNINDEXED, content=policy_chunk,
                                           content_rowid=id, tokenize='porter unicode61');
CREATE TRIGGER policy_ai AFTER INSERT ON policy_chunk BEGIN
  INSERT INTO policy_fts(rowid, text, clause_path) VALUES (new.id, new.text, new.clause_path);
END;
CREATE TRIGGER policy_ad AFTER DELETE ON policy_chunk BEGIN
  INSERT INTO policy_fts(policy_fts, rowid, text, clause_path) VALUES ('delete', old.id, old.text, old.clause_path);
END;
-- dense vectors go in a side table (nomic-embed, 768f, float32 little-endian blob).
-- RRF fusion in retrieval/index.py; no ANN index needed below ~50k clauses.
CREATE TABLE policy_vec (
  chunk_id INTEGER PRIMARY KEY REFERENCES policy_chunk(id),
  dim INTEGER NOT NULL, model TEXT NOT NULL, vec BLOB NOT NULL
);

-- Audit trail: every state change, replayable. Judges can verify you. `sqlite3 alibi.db
-- "SELECT * FROM audit ORDER BY id DESC LIMIT 50"`.
CREATE TABLE audit (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, who TEXT NOT NULL DEFAULT 'twin',
  entity TEXT NOT NULL, entity_id TEXT NOT NULL, op TEXT NOT NULL, before TEXT, after TEXT
);

-- `DELETE FROM ...` for all tables in one shot is the user's right; expose as POST /me/wipe
-- and cover with a test that asserts zero rows remain in claim/action/capacity_signal.
