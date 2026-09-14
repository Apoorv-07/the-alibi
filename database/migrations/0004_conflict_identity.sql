-- 0004 — a conflict has an identity, so re-syncing cannot mint copies of it.
--
-- `sync_conflicts()` used `INSERT OR IGNORE`, whose whole trick is a UNIQUE constraint to push against —
-- and `conflict` had none. Every sync therefore inserted a fresh row per disagreement, because `opened_at`
-- (always `now()`) differs, and `OR IGNORE` had nothing to ignore. Measured after three syncs of the demo
-- corpus: the UI reported 26 open conflicts where there are 3 disagreements, and the "8×… changed" tail of
-- the change summary was the same leak. The count a reviewer checks is the one they can reproduce, so:
-- collapse the duplicates, then constrain the table so this cannot come back quietly.
--
-- Retired conflicts (`state != 'open'`) are deliberately exempt: re-opening the same disagreement next
-- month should be a new row with its own history, which is the whole point of the bi-temporal ledger.

-- 1 ─ keep the earliest row per (subject, predicate), drop the copies ─────────────────────────────────
CREATE TEMP TABLE conflict__keep AS
  SELECT min(id) AS keep_id, subject_type, subject_id, predicate
  FROM conflict WHERE state = 'open' GROUP BY subject_type, subject_id, predicate;

DELETE FROM conflict
 WHERE state = 'open'
   AND id NOT IN (SELECT keep_id FROM conflict__keep);

-- 2 ─ the constraint the code was pretending to rely on ──────────────────────────────────────────────
CREATE UNIQUE INDEX ux_conflict_open
  ON conflict(subject_type, subject_id, predicate) WHERE state = 'open';

-- 3 ─ nothing else.
--     `sync_conflicts()` also wrote `str(conflict.id)` into `review_item.run_id`, which is a *run* id.
--     It survives only because the column is TEXT. Fixed at the writer rather than here: rewriting
--     historical rows to NULL would look tidy and accomplish nothing, since no code reads that field for
--     reviews of this kind — and a migration should change data when the data is the problem, not when
--     the producer was.
