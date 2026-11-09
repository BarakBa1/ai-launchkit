#!/usr/bin/env bash
# Weekly VPS disk cleanup for the AI4TRADE paper-trading stack.
# Keeps Langfuse traces/observations/scores and its MinIO event files for
# RETENTION_DAYS, empties ClickHouse internal system logs, and trims the
# journal and Docker caches. Safe to re-run. Cron: Sunday 05:07 UTC.
#   7 5 * * 0 /root/ops/weekly_cleanup.sh >> /var/log/ai4trade-weekly-cleanup.log 2>&1
set -uo pipefail

RETENTION_DAYS="${RETENTION_DAYS:-14}"
CH_CONTAINER="${CH_CONTAINER:-localai-clickhouse-1}"
MINIO_CONTAINER="${MINIO_CONTAINER:-localai-minio-1}"
MINIO_BUCKET="${MINIO_BUCKET:-langfuse}"

ch() { docker exec "$CH_CONTAINER" clickhouse-client --query "$1"; }
log() { echo "$(date -u +%FT%TZ) $*"; }

log "start retention=${RETENTION_DAYS}d disk=$(df -h / | awk 'NR==2{print $5" used, "$4" free"}')"

# 1. ClickHouse internal diagnostic logs (server telemetry, not Langfuse data).
for t in $(ch "SELECT DISTINCT table FROM system.parts WHERE database='system' AND active AND table LIKE '%_log%' FORMAT TSV"); do
  ch "TRUNCATE TABLE system.${t}" && log "clickhouse system.${t} truncated"
done

# 2. Langfuse rows older than the retention window.
for spec in "traces:timestamp" "observations:start_time" "scores:timestamp"; do
  table="${spec%%:*}"; column="${spec##*:}"
  ch "ALTER TABLE default.${table} DELETE WHERE ${column} < now() - INTERVAL ${RETENTION_DAYS} DAY" \
    && log "langfuse ${table}: delete older than ${RETENTION_DAYS}d queued"
done

# 3. Langfuse raw event files in MinIO older than the retention window.
#    Credentials stay inside the MinIO container.
docker exec "$MINIO_CONTAINER" sh -c '
  mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null &&
  mc rm --recursive --force --older-than '"${RETENTION_DAYS}"'d local/'"${MINIO_BUCKET}"' | tail -1
' && log "minio ${MINIO_BUCKET}: objects older than ${RETENTION_DAYS}d removed"

# 4. Journal and Docker caches.
journalctl --vacuum-size=500M >/dev/null 2>&1 && log "journal vacuumed to 500M"
docker builder prune -f --filter until=168h >/dev/null 2>&1 && log "docker build cache older than 7d pruned"
docker image prune -f >/dev/null 2>&1 && log "dangling docker images pruned"

# 5. Host temp files and caches (files only, untouched for 7+ days).
find /tmp /var/tmp -xdev -type f -atime +7 -delete 2>/dev/null && log "/tmp and /var/tmp files idle 7d+ removed"
find /tmp /var/tmp -xdev -mindepth 1 -type d -empty -mtime +7 -delete 2>/dev/null
apt-get clean >/dev/null 2>&1 && log "apt cache cleaned"
find /root/.cache -xdev -type f -atime +14 -delete 2>/dev/null && log "/root/.cache files idle 14d+ removed"
find /var/log -xdev -type f \( -name '*.gz' -o -name '*.[0-9]' -o -name '*.old' \) -mtime +14 -delete 2>/dev/null \
  && log "rotated logs older than 14d removed"

log "done disk=$(df -h / | awk 'NR==2{print $5" used, "$4" free"}')"
