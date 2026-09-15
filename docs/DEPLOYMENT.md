# Deployment

Four ways to run this, in increasing order of "someone else has to trust it". Every number quoted below was
produced by one of these paths on this repository, and `make verify` (test + checks + smoke) reproduces them.

---

## 1. Bare metal (the normal path)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # 7 pins: ortools, fastapi, uvicorn[standard], jinja2,
                                          # python-multipart, pytest, httpx — no PDF/OCR/LLM framework
cp .env.example .env                      # optional; an empty .env is a valid offline configuration
make seed                                 # ALIBI_DB=… python3 -m alibi.cli sync
make serve                                # uvicorn alibi.server:app --host 0.0.0.0 --port 8000
make serve PORT=8080                      # same, on another port (also `PORT=8080 make dev`)
```

`make seed` is **idempotent**: a second run reports `promoted=0` per source, the ledger stays at 18 claims and
`claims: 0` in the sync result (the number now comes from `COUNT(*) … WHERE run_id=?`, not from summing
per-report counters). Run it twice and check that both statements are true; that is the whole point.

Requirements: Python ≥ 3.11 (`X | None` syntax, `tomllib`-free), 1 CPU for the CP-SAT solver (2 s budget per
run, 4 s in the harness), ~250 MB of disk for `ortools`. **No GPU needed in `demo` mode** — that mode runs
rules + the deterministic simulator and says so on every page.

## 2. Container

```bash
docker compose up --build alibi                     # app only, rules-only, no cloud
docker compose run --rm seed                        # ingest the demo corpus into the volume
docker compose --profile llm --profile seed up      # + an Ollama sidecar (needs a GPU)
```

What the image guarantees:

- `python:3.13-slim`, non-root (`uid 10001`), `/data` the only writable path — the ledger, the receipts and
  the retention state all live there, so `docker rm` loses nothing the user did not choose to lose;
- `ALIBI_DB=/data/alibi.db`, `ALIBI_UPLOAD_DIR=/data/uploads`, `PORT` honoured by the healthcheck;
- **HEALTHCHECK = `GET /readyz`**, which is a **503 until the first sync has run**. A container that is up but
  has ingested nothing is unhealthy on purpose: `restart: unless-stopped` will keep poking it, and you cannot
  mistake "process exists" for "the ledger has evidence in it";
- `OLLAMA_BASE_URL: http://ollama:11434` for the `llm` profile (the name the code reads; `OLLAMA_URL` would
  be a comment wearing a setting).

Back up one file:

```bash
docker run --rm -v alibi-data:/d -v "$PWD":/o alpine sh -c 'cp /d/alibi.db /o/alibi-$(date +%F).db'
```

SQLite in WAL mode also writes `alibi.db-wal`; copy the volume, not just the file, if the app is live. For a
portable, human-readable copy of the *evidence*, use `GET /api/export/ledger.json` — it carries the claims,
their provenance, the change log, the conflicts and the policy in force.

## 2b. With LM Studio (the AI runtime this product is built for)

```bash
# LM Studio: install a model (e.g. qwen2.5-7b-instruct), Developer → Start Server.
# It listens on http://127.0.0.1:1234/v1 by default and needs no API key.
cat > .env <<'ENV'
ALIBI_DB=./alibi.db
ALIBI_MODE=local
LM_STUDIO_BASE_URL=http://127.0.0.1:1234/v1
LM_STUDIO_MODEL=qwen2.5-7b-instruct
ENV
make seed && make serve
curl -s localhost:8000/api/ai-check?dry=false | python3 -m json.tool   # one real round trip
curl -s localhost:8000/api/health | python3 -m json.tool | head -30
```

The app **starts and works with LM Studio closed**: extraction falls back to the rules path, one
`EXTRACTION_UNAVAILABLE` review is opened per source per run, `/api/health` reports `ai: degraded` (never
`healthy`, never a crash), and the cockpit says which engine read the document. Close the server mid-demo and
re-sync to show it: that is the demonstration reviewers actually care about.

Nothing in this path sends bytes off the machine: `is_loopback()` decides redaction and budgeting from the
address, so a URL pointing at another host — even a LAN laptop — is treated as cloud. `ALIBI_MODE=local`
also *refuses* cloud escalation structurally, and `/metrics` still prints `cloud_bytes`.

Container: pass the runtime through, since `localhost` inside a container is the container.

```yaml
services:
  alibi:
    environment:
      ALIBI_MODE: local
      LM_STUDIO_BASE_URL: http://host.docker.internal:1234/v1   # loopback *inside* the container only if
                                                                 # you use `network_mode: host`
```

## 3. Offline / air-gapped college network

```bash
pip download -r requirements.txt -d wheels/ -i https://pypi.org/simple   # on a machine with internet
# copy wheels/ + the repo on a USB stick, then:
pip install --no-index --find-links wheels/ -r requirements.txt
ALIBI_MODE=demo python3 -m alibi.cli sync && ALIBI_MODE=demo python3 -m alibi.server   # or uvicorn
```

Nothing in `demo` mode opens a socket: the only outbound HTTP in the whole package is in
`alibi/adapters.py` (Ollama/Gemini/OpenAI-compat), and in `demo` mode none of those adapters is constructed — 32 model
calls were served by the deterministic simulator with `egress=LOCAL` and `bytes_out=0` (measured:
`/metrics` → `cloud_bytes 0`). `egress=LOCAL` means "a call that would leave the process in another mode",
so a wire capture on `lo` during a demo run is empty; `bytes_out=0` is what the privacy claim rests on. `/eval`, `/docs` and `/api/doc/*`
read files in the repo, not a CDN. The UI has no external asset (`docs/UI-DESIGN.md` §1). If you also ship
Ollama, put the model blobs on the same stick and set `LOCAL_MODEL_NAME`; the app degrades to rules-only with
one review per source if the model is missing, and reports `EXTRACTION_UNAVAILABLE` rather than pretending the
documents said nothing.

