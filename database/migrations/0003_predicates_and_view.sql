-- 0003 — make the ledger's constraints say what the code already promises.
--
-- Four defects, all found by *writing* to the schema rather than reading it, and none of them visible
-- from a unit test of the component that owns them:
--
--  1. `claim.predicate` did not permit `submitted` / `submitted_at` / `schedule_change`, although
--     `router.ALLOWED_PREDICATES` — what the pipeline may legitimately produce — does. A submission
--     receipt, the only evidence that separates "past its date" from "missed it", was unrepresentable:
--     every such write died in `ClaimWriteError` and reached the user as a schema objection.
--  2. `claim.method` permitted only rule/ics/lms_api/llm/llm+verified, so a *human* answer to a review
--     question had no method value and `resolve_review(keep_own)` could only promise that the answer
--     "becomes the source of truth". `claim_evidence.method`'s own comment listed `manual`, so even the
--     sibling table expected it.
--  3. `claim`'s UNIQUE key was `(subject, predicate, source, char_start, char_end)` — span identity, not
--     fact identity. Re-ingesting one document whose layout shifted by a character inserted a second
--     *trusted* claim for the same deadline, and "the portal says X twice" outvoted "the syllabus says Y
--     once". `db._promote` enforced the right key in Python, which only helped callers going through it:
--     `write_claim`, the door most code uses, still duplicated (pinned by
--     `tests/test_twin_db.py::test_re_ingest_does_not_duplicate_the_ledger`).
--  4. `claim_lineage_graph` (0002) referenced `s.label` and `c.verified`; neither exists (`label` lives
--     on `source_meta`; `claim` has `verify_state`). No code selected from it, so it sat broken in the
--     schema since it was written. A view nobody queries is documentation that happens to be SQL — hence
--     `tests/test_twin_db.py::test_every_view_in_the_schema_is_selectable`.
--
-- WHY THIS FILE IS LONG: SQLite cannot ALTER a CHECK or a UNIQUE, so `claim` is rebuilt. But
-- `ALTER TABLE … RENAME TO` also rewrites every *inbound* foreign-key clause, which would leave
-- `claim_evidence`, `task` and `conflict` pointing at the temporary name — invisible while
-- `PRAGMA foreign_keys` is OFF (pysqlite's default, so every runtime query here) and fatal the moment a
-- later migration rebuilds one of them. So the three children are rebuilt too. `claim_provenance` is a
-- cache written with `INSERT OR REPLACE` keyed on `claim_id` (its own PK), so it needs no rebuild.
--
-- HOW IT STAYS SAFE: `DB.migrate()` runs this whole file inside one transaction and records it in
-- `applied_migrations` only on success, then asserts `PRAGMA foreign_key_check` is empty. A failure
-- here leaves the database exactly as it was and retries on the next start.

-- 1 ─ claim: preserved, rebuilt with wider CHECK lists and fact identity ─────────────────────────────
CREATE TABLE claim__pre0003 AS SELECT * FROM claim;

DROP TABLE claim;

CREATE TABLE claim (
  id INTEGER PRIMARY KEY,
  subject_type TEXT NOT NULL CHECK (subject_type IN
    ('task','session','attendance','grade','policy','capacity','wellness')),
  subject_id TEXT NOT NULL,
  -- Storage contract, deliberately a superset of `router.ALLOWED_PREDICATES` (an input filter): manual
  -- entries and legacy rows exist. tests/test_twin_db.py asserts it never goes narrower than the router.
  predicate TEXT NOT NULL CHECK (predicate IN
    ('due_at','released_at','weight','late_policy','submission_channel','submission_time',
     'submitted','submitted_at','schedule_change','venue','attendance_pct','grade_pct','exam_date',
     'review_gap','clause','capacity_rate','sleep_floor_min')),
  value_json TEXT NOT NULL,
  source_id INTEGER NOT NULL REFERENCES source(id),
  evidence_span TEXT NOT NULL CHECK (length(evidence_span) >= 12),
  char_start INTEGER NOT NULL DEFAULT -1,
  char_end   INTEGER NOT NULL DEFAULT -1,
  confidence REAL NOT NULL DEFAULT 0.5,
  verify_state TEXT NOT NULL DEFAULT 'unverified'
    CHECK (verify_state IN ('grounded','grounded_relative_ambiguous','review','rejected')),
  -- `manual` is what lets a human answer become a ledger row rather than only a closed review item.
  method TEXT NOT NULL DEFAULT 'llm'
    CHECK (method IN ('rule','ics','lms_api','llm','llm+verified','manual')),
  recorded_at TEXT NOT NULL,
  valid_from TEXT NOT NULL,
  valid_to TEXT,                       -- NULL = currently believed; never deleted on change
  supersedes INTEGER REFERENCES claim(id),
  superseded_by INTEGER REFERENCES claim(id),
  run_id INTEGER REFERENCES run(id),
  -- No UNIQUE table constraint, and none after the foreign keys, on purpose: the identity that matters
  -- ("one open fact per subject+predicate+source+value") must exclude `valid_to` so retired rows stay
  -- readable as history, which only a partial index can express; and SQLite's column-list grammar
  -- accepts FK clauses before table constraints but not after them (a UNIQUE written last is what
  -- produced `near "run_id": syntax error` the first time this file was run).
  method_detail TEXT NOT NULL DEFAULT ''
);

-- Collapse span-duplicates *before* inserting, so a database that accumulated them migrates instead of
-- aborting on the new index. The survivor is the earliest row: that is the id every receipt and every UI
-- link in the existing history already points at.
CREATE TEMP TABLE claim__survivor AS
  SELECT min(id) AS keep_id, subject_type, subject_id, predicate, source_id, value_json
  FROM claim__pre0003 GROUP BY subject_type, subject_id, predicate, source_id, value_json;

INSERT INTO claim (id, subject_type, subject_id, predicate, value_json, source_id, evidence_span,
                   char_start, char_end, confidence, verify_state, method, recorded_at, valid_from,
                   valid_to, supersedes, superseded_by, run_id, method_detail)
SELECT o.id, o.subject_type, o.subject_id, o.predicate, o.value_json, o.source_id, o.evidence_span,
       o.char_start, o.char_end, o.confidence, o.verify_state,
       CASE WHEN o.method = 'manual' THEN 'llm' ELSE o.method END,  -- 'manual' never existed pre-0003
       o.recorded_at, o.valid_from, o.valid_to,
       CASE WHEN o.supersedes IS NULL THEN NULL
            ELSE (SELECT s.keep_id FROM claim__survivor s
                   WHERE s.keep_id = o.supersedes)                  -- chains re-pointed, not orphaned
       END,
       CASE WHEN o.superseded_by IS NULL THEN NULL
            WHEN EXISTS (SELECT 1 FROM claim__survivor s WHERE s.keep_id = o.superseded_by)
            THEN o.superseded_by
            ELSE (SELECT s.keep_id FROM claim__survivor s
                   WHERE s.subject_type = o.subject_type AND s.subject_id = o.subject_id
                     AND s.predicate = o.predicate LIMIT 1)
       END,
       o.run_id, COALESCE(o.method_detail, '')
FROM claim__pre0003 o
WHERE o.id IN (SELECT keep_id FROM claim__survivor)
   OR o.valid_to IS NOT NULL;          -- retired rows are history; duplicates among them are allowed

DROP TABLE claim__pre0003;

-- Fact identity for open rows. Partial, because supersession is the whole point of the bi-temporal
-- ledger: a retired row must survive alongside its replacement rather than violate the constraint.
CREATE UNIQUE INDEX ux_claim_fact
  ON claim(subject_type, subject_id, predicate, source_id, value_json) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS ix_claim_open ON claim(subject_type, subject_id, predicate) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS ix_claim_src  ON claim(source_id);
CREATE INDEX IF NOT EXISTS ix_claim_run  ON claim(run_id, verify_state);

-- 2 ─ the inbound-FK children, repointed at the rebuilt parent ───────────────────────────────────────
CREATE TABLE claim_evidence__pre0003 AS SELECT * FROM claim_evidence;
DROP TABLE claim_evidence;
CREATE TABLE claim_evidence (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER NOT NULL,
    source_id INTEGER,
    observation_id INTEGER,
    quote TEXT NOT NULL,
    offset_start INTEGER NOT NULL DEFAULT 0,
    offset_end INTEGER NOT NULL DEFAULT 0,
    verifier TEXT NOT NULL DEFAULT 'ground',
    checks_json TEXT NOT NULL DEFAULT '[]',
    verified INTEGER NOT NULL DEFAULT 0,
    reject_reason TEXT NOT NULL DEFAULT '',
    llm_involved INTEGER NOT NULL DEFAULT 0,
    provider TEXT NOT NULL DEFAULT 'rules',
    model TEXT NOT NULL DEFAULT '',
    model_confidence REAL NOT NULL DEFAULT 0,    -- evidence only; never trust
    method TEXT NOT NULL DEFAULT 'llm',          -- rule|llm+verified|cloud+verified|manual
    created_at TEXT NOT NULL,
    -- Receipts are keyed to the span they came from, so two quotes for one claim are two receipts. That
    -- is legitimate (a document may state the same thing twice) and stays a table constraint: only the
    -- *claim* needed fact identity, not the evidence.
    UNIQUE (claim_id, source_id, offset_start, quote),
    -- FK on claim_id, not just an index: an evidence row pointing at a claim that no longer exists is the
    -- exact shape of a broken receipt, and without the constraint nothing notices until a UI panel
    -- renders blanks.
    FOREIGN KEY(claim_id) REFERENCES claim(id) ON DELETE CASCADE
);
-- A receipt whose claim was collapsed away is a receipt for nothing: keeping it would be keeping a
-- dangling pointer that `ON DELETE CASCADE` was written to prevent.
INSERT INTO claim_evidence (id, claim_id, source_id, observation_id, quote, offset_start, offset_end,
                            verifier, checks_json, verified, reject_reason, llm_involved, provider,
                            model, model_confidence, method, created_at)
SELECT e.id, e.claim_id, e.source_id, e.observation_id, e.quote, e.offset_start, e.offset_end,
       e.verifier, e.checks_json, e.verified, e.reject_reason, e.llm_involved, e.provider,
       e.model, e.model_confidence, e.method, e.created_at
FROM claim_evidence__pre0003 e
WHERE e.claim_id IN (SELECT id FROM claim)
  AND NOT EXISTS (SELECT 1 FROM claim_evidence__pre0003 x
                   WHERE x.id <> e.id AND x.claim_id = e.claim_id
                     AND x.source_id IS e.source_id AND x.offset_start = e.offset_start
                     AND x.quote = e.quote AND x.id < e.id);
DROP TABLE claim_evidence__pre0003;
CREATE INDEX IF NOT EXISTS ix_ev_claim ON claim_evidence(claim_id);

-- The lineage graph must not lose edges when a duplicate collapses, and must not keep self-edges.
CREATE TABLE claim_lineage__pre0003 AS SELECT * FROM claim_lineage;
DROP TABLE claim_lineage;
CREATE TABLE claim_lineage (
    id INTEGER PRIMARY KEY,
    from_claim INTEGER NOT NULL,
    to_claim INTEGER NOT NULL,
    edge TEXT NOT NULL,                 -- supersedes|conflicts_with|derived_from|resolved_by|
                                        -- supports|contradicts_injection
    why TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (from_claim, to_claim, edge)
);
INSERT INTO claim_lineage (id, from_claim, to_claim, edge, why, created_at)
SELECT l.id, l.from_claim, l.to_claim, l.edge, l.why, l.created_at
FROM claim_lineage__pre0003 l
WHERE l.from_claim IN (SELECT id FROM claim) AND l.to_claim IN (SELECT id FROM claim)
  AND l.from_claim <> l.to_claim
  AND NOT EXISTS (SELECT 1 FROM claim_lineage__pre0003 x
                   WHERE x.id <> l.id AND x.from_claim = l.from_claim
                     AND x.to_claim = l.to_claim AND x.edge = l.edge AND x.id < l.id);
DROP TABLE claim_lineage__pre0003;
CREATE INDEX IF NOT EXISTS ix_lin_from ON claim_lineage(from_claim);
CREATE INDEX IF NOT EXISTS ix_lin_to   ON claim_lineage(to_claim);

CREATE TABLE task__pre0003 AS SELECT * FROM task;
DROP TABLE task;
CREATE TABLE task (
  id TEXT PRIMARY KEY,
  course_id INTEGER REFERENCES course(id),
  title TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'assignment',
  weight REAL,
  est_minutes INTEGER, actual_minutes INTEGER,
  status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open','in_progress','done','missed','not_found','withdrawn')),
  due_claim_id INTEGER REFERENCES claim(id)      -- the safe value chosen by reconciliation
);
INSERT INTO task (id, course_id, title, kind, weight, est_minutes, actual_minutes, status, due_claim_id)
SELECT id, course_id, title, kind, weight, est_minutes, actual_minutes, status, due_claim_id
FROM task__pre0003;
DROP TABLE task__pre0003;

CREATE TABLE conflict__pre0003 AS SELECT * FROM conflict;
DROP TABLE conflict;
CREATE TABLE conflict (
  id INTEGER PRIMARY KEY,
  subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, predicate TEXT NOT NULL,
  claim_ids TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('date','weight','schedule','existence','missing_date','venue')),
  severity TEXT NOT NULL CHECK (severity IN ('LOW','MED','HIGH')),
  state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','accepted_risk','resolved','dismissed')),
  rule TEXT NOT NULL,
  resolution_claim_id INTEGER REFERENCES claim(id),
  opened_at TEXT NOT NULL, resolved_at TEXT
);
INSERT INTO conflict (id, subject_type, subject_id, predicate, claim_ids, kind, severity, state, rule,
                      resolution_claim_id, opened_at, resolved_at)
