-- 0002_twin.sql — the academic-twin layer: observations, claim lineage, typed change events,
-- risk forecasts, action requests, model invocations, review queue.
--
-- Design rules that matter more than the individual columns:
--   * Nothing here overwrites 0001. `claim` stays the trusted-state table; these tables are the
--     *evidence and history* around it. Dropping 0002 loses the audit story, not the product.
--   * Every table keeps its own timeline. No UPDATE ... SET old_value = new_value anywhere.
--   * content_hash addresses a file on disk, so the DB row stays small and the raw text can be
--     purged by the retention job while the receipt survives.

PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------------ ingest ---
CREATE TABLE IF NOT EXISTS observation (
    id INTEGER PRIMARY KEY,
    run_id TEXT,
    source_id INTEGER,
    kind TEXT NOT NULL,                 -- whatsapp_line|email|ics_event|lms_export|erp_table|
                                        -- syllabus_block|notice_photo_ocr|assignment_sheet|
                                        -- manual_entry|telegram|discord|announcement
    subject_ref TEXT,                   -- what the observation is about, before attribution
    content TEXT NOT NULL,              -- the observed text (retention-managed)
    content_hash TEXT NOT NULL,
    offset_start INTEGER NOT NULL DEFAULT 0,
    offset_end INTEGER NOT NULL DEFAULT 0,
    occurred_at TEXT,                   -- when the world said it
    captured_at TEXT NOT NULL,          -- when we saw it
    status TEXT NOT NULL DEFAULT 'ingested',   -- ingested|extracted|skipped|error
    error TEXT,
    meta_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source_id, content_hash, offset_start)      -- idempotent re-ingest, in the schema
);
CREATE INDEX IF NOT EXISTS ix_obs_time ON observation(source_id, occurred_at);

