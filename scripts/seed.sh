#!/bin/sh
# Seed the ledger from the demo corpus. Idempotent: `ingest_text` is content-addressed (UNIQUE
# (sha256, kind) on the source row), so running this twice adds zero claims — which is the property the
# UI's "Sync" button relies on, tested in tests/test_attribution.py.
set -eu
: "${ALIBI_DB:=/home/user/alibi-twin/alibi.db}"
cd "$(dirname "$0")/.."
echo "seeding $ALIBI_DB from corpus/demo_corpus.py"
python3 -m alibi.cli --db "$ALIBI_DB" sync
python3 -m alibi.cli --db "$ALIBI_DB" status
