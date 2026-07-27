# Backend container for Railway (or any Docker host).
# NOTE: R / PowerTOST is intentionally NOT installed here — the sample-size
# engine falls back to the validated Python implementation when R is absent
# (services/powertost_r.py degrades gracefully). Installing R would balloon the
# image; add it only if you need the exact R PowerTOST provenance in production.
FROM python:3.13-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# Dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# App code
COPY . .

# Railway injects $PORT; default to 8000 for local `docker run`.
# NOTE: worker count (--workers 2) is duplicated in the Procfile — keep both in
# sync. See database.py's DB_POOL_SIZE/DB_MAX_OVERFLOW comment for how worker
# count relates to the DB connection ceiling.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2"]
