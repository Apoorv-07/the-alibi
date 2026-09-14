# ALIBI — the whole product in one container: SQLite ledger, deterministic verifier, CP-SAT solver,
# server-rendered UI. No node build, no CDN, no asset pipeline: every page is HTML+inline SVG so it
# renders on a campus network that blocks Google, which is the environment this is built for.
FROM python:3.13-slim AS base

# PIP_NO_CACHE_DIR: an image is not a place to keep a wheel cache. PYTHONUNBUFFERED so `docker logs`
# shows the ingest trace as it happens instead of after the process exits.
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1 \
    ALIBI_DB=/data/alibi.db ALIBI_UPLOAD_DIR=/data/uploads PORT=8000

# ortools ships many-megabyte wheels with bundled native libs; it is the only heavy dependency and the
# only component without a hand-written substitute.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# A container is not a root workload, and /data is the only writable path: the ledger, the receipts and
# the raw-text retention all live there, so `docker rm` loses nothing the user did not choose to lose.
RUN useradd -r -u 10001 alibi && mkdir -p /data /data/uploads && chown -R alibi:alibi /data /app
USER alibi

EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=40s --retries=3 \
  CMD python3 -c "import os,urllib.request as u;\
print(u.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/readyz',timeout=2).status)" || exit 1

# `seed` on first boot only if the volume is empty (build_app(seed=True) checks the row count), so a
# restart never overwrites a real ledger.
CMD ["python3", "-m", "uvicorn", "alibi.server:app", "--host", "0.0.0.0", "--port", "8000"]