SELECT id, subject_type, subject_id, predicate, claim_ids, kind, severity, state, rule,
       resolution_claim_id, opened_at, resolved_at
FROM conflict__pre0003;
DROP TABLE conflict__pre0003;

-- 3 ─ views: DROP then CREATE, never `IF NOT EXISTS` ─────────────────────────────────────────────────
-- An `IF NOT EXISTS` on a view whose definition is wrong keeps the wrong definition and reports success.
-- That is precisely how defect 4 survived every migration since 0002, so all three are recreated here.
DROP VIEW IF EXISTS task_safe;
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

DROP VIEW IF EXISTS live_claims;
CREATE VIEW live_claims AS
SELECT * FROM claim WHERE valid_to IS NULL AND verify_state LIKE 'grounded%';

DROP VIEW IF EXISTS claim_lineage_graph;
CREATE VIEW claim_lineage_graph AS
SELECT c.id AS claim_id, c.subject_type, c.subject_id, c.predicate,
       c.source_id, s.kind AS source_kind, COALESCE(m.label, s.uri, '') AS source_label,
       c.verify_state, c.confidence, c.method, c.valid_from, c.valid_to,
       l.edge AS edge, l.to_claim AS related_claim, l.why AS edge_why,
       e.quote AS quote, e.checks_json AS checks, e.llm_involved, e.provider, e.model
FROM claim c
LEFT JOIN source s           ON s.id = c.source_id
LEFT JOIN source_meta m      ON m.source_id = c.source_id
LEFT JOIN claim_lineage l    ON l.from_claim = c.id
LEFT JOIN claim_evidence e   ON e.claim_id = c.id;
