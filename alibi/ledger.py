"""Alibi — the claim ledger (temporal, per-field provenance, supersession).

SQLite-shaped logic implemented over plain dicts so the demo runs without a DB file,
but every function mirrors the SQL in database/schema.sql one-for-one.

Two invariants, enforced here (not in prompts):
  1. verify=False => the claim can never become the currently-valid value; it goes to review.
  2. supersession is recorded both ways; nothing is deleted. Receipts survive.
"""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Source:
    id: int
    kind: str
    captured_at: str          # when *we* saw it
    effective_at: str         # when the *document* asserted it
    trust_prior: float = 0.6
    label: str = ""

    @staticmethod
    def fingerprint(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class Claim:
    id: int
    subject_type: str          # task|session|attendance|grade|policy|capacity|wellness
    subject_id: str
    predicate: str             # due_at|weight|late_policy|attendance_pct|...
    value: dict[str, Any]
    source_id: int
    evidence_span: str
    confidence: float = 0.5
    verified: bool = False
    valid_from: str = ""          # recorded_at == when WE learned it (supersession axis)
    valid_to: str | None = None
    supersedes: int | None = None
    superseded_by: int | None = None
    method: str = "llm"

    def as_row(self) -> dict:
        return asdict(self)


class Ledger:
    def __init__(self, policy: dict | None = None) -> None:
        self.sources: dict[int, Source] = {}
        self.claims: list[Claim] = []
        self.review: list[Claim] = []      # ungrounded: visible, never trusted
        self.conflicts_found: list[dict] = []
        self.policy = policy or {}
        # Resolved ONCE, deliberately conservative: a source that cannot bear the cost of being
        # wrong never overwrites an authoritative claim, whether or not a policy was supplied.
        # (A `policy.get(...)` buried in the rule would make an unconfigured ledger behave
        # differently from a configured one, which is the kind of default that ships a bug.)
        self._floor = float(self.policy.get("supersede_trust_floor", 0.5))
        self._id = itertools.count(1)

    # ---------- ingestion ----------
    def add_source(self, src: Source) -> Source:
        self.sources[src.id] = src
        return src

    def upsert(self, subject_type: str, subject_id: str, predicate: str,
               value: dict, source: Source, evidence_span: str, *, verify: bool,
               confidence: float = 0.9, method: str = "llm") -> Claim | None:
        """Idempotent, verified-gated, supersession-recording write."""
        if not verify:
            c = Claim(next(self._id), subject_type, subject_id, predicate, value,
                      source.id, evidence_span, confidence, verified=False,
                      valid_from=source.captured_at, method=method)
            self.review.append(c)
            return None

        # idempotency: same subject+predicate+source+same value => no-op (safe re-ingest)
        for c in self.open_claims(subject_type, subject_id, predicate):
            if c.source_id == source.id and c.value == value:
                return c

        new = Claim(next(self._id), subject_type, subject_id, predicate, value,
                    source.id, evidence_span, confidence, verified=True,
                    valid_from=source.captured_at, method=method)
        # A claim that supersedes an older *assertion* retires it (receipt kept).
        # A claim that merely *disagrees* with a live one must NOT be discarded and must
        # NOT overwrite it: both stay open so the reconciler reports a conflict. Silently
        # dropping the loser is the exact failure this product exists to eliminate.
        for old in self.open_claims(subject_type, subject_id, predicate):
            if self._supersedes(old, new, source):
                old.valid_to = new.valid_from      # retired when the newer record arrived
                old.superseded_by = new.id
                new.supersedes = old.id
                break
            if old.value != value:
                # Not a supersession, a *challenge*: a weaker source tried to overwrite an
                # authoritative one. Recorded because it is worth more than the disagreement
                # itself — it tells the reconciler (and the user) that somebody is already acting
                # on the rumour, which is exactly when a nudge prevents a missed deadline.
                self.conflicts_found.append(
                    {"subject_type": subject_type, "subject_id": subject_id,
                     "predicate": predicate,
                     "kept": {"value": old.value, "source_id": old.source_id,
                              "trust": self.sources[old.source_id].trust_prior},
                     "rejected_supersession": {"value": value, "source_id": source.id,
                                               "trust": source.trust_prior,
                                               "because": "below supersede_trust_floor"}})
        self.claims.append(new)
        return new

    def _supersedes(self, old: Claim, new: Claim, source: Source) -> bool:
        """Recency is necessary but NOT sufficient — and this is the single most load-bearing rule
        in the product.

        Plain 'later recorded wins' is what a naive sync does, and it is catastrophic here: the
        newest thing a student's phone saw is usually a rumour in a class group, while the
        authoritative artefact (the syllabus PDF, uploaded once at enrolment) is permanently the
        oldest. Recency alone lets 'prof said maybe Monday' retire a signed academic document.

        So a newer claim retires an older one only when its source is strong enough to bear the
        cost of being wrong (policy `supersede_trust_floor`, default 0.5). Below the floor the two
        coexist as an open conflict, the reconciler plans against the safer date, and a human sees
        both receipts. Deterministic either way — recency decides *among equals*, trust decides
        *whether it may overwrite at all*.
        """
        if new.valid_from > old.valid_from:
            return source.trust_prior >= self._floor
        if new.valid_from == old.valid_from:
            return source.trust_prior > Ledger._trust(old)
        return False

    @staticmethod
    def _trust(c: Claim) -> float:
        return c.confidence

    # ---------- queries ----------
    def open_claims(self, subject_type: str | None = None, subject_id: str | None = None,
                    predicate: str | None = None) -> list[Claim]:
        out = []
        for c in self.claims:
            if c.valid_to is not None:
                continue
            if subject_type and c.subject_type != subject_type:
                continue
            if subject_id and c.subject_id != subject_id:
                continue
            if predicate and c.predicate != predicate:
                continue
            out.append(c)
        return out

    def history(self, subject_id: str, predicate: str) -> list[Claim]:
        return [c for c in self.claims
                if c.subject_id == subject_id and c.predicate == predicate]

    # ---------- reconciliation (pure code; no LLM) ----------
    def conflicts(self, policy: dict) -> list[dict]:
        """A conflict = >1 distinct asserted value for a task's predicate from different sources."""
        out = []
        pairs = sorted({(c.subject_type, c.subject_id) for c in self.open_claims()})
        for st, sid in pairs:
            for pred in {c.predicate for c in self.open_claims(st, sid)}:
                cs = self.open_claims(st, sid, pred)
                vals = {repr(c.value) for c in cs}
                if len(cs) >= 2 and len(vals) >= 2:
                    challenged = any(e["subject_id"] == sid and e["predicate"] == pred
                                     for e in self.conflicts_found)
                    sev = self._severity(sid, pred, policy)
                    if challenged and sev == "LOW":
                        # A claim from a weaker source that tried to *overwrite* an authoritative
                        # one is itself a signal, independent of marks at stake: someone is acting
                        # on a rumour. Escalate so the human resolves it before it spreads.
                        sev = "MEDIUM"
                    out.append({
                        "subject_type": st, "subject_id": sid, "predicate": pred,
                        "claim_ids": [c.id for c in cs],
                        "values": [c.value for c in cs],
                        "severity": sev,
                        "authority_challenge": challenged,
                        "safe_value": self.safe_value(st, sid, pred, policy),
                        "policy": policy["precedence"],
                    })
        return out

    def _severity(self, subject_id: str, predicate: str, policy: dict) -> str:
        if predicate != "due_at":
            return "LOW"
        w = self.open_claims("task", subject_id, "weight")
        weight = float(w[0].value.get("weight", 0)) if w else 0.0
        # Only parseable ISO dates can contribute to a spread. `safe_value` already filters on
        # `"date" in c.value`; severity had not, so a free-text due_at (a human answer we could not
        # coerce, or an OCR row) produced `str(None)` and `date.fromisoformat('None')` — a ValueError
        # inside conflict bookkeeping, i.e. the code that runs *because* evidence is disagreeing.
        dates = sorted(d for d in (c.value.get("date") for c in
                                  self.open_claims("task", subject_id, "due_at"))
                       if d and _ok(str(d)))
        spread = (_d(dates[-1]) - _d(dates[0])).days if len(dates) >= 2 else 0
        if weight >= policy["high_weight"] and spread >= 2:
            return "HIGH"
        if weight >= policy["high_weight"] or spread >= 3:
            return "MED"
        return "LOW"

    def safe_value(self, subject_type: str, subject_id: str, predicate: str,
                   policy: dict) -> dict:
        """Deterministic, explainable tie-break. `earliest_safe` = plan against the worst case."""
        cs = self.open_claims(subject_type, subject_id, predicate)
        if not cs:
            return {}
        if predicate != "due_at" or policy.get("tie_break", "earliest_safe") != "earliest_safe":
            best = max(cs, key=lambda c: (c.valid_from, c.confidence))
            return {**best.value, "_rule": "latest_asserted", "_claim_id": best.id}
        dates = sorted({str(c.value["date"]) for c in cs if "date" in c.value})
        chosen = dates[0]
        winner = next(c for c in cs if str(c.value.get("date")) == chosen)
        return {"date": chosen,
                "_rule": "earliest_safe",
                "_why": f"{len(dates)} sources disagree ({', '.join(dates)}); "
                        f"planning against the earliest. {policy['rationale']}",
                "_claim_id": winner.id,
                "_n_sources": len(dates)}


def _d(iso: str):
    import datetime as dt
    return dt.date.fromisoformat(iso)


def _ok(iso: str) -> bool:
    try:
        _d(iso)
        return True
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# Attendance / eligibility model  (closed form, no model, no solver)
# --------------------------------------------------------------------------- #
def attendance_state(present: int, total: int, threshold: float = 0.75,
                     condonation_floor: float = 0.65) -> dict:
    pct = (present / total) if total else 1.0
    remaining_capacity = float("inf")
    if pct < threshold and total > 0:
        # N = ceil((threshold*T - A) / (1 - threshold))   [verified formula, learntube/gradekar]
        import math
        remaining_capacity = math.ceil((threshold * total - present) / (1 - threshold))
    can_skip = 0
    if total > 0:
        # largest k such that (present)/(total+k) >= threshold
        import math
        lo = 0
        while (present) / (total + lo + 1) >= threshold:
            lo += 1
        can_skip = lo
    state = "SAFE"
    if pct < condonation_floor:
        state = "DEBARRED_TYPICALLY"
    elif pct < threshold:
        state = "SHORT_CONDONABLE"
    elif pct < threshold + 0.02:
        state = "EDGE"
    return {
        "pct": round(pct * 100, 2),
        "state": state,
        "sessions_you_may_skip": can_skip,
        "sessions_you_must_attend_to_recover": remaining_capacity,
        "threshold": threshold,
        "condonation_floor": condonation_floor,
    }
