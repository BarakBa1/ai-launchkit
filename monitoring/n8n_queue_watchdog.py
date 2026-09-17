#!/usr/bin/env python3
"""Read-only n8n queue watchdog and Prometheus exporter.

The process is intentionally outside n8n's execution queue.  It reads recent
n8n executions, optionally correlates the configured error/Slack workflows,
probes Redis/PostgreSQL/HTTP health, and exposes bounded Prometheus metrics.
It never writes to n8n, Redis, PostgreSQL, or the broker.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import threading
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("n8n_queue_watchdog")
QUEUE_FAILURE_RE = re.compile(
    r"(?:timeout\s+exceeded\s+when\s+trying\s+to\s+connect|\bqueue\.onfailed\b|\bbull\b)",
    re.IGNORECASE,
)
QUEUE_KEY_RE = re.compile(r"^bull:(?P<queue>.+):(?P<state>wait|active|failed|delayed|stalled)$")
METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _bounded_int(value: str, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _bounded_float(value: str, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _csv_set(value: str) -> frozenset[str]:
    return frozenset(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class WatchdogConfig:
    n8n_api_url: str = ""
    n8n_api_key: str = ""
    execution_limit: int = 100
    workflow_ids: frozenset[str] = frozenset()
    max_workflow_labels: int = 50
    error_workflow_id: str = ""
    slack_workflow_id: str = ""
    notification_grace_seconds: float = 60.0
    failure_retention_seconds: float = 3600.0
    redis_host: str = ""
    redis_port: int = 6379
    redis_password: str = ""
    redis_username: str = ""
    redis_scan_count: int = 100
    redis_max_keys: int = 500
    postgres_host: str = ""
    postgres_port: int = 5432
    probe_timeout_seconds: float = 2.0
    n8n_health_url: str = ""
    worker_health_url: str = ""
    scrape_cache_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> "WatchdogConfig":
        return cls(
            n8n_api_url=os.getenv("N8N_WATCHDOG_API_URL", "").rstrip("/"),
            n8n_api_key=os.getenv("N8N_API_KEY", ""),
            execution_limit=_bounded_int(os.getenv("N8N_WATCHDOG_EXECUTION_LIMIT", "100"), 100, 1, 500),
            workflow_ids=_csv_set(os.getenv("N8N_WATCHDOG_WORKFLOW_IDS", "")),
            max_workflow_labels=_bounded_int(
                os.getenv("N8N_WATCHDOG_MAX_WORKFLOW_LABELS", "50"), 50, 1, 500
            ),
            error_workflow_id=os.getenv("N8N_WATCHDOG_ERROR_WORKFLOW_ID", "").strip(),
            slack_workflow_id=os.getenv("N8N_WATCHDOG_SLACK_WORKFLOW_ID", "").strip(),
            notification_grace_seconds=_bounded_float(
                os.getenv("N8N_WATCHDOG_NOTIFICATION_GRACE_SECONDS", "60"), 60.0, 1.0, 3600.0
            ),
            failure_retention_seconds=_bounded_float(
                os.getenv("N8N_WATCHDOG_FAILURE_RETENTION_SECONDS", "3600"), 3600.0, 60.0, 86400.0
            ),
            redis_host=os.getenv("N8N_WATCHDOG_REDIS_HOST", "").strip(),
            redis_port=_bounded_int(os.getenv("N8N_WATCHDOG_REDIS_PORT", "6379"), 6379, 1, 65535),
            redis_password=os.getenv("N8N_WATCHDOG_REDIS_PASSWORD", ""),
            redis_username=os.getenv("N8N_WATCHDOG_REDIS_USERNAME", ""),
            redis_scan_count=_bounded_int(os.getenv("N8N_WATCHDOG_REDIS_SCAN_COUNT", "100"), 100, 10, 500),
            redis_max_keys=_bounded_int(os.getenv("N8N_WATCHDOG_REDIS_MAX_KEYS", "500"), 500, 1, 5000),
            postgres_host=os.getenv("N8N_WATCHDOG_POSTGRES_HOST", "").strip(),
            postgres_port=_bounded_int(os.getenv("N8N_WATCHDOG_POSTGRES_PORT", "5432"), 5432, 1, 65535),
            probe_timeout_seconds=_bounded_float(
                os.getenv("N8N_WATCHDOG_PROBE_TIMEOUT_SECONDS", "2"), 2.0, 0.2, 10.0
            ),
            n8n_health_url=os.getenv("N8N_WATCHDOG_N8N_HEALTH_URL", "").strip(),
            worker_health_url=os.getenv("N8N_WATCHDOG_WORKER_HEALTH_URL", "").strip(),
            scrape_cache_seconds=_bounded_float(
                os.getenv("N8N_WATCHDOG_SCRAPE_CACHE_SECONDS", "10"), 10.0, 0.0, 60.0
            ),
        )


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _nested_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return value
        return decoded
    return value


def _execution_data(execution: Mapping[str, Any]) -> Mapping[str, Any]:
    return _as_mapping(_nested_json(execution.get("data", {})))


def executed_node_count(execution: Mapping[str, Any]) -> Optional[int]:
    """Return the known executed-node count, or None when the API omitted it."""

    for source in (execution, _execution_data(execution)):
        summary = _as_mapping(source.get("summary"))
        for key in ("executedNodes", "executed_nodes", "nodeCount"):
            if key in summary:
                try:
                    return int(summary[key])
                except (TypeError, ValueError):
                    pass

    result_data = _as_mapping(_execution_data(execution).get("resultData"))
    run_data = _as_mapping(result_data.get("runData"))
    if run_data:
        return len(run_data)
    if "runData" in result_data:
        return 0
    return None


def execution_error_text(execution: Mapping[str, Any]) -> str:
    error = execution.get("error")
    pieces: List[str] = []
    if isinstance(error, Mapping):
        for key in ("message", "stack", "name"):
            value = error.get(key)
            if value:
                pieces.append(str(value))
    elif error:
        pieces.append(str(error))
    for key in ("message", "stack"):
        if execution.get(key):
            pieces.append(str(execution[key]))
    data_error = _as_mapping(_as_mapping(_execution_data(execution).get("resultData")).get("error"))
    for key in ("message", "stack", "name"):
        if data_error.get(key):
            pieces.append(str(data_error[key]))
    return " ".join(pieces)


def is_queue_failure(execution: Mapping[str, Any]) -> bool:
    """Identify a zero-node Bull queue failure without guessing on other errors."""

    if str(execution.get("status", "")).lower() != "error":
        return False
    if not QUEUE_FAILURE_RE.search(execution_error_text(execution)):
        return False
    started_at = execution.get("startedAt")
    node_count = executed_node_count(execution)
    return started_at in (None, "") or node_count == 0


def _contains_reference(value: Any, reference: str) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_reference(child, reference) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_reference(child, reference) for child in value)
    return str(value) == reference if value is not None else False


def handler_references_execution(handler_execution: Mapping[str, Any], execution_id: str) -> bool:
    """Check nested n8n execution data without depending on one node layout."""

    return bool(execution_id) and _contains_reference(handler_execution, str(execution_id))


def _successful(execution: Mapping[str, Any]) -> bool:
    return str(execution.get("status", "")).lower() == "success" and execution.get("finished", True) is not False


def _workflow_id(execution: Mapping[str, Any]) -> str:
    return str(execution.get("workflowId") or execution.get("workflow_id") or "unknown")


def _workflow_name(execution: Mapping[str, Any]) -> str:
    return str(execution.get("workflowName") or execution.get("workflow_name") or "unknown")


def _timestamp(value: Any, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        from datetime import datetime

        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return default


def _label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(values: Mapping[str, Any]) -> str:
    if not values:
        return ""
    return "{" + ",".join(f'{key}="{_label(value)}"' for key, value in sorted(values.items())) + "}"


def metric_line(name: str, value: Any, labels: Optional[Mapping[str, Any]] = None) -> str:
    return f"{name}{_labels(labels or {})} {value}"


def _metric_block(name: str, metric_type: str, help_text: str, samples: Iterable[str]) -> List[str]:
    return [f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}", *samples]


class N8nApiClient:
    def __init__(self, base_url: str, api_key: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _get(self, resource: str, params: Mapping[str, Any]) -> Any:
        if not self.configured:
            raise RuntimeError("n8n API is not configured")
        query = urlencode({key: str(value) for key, value in params.items()})
        url = urljoin(self.base_url + "/", resource.lstrip("/"))
        if query:
            url += "?" + query
        request = Request(url, headers={"X-N8N-API-KEY": self.api_key, "Accept": "application/json"})
        with urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def list_executions(self, workflow_id: str = "", limit: int = 100) -> List[Mapping[str, Any]]:
        params: Dict[str, Any] = {"limit": limit, "includeData": "true"}
        if workflow_id:
            params["workflowId"] = workflow_id
        payload = self._get("executions", params)
        rows = payload if isinstance(payload, list) else _as_mapping(payload).get("data", [])
        return [row for row in rows if isinstance(row, Mapping)]

    def probe(self) -> bool:
        try:
            self.list_executions(limit=1)
        except (HTTPError, URLError, OSError, RuntimeError, ValueError, json.JSONDecodeError):
            return False
        return True


def _redis_encode(parts: Sequence[str]) -> bytes:
    encoded = [part.encode("utf-8") for part in parts]
    return b"*" + str(len(encoded)).encode("ascii") + b"\r\n" + b"".join(
        b"$" + str(len(part)).encode("ascii") + b"\r\n" + part + b"\r\n" for part in encoded
    )


def _redis_read_line(sock: socket.socket) -> bytes:
    data = bytearray()
    while True:
        byte = sock.recv(1)
        if not byte:
            raise RuntimeError("Redis closed the connection")
        data.extend(byte)
        if data.endswith(b"\r\n"):
            return bytes(data[:-2])
        if len(data) > 1024 * 1024:
            raise RuntimeError("Redis response line exceeded limit")


def _redis_read_response(sock: socket.socket) -> Any:
    prefix = sock.recv(1)
    if not prefix:
        raise RuntimeError("Redis closed the connection")
    if prefix == b"+":
        return _redis_read_line(sock).decode("utf-8", "replace")
    if prefix == b"-":
        raise RuntimeError(_redis_read_line(sock).decode("utf-8", "replace"))
    if prefix == b":":
        return int(_redis_read_line(sock))
    if prefix == b"$":
        size = int(_redis_read_line(sock))
        if size < 0:
            return None
        data = bytearray()
        while len(data) < size + 2:
            chunk = sock.recv(size + 2 - len(data))
            if not chunk:
                raise RuntimeError("Redis returned a truncated bulk value")
            data.extend(chunk)
        return bytes(data[:size]).decode("utf-8", "replace")
    if prefix == b"*":
        count = int(_redis_read_line(sock))
        if count < 0:
            return None
        return [_redis_read_response(sock) for _ in range(count)]
    raise RuntimeError("Redis returned an unknown response type")


class RedisProbe:
    _COUNT_BY_TYPE = {"list": "LLEN", "zset": "ZCARD", "set": "SCARD", "stream": "XLEN", "hash": "HLEN"}

    def __init__(self, config: WatchdogConfig) -> None:
        self.config = config

    def _command(self, sock: socket.socket, *parts: str) -> Any:
        sock.sendall(_redis_encode(parts))
        return _redis_read_response(sock)

    def collect(self) -> Tuple[bool, Dict[Tuple[str, str], int]]:
        if not self.config.redis_host:
            return False, {}
        counts: Dict[Tuple[str, str], int] = {}
        try:
            with socket.create_connection(
                (self.config.redis_host, self.config.redis_port), timeout=self.config.probe_timeout_seconds
            ) as sock:
                sock.settimeout(self.config.probe_timeout_seconds)
                if self.config.redis_password:
                    auth_parts = ["AUTH"]
                    if self.config.redis_username:
                        auth_parts.append(self.config.redis_username)
                    auth_parts.append(self.config.redis_password)
                    self._command(sock, *auth_parts)
                if str(self._command(sock, "PING")).upper() != "PONG":
                    return False, {}
                cursor = "0"
                pages = 0
                keys_seen = 0
                while pages < 50 and keys_seen < self.config.redis_max_keys:
                    response = self._command(
                        sock,
                        "SCAN",
                        cursor,
                        "MATCH",
                        "bull:*",
                        "COUNT",
                        str(self.config.redis_scan_count),
                    )
                    if not isinstance(response, list) or len(response) != 2:
                        break
                    cursor = str(response[0])
                    for key_value in response[1] if isinstance(response[1], list) else []:
                        if keys_seen >= self.config.redis_max_keys:
                            break
                        match = QUEUE_KEY_RE.match(str(key_value))
                        if not match:
                            continue
                        queue = match.group("queue")
                        state = match.group("state")
                        redis_type = str(self._command(sock, "TYPE", str(key_value))).lower()
                        count_command = self._COUNT_BY_TYPE.get(redis_type)
                        if not count_command:
                            continue
                        count = self._command(sock, count_command, str(key_value))
                        counts[(queue, state)] = int(count or 0)
                        keys_seen += 1
                    pages += 1
                    if cursor == "0":
                        break
        except (OSError, RuntimeError, ValueError):
            return False, {}
        return True, counts


def postgres_accepting_connections(config: WatchdogConfig) -> bool:
    if not config.postgres_host:
        return False
    try:
        with socket.create_connection(
            (config.postgres_host, config.postgres_port), timeout=config.probe_timeout_seconds
        ):
            return True
    except OSError:
        return False


def http_probe(url: str, timeout_seconds: float) -> bool:
    if not url:
        return False
    try:
        request = Request(url, headers={"Accept": "*/*"})
        with urlopen(request, timeout=timeout_seconds) as response:
            return 200 <= int(response.status) < 400
    except (HTTPError, URLError, OSError, ValueError):
        return False


@dataclass
class FailureState:
    execution: Mapping[str, Any]
    first_seen: float
    source_timestamp: float
    notified: Optional[bool] = None


class WatchdogCollector:
    def __init__(self, config: WatchdogConfig, api_client: Optional[N8nApiClient] = None) -> None:
        self.config = config
        self.api = api_client or N8nApiClient(config.n8n_api_url, config.n8n_api_key, config.probe_timeout_seconds)
        self.redis = RedisProbe(config)
        self.failures: "OrderedDict[str, FailureState]" = OrderedDict()
        self.failure_totals: Dict[Tuple[str, str], int] = defaultdict(int)
        self.last_body = ""
        self.last_collection = 0.0
        self.lock = threading.Lock()

    def _source_executions(self) -> Tuple[bool, List[Mapping[str, Any]]]:
        if not self.api.configured:
            return False, []
        try:
            rows = self.api.list_executions(limit=self.config.execution_limit)
        except (HTTPError, URLError, OSError, RuntimeError, ValueError, json.JSONDecodeError):
            return False, []
        if self.config.workflow_ids:
            rows = [row for row in rows if _workflow_id(row) in self.config.workflow_ids]
        return True, rows

    def _notification_rows(
        self,
    ) -> Optional[Tuple[List[Mapping[str, Any]], List[Mapping[str, Any]]]]:
        """Read each notification workflow once per scrape, not once per failure."""

        if not self.config.error_workflow_id:
            return None
        try:
            handler_rows = self.api.list_executions(self.config.error_workflow_id, self.config.execution_limit)
            slack_rows: List[Mapping[str, Any]] = []
            if self.config.slack_workflow_id:
                slack_rows = self.api.list_executions(self.config.slack_workflow_id, self.config.execution_limit)
            return handler_rows, slack_rows
        except (HTTPError, URLError, OSError, RuntimeError, ValueError, json.JSONDecodeError):
            return None

    def _notification_state(
        self,
        source_id: str,
        notification_rows: Optional[Tuple[List[Mapping[str, Any]], List[Mapping[str, Any]]]],
    ) -> Optional[bool]:
        if not self.config.error_workflow_id or notification_rows is None:
            return None
        handler_rows, slack_rows = notification_rows
        handler_ok = any(_successful(row) and handler_references_execution(row, source_id) for row in handler_rows)
        if not handler_ok:
            return False
        if not self.config.slack_workflow_id:
            return True
        return any(_successful(row) and handler_references_execution(row, source_id) for row in slack_rows)

    def _prune_failures(self, now: float) -> None:
        cutoff = now - self.config.failure_retention_seconds
        for execution_id, state in list(self.failures.items()):
            if state.source_timestamp < cutoff:
                del self.failures[execution_id]
        while len(self.failures) > 1000:
            self.failures.popitem(last=False)

    def collect(self, force: bool = False) -> str:
        now = time.time()
        with self.lock:
            if (
                not force
                and self.last_body
                and now - self.last_collection < self.config.scrape_cache_seconds
            ):
                return self.last_body

            api_up, executions = self._source_executions()
            for execution in executions:
                if not is_queue_failure(execution):
                    continue
                workflow_id = _workflow_id(execution)
                known_workflows = {key[0] for key in self.failure_totals}
                if (
                    not self.config.workflow_ids
                    and workflow_id not in known_workflows
                    and len(known_workflows) >= self.config.max_workflow_labels
                ):
                    continue
                execution_id = str(execution.get("id") or "")
                if not execution_id:
                    continue
                source_time = _timestamp(execution.get("stoppedAt") or execution.get("createdAt"), now)
                state = self.failures.get(execution_id)
                if state is None:
                    state = FailureState(execution, now, source_time)
                    self.failures[execution_id] = state
                    self.failure_totals[(_workflow_id(execution), _workflow_name(execution))] += 1
                else:
                    state.execution = execution
                    state.source_timestamp = source_time
                self.failures.move_to_end(execution_id)

            self._prune_failures(now)
            notification_rows = self._notification_rows() if self.failures else None
            for execution_id, state in self.failures.items():
                state.notified = self._notification_state(execution_id, notification_rows)
            redis_configured = bool(self.config.redis_host)
            redis_up, queue_counts = self.redis.collect() if redis_configured else (False, {})
            postgres_configured = bool(self.config.postgres_host)
            postgres_up = postgres_accepting_connections(self.config) if postgres_configured else False
            n8n_configured = bool(self.config.n8n_health_url)
            n8n_up = http_probe(self.config.n8n_health_url, self.config.probe_timeout_seconds) if n8n_configured else False
            worker_configured = bool(self.config.worker_health_url)
            worker_up = http_probe(self.config.worker_health_url, self.config.probe_timeout_seconds) if worker_configured else False

            lines: List[str] = []
            lines += _metric_block(
                "n8n_watchdog_collection_success",
                "gauge",
                "Whether the watchdog completed its most recent collection cycle.",
                [metric_line("n8n_watchdog_collection_success", 1)],
            )
            lines += _metric_block(
                "n8n_watchdog_last_collection_timestamp_seconds",
                "gauge",
                "Unix timestamp of the most recent watchdog collection cycle.",
                [metric_line("n8n_watchdog_last_collection_timestamp_seconds", now)],
            )
            component_values = {
                "n8n_api": (self.api.configured, api_up),
                "n8n": (n8n_configured, n8n_up),
                "n8n_worker": (worker_configured, worker_up),
                "redis": (redis_configured, redis_up),
                "postgres": (postgres_configured, postgres_up),
            }
            lines += _metric_block(
                "n8n_watchdog_component_configured",
                "gauge",
                "Whether a watchdog component probe has been configured.",
                [
                    metric_line("n8n_watchdog_component_configured", int(configured), {"component": component})
                    for component, (configured, _up) in sorted(component_values.items())
                ],
            )
            lines += _metric_block(
                "n8n_watchdog_component_up",
                "gauge",
                "Whether the most recent watchdog probe succeeded.",
                [
                    metric_line("n8n_watchdog_component_up", int(up), {"component": component})
                    for component, (_configured, up) in sorted(component_values.items())
                ],
            )

            totals_samples = []
            last_seen: Dict[Tuple[str, str], float] = defaultdict(float)
            unnotified: Dict[Tuple[str, str], int] = defaultdict(int)
            for state in self.failures.values():
                key = (_workflow_id(state.execution), _workflow_name(state.execution))
                last_seen[key] = max(last_seen[key], state.source_timestamp)
                if state.notified is False and now - state.source_timestamp >= self.config.notification_grace_seconds:
                    unnotified[key] += 1
            for (workflow_id, workflow_name), total in sorted(self.failure_totals.items()):
                labels = {"workflow_id": workflow_id, "workflow_name": workflow_name}
                totals_samples.append(metric_line("n8n_queue_failure_events_total", total, labels))
            lines += _metric_block(
                "n8n_queue_failure_events_total",
                "counter",
                "Zero-node Bull queue failures first observed by workflow.",
                totals_samples or [metric_line("n8n_queue_failure_events_total", 0, {"workflow_id": "none", "workflow_name": "none"})],
            )
            lines += _metric_block(
                "n8n_queue_failure_last_seen_timestamp_seconds",
                "gauge",
                "Unix timestamp of the most recent retained queue failure.",
                [
                    metric_line(
                        "n8n_queue_failure_last_seen_timestamp_seconds",
                        timestamp,
                        {"workflow_id": workflow_id, "workflow_name": workflow_name},
                    )
                    for (workflow_id, workflow_name), timestamp in sorted(last_seen.items())
                ]
                or [metric_line("n8n_queue_failure_last_seen_timestamp_seconds", 0, {"workflow_id": "none", "workflow_name": "none"})],
            )
            lines += _metric_block(
                "n8n_queue_failure_unnotified",
                "gauge",
                "Retained queue failures older than the grace period without a successful configured notification path.",
                [
                    metric_line(
                        "n8n_queue_failure_unnotified",
                        count,
                        {"workflow_id": workflow_id, "workflow_name": workflow_name},
                    )
                    for (workflow_id, workflow_name), count in sorted(unnotified.items())
                ]
                or [metric_line("n8n_queue_failure_unnotified", 0, {"workflow_id": "none", "workflow_name": "none"})],
            )
            lines += _metric_block(
                "n8n_bull_queue_jobs",
                "gauge",
                "Bounded Bull queue key counts read from Redis.",
                [metric_line("n8n_bull_queue_jobs", count, {"queue": queue, "state": state}) for (queue, state), count in sorted(queue_counts.items())]
                or [metric_line("n8n_bull_queue_jobs", 0, {"queue": "none", "state": "none"})],
            )
            self.last_collection = now
            self.last_body = "\n".join(lines) + "\n"
            return self.last_body


class WatchdogHandler(BaseHTTPRequestHandler):
    collector: WatchdogCollector

    def do_GET(self) -> None:  # noqa: N802 - stdlib HTTP handler API
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")
            return
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = self.collector.collect().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", METRICS_CONTENT_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), format_string % args)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("N8N_WATCHDOG_BIND", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=_bounded_int(os.getenv("N8N_WATCHDOG_PORT", "9105"), 9105, 1, 65535))
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.getenv("N8N_WATCHDOG_LOG_LEVEL", "INFO"))
    collector = WatchdogCollector(WatchdogConfig.from_env())
    WatchdogHandler.collector = collector
    server = ThreadingHTTPServer((args.host, args.port), WatchdogHandler)
    LOGGER.info("n8n queue watchdog listening on %s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
