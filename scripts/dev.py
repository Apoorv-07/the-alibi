#!/usr/bin/env python3
"""`make dev` — diagnose, seed if needed, serve. One command, and it tells the truth about all three.

The reason this is a script and not four Makefile lines: the useful part of a dev launcher is *saying what
the machine can actually do before you start trusting its numbers* (is the solver installed? is LM Studio
answering? is the ledger empty?), and that is Python, not shell. It never lies — a failed probe is
reported as failed, and the app still starts, because the app is designed to work without a model.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def line(label: str, value: str, note: str = "") -> None:
    print("  {:<11} {}{}".format(label, value, ("   ← " + note) if note else ""))


def probe_lm_studio(cfg) -> None:
    base = cfg.local_openai_base_url or os.environ.get("LM_STUDIO_BASE_URL", "")
    if not base:
        line("LM Studio", "not configured",
             "set LM_STUDIO_BASE_URL (e.g. http://127.0.0.1:1234/v1) to use a model")
        return
    url = base.rsplit("/v1", 1)[0] + "/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=2.5) as r:
            models = [m.get("id") or "" for m in json.load(r).get("data", [])]
    except Exception as e:
        line("LM Studio", "{} unreachable ({})".format(base, type(e).__name__),
             "the app runs rules-only and labels every claim accordingly")
        return
    want = cfg.local_model_name if cfg.local_model_source else ""
    if want and want not in models:
        line("LM Studio", "up, but {!r} is not loaded".format(want),
             "loaded: " + (", ".join(models[:5]) or "(none)"))
    else:
        line("LM Studio", "up — " + (", ".join(models[:5]) or "no models loaded"))


def main() -> int:
    print("ALIBI · dev")
    line("python", sys.version.split()[0])
    for mod, label in (("ortools", "solver"), ("fastapi", "api"), ("jinja2", "templates"),
                       ("multipart", "uploads")):
        try:
            __import__(mod)
        except ImportError:
            # `pip install -r …` alone is the wrong advice in two of the three cases that reach here: on a
            # Debian/PEP-668 system Python it is refused outright, and in a VM whose .venv was deleted it
            # installs into the interpreter that is about to be thrown away. `make setup` creates the venv
            # and installs into it, and every other target then finds it.
            line(label, "MISSING", "make setup   (or: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)")
            return 2
        line(label, "present")

    from alibi.config import Config
    cfg = Config.from_env()
    line("db", str(cfg.db_path))
    line("mode", cfg.mode)
    probe_lm_studio(cfg)

    from alibi.db import init_db
    db = init_db(cfg.db_path)
    sources = db.one("SELECT COUNT(*) n FROM source")["n"]
    claims = db.one("SELECT COUNT(*) n FROM claim")["n"]
    trust = db.stats()["false_trust_count"]
    db.close()
    if not sources:
        line("ledger", "empty → ingesting the demo corpus")
        subprocess.run([sys.executable, "-m", "alibi.cli", "sync"], cwd=str(ROOT), check=False)
    else:
        line("ledger", "{} source(s), {} claim(s), false trust {}".format(sources, claims, trust))

    port = os.environ.get("PORT", "8000")
    print("\n  → http://localhost:{}".format(port))
    print("    health  /api/health        AI  /api/ai-check?dry=false        metrics  /metrics\n")
    sys.stdout.flush()
    if "--check" in sys.argv:                      # the diagnostics above are the test; skip the server
        return 0
    os.execvp(sys.executable, [sys.executable, "-m", "uvicorn", "alibi.server:app",
                               "--host", "0.0.0.0", "--port", port])


if __name__ == "__main__":
    raise SystemExit(main())
