#!/bin/sh
# Seed the ledger from the demo corpus. Idempotent: `ingest_text` is content-addressed (UNIQUE
# (sha256, kind) on the source row), so running this twice adds zero claims — which is the property the
# UI's "Sync" button relies on, tested in tests/test_attribution.py.
# Set ALIBI_PY to force an interpreter; leave it unset and the script finds ./venv or ./.venv by itself.
#
# The interpreter is chosen, not assumed. A bare `python3` here used to run the *system* Python while the
# venv held ortools/fastapi: every block reported `promoted=0`, the solver raised
# `ModuleNotFoundError: No module named 'ortools'`, and `set -eu` turned that into a nonzero exit — a seed
# that half-ran and then failed, which is worse than one that refused to start. So: honour $PY, else a
# ./venv or ./.venv if it can import the product, else the system python3, and say so if none of them can.
set -eu
: "${ALIBI_DB:=/home/user/alibi-twin/alibi.db}"
cd "$(dirname "$0")/.."

probe() { "$1" -c "import fastapi, jinja2" >/dev/null 2>&1; }
if [ -n "${ALIBI_PY:-}" ]; then          # explicit override always wins (CI, Nix, a packaged interpreter)
  PYBIN="$ALIBI_PY"
else
  PYBIN=python3
  for cand in ./.venv/bin/python ./venv/bin/python; do
    if [ -x "$cand" ] && probe "$cand"; then PYBIN="$cand"; break; fi
  done
fi
if ! probe "$PYBIN"; then
  echo "seed: $PYBIN cannot import fastapi — activate the venv (see README step 1) or set PY=/path/to/python" >&2
  exit 3
fi

: "${ALIBI_DB:=$PWD/alibi.db}"
echo "seeding $ALIBI_DB from corpus/demo_corpus.py (interpreter: $PYBIN)"
"$PYBIN" -m alibi.cli --db "$ALIBI_DB" sync
"$PYBIN" -m alibi.cli --db "$ALIBI_DB" status
