"""tests/test_cli.py — the command line as a user meets it: exit codes, and what a failure *says*.

The CLI and the web app call the same `Twin`, so the interesting surface here is not the logic (tested in
test_core/test_twin_db) but the contract at the edge: a green run must mean what it appears to mean, and a
red one must be actionable without opening a traceback.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = [sys.executable, "-m", "alibi.cli"]


def run(*args: str, db: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    cmd = list(PY) + ([f"--db={db}"] if db else []) + list(args)
    return subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)


def test_status_and_conflicts_run_on_a_fresh_database(tmp_path):
    """A first-time user's very two first commands, on an empty ledger: they must succeed, and must say that
    there is nothing rather than printing a table of zeroes that reads like a populated system."""
    db = str(tmp_path / "cold.db")
    r = run("status", db=db)
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "sources 0" in out and "claims 0" in out, out
    assert "FALSE TRUST: 0 / 0" not in out, "a divide by nothing must not be dressed as a percentage"
    assert "does not fill this in" in out, "the CLI must say what is absent, in the UI's own words"
    c = run("conflicts", db=db)
    out = (c.stdout + c.stderr).lower()
    assert c.returncode == 0 and "no conflicts recorded" in out, c.stdout
    # the empty answer qualifies itself: an absence of contradictions means nothing until sources exist
    assert "only meaningful" in out, "an empty ledger's 'no conflicts' must be labelled as uninformative"


def test_sync_then_status_reports_the_seeded_ledger(tmp_path):
    db = str(tmp_path / "warm.db")
    s = run("sync", db=db)
    assert s.returncode == 0, s.stdout[-400:] + s.stderr[-400:]
    st = run("status", db=db)
    assert st.returncode == 0, st.stdout[-400:]
    assert "claims 18" in st.stdout, st.stdout[-600:]
    assert "18 of 18 facts re-checked on read" in st.stdout, st.stdout[-400:]
    # `why` is the one command a student reaches for on a specific task, so it is exercised on a real id
    w = run("why", "dbms-lab_4", db=db)
    assert w.returncode == 0, w.stdout[-300:] + w.stderr[-300:]
    assert "claim" in w.stdout.lower()


def test_an_unopenable_ledger_is_explained_rather_than_tracedback(tmp_path):
    """`rm alibi.db` while a process holds it strands the `-wal`/`-shm` journals, and every later open
    answers `sqlite3.OperationalError: disk I/O error`. The CLI used to print that traceback and nothing
    else — no path, no remedy — which is precisely the moment a user needs instructions. Reproduced here
    without needing a live server: hold the file, delete it, run `status`."""
    import sqlite3

    db = tmp_path / "locked.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    db.unlink()
    try:
        r = run("status", db=str(db))
    finally:
        conn.close()
    out = r.stdout + r.stderr
    assert r.returncode != 0, "an unreadable ledger must not look like success"
    assert "Traceback" not in out, out[-400:]
    assert "cannot open the ledger" in out and str(db) in out, out[-300:]
    assert "make seed" in out and "Nothing was written" in out, "name a remedy and the blast radius"


def test_the_cli_exposes_every_page_level_capability(capsys):
    """Standing rule for this repo: nothing user-facing may live only in the UI. The commands below are the
    ones the six pages perform, and the help text must list them, or a reviewer will conclude otherwise."""
    h = run("--help")
    assert h.returncode == 0
    for verb in ("status", "sync", "ingest", "why", "conflicts", "plan", "retention", "serve"):
        assert verb in h.stdout, f"`{verb}` is not in the CLI's own help"
    sub = run("plan", "--help")
    assert sub.returncode == 0 and "--draft" in sub.stdout, "the extension draft must be reachable from here"


@pytest.mark.parametrize("args", (["status", "--db"], ["nope"], ["plan", "--days", "x"]))
def test_bad_invocations_fail_loudly_not_silently(tmp_path, args):
    db = str(tmp_path / "x.db")
    r = run(*args, db=db)
    assert r.returncode != 0, f"{args} was accepted and said nothing"
    assert "Traceback" not in r.stdout + r.stderr, (r.stdout + r.stderr)[-400:]
