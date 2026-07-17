#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FASTAPI_DIR="$(cd "$SCRIPT_DIR/../../../../../.." && pwd)"

cd "$FASTAPI_DIR" || exit 1

PYTHON_BIN="${PYTHON_BIN:-python}"
SLEEP_SECONDS="${BOOKING_RATING_SLEEP_SECONDS:-30}"

while true; do
  echo "=================================================="
  echo "Booking rating run started at $(date)"

  "$PYTHON_BIN" -m app.services.analytics_engine.jobs.regular.run_booking_rating_reviews \
    --mode changed \
    --include-contact-scans \
    --pretty

  EXIT_CODE=$?

  echo "Booking rating run finished at $(date), exit code=$EXIT_CODE"
  echo "Sleeping ${SLEEP_SECONDS} seconds..."
  sleep "$SLEEP_SECONDS"
done