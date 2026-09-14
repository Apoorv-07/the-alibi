"""alibi.cli — the terminal door. Same service object the web app uses, so the two cannot disagree.

    python3 -m alibi.cli status                 # the numbers, from rows
    python3 -m alibi.cli sync                   # ingest the demo corpus
    python3 -m alibi.cli ingest FILE [--kind k] # ingest one artifact
    python3 -m alibi.cli why TASK               # lineage + provenance for one subject
    python3 -m alibi.cli conflicts              # explanation per open conflict
    python3 -m alibi.cli plan [--draft]         # solver proof, remedies, optional draft
    python3 -m alibi.cli retention [--days 30]  # drop raw text, keep every receipt
    python3 -m alibi.cli serve [--port 8000]    # the web app
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .twin import Twin

ROOT = Path(__file__).resolve().parents[1]


def _twin(args) -> Twin:
    twin = Twin.open(args.db or os.environ.get("ALIBI_DB", str(ROOT / "alibi.db")),
                     mode=args.mode)
    from .log import configure
    configure(twin.cfg)                 # LOG_LEVEL / ALIBI_LOG_JSON are read by Config, so honour them
    return twin


def cmd_retention(t: Twin, args) -> int:
    r = t.run_retention(days=args.days, now=args.now or None)
    print(f"retention: window {r['retention_days']}d, cutoff {r['cutoff']}")
    print(f"  sources purged {r['sources_purged']} · stored upload files removed {r['upload_files_removed']}")
    print(f"  claims {r['claims']} · now unverifiable {r['unverifiable']} "
          f"· FALSE TRUST {r['false_trust_count']}")
    print("  (a purge is reported as *unverifiable*, never as a clean bill of health)")
    return 0


def cmd_status(t: Twin, args) -> int:
    c = t.cockpit()
    st, prov = c["stats"], c["provider"]
    print(f"ALIBI · mode={prov['mode']} local={prov['local'] or '—'} cloud={prov['cloud'] or 'off'}")
    print(f"  sources {st['sources']} · claims {st['claims']} · grounded {st['claims_verified']}"
          f" · in review {st['claims_review']} · superseded {st['supersessions']}")
    print(f"  conflicts open {c['conflicts_open']} · reviews {c['reviews_open']}"
          f" · actions pending {c['actions_pending']}")
    print(f"  model calls {st['model_calls']} (local {st['local_calls']}, cloud {st['cloud_calls']},"
          f" {st['cloud_bytes']} bytes out) · invalid outputs {st['invalid_outputs']}")
    print(f"  FALSE TRUST: {st['false_trust_count']} / {st['claims_verified']} grounded"
          f" = {st['false_trust_rate'] * 100:.2f}%   (unverifiable: {st.get('unverifiable', 0)})")
    print(f"  tasks tracked {c['live_tasks']} · {c['changes']['head_line']}")
    if prov["mode"] == "rules_only" or t.routing().get("simulated"):
        print("  note: no language model is installed; extraction ran on the offline deterministic"
              " simulator. The ledger, verifier, conflicts and solver are the production paths.")
    return 0


def cmd_sync(t: Twin, args) -> int:
    sys.path.insert(0, str(ROOT))
    import corpus.demo_corpus as C     # noqa: E402
    rid = t.db.open_run("cli", note="sync")
    plan = [("SYLLABUS_DBMS", "syllabus_text", "DBMS syllabus p.1"),
            ("SYLLABUS_OS", "syllabus_text", "OS syllabus p.1"),
            ("SYLLABUS_DAA", "syllabus_text", "DAA syllabus p.1"),
            ("WHATSAPP_CHAT", "whatsapp", "class group export"),
            ("NOTICE_BOARD_PHOTO", "notice_photo", "notice board photo"),
            ("ERP_ATTENDANCE", "erp_table", "ERP attendance screen")]
    for key, kind, label in plan:
        text = getattr(C, key, "")
        if not text:
            continue
        rep = t.ingest_text(text, kind=kind, label=label, run_id=str(rid))
        print(f"{key:22} blocks={rep.blocks:3} rule={rep.rule_claims} promoted={rep.promoted}"
              f" rejected={rep.rejected} review+{rep.reviews} "
              f"provider={rep.provider}/{rep.provider_outcome or '-'}")
        if args.verbose:
            for line in rep.trace:
                print("      ", line)
    out = t.run_pipeline()
    t.db.close_run(rid, "done")
    print(f"\n{out['summary']['head_line']}")
    for r in out["risks"][:8]:
        print("  ·", r[:150])
    return 0


def cmd_ingest(t: Twin, args) -> int:
    raw = Path(args.file).read_bytes()
    text = raw.decode("utf-8", "replace")
    rid = t.db.open_run("cli", note=f"ingest {args.file}")
    rep = t.ingest_text(text, kind=args.kind, label=args.label or Path(args.file).name,
                        uri=args.file, run_id=str(rid))
    t.run_pipeline()
    t.db.close_run(rid, "done")
    print(json.dumps(rep.as_dict(), indent=1, ensure_ascii=False))
    return 0


def cmd_why(t: Twin, args) -> int:
    lin = t.db.lineage(args.task, args.type)
    print(f"# {lin['subject_id']} · {len(lin['claims'])} claim(s) · {len(lin['edges'])} edge(s)")
    for c in lin["claims"]:
        print(f"\n[{c['id']}] {c['predicate']} = {json.dumps(c['value'], ensure_ascii=False)}"
              f"  ({c['method']}, {c['verify_state']}"
              f"{', RETIRED ' + str(c['valid_to'])[:10] if c['valid_to'] else ''})")
        print(f"     source: {c.get('source_label') or c.get('orig_kind') or c['source_id']}"
              f"  prior={c.get('trust_prior')}")
        print(f"     quote : {c.get('quote') or c['evidence_span']}")
        if c.get("checks"):
            print(f"     checks: {', '.join(c['checks'])}")
        if c.get("llm_involved"):
            print(f"     model : {c.get('provider')}/{c.get('model') or '?'} —"
                  f" confidence is evidence, not trust")
        if c.get("reject_reason"):
            print(f"     reject: {c['reject_reason']}")
    for e in lin["edges"]:
        print(f"\nedge: claim#{e['from']} —{e['kind']}→ claim#{e['to']}  ({e['why']})")
    if lin["changes"]:
        print("\nchanges:")
        for ch in lin["changes"][:12]:
            print(f"  {ch['ts'][:19]} {ch['kind']:22} {ch['old_value']} → {ch['new_value']}"
                  f"  [{ch['severity']}]")
    return 0


def cmd_conflicts(t: Twin, args) -> int:
    t.db.sync_conflicts()
    rows = t.db.conflicts(include_resolved=args.all)
    if not rows:
        print("no conflicts recorded — which is only meaningful if more than one source has been synced")
        return 0
    for r in rows:
        ex = r.get("explanation") or t.db.explain_conflict(r)
        print(f"\n=== {r['subject_id']} · {r['predicate']} · {r['state']} · severity {r['severity']}")
        print(f"    {ex.get('what','')}")
        for row in ex.get("rows") or []:
            print(f"    - {row['source']} (authority {row['authority']:.2f}) {json.dumps(row['value'])}"
                  f" → {row['verdict']}")
            print(f"      quote: {row['quote'][:110]}")
        print(f"    why: {ex.get('why','')}")
        print(f"    plan against: {json.dumps(ex.get('plan_against'))}  (rule: {ex.get('rule')})")
        print(f"    uncertain: {ex.get('uncertain','')}")
        print(f"    do: {ex.get('do','')}")
        print(f"    silent deletion: {ex.get('silent_deletion')}")
    return 0


def cmd_plan(t: Twin, args) -> int:
    out = t.run_pipeline()
    f = out.get("feasibility") or {}
    print(f"solver: {f.get('status')} · {f.get('head_line') or f.get('reason') or ''}")
    if f.get("core"):
        print(f"  unsat core: {', '.join(f['core'])}")
        for line in (f.get("why") or [])[:6]:
            print(f"  · {line}")
    for r in (f.get("remedies") or [])[:6]:
        print(f"  remedy {r['kind']:20} feasible={r.get('makes_feasible')}"
              f" cost={r.get('cost_pct')}% class={r.get('cost_class')}")
    print(f"\n{out['summary']['head_line']}")
    for r in out["risks"]:
        print("  ", r[:160])
    if args.draft:
        tasks = [x["id"] for x in _planned(t)]
        if not tasks:
            print("no task with a grounded due date to draft about")
            return 1
        from .actions import draft_extension_email, open_action
        L = t.db.derive_ledger()
        sid = tasks[0]
        rows = [t.db.claim(c.id) for pred in ("due_at", "late_policy", "weight")
                for c in L.open_claims("task", sid, pred)]
        a = draft_extension_email(rows, {"kind": "extension_request", "days": 1, "cost_pct": 10.0,
                                        "minutes_left": 360, "shortfall_hours": 4.0},
                                  student=os.environ.get("ALIBI_STUDENT", "the student"),
                                  to=os.environ.get("ALIBI_FACULTY_EMAIL", "faculty@college"),
                                  course=sid.split("_")[0].upper(), task_id=sid,
                                  evidence_lines=[f"{r['predicate']} ← claim {r['id']}" for r in rows])
        v, aid = open_action(t.db, a, policy=t.policy)
        print(f"\naction:{aid} → {v.decision}: {v.reason}")
        print("\n".join(a.payload["body"]))
    return 0


def _planned(t: Twin) -> list[dict]:
    from .feasibility import Task    # noqa: F401
    return [{"id": x.id} for x in t._tasks_from_ledger(t.db.derive_ledger())]


def cmd_serve(t: Twin, args) -> int:
    import uvicorn
    from .server import create_app
    app, state = create_app(t)
    if args.seed:
        state.seed_if_empty()
    print(f"ALIBI on http://{args.host}:{args.port}  (db={t.cfg.db_path}, mode={t.routing()['mode']})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="alibi", description="evidence-backed academic twin")
    ap.add_argument("--db", default="", help="sqlite path (default $ALIBI_DB or ./alibi.db)")
    ap.add_argument("--mode", default=None, choices=[None, "auto", "demo", "local", "hybrid"])
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("status")
    p = sp.add_parser("sync"); p.add_argument("-v", "--verbose", action="store_true")
    p = sp.add_parser("ingest"); p.add_argument("file"); p.add_argument("--kind", default="auto")
    p.add_argument("--label", default="")
    p = sp.add_parser("why"); p.add_argument("task"); p.add_argument("--type", default="task")
    p = sp.add_parser("conflicts"); p.add_argument("--all", action="store_true")
    p = sp.add_parser("plan"); p.add_argument("--draft", action="store_true")
    p = sp.add_parser("retention"); p.add_argument("--days", type=int, default=None,
                                                    help="override raw_content_retention_days")
    p.add_argument("--now", default="", help="ISO date to evaluate the window against (for replay/tests)")
    p = sp.add_parser("serve"); p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    p.add_argument("--no-seed", dest="seed", action="store_false")
    args = ap.parse_args(argv)
    if args.cmd == "serve":
        args.seed = getattr(args, "seed", True)
    t = _twin(args)
    try:
        return {"status": cmd_status, "sync": cmd_sync, "ingest": cmd_ingest, "why": cmd_why,
                "conflicts": cmd_conflicts, "plan": cmd_plan, "retention": cmd_retention,
                "serve": cmd_serve}[args.cmd](t, args)
    finally:
        t.db.close()


if __name__ == "__main__":
    raise SystemExit(main())
