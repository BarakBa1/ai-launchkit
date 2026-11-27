#!/usr/bin/env bash
# Weekly VPS disk cleanup for the AI4TRADE paper-trading stack.
# Trims the journal, Docker caches and host temp files. The Langfuse,
# ClickHouse and MinIO steps were removed on 2026-11-27 with those services.
# Safe to re-run. Cron: Sunday 05:07 UTC.
#   7 5 * * 0 /root/ops/weekly_cleanup.sh >> /var/log/ai4trade-weekly-cleanup.log 2>&1
set -uo pipefail

log() { echo "$(date -u +%FT%TZ) $*"; }

log "start disk=$(df -h / | awk 'NR==2{print $5" used, "$4" free"}')"

# 1. Journal and Docker caches.
journalctl --vacuum-size=500M >/dev/null 2>&1 && log "journal vacuumed to 500M"
docker builder prune -f --filter until=168h >/dev/null 2>&1 && log "docker build cache older than 7d pruned"
docker image prune -f >/dev/null 2>&1 && log "dangling docker images pruned"

# 2. Host temp files and caches (files only, untouched for 7+ days).
find /tmp /var/tmp -xdev -type f -atime +7 -delete 2>/dev/null && log "/tmp and /var/tmp files idle 7d+ removed"
find /tmp /var/tmp -xdev -mindepth 1 -type d -empty -mtime +7 -delete 2>/dev/null
apt-get clean >/dev/null 2>&1 && log "apt cache cleaned"
find /root/.cache -xdev -type f -atime +14 -delete 2>/dev/null && log "/root/.cache files idle 14d+ removed"
find /var/log -xdev -type f \( -name '*.gz' -o -name '*.[0-9]' -o -name '*.old' \) -mtime +14 -delete 2>/dev/null \
  && log "rotated logs older than 14d removed"

log "done disk=$(df -h / | awk 'NR==2{print $5" used, "$4" free"}')"
