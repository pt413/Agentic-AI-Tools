#!/usr/bin/env bash
set -Eeuo pipefail

# Resolve paths from this script location.
# Expected location:
#   <FastApi>/app/services/analytics_engine/scripts/run_daily_analytics_incremental.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANALYTICS_ENGINE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULT_APP_DIR="$(cd "$ANALYTICS_ENGINE_DIR/../../.." && pwd)"

# =============================================================================
# AnalyticsEngine daily incremental cron runner
#
# Daily flow:
#   1) Open SSH tunnel to AWS/EC2 for RMS MySQL RDS
#   2) Run normal staging syncs, excluding call logs
#   3) Run unified call-log builder
#   4) Run processors from processor_checkpoint
#
# Given tunnel command, converted for cron:
#   ssh -4 -i ec2-key.pem \
#     -L 127.0.0.1:3307:database-1.clsi2m2gmodx.ap-south-1.rds.amazonaws.com:3306 \
#     ec2-user@65.1.44.87
#
# Recommended key location:
#   /home/<cron-user>/.ssh/bpai-analytics/ec2-key.pem
# =============================================================================

# -----------------------------------------------------------------------------
# Repo/runtime paths
# -----------------------------------------------------------------------------
APP_DIR="${APP_DIR:-$DEFAULT_APP_DIR}"
VENV_DIR="${VENV_DIR:-$APP_DIR/venv}"
PYTHON_BIN="${PYTHON_BIN:-python}"
LOG_DIR="${LOG_DIR:-$APP_DIR/logs/analytics_cron}"
LOCK_FILE="${LOCK_FILE:-/tmp/analytics_daily_incremental.lock}"

