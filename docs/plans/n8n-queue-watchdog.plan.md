# n8n Queue Watchdog Plan

## Scope

Add an external, read-only Prometheus exporter for n8n queue failures and
dependency health. Keep the collector outside the n8n execution queue, use
only Python's standard library, and integrate the exporter and scrape target
atomically in the existing `monitoring` Compose profile. The implementation
does not add credentials, database writes, or deployment actions.

## Ordered tasks

1. Add focused tests for queue-failure classification, nested handler
   correlation, bounded Redis RESP parsing, and Prometheus exposition.
2. Implement the stdlib collector/server with explicit configuration, bounded
   probes, stable metric labels, and in-process event deduplication.
3. Add Prometheus scrape/rule integration, a monitoring-profile Compose
   service, and a systemd alternative without real endpoints or secrets.
4. Add the pinned monitoring-profile Alertmanager delivery service, fail-fast
   receiver-secret validation, and operator-facing setup/readback/reporting.
5. Document installation, read-only boundaries, rollback, and known
   limitations.
6. Run focused tests, shell/Compose/config validation, and repository checks;
   inspect the final diff and commit only the isolated branch.

## Acceptance criteria

- Queue failures require an error status, Bull timeout signature, and either a
  missing start timestamp or zero executed nodes.
- Handler/Slack correlation requires both workflow IDs. A missing ID is
  explicitly unknown/unhealthy and never completes the notification path.
- Redis, PostgreSQL, n8n API, and worker probes fail closed without leaking
  credentials.
- Metrics are bounded by configured workflow labels and rules are compatible
  with Prometheus/Alertmanager. The monitoring profile starts both services,
  routes only queue-watchdog critical/warning alerts to the operator-supplied
  HTTPS Slack webhook, and fails closed when its root-owned receiver file is
  absent, empty, oversized, or not HTTPS.
- The receiver path is external, absolute, regular, non-symlink, and on Unix
  owned by `root:65534` with mode `0440` or `0640`; all monitoring image tags
  are explicit version pins.
- A critical watchdog alert inhibits warning symptoms from the same scrape job
  without inhibiting distinct critical causes.
- No ai-n8n-trading file, production state, host state, or secret changes.
- The Compose profile cannot advertise an exporter target without defining the
  matching watchdog service in the same configuration change.
