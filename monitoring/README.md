# n8n queue watchdog

`n8n_queue_watchdog.py` is an external, read-only Prometheus exporter for n8n
queue failures. It runs outside n8n's execution queue so a worker outage does
not also disable the collector. The process reads recent n8n executions,
classifies only error records with a Bull timeout signature and a zero-node or
missing-start boundary, and can correlate source IDs with successful
`errorWorkflow` and Slack child executions.

It does not write n8n, Redis, PostgreSQL, broker keys, or Docker state. Redis
queue inspection is bounded to one `SCAN` pass budget and allow-listed Bull
states. PostgreSQL probing is TCP acceptance only; it does not authenticate or
run SQL. The collector never logs API keys or passwords.

## Configuration

Start from [`n8n-queue-watchdog.env.example`](n8n-queue-watchdog.env.example).
`N8N_WATCHDOG_API_URL` and `N8N_API_KEY` are required to inspect executions.
The API URL must be HTTPS, and the client refuses redirects to another origin
or to HTTP before sending the API key. Missing or invalid API configuration is
an unhealthy collection (`n8n_watchdog_collection_success=0`), so it is
alertable rather than treated as a successful empty scan.
Set `N8N_WATCHDOG_WORKFLOW_IDS` to a comma-separated allowlist to bound metric
labels. Set both workflow IDs for notification-missing alerts; if the handler
or Slack ID is absent, the configuration is explicitly unhealthy/unknown and
the retained failure remains unnotified; a partial pair is never treated as a
complete notification path. The correlation rule therefore requires both
`N8N_WATCHDOG_ERROR_WORKFLOW_ID` and `N8N_WATCHDOG_SLACK_WORKFLOW_ID`.
When an allowlist is not supplied, `N8N_WATCHDOG_MAX_WORKFLOW_LABELS` bounds the
number of workflow label values retained; an allowlist is preferred for
production.
Execution list responses are byte-capped and paginated to a configured time
boundary (`N8N_WATCHDOG_SOURCE_LOOKBACK_SECONDS`). If the boundary cannot be
proven within `N8N_WATCHDOG_MAX_PAGES`, collection is marked unhealthy instead
of silently treating a fixed latest-N sample as complete. Listing calls request
metadata only; full `includeData=true` payloads are fetched when inline error
metadata cannot prove that an error row is not a zero-node queue failure and
for configured notification correlation. On n8n 1.123.27, continuation uses
the last execution ID (`lastId`) from the page boundary; the response's
`nextCursor` is only a server-side indication that another `lastId` request is
needed. A successful detail response must still materialize a node summary,
runData, or enough error evidence to classify the row. Empty, non-object, or
dataTooLargeToDisplay detail responses are retained as incomplete coverage
and make collection unhealthy; they are never treated as an ordinary
non-queue error.

Health URLs and Redis/PostgreSQL hosts are optional. An unset probe is exposed
as `configured=0` and is excluded from the dependency alert rule. Configure
only endpoints reachable from the host-run process. Keep the runtime env file
root-owned and outside this repository.

## Local run

```bash
python3 monitoring/n8n_queue_watchdog.py --host 0.0.0.0 --port 9105
curl http://127.0.0.1:9105/healthz
curl http://127.0.0.1:9105/metrics
```

The `monitoring` Compose profile installs the exporter and Prometheus target as
one configuration unit. Start it with `docker compose --profile monitoring up
-d`; Prometheus addresses `n8n-queue-watchdog:9105` on the Compose network, so
the target cannot silently refer to an uninstalled host process. The API key
and URL still need to be supplied through the operator environment. The
[`n8n-queue-watchdog.service.example`](n8n-queue-watchdog.service.example)
unit remains an alternative for operators that deliberately run the exporter
on the host; do not enable both instances for the same target.

## Prometheus and Alertmanager

`prometheus/prometheus.yml` scrapes `n8n-queue-watchdog:9105` when the
`monitoring` profile is enabled. It also loads
`prometheus/rules/n8n_queue_watchdog.yml` through a read-only rules mount.
The same profile starts the pinned `alertmanager` service and Prometheus sends
alerts to `alertmanager:9093` over the private Compose network. Alertmanager
routes only `n8n-queue` and `n8n-queue-watchdog` alerts with `critical` or
`warning` severity to Slack; unrelated alerts go to a discard receiver.
When a critical watchdog alert is active, it inhibits warning alerts from the
same watchdog scrape job; distinct critical alerts, including queue-failure
alerts, remain visible.
Alertmanager is intentionally not exposed through Caddy.

