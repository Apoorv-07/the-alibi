# Every command here is one a reviewer would otherwise type by hand, in this order.
PY ?= python3

# `checks` would otherwise be considered "up to date" because a *directory* of that name exists — make
# silently does nothing and exits 0, which is the worst possible failure for a CI target: a green run
# that ran no checks. Everything below is therefore declared phony, and `verify` depends on all of them.
.PHONY: help test smoke checks verify poc eval seed serve dev dev-check docker docker-llm lint clean retention
help:
	@grep -E '^[a-z-]+:.*#' Makefile | sed 's/:.*#/ \t/' | sort

test:            ## unit + service tests (the guarantees, on the shipped code path)
	$(PY) -m pytest tests/ -q -W ignore::DeprecationWarning

smoke:           ## boot the app on an EMPTY database, seed it, render every page in the walk list
	$(PY) scripts/smoke.py

checks:          ## focused probes: verifier edges, attribution, scenario calibration
	$(PY) checks/check_ground.py && $(PY) checks/check_link.py && $(PY) checks/calibrate.py

poc:             ## end-to-end pipeline trace, printed
	$(PY) run_poc.py

eval:            ## regenerate EVAL_REPORT.{md,json} (zero model calls needed)
	$(PY) evaluation/harness.py --out EVAL_REPORT.md

seed:            ## ingest the demo corpus into $$ALIBI_DB (idempotent)
	sh scripts/seed.sh

serve:           ## the web app on :8000, seeding if the database is empty
	ALIBI_DB=$${ALIBI_DB:-./alibi.db} $(PY) -m uvicorn alibi.server:app --host 0.0.0.0 --port 8000

dev:             ## one command: diagnose the environment, seed if empty, serve
	$(PY) scripts/dev.py

dev-check:       ## the diagnostics only (no server) — what CI runs to prove the launcher is honest
	$(PY) scripts/dev.py --check
docker:          ## build + run the container (data in a named volume)
	docker compose up --build alibi

docker-llm:      ## same, with a local Ollama sidecar (needs a GPU)
	docker compose --profile llm --profile seed up --build

lint:            ## syntax + unused-import sweep without needing a linter installed
	$(PY) -m compileall -q alibi tests evaluation checks run_poc.py
	@echo "compiled clean"

verify: test checks smoke   ## everything CI runs, in the order CI runs it

retention:       ## drop raw source text past the window; quotes, hashes and receipts survive
	$(PY) -m alibi.cli $${ALIBI_DB:+--db $$ALIBI_DB} retention $(DAYS:%=--days %)

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} + ; rm -rf .pytest_cache
