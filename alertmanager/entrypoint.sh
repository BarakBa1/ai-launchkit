#!/bin/sh
set -eu

secret_file=/run/secrets/alertmanager_slack_webhook
if [ ! -f "$secret_file" ] || [ ! -r "$secret_file" ] || [ ! -s "$secret_file" ]; then
  echo "Alertmanager Slack delivery is enabled but ALERTMANAGER_SLACK_WEBHOOK_FILE is missing or empty" >&2
  exit 64
fi

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
