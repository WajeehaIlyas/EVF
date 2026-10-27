#!/bin/sh
# Container entrypoint. Only the API container sets RUN_DB_INIT=1; it creates any missing tables
# (idempotent) and the other services wait for it to become healthy.
# NOTE: create_all() does not alter existing tables - adopt Alembic before changing the schema.
set -eu

if [ "${RUN_DB_INIT:-0}" = "1" ]; then
  echo "Initialising database schema..."
  python - <<'PY'
import sys
import time

from sqlalchemy.exc import OperationalError

from app.core.database import init_db

for attempt in range(1, 31):
    try:
        init_db()
        print("Database schema is ready.", flush=True)
        break
    except OperationalError as exc:
        print(f"Database not ready ({attempt}/30): {exc.__class__.__name__}", flush=True)
        time.sleep(2)
else:
    sys.exit("Database unavailable after 60 seconds")
PY
fi

exec "$@"