# Optional shell-compatible env override file.
# Recommended: keep secrets and server-specific overrides here, not in this file.
ANALYTICS_CRON_ENV="${ANALYTICS_CRON_ENV:-$ANALYTICS_ENGINE_DIR/.analytics_cron.env}"
if [[ -f "$ANALYTICS_CRON_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$ANALYTICS_CRON_ENV"
fi

# -----------------------------------------------------------------------------
# SSH tunnel config: RMS MySQL RDS via EC2
# -----------------------------------------------------------------------------
ENABLE_SSH_TUNNEL="${ENABLE_SSH_TUNNEL:-1}"

# Store this pem OUTSIDE the repo.
SSH_KEY_PATH="${SSH_KEY_PATH:-/home/bpai/.ssh/bpai-analytics/ec2-key.pem}"
SSH_USER="${SSH_USER:-ec2-user}"
SSH_HOST="${SSH_HOST:-65.1.44.87}"
SSH_PORT="${SSH_PORT:-22}"

LOCAL_MYSQL_HOST="${LOCAL_MYSQL_HOST:-127.0.0.1}"
LOCAL_MYSQL_PORT="${LOCAL_MYSQL_PORT:-3307}"
REMOTE_MYSQL_HOST="${REMOTE_MYSQL_HOST:-database-1.clsi2m2gmodx.ap-south-1.rds.amazonaws.com}"
REMOTE_MYSQL_PORT="${REMOTE_MYSQL_PORT:-3306}"

# Optional: recording/transcript Postgres source.
# Leave DISABLED unless this source also needs SSH tunneling.
ENABLE_RECORDING_PG_TUNNEL="${ENABLE_RECORDING_PG_TUNNEL:-0}"
LOCAL_RECORDING_PG_HOST="${LOCAL_RECORDING_PG_HOST:-127.0.0.1}"
LOCAL_RECORDING_PG_PORT="${LOCAL_RECORDING_PG_PORT:-5435}"
REMOTE_RECORDING_PG_HOST="${REMOTE_RECORDING_PG_HOST:-<private-recording-postgres-host>}"
REMOTE_RECORDING_PG_PORT="${REMOTE_RECORDING_PG_PORT:-5432}"

# -----------------------------------------------------------------------------
# Source DB URLs
# -----------------------------------------------------------------------------
# MySQL source URL should point to the local tunnel port.
# Put the real username/password/database in $APP_DIR/.analytics_cron.env.
export MYSQL_DATABASE_URL="${MYSQL_DATABASE_URL:-mysql+pymysql://<mysql_user>:<mysql_password>@127.0.0.1:${LOCAL_MYSQL_PORT}/<mysql_database>}"

# If recording Postgres is directly reachable from server, set this to the direct URL.
# If using optional SSH tunnel, set host to 127.0.0.1:${LOCAL_RECORDING_PG_PORT}.
export THIRDPARTY_POSTGRES_URL="${THIRDPARTY_POSTGRES_URL:-postgresql+psycopg2://<pg_user>:<pg_password>@<recording_pg_host>:5432/<recording_pg_database>}"

# Analytics DB should normally come from app .env / server env.
# Uncomment only if needed.
# export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg2://<analytics_user>:<analytics_password>@<analytics_host>:5432/<analytics_db>}"

# -----------------------------------------------------------------------------
# Job tuning
# -----------------------------------------------------------------------------
STAGE_LIMIT="${STAGE_LIMIT:-50000}"
STAGE_MAX_BATCHES="${STAGE_MAX_BATCHES:-20}"
CALL_LIMIT="${CALL_LIMIT:-50000}"
CALL_RESYNC_WINDOW="${CALL_RESYNC_WINDOW:-5000}"
CALL_MATCH_WINDOW_SECONDS="${CALL_MATCH_WINDOW_SECONDS:-300}"
CALL_SKIP_COUNTS="${CALL_SKIP_COUNTS:-1}"
CALL_SKIP_DUPLICATE_REFRESH="${CALL_SKIP_DUPLICATE_REFRESH:-1}"
CALL_SKIP_BACKFILL_EXISTING_KEYS="${CALL_SKIP_BACKFILL_EXISTING_KEYS:-1}"
PROCESSOR_LIMIT="${PROCESSOR_LIMIT:-10000}"

RUN_ID="$(date +'%Y%m%d_%H%M%S')"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/analytics_daily_${RUN_ID}.log"
STAGE_SUMMARY_FILE="$LOG_DIR/stage_sync_${RUN_ID}.json"

# Mirror stdout/stderr to log file.
exec > >(tee -a "$LOG_FILE") 2>&1

MYSQL_TUNNEL_PID=""
RECORDING_PG_TUNNEL_PID=""

log() {
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*"
}

fail() {
  log "ERROR: $*"
  exit 1
}

validate_no_placeholder() {
  local name="$1"
  local value="$2"
  if [[ -z "$value" || "$value" == *"<"* || "$value" == *">"* ]]; then
    fail "$name still contains a placeholder. Set it in $ANALYTICS_CRON_ENV"
  fi
}

wait_for_port() {
  local host="$1"
  local port="$2"
  local label="$3"
  local attempts="${4:-30}"

  for _ in $(seq 1 "$attempts"); do
    if timeout 1 bash -c "cat < /dev/null > /dev/tcp/${host}/${port}" 2>/dev/null; then
      log "$label is reachable at ${host}:${port}"
      return 0
    fi
    sleep 1
  done

  fail "$label did not become reachable at ${host}:${port}"
}

start_mysql_ssh_tunnel() {
  if [[ "$ENABLE_SSH_TUNNEL" != "1" ]]; then
    log "MySQL SSH tunnel disabled: ENABLE_SSH_TUNNEL=$ENABLE_SSH_TUNNEL"
    return 0
  fi

  validate_no_placeholder "SSH_KEY_PATH" "$SSH_KEY_PATH"
  validate_no_placeholder "SSH_HOST" "$SSH_HOST"
  validate_no_placeholder "REMOTE_MYSQL_HOST" "$REMOTE_MYSQL_HOST"
  validate_no_placeholder "MYSQL_DATABASE_URL" "$MYSQL_DATABASE_URL"

  [[ -f "$SSH_KEY_PATH" ]] || fail "SSH key not found: $SSH_KEY_PATH"

  log "Opening MySQL SSH tunnel using ${SSH_USER}@${SSH_HOST}:${SSH_PORT}"
  log "Forward: ${LOCAL_MYSQL_HOST}:${LOCAL_MYSQL_PORT} -> ${REMOTE_MYSQL_HOST}:${REMOTE_MYSQL_PORT}"

  ssh -4 \
    -i "$SSH_KEY_PATH" \
    -p "$SSH_PORT" \
    -o BatchMode=yes \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -N \
    -L "${LOCAL_MYSQL_HOST}:${LOCAL_MYSQL_PORT}:${REMOTE_MYSQL_HOST}:${REMOTE_MYSQL_PORT}" \
    "${SSH_USER}@${SSH_HOST}" &

  MYSQL_TUNNEL_PID="$!"
  sleep 2

  if ! kill -0 "$MYSQL_TUNNEL_PID" 2>/dev/null; then
    fail "MySQL SSH tunnel process exited immediately"
  fi

  wait_for_port "$LOCAL_MYSQL_HOST" "$LOCAL_MYSQL_PORT" "MySQL tunnel"
  log "MySQL SSH tunnel started with pid=$MYSQL_TUNNEL_PID"
}

start_recording_pg_tunnel_if_enabled() {
  if [[ "$ENABLE_RECORDING_PG_TUNNEL" != "1" ]]; then
    log "Recording Postgres SSH tunnel disabled: ENABLE_RECORDING_PG_TUNNEL=$ENABLE_RECORDING_PG_TUNNEL"
    return 0
  fi

  validate_no_placeholder "SSH_KEY_PATH" "$SSH_KEY_PATH"
  validate_no_placeholder "SSH_HOST" "$SSH_HOST"
  validate_no_placeholder "REMOTE_RECORDING_PG_HOST" "$REMOTE_RECORDING_PG_HOST"
  validate_no_placeholder "THIRDPARTY_POSTGRES_URL" "$THIRDPARTY_POSTGRES_URL"

  log "Opening recording Postgres SSH tunnel using ${SSH_USER}@${SSH_HOST}:${SSH_PORT}"
  log "Forward: ${LOCAL_RECORDING_PG_HOST}:${LOCAL_RECORDING_PG_PORT} -> ${REMOTE_RECORDING_PG_HOST}:${REMOTE_RECORDING_PG_PORT}"

  ssh -4 \
    -i "$SSH_KEY_PATH" \
    -p "$SSH_PORT" \
    -o BatchMode=yes \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -N \
    -L "${LOCAL_RECORDING_PG_HOST}:${LOCAL_RECORDING_PG_PORT}:${REMOTE_RECORDING_PG_HOST}:${REMOTE_RECORDING_PG_PORT}" \
    "${SSH_USER}@${SSH_HOST}" &

  RECORDING_PG_TUNNEL_PID="$!"
  sleep 2

  if ! kill -0 "$RECORDING_PG_TUNNEL_PID" 2>/dev/null; then
    fail "Recording Postgres SSH tunnel process exited immediately"
  fi

  wait_for_port "$LOCAL_RECORDING_PG_HOST" "$LOCAL_RECORDING_PG_PORT" "Recording Postgres tunnel"
  log "Recording Postgres SSH tunnel started with pid=$RECORDING_PG_TUNNEL_PID"
}

cleanup() {
  local code=$?

  if [[ -n "${RECORDING_PG_TUNNEL_PID:-}" ]] && kill -0 "$RECORDING_PG_TUNNEL_PID" 2>/dev/null; then
    log "Closing recording Postgres SSH tunnel pid=$RECORDING_PG_TUNNEL_PID"
    kill "$RECORDING_PG_TUNNEL_PID" 2>/dev/null || true
    wait "$RECORDING_PG_TUNNEL_PID" 2>/dev/null || true
  fi

  if [[ -n "${MYSQL_TUNNEL_PID:-}" ]] && kill -0 "$MYSQL_TUNNEL_PID" 2>/dev/null; then
    log "Closing MySQL SSH tunnel pid=$MYSQL_TUNNEL_PID"
    kill "$MYSQL_TUNNEL_PID" 2>/dev/null || true
    wait "$MYSQL_TUNNEL_PID" 2>/dev/null || true
  fi

  if [[ "$code" -eq 0 ]]; then
    log "Analytics daily incremental job completed successfully"
  else
    log "Analytics daily incremental job failed with exit_code=$code"
  fi

  exit "$code"
}
trap cleanup EXIT

run_cmd() {
  log "RUN: $*"
  "$@"
}

main() {
  log "Starting AnalyticsEngine daily incremental job"
  log "APP_DIR=$APP_DIR"
  log "LOG_FILE=$LOG_FILE"

  # Prevent overlapping cron runs.
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    fail "Another analytics cron run is already active. lock=$LOCK_FILE"
  fi

  cd "$APP_DIR"

  if [[ -d "$VENV_DIR" ]]; then
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    PYTHON_BIN="${PYTHON_BIN:-python}"
  fi

  run_cmd "$PYTHON_BIN" --version

  start_mysql_ssh_tunnel
  start_recording_pg_tunnel_if_enabled

  # 1) Normal staging sync. Call logs are intentionally skipped here because
  #    build_unified_call_log handles RMS + recording/transcript call sources.
  run_cmd "$PYTHON_BIN" -m app.services.analytics_engine.jobs.regular.run_daily_stage_sync_all \
    --skip call_log_tracking,call_recordings_transcript,call_log_unified,unified_call_log,staging_call_log_unified \
    --limit "$STAGE_LIMIT" \
    --max-batches "$STAGE_MAX_BATCHES" \
    --json-summary "$STAGE_SUMMARY_FILE"

  # 2) Unified call source-of-truth builder.
  #    This syncs RMS call_tracking_log + recording/transcript Postgres into
  #    staging_call_log_unified, including late transcript/audio updates.
  CALL_BUILD_ARGS=(
    --limit "$CALL_LIMIT"
    --resync-window "$CALL_RESYNC_WINDOW"
    --match-window-seconds "$CALL_MATCH_WINDOW_SECONDS"
    --pretty
  )
  if [[ "$CALL_SKIP_COUNTS" == "1" ]]; then
    CALL_BUILD_ARGS+=(--skip-counts)
  fi
  if [[ "$CALL_SKIP_DUPLICATE_REFRESH" == "1" ]]; then
    CALL_BUILD_ARGS+=(--skip-duplicate-refresh)
  fi
  if [[ "$CALL_SKIP_BACKFILL_EXISTING_KEYS" == "1" ]]; then
    CALL_BUILD_ARGS+=(--skip-backfill-existing-keys)
  fi

  run_cmd "$PYTHON_BIN" -m app.services.analytics_engine.jobs.regular.build_unified_call_log "${CALL_BUILD_ARGS[@]}"

  # 3) Process staging data into event/fact/current-state tables.
  #    Skip its internal call-log build because step 2 already refreshed it.
  run_cmd "$PYTHON_BIN" -m app.services.analytics_engine.jobs.regular.run_from_checkpoint_10k \
    --limit "$PROCESSOR_LIMIT" \
    --skip-call-log-build

  log "Stage summary: $STAGE_SUMMARY_FILE"
  log "Log file: $LOG_FILE"
}

main "$@"
