#!/bin/sh
set -eu

secret_file=/run/secrets/alertmanager_slack_webhook
if [ ! -f "$secret_file" ] || [ ! -r "$secret_file" ] || [ ! -s "$secret_file" ]; then
  echo "Alertmanager Slack delivery is enabled but ALERTMANAGER_SLACK_WEBHOOK_FILE is missing or empty" >&2
  exit 64
fi

if [ -L "$secret_file" ]; then
  echo "Alertmanager Slack delivery receiver must not be a symlink" >&2
  exit 64
fi

if ! secret_uid=$(stat -c '%u' "$secret_file") || \
   ! secret_gid=$(stat -c '%g' "$secret_file") || \
   ! secret_mode=$(stat -c '%a' "$secret_file"); then
  echo "Alertmanager Slack delivery receiver metadata could not be read" >&2
  exit 64
fi
if [ "$secret_uid" != "0" ] || [ "$secret_gid" != "65534" ]; then
  echo "Alertmanager Slack delivery receiver must be owned by root:65534" >&2
  exit 64
fi
case "$secret_mode" in
  440|640) ;;
  *)
    echo "Alertmanager Slack delivery receiver mode must be 0440 or 0640" >&2
    exit 64
    ;;
esac

secret_bytes=$(wc -c < "$secret_file")
if [ "$secret_bytes" -gt 4096 ]; then
  echo "Alertmanager Slack delivery receiver file is too large" >&2
  exit 64
fi

webhook_url=$(cat "$secret_file")
case "$webhook_url" in
  https://?*) ;;
  *)
    echo "Alertmanager Slack delivery requires an HTTPS receiver URL" >&2
    exit 64
    ;;
esac

exec /bin/alertmanager "$@"