The monitoring images are version-pinned: Python `3.12.14-alpine3.24`,
Prometheus `v3.5.0`, Alertmanager `v0.28.1`, node-exporter `v1.11.1`, cAdvisor
`v0.60.5`, and Grafana `13.2.2`. Review and update these pins as a coordinated
monitoring change.

The Slack incoming-webhook URL is supplied through a root-owned file, never
committed to this repository. Set `ALERTMANAGER_SLACK_WEBHOOK_FILE` in the
operator `.env` (the example defaults to `/etc/ai-launchkit/alertmanager-slack-webhook`)
and create a non-empty file at that path before selecting the `monitoring`
profile. The Compose secret is mounted read-only at
`/run/secrets/alertmanager_slack_webhook`; the configuration uses
Alertmanager's `api_url_file` field. The installer runs
`monitoring/validate_alertmanager_delivery.py` before starting services, and
the Alertmanager entrypoint fails closed if the mounted file is absent, empty,
or not an HTTPS URL. On Unix, validation also requires an external regular
non-symlink file owned by `root:65534` with mode `0440` or `0640`; relative or
repository-local paths are rejected. Non-monitoring profiles do not require
the file.

For example, create the protected placeholder and then populate it through the
operator's secret-management process:

```bash
sudo install -d -o root -g root -m 0750 /etc/ai-launchkit
sudo install -o root -g 65534 -m 0640 /dev/null /etc/ai-launchkit/alertmanager-slack-webhook
sudoedit /etc/ai-launchkit/alertmanager-slack-webhook
```

The file must remain owned by `root`, be readable by the container's fixed
UID/GID `65534:65534`, and contain the HTTPS Slack incoming-webhook URL on one
line. File-backed Compose secrets are bind mounts, so `root:root` mode `0600`
would make the non-root Alertmanager unable to read the file. Do not print the
URL in shell output, logs, reports, or support requests.

Because the alert labels contain workflow identity but not execution IDs,
Alertmanager deduplicates repeated scrapes for the same workflow. The process
also deduplicates execution IDs in memory. A restart may re-observe retained
records, so the Alertmanager fingerprint remains the final cross-restart
deduplication boundary.

## Rollout and rollback

Rollout requires an explicit production change window and review of the exact
workflow IDs, API key scope, Prometheus reload, and operator-supplied Slack
receiver file. Create a dedicated n8n read-only API key if the n8n installation
supports that scope. Do not run this using a broad database or Docker-socket
credential. For the Compose profile, verify the watchdog service and
Prometheus target are enabled together; for the systemd alternative, install
the unit and configure the matching target deliberately.

The first production readback must verify `/healthz`, `/metrics`, Prometheus
target health, rule loading, Alertmanager `/api/v2/status` and receivers, and
one synthetic/read-only fixture evaluation. Do not execute an n8n workflow or
mutate broker/database state to test it.

Rollback is to stop/disable the selected watchdog and Alertmanager services,
restore the previous Prometheus configuration and rules, reload Prometheus, and
verify the previous target/rule state. Preserve the operator-managed webhook
file and restore its previous path setting if the Compose environment changed.
No n8n, Redis, PostgreSQL, broker, credential, schedule, or workflow mutation
is part of this implementation.

## Limitations

- A main-process crash before n8n persists an execution cannot be discovered
  through the n8n API; host/container health probes are the fallback.
- Worker health URL availability depends on the deployed n8n 1.* topology; the
  collector does not assume a worker HTTP server exists.
- Redis key names are implementation details of Bull and are telemetry only;
  source execution records remain the authoritative failure signal.
- A notification correlation read that fails, reaches its page cap, or lacks
  either required workflow ID is explicitly unknown and emits a dependency
  metric; retained failures remain unnotified until a successful receipt is
  observed.
- The exporter is intentionally not a broker reconciliation or trading
  decision component.
