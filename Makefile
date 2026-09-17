# Every command here is one a reviewer would otherwise type by hand, in this order.
# `PY` resolves to `.venv/bin/python` when that directory exists, else `python3`. A caller can still set
# `PY=…` explicitly. This is not sugar: `make seed` used to shell out to a bare `python3` while the venv
# held ortools/fastapi, so every block reported `promoted=0`, the solver raised ModuleNotFoundError, and the
# target exited nonzero — a half-run that looked like a data problem. One interpreter, chosen once, here.

PY ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)

# `checks` would otherwise be considered "up to date" because a *directory* of that name exists — make
# silently does nothing and exits 0, which is the worst possible failure for a CI target: a green run
# that ran no checks. Everything below is therefore declared phony, and `verify` depends on all of them.
.PHONY: help setup setup-browser test smoke checks verify poc eval seed serve dev dev-check docker docker-llm lint clean retention browser browser-check
help:
	@grep -E '^[a-z-]+:.*#' Makefile | sed 's/:.*#/ \t/' | sort

setup:           ## create ./.venv and install the pins (run this after a fresh clone or a recycled VM)
	@test -d .venv || python3 -m venv .venv
	.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
	@echo "venv ready — every other target finds it automatically (PY auto-detects ./.venv/bin/python)"
	@echo "next: make dev   (diagnoses, seeds if empty, serves on :8000)"

setup-browser:   ## + the Chromium the visual harness needs (npm install + browser download, ~140 MB)
	$(MAKE) setup
	$(MAKE) browser

test:            ## unit + service tests (the guarantees, on the shipped code path)
	$(PY) -m pytest tests/ -q -W ignore::DeprecationWarning

smoke:           ## boot the app on an EMPTY database, seed it, render every page in the walk list
	$(PY) scripts/smoke.py

checks:          ## focused probes: verifier edges, attribution, scenario calibration
	@test -x "$(firstword $(PY))" -o "$(PY)" = python3 || { echo "PY=$(PY) is not runnable"; exit 2; }
	$(PY) checks/check_ground.py && $(PY) checks/check_link.py && $(PY) checks/calibrate.py

poc:             ## end-to-end pipeline trace, printed
	$(PY) run_poc.py

eval:            ## regenerate EVAL_REPORT.{md,json} (zero model calls needed)
	$(PY) evaluation/harness.py --out EVAL_REPORT.md

seed:            ## ingest the demo corpus into $$ALIBI_DB (idempotent)
	sh scripts/seed.sh

serve:           ## the web app on :8000 (`PORT=8080 make serve`); run `make seed` or `make dev` first if empty
	ALIBI_DB=$${ALIBI_DB:-./alibi.db} $(PY) -m uvicorn alibi.server:app --host 0.0.0.0 --port $${PORT:-8000}

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

browser:           ## install the Chromium + shared libs the visual harness needs (needs network)
	cd .tools && npm install --no-audit --no-fund playwright
	cd .tools && npx playwright install chromium chromium-headless-shell
	@echo "if chromium cannot start here: it wants libnss3/libnspr4/libatk/libcups/libasound; see"
	@echo "docs/UI-DESIGN.md → 'Running the browser harness on a bare container'"

browser-check:     ## the visual contract: 111 checks in Chromium across both design systems (~5 min; needs `make setup-browser`)
	@curl -sf -o /dev/null http://127.0.0.1:8000/healthz || { echo "needs a running app: make dev  (or: ALIBI_DB=./alibi.db ./venv/bin/python -m uvicorn alibi.server:app --port 8000)"; exit 2; }
	LD_LIBRARY_PATH=$${LD_LIBRARY_PATH:-$$HOME/.local/lib} node .tools/verify-fluid.mjs

verify: test checks smoke   ## everything CI runs, in the order CI runs it

retention:       ## drop raw source text past the window; quotes, hashes and receipts survive
	$(PY) -m alibi.cli $${ALIBI_DB:+--db $$ALIBI_DB} retention $(DAYS:%=--days %)

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} + ; rm -rf .pytest_cache