CREATE TABLE IF NOT EXISTS model_invocation (
    id INTEGER PRIMARY KEY,
    run_id TEXT,
    observation_id INTEGER,
    provider TEXT NOT NULL,             -- ollama|gemini|openai_compat|rules
    model TEXT NOT NULL,
    model_version TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL,              -- extract|classify|draft|summarize
    schema_sha TEXT NOT NULL DEFAULT '',
    egress TEXT NOT NULL,               -- LOCAL | CLOUD | REDACTED_CLOUD | NONE
    bytes_in INTEGER NOT NULL DEFAULT 0,
    bytes_out INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    est_cost_usd REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL,              -- SUCCESS|PARTIAL|UNAVAILABLE|INVALID_OUTPUT|TIMEOUT
    json_valid INTEGER NOT NULL DEFAULT 1,
    claims_returned INTEGER NOT NULL DEFAULT 0,
    claims_grounded INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_inv_run ON model_invocation(run_id, created_at);

-- ------------------------------------------------------- claim provenance ---
-- One row per (claim, evidence). Kept separate from `claim` so a claim can carry several quotes
-- from the same source (a deadline stated twice in a syllabus is two receipts, not one).
CREATE TABLE IF NOT EXISTS claim_evidence (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER NOT NULL,
    source_id INTEGER,
    observation_id INTEGER,
    quote TEXT NOT NULL,                -- verbatim, always
    offset_start INTEGER NOT NULL DEFAULT 0,
    offset_end INTEGER NOT NULL DEFAULT 0,
    verifier TEXT NOT NULL DEFAULT 'ground',
    checks_json TEXT NOT NULL DEFAULT '[]',      -- e.g. ["span","date","late_days"]
    verified INTEGER NOT NULL DEFAULT 0,
    reject_reason TEXT NOT NULL DEFAULT '',
    llm_involved INTEGER NOT NULL DEFAULT 0,
    provider TEXT NOT NULL DEFAULT 'rules',
    model TEXT NOT NULL DEFAULT '',
    model_confidence REAL NOT NULL DEFAULT 0,    -- evidence only; never trust
    method TEXT NOT NULL DEFAULT 'llm',          -- rule|llm+verified|cloud+verified|manual
    created_at TEXT NOT NULL,
    -- FK on claim_id, not just an index: an evidence row pointing at a claim that no longer exists
    -- is the exact shape of a broken receipt, and without the constraint nothing anywhere would
    -- notice until a UI panel rendered blanks.
    FOREIGN KEY(claim_id) REFERENCES claim(id) ON DELETE CASCADE
    UNIQUE (claim_id, source_id, offset_start, quote)
);
CREATE INDEX IF NOT EXISTS ix_ev_claim ON claim_evidence(claim_id);

-- The lineage graph: how a trusted claim came to be, what it replaced, what disputes it.
CREATE TABLE IF NOT EXISTS claim_lineage (
    id INTEGER PRIMARY KEY,
    from_claim INTEGER NOT NULL,
    to_claim INTEGER NOT NULL,
    edge TEXT NOT NULL,                 -- supersedes|conflicts_with|derived_from|resolved_by|
                                        -- supports|contradicts_injection
    why TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (from_claim, to_claim, edge)
);
CREATE INDEX IF NOT EXISTS ix_lin_from ON claim_lineage(from_claim);
CREATE INDEX IF NOT EXISTS ix_lin_to   ON claim_lineage(to_claim);

-- Flat, denormalised answer to "where did this come from?" — one row per claim, assembled by the
-- app at promotion time so the UI's single click is a SELECT, not a graph walk.
CREATE TABLE IF NOT EXISTS claim_provenance (
    claim_id INTEGER PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    predicate TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source_label TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT '',
    authority REAL NOT NULL DEFAULT 0,
    quote TEXT NOT NULL DEFAULT '',
    offset_start INTEGER NOT NULL DEFAULT 0,
    offset_end INTEGER NOT NULL DEFAULT 0,
    ingested_at TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL DEFAULT '',
    llm_involved INTEGER NOT NULL DEFAULT 0,
    provider TEXT NOT NULL DEFAULT 'rules',
    model TEXT NOT NULL DEFAULT '',
    checks_passed TEXT NOT NULL DEFAULT '',
    validation_status TEXT NOT NULL DEFAULT 'verified',  -- verified|disputed|expired|review
    superseded_by INTEGER,
    supersedes INTEGER,
    conflict_ids TEXT NOT NULL DEFAULT '',
    lineage_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);

-- ------------------------------------------------------------ temporal ops ---
CREATE TABLE IF NOT EXISTS change_event (
    id INTEGER PRIMARY KEY,
    run_id TEXT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- DEADLINE_CHANGED|POLICY_CHANGED|ROOM_CHANGED|
                                        -- LECTURE_CANCELLED|ASSIGNMENT_SCOPE_CHANGED|
                                        -- NEW_REQUIREMENT|SOURCE_CONFLICT|ATTENDANCE_RISK_CHANGED|
                                        -- FEASIBILITY_STATUS_CHANGED|CLAIM_RETIRED|REMEDIY_OPENED|
                                        -- INJECTION_QUARANTINED|REVIEW_RESOLVED
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    predicate TEXT NOT NULL DEFAULT '',
    old_value TEXT NOT NULL DEFAULT '',
    new_value TEXT NOT NULL DEFAULT '',
    source_id INTEGER,
    claim_id INTEGER,
    why TEXT NOT NULL DEFAULT '',       -- the human-readable mechanism, computed not generated
    effect TEXT NOT NULL DEFAULT '',    -- what the system did about it
    severity TEXT NOT NULL DEFAULT 'INFO'
);
-- Three more columns for the typed-change layer, added here rather than baked into the table above
-- so the migration stays the single definition of the schema for a fresh database too. `delta_json`
-- is what makes a change reproducible from the DB alone (direction, which source overruled which,
-- the verbatim span); `buffer_delta_hours` is the arithmetic the severity was derived from.
ALTER TABLE change_event ADD COLUMN delta_json TEXT NOT NULL DEFAULT '';
ALTER TABLE change_event ADD COLUMN buffer_delta_hours REAL NOT NULL DEFAULT 0;
ALTER TABLE change_event ADD COLUMN detected_by TEXT NOT NULL DEFAULT 'deterministic_diff';
CREATE INDEX IF NOT EXISTS ix_change_time ON change_event(ts DESC);
CREATE INDEX IF NOT EXISTS ix_change_subj ON change_event(subject_type, subject_id, ts DESC);

-- ------------------------------------------------------------ decisions -----
CREATE TABLE IF NOT EXISTS risk_forecast (
    id INTEGER PRIMARY KEY,
    run_id TEXT,
    computed_at TEXT NOT NULL,
    horizon_start TEXT NOT NULL,
    horizon_end TEXT NOT NULL,
    subject_type TEXT NOT NULL DEFAULT 'week',
    subject_id TEXT NOT NULL DEFAULT 'current',
    probability REAL NOT NULL,          -- modelled estimate, NOT a proof
    method TEXT NOT NULL,               -- solver_enumeration | capacity_ratio | attendance_closed_form
    basis_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,               -- ON_TRACK|ELEVATED|LIKELY_INFEASIBLE|PROVEN_INFEASIBLE
    evidence TEXT NOT NULL DEFAULT '',
    recommendation TEXT NOT NULL DEFAULT '',
    is_prediction INTEGER NOT NULL DEFAULT 1,   -- prediction vs proven fact: the label is in the row
    UNIQUE (run_id, subject_id, horizon_end, method)
);

CREATE TABLE IF NOT EXISTS action_request (
    id INTEGER PRIMARY KEY,
    run_id TEXT,
    requested_at TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- create_event|set_reminder|draft_email|send_email|
                                        -- submit_assignment|mark_attendance|drop_course
    risk_class TEXT NOT NULL,           -- READ_ONLY|REVERSIBLE|LOW_RISK_WRITE|HIGH_IMPACT_WRITE|IRREVERSIBLE
    target TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    justification TEXT NOT NULL DEFAULT '',     -- the solver/policy output, verbatim
    supporting_claims TEXT NOT NULL DEFAULT '[]',
    solver_status TEXT NOT NULL DEFAULT '',
    policy_decision TEXT NOT NULL,      -- AUTO_OK|APPROVAL_REQUIRED|REFUSED
    policy_reason TEXT NOT NULL DEFAULT '',
    approval_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',     -- pending|approved|rejected|executed|failed|dry_run
    executor_result TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL DEFAULT '',
    finished_at TEXT,
    UNIQUE (idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_act_status ON action_request(status, requested_at DESC);

CREATE TABLE IF NOT EXISTS approval (
    id INTEGER PRIMARY KEY,
    action_id INTEGER NOT NULL,
    requested_at TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'web',        -- web|telegram|cli
    decided_at TEXT,
    decision TEXT NOT NULL DEFAULT 'pending',   -- pending|approved|rejected|expired
    decided_by TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS review_item (
    id INTEGER PRIMARY KEY,
    run_id TEXT,
    created_at TEXT NOT NULL,
    question TEXT NOT NULL,
    why TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,                 -- AMBIGUOUS_DATE|CONFLICTING_STATEMENTS|SUBJECT_ATTRIBUTION|
                                        -- INJECTION_SUSPECT|HIGH_IMPACT_ACTION|UNRESOLVED_AUTHORITY
    subject_type TEXT NOT NULL DEFAULT 'task',
    subject_id TEXT NOT NULL DEFAULT '',
    claim_id INTEGER,
    source_id INTEGER,
    options_json TEXT NOT NULL DEFAULT '[]',    -- candidate interpretations, each with consequences
    evidence_json TEXT NOT NULL DEFAULT '[]',   -- quotes shown inline so no re-lookup is needed
    recommended INTEGER NOT NULL DEFAULT -1,
    status TEXT NOT NULL DEFAULT 'open',        -- open|answered|dismissed|expired
    resolution TEXT NOT NULL DEFAULT '',
    resolved_at TEXT,
    resolved_by TEXT NOT NULL DEFAULT '',
    consequence TEXT NOT NULL DEFAULT ''        -- what the answer caused, recorded after the fact
);
CREATE INDEX IF NOT EXISTS ix_review_open ON review_item(status, created_at DESC);

-- ------------------------------------------------------------- integration ---
CREATE TABLE IF NOT EXISTS integration (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL UNIQUE,          -- google_calendar|lms|erp|telegram|email|discord
    label TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'simulation',    -- live|simulation|off
    capability TEXT NOT NULL DEFAULT 'read',    -- read|write
    credential_ref TEXT NOT NULL DEFAULT '',    -- keyring/env name only; never the secret
    last_sync_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS doc_store (
    sha256 TEXT PRIMARY KEY,
    bytes INTEGER NOT NULL,
    media_type TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL DEFAULT '',
    stored_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    purged_at TEXT                      -- retention keeps the receipt, drops the raw text
);

-- Convenience: the "show me why you believe this" query in one view.
CREATE VIEW IF NOT EXISTS claim_lineage_graph AS
SELECT c.id AS claim_id, c.subject_type, c.subject_id, c.predicate,
       c.source_id, s.kind AS source_kind, s.label AS source_label,
       c.verified, c.confidence, c.method, c.valid_from, c.valid_to,
       l.edge AS edge, l.to_claim AS related_claim, l.why AS edge_why,
       e.quote AS quote, e.checks_json AS checks, e.llm_involved, e.provider, e.model
FROM claim c
LEFT JOIN source s          ON s.id = c.source_id
LEFT JOIN claim_lineage l   ON l.from_claim = c.id
LEFT JOIN claim_evidence e   ON e.claim_id = c.id;

-- ----------------------------------------------------------------- helpers ---
-- The original ingestion identity. `source.kind` carries a CHECK list from 0001; new adapters
-- (telegram, discord, LMS export, OCR image...) must not require altering a working table, so the
-- unconstrained kind + human label live here and callers read through it.
CREATE TABLE IF NOT EXISTS source_meta (
    source_id INTEGER PRIMARY KEY REFERENCES source(id),
    orig_kind TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    chars INTEGER NOT NULL DEFAULT 0,
    captured_at TEXT NOT NULL DEFAULT '',
    full_text TEXT NOT NULL DEFAULT '',   -- the receipt, so re-verification is possible offline;
                                          -- purged by the retention job (see alibi_retention)
    purged_at TEXT
);

CREATE TABLE IF NOT EXISTS applied_migrations (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    sha TEXT NOT NULL
);

-- 0001's `conflict` has no uniqueness rule, so a naive sync would insert a fresh row for the same
-- disagreement on every refresh. This index makes the materialisation idempotent *while* still
-- allowing a resolved conflict to be re-opened later (state is part of the key).
CREATE UNIQUE INDEX IF NOT EXISTS ux_conflict_open ON
    conflict(state, subject_type, subject_id, predicate);

-- 0001's `claim` had no run attribution, which makes "what did this ingestion produce?" a guess.
-- One added column, no rewrite of a table that already works.
ALTER TABLE claim ADD COLUMN run_id INTEGER REFERENCES run(id);
ALTER TABLE claim ADD COLUMN method_detail TEXT;   -- SQLite forbids a non-constant default in
UPDATE claim SET method_detail = '' WHERE method_detail IS NULL;   -- ADD COLUMN, so backfill instead
CREATE INDEX IF NOT EXISTS ix_claim_run ON claim(run_id, verify_state);

-- `audit` exists in 0001 but has no run linkage, and the UI's "everything this run touched" view
-- needs one. Additive, so an existing database keeps its log intact.
ALTER TABLE audit ADD COLUMN run_id TEXT;
CREATE INDEX IF NOT EXISTS ix_audit_run ON audit(run_id, id DESC);
