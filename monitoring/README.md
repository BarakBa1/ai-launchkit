# n8n queue watchdog

`n8n_queue_watchdog.py` is a host-run, read-only Prometheus exporter for n8n
queue failures. It runs outside n8n's execution queue so a worker outage does
not also disable the collector. The process reads recent n8n executions,
classifies only error records with a Bull timeout signature and a zero-node or
missing-start boundary, and optionally correlates source IDs with successful
`errorWorkflow` and Slack child executions.

It does not write n8n, Redis, PostgreSQL, broker keys, or Docker state. Redis
queue inspection is bounded to one `SCAN` pass budget and allow-listed Bull
states. PostgreSQL probing is TCP acceptance only; it does not authenticate or
run SQL. The collector never logs API keys or passwords.

## Configuration

Start from [`n8n-queue-watchdog.env.example`](n8n-queue-watchdog.env.example).
`N8N_WATCHDOG_API_URL` and `N8N_API_KEY` are required to inspect executions.
Set `N8N_WATCHDOG_WORKFLOW_IDS` to a comma-separated allowlist to bound metric
labels. Set both workflow IDs for notification-missing alerts; if the handler
or Slack ID is absent, the collector deliberately reports correlation as
unknown rather than raising a false missing-notification alert.
When an allowlist is not supplied, `N8N_WATCHDOG_MAX_WORKFLOW_LABELS` bounds the
number of workflow label values retained; an allowlist is preferred for
production.

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

The [`n8n-queue-watchdog.service.example`](n8n-queue-watchdog.service.example)
unit is an installation template only. It is not installed by Compose or this
change. Bind/firewall port 9105 so only the Prometheus container can scrape it.

## Prometheus and Alertmanager

`prometheus/prometheus.yml` scrapes `host.docker.internal:9105`; the existing
Prometheus Compose service already supplies the host-gateway entry. It also
loads `prometheus/rules/n8n_queue_watchdog.yml` through a read-only rules mount.
This repository contains no Alertmanager service or endpoint configuration.
The operator must configure the existing external Alertmanager receiver during
rollout; the rule file alone evaluates alerts but cannot deliver them.

Because the alert labels contain workflow identity but not execution IDs,
Alertmanager deduplicates repeated scrapes for the same workflow. The process
also deduplicates execution IDs in memory. A restart may re-observe retained
records, so the Alertmanager fingerprint remains the final cross-restart
deduplication boundary.

## Rollout and rollback

Rollout requires an explicit production change window and review of the exact
workflow IDs, API key scope, host firewall rule, Prometheus reload, and external
Alertmanager receiver. Create a dedicated n8n read-only API key if the n8n
installation supports that scope. Do not run this using a broad database or
Docker-socket credential.

The first production readback must verify `/healthz`, `/metrics`, Prometheus
target health, rule loading, and one synthetic/read-only fixture evaluation.
Do not execute an n8n workflow or mutate broker/database state to test it.

Rollback is to stop/disable the host unit, remove the watchdog scrape/rules
mount, reload Prometheus, and verify the previous target/rule state. No n8n,
Redis, PostgreSQL, broker, credential, schedule, or workflow mutation is part
of this implementation.

## Limitations

- A main-process crash before n8n persists an execution cannot be discovered
  through the n8n API; host/container health probes are the fallback.
- Worker health URL availability depends on the deployed n8n 1.* topology; the
  collector does not assume a worker HTTP server exists.
- Redis key names are implementation details of Bull and are telemetry only;
  source execution records remain the authoritative failure signal.
- The exporter is intentionally not a broker reconciliation or trading
  decision component.