## 4. systemd (a long-lived single-user box)

```ini
[Service]
ExecStart=/srv/alibi/.venv/bin/python -m uvicorn alibi.server:app --host 127.0.0.1 --port 8000
Environment=ALIBI_DB=/srv/alibi/alibi.db ALIBI_MODE=local LOCAL_MODEL_NAME=gemma4:e4b
DynamicUser=yes
ReadWritePaths=/srv/alibi
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
```

Bind to `127.0.0.1` here, not `0.0.0.0`: there is **no authentication** (THREAT-MODEL T10). If students reach
it over the network, put Caddy/nginx with per-student sessions in front and one SQLite file per student —
`ALIBI_DB=/srv/alibi/students/{{uid}}.db` — which is why the app has no cross-student state to leak.

---

## Retention and erasure (the two legal-shaped jobs)

```bash
python3 -m alibi.cli retention [--days 30] [--now 2026-09-12]
```

`--now` is *today*; the cutoff is always `today − days` (getting that backwards turns "keep 30 days" into
"keep nothing before the date you passed me"). The job clears `source_meta.full_text`, marks `purged_at`,
deletes stored upload files past the window, writes one `change_event` (`RETENTION_RUN`) and one `audit` row,
and reports what it cost the ledger:

```
retention: window 30d, cutoff 2026-08-13
  sources purged 6 · stored upload files removed 0
  claims 18 · now unverifiable 18 · FALSE TRUST 0
```

`claims` is unchanged and every `evidence_span` survives: retention drops documents, never receipts. After a
purge the affected claims move to `stats()["unverifiable"]` and are **excluded** from `false_trust_count`, so
a purge can never manufacture a clean bill of health (pinned by
`tests/test_api.py::test_retention_drops_text_and_reports_the_loss_rather_than_hiding_it`).

Erasure on request is the UI's `POST /me/wipe` (type `WIPE`), which `DELETE`s 15 tables and answers with the
row counts the database reports afterwards. Cron it if you need the retention to be automatic:

```cron
17 3 * * *  ALIBI_DB=/srv/alibi/alibi.db /srv/alibi/.venv/bin/python -m alibi.cli retention
```

## Rollout order for a real cohort

1. **Read-only week.** `ALIBI_MODE=demo`, no model, no actions. Judge whether the ledger matches the paper
   notices. Nothing writes to anything outside the machine.
2. **Local model.** `ALIBI_MODE=local` with Ollama. Watch `invalid_outputs` and `false_trust_count` on
   `/metrics`; both must stay at 0 for the trust claim to mean anything. If a model starts inventing rows,
   they land in review, not in the plan — that is the design working, but read `/queue` before continuing.
3. **Reviews.** Turn on the human loop (`/queue`) and make someone answer the conflict cards. A system whose
   conflicts go unanswered for a month has become a log file.
4. **Cloud escalation last, per student, opt-in.** `CLOUD_ESCALATION_ENABLED=true` + a key + a byte budget,
   with `docs/CONFIGURATION.md` §4 as the checklist. Expect `cloud_bytes > 0` to be visible on /privacy the
   moment you do this — that is the audit trail, not a bug.

## Troubleshooting

| symptom | cause and fix |
|---|---|
| `/readyz` 503, pages render | the ledger is empty: `POST /api/sync` (the Sources page button) or `make seed` |
| footer says `simulated` | `ALIBI_MODE=demo`, or the local model was unreachable and the fallback was used; `/settings` shows which |
| every page renders but tables are empty | the ingest ran in another database: check `ALIBI_DB` vs `DATABASE_URL` — `Config` now reads both, and `/readyz` prints `db` so you can see which file is in play |
| `claims 0` after a re-sync | correct: identical bytes are the same source (`UNIQUE(sha256, kind)`) and the run added no rows |
| `INFEASIBLE` with `slack_hours=-3.0` for a date in the past | also correct: a deadline at/before the horizon start has no legal time left; the head-line says "no submission on file", which is not a verdict about the student |
| `unverifiable` climbing | the retention job ran; the quotes remain, the documents do not |
| `ai: degraded` on /api/health | LM Studio is not answering: `reason` says whether the port is closed or the model is not loaded, `hint` says what to click. Set `LM_STUDIO_MODEL` to an id in `/v1/models` |
| `SSL: WRONG_VERSION_NUMBER` | only possible if you forced a non-loopback host onto plain HTTP; use `http://` for loopback and `https://` for anything else — the transport now follows the scheme you give it |
| a migration that fails halfway | cannot happen any more: `DB.migrate()` runs one file per transaction and refuses to record a failed one (`MigrationError: … rolled back … the database is unchanged`). If a database *is* broken (it was, once, during development), the documented recovery is `rm alibi.db && make seed` — the ledger is derived from the corpus, so a rebuild is not a data-loss event |
| a `500` on any page | a template bug, not a data bug — the empty database renders all 17 pages; run `python3 scripts/smoke.py` and it will name the page |
| `pip install ortools` fails | there is no flag that makes the solver optional — `ortools` *is* the difference between "here is a plan" and "here is a proof". Use an official manylinux wheel (or the container); cross-compiling `libortools` is a day you will not get back |
