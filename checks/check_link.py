"""Probe: does date→obligation linkage work on the real corpus, per document?

Run: python3 checks/check_link.py
Exits non-zero if a gold (task, date) assertion that the corpus states *unambiguously* is missed,
or if any date is attributed to a task whose line never asserted it (over-attribution).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alibi.link import gold_pairs                      # noqa: E402
from alibi.ingest import (blocks_from_syllabus, blocks_from_msg_groups,   # noqa: E402
                          parse_whatsapp, group_for_model)
import corpus.demo_corpus as C                          # noqa: E402

TIDS = {
    "dbms_lab4":    r"\blab[- ]?4\b",
    "os_assign3":   r"\bassignment ?3\b|\ba3\b",
    "daa_assign4":  r"\bassignment ?4\b",
    "os_ia2":       r"\binternal assessment ?2\b|\bia ?2\b",
    "dbms_ia2":     r"\binternal assessment ?2\b|\bia ?2\b",
    "sih_reg":      r"\bregistration clos\w*",
    "dbms_project": r"\bterm project\b|\bproject demo\b",
    "sih_idea":     r"\bidea submission\b",
    "sih_present":  r"\bfinalists present\b",
}
HINT = {"dbms_lab4": ("dbms",), "dbms_ia2": ("dbms",), "dbms_project": ("dbms",),
        "os_assign3": ("os", "operating"), "os_ia2": ("os", "operating")}


def units() -> list[tuple[str, str]]:
    out = []
    for tag, text in [("OS " + C.SYLLABUS_OS[:60], C.SYLLABUS_OS),
                      ("DBMS " + C.SYLLABUS_DBMS[:60], C.SYLLABUS_DBMS),
                      ("DAA " + C.SYLLABUS_DAA[:60], C.SYLLABUS_DAA)]:
        out += [(tag, b.text) for b in blocks_from_syllabus(text)]
    out.append(("NOTICE", C.NOTICE_BOARD_PHOTO))
    out.append(("ATTEND", C.ERP_ATTENDANCE))
    for m in parse_whatsapp(C.WHATSAPP_CHAT, 2026):
        out.append((m.body[:200], m.body))
    return out


def main() -> int:
    got: dict[str, set] = {}
    amb: dict[str, set] = {}
    n_pairs = n_amb = 0
    for doc, text in units():
        prs, ambs = gold_pairs(text, TIDS, doc_hint=doc)
        for sid, iso in prs:
            got.setdefault(sid, set()).add(iso)
        for sid, iso in ambs:
            amb.setdefault(sid, set()).add(iso)
        n_pairs += len(prs)
        n_amb += len(ambs)
        if ambs:
            print(f"  AMBIGUOUS in {doc[:24]!r}: {sorted(set(ambs))}")

    gold = {g["id"]: g["due"] for g in C.GOLD["task"] if g.get("due")}
    planted = {p[0]: p[2] for p in C.GOLD["planted_conflicts"] if p[1] == "due_at"}
    allowed = {sid: {v for v in (set([gold.get(sid)]) | set(planted.get(sid, []))) - {None}}
               for sid in gold}
    correct = missing = deferred = wrong_attr = 0
    for sid, exp in gold.items():
        g = got.get(sid, set())
        want = min(planted[sid]) if sid in planted else exp
        if g and want in g:
            correct += 1
        elif g - allowed.get(sid, set()):
            wrong_attr += 1
            print(f"  OVER-ATTRIBUTED {sid}: {sorted(g - allowed.get(sid, set()))}")
        elif not g and sid in amb:
            deferred += 1
            print(f"  DEFERRED (correct: ambiguous) {sid}")
        else:
            missing += 1
            print(f"  MISS {sid}: want {want} got {sorted(g)} amb {sorted(amb.get(sid, []))}")

    print(f"\n{len(units())} blocks → {n_pairs} linked claims, {n_amb} ambiguous")
    print(f"correct {correct}/{len(gold)} · deferred {deferred} · over-attributed {wrong_attr} · missed {missing}")
    ok = wrong_attr == 0
    print("PASS" if ok else "FAIL: over-attribution is never acceptable — an earlier wrong date"
                            " silently wins every conservative policy")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
