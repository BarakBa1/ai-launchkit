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
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


LOGGER = logging.getLogger("n8n_queue_watchdog")
QUEUE_FAILURE_RE = re.compile(
    r"(?:timeout\s+exceeded\s+when\s+trying\s+to\s+connect|\bqueue\.onfailed\b|\bbull@\d+(?:\.\d+){1,2}\b)",
    re.IGNORECASE,
)
QUEUE_KEY_RE = re.compile(r"^bull:(?P<queue>.+):(?P<state>wait|active|failed|delayed|stalled)$")
METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
REDIS_MAX_LINE_BYTES = 1_048_576
REDIS_MAX_BULK_BYTES = 1_048_576
REDIS_MAX_ARRAY_ITEMS = 10_000
REDIS_MAX_NESTING = 32
REDIS_MAX_RESPONSE_BYTES = 5_000_000


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
    max_response_bytes: int = 5_000_000
    max_pages: int = 10
    source_lookback_seconds: float = 3600.0
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
            execution_limit=_bounded_int(os.getenv("N8N_WATCHDOG_EXECUTION_LIMIT", "100"), 100, 1, 250),
            workflow_ids=_csv_set(os.getenv("N8N_WATCHDOG_WORKFLOW_IDS", "")),
            max_workflow_labels=_bounded_int(
                os.getenv("N8N_WATCHDOG_MAX_WORKFLOW_LABELS", "50"), 50, 1, 500
            ),
            max_response_bytes=_bounded_int(
                os.getenv("N8N_WATCHDOG_MAX_RESPONSE_BYTES", "5000000"), 5_000_000, 100_000, 50_000_000
            ),
            max_pages=_bounded_int(os.getenv("N8N_WATCHDOG_MAX_PAGES", "10"), 10, 1, 100),
            source_lookback_seconds=_bounded_float(
                os.getenv("N8N_WATCHDOG_SOURCE_LOOKBACK_SECONDS", "3600"), 3600.0, 60.0, 86400.0
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
    data_error = _as_mapping(_execution_data(execution).get("resultData")).get("error")
    if isinstance(data_error, Mapping):
        for key in ("message", "stack", "name"):
            if data_error.get(key):
                pieces.append(str(data_error[key]))
    elif data_error:
        pieces.append(str(data_error))
    return " ".join(pieces)


def _result_error(execution: Mapping[str, Any]) -> Any:
    return _as_mapping(_execution_data(execution).get("resultData")).get("error")


def has_queue_failure_signature(execution: Mapping[str, Any]) -> bool:
    status = str(execution.get("status", "")).lower()
    if status and status != "error":
        return False
    if not status and not execution.get("error") and not _result_error(execution):
        return False
    return bool(QUEUE_FAILURE_RE.search(execution_error_text(execution)))


def is_queue_failure(execution: Mapping[str, Any]) -> bool:
    """Identify a zero-node Bull queue failure without guessing on other errors."""

    if not has_queue_failure_signature(execution):
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
    status = str(execution.get("status", "")).lower()
    if status:
        return status == "success" and execution.get("finished", True) is not False
    if execution.get("finished") is not True:
        return False
    return not execution.get("error") and not _result_error(execution)


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


class ResponseTooLarge(ValueError):
    """Raised before an unbounded remote response can enter process memory."""


def _read_limited(response: Any, max_bytes: int) -> bytes:
    content_length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
    if content_length:
        try:
            parsed_length = int(content_length)
        except (TypeError, ValueError):
            parsed_length = None
        if parsed_length is not None and parsed_length > max_bytes:
            raise ResponseTooLarge("remote response exceeds configured byte limit")
    body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ResponseTooLarge("remote response exceeds configured byte limit")
    return body


def _url_origin(url: str) -> Tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        default_port = 443 if parsed.scheme.lower() == "https" else 80
        return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or default_port
    except ValueError as exc:
        raise URLError("invalid n8n API redirect URL") from exc


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Mapping[str, Any],
        newurl: str,
    ) -> Request:
        target = urljoin(req.full_url, newurl)
        if urlsplit(target).scheme.lower() != "https" or _url_origin(req.full_url) != _url_origin(target):
            raise URLError("refusing cross-origin or non-HTTPS n8n API redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, target)
        if redirected is None:
            raise URLError("n8n API redirect was not converted to a request")
        return redirected


def _secure_api_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme.lower() == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password


@dataclass(frozen=True)
class ExecutionPage:
    rows: List[Mapping[str, Any]]
    # The n8n 1.* executions endpoint returns an opaque ``nextCursor`` but
    # accepts the last execution ID as ``lastId`` on the next request. Keep
    # only the request-side continuation value so the watchdog cannot send a
    # cursor token to an endpoint that does not accept it.
    next_last_id: str = ""


class N8nApiClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        opener: Any = None,
        max_response_bytes: int = 5_000_000,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.opener = opener or build_opener(_SameOriginRedirectHandler())
        self.max_response_bytes = max_response_bytes

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and _secure_api_url(self.base_url))

    def _get(self, resource: str, params: Mapping[str, Any]) -> Any:
        if not self.configured:
            raise RuntimeError("n8n API is not configured")
        query = urlencode({key: str(value) for key, value in params.items()})
        url = urljoin(self.base_url + "/", resource.lstrip("/"))
        if query:
            url += "?" + query
        request = Request(url, headers={"X-N8N-API-KEY": self.api_key, "Accept": "application/json"})
        with self.opener.open(request, timeout=self.timeout_seconds) as response:
            return json.loads(_read_limited(response, self.max_response_bytes).decode("utf-8"))

    def list_execution_page(
        self,
        workflow_id: str = "",
        limit: int = 100,
        include_data: bool = False,
        last_id: str = "",
        status: str = "",
    ) -> ExecutionPage:
        limit = max(1, min(250, int(limit)))
        params: Dict[str, Any] = {"limit": limit, "includeData": "true" if include_data else "false"}
        if workflow_id:
            params["workflowId"] = workflow_id
        if last_id:
            params["lastId"] = last_id
        if status:
            params["status"] = status
        payload = self._get("executions", params)
        if isinstance(payload, list):
            rows = payload
        else:
            rows = _as_mapping(payload).get("data")
            if not isinstance(rows, list):
                raise ValueError("n8n execution response did not contain a data list")
        normalized_rows = [row for row in rows if isinstance(row, Mapping)]
        next_cursor = "" if isinstance(payload, list) else _as_mapping(payload).get("nextCursor")
        if next_cursor in (None, ""):
            next_last_id = ""
        elif not normalized_rows or not normalized_rows[-1].get("id"):
            raise ValueError("n8n execution page advertised nextCursor without a row ID")
        else:
            # n8n 1.123.27 encodes lastId/limit/count in nextCursor, while
            # the handler reads lastId. The last row is the authoritative ID
            # boundary and avoids depending on the token's private encoding.
            next_last_id = str(normalized_rows[-1]["id"])
        return ExecutionPage(normalized_rows, next_last_id)

    def list_recent_executions(
        self,
        workflow_id: str = "",
        limit: int = 100,
        include_data: bool = False,
        max_pages: int = 10,
        since: Optional[float] = None,
        status: str = "",
    ) -> ExecutionPage:
        rows: List[Mapping[str, Any]] = []
        last_id = ""
        complete = True
        for _page_number in range(max_pages):
            page = self.list_execution_page(workflow_id, limit, include_data, last_id, status)
            rows.extend(page.rows)
            if since is not None and page.rows:
                oldest = min(
                    _timestamp(row.get("stoppedAt") or row.get("startedAt") or row.get("createdAt"), time.time())
                    for row in page.rows
                )
                if oldest < since:
                    break
            if not page.next_last_id:
                if len(page.rows) >= limit:
                    complete = False
                break
            last_id = page.next_last_id
        else:
            complete = False
        return ExecutionPage(rows, "" if complete else "unknown")

    def list_executions(
        self,
        workflow_id: str = "",
        limit: int = 100,
        include_data: bool = False,
        status: str = "",
    ) -> List[Mapping[str, Any]]:
        return self.list_execution_page(workflow_id, limit, include_data, status=status).rows

    def get_execution(self, execution_id: str) -> Mapping[str, Any]:
        payload = self._get("executions/" + quote(str(execution_id), safe=""), {"includeData": "true"})
        return payload if isinstance(payload, Mapping) else {}

    def probe(self) -> bool:
        try:
            self.list_executions(limit=1, include_data=False)
        except Exception:  # noqa: BLE001 - a probe must fail closed for every client-side read error
            return False
        return True


def _redis_encode(parts: Sequence[str]) -> bytes:
    encoded = [part.encode("utf-8") for part in parts]
    return b"*" + str(len(encoded)).encode("ascii") + b"\r\n" + b"".join(
        b"$" + str(len(part)).encode("ascii") + b"\r\n" + part + b"\r\n" for part in encoded
    )


@dataclass
class _RedisResponseBudget:
    remaining: int

    def consume(self, amount: int) -> None:
        if amount < 0 or amount > self.remaining:
            raise RuntimeError("Redis response exceeded total byte limit")
        self.remaining -= amount


def _redis_read_line(
    sock: socket.socket,
    budget: _RedisResponseBudget,
    max_line_bytes: int,
) -> bytes:
    data = bytearray()
    while True:
        byte = sock.recv(1)
        if not byte:
            raise RuntimeError("Redis closed the connection")
        if len(data) + len(byte) > max_line_bytes:
            raise RuntimeError("Redis response line exceeded limit")
        budget.consume(len(byte))
        data.extend(byte)
        if data.endswith(b"\r\n"):
            return bytes(data[:-2])


def _redis_read_exact(sock: socket.socket, size: int, budget: _RedisResponseBudget) -> bytes:
    if size < 0:
        raise RuntimeError("Redis response requested a negative read")
    # Check the shared budget before allocating or reading the declared size.
    budget.consume(size)
    data = bytearray(size)
    offset = 0
    while offset < size:
        chunk = sock.recv(size - offset)
        if not chunk:
            raise RuntimeError("Redis returned a truncated bulk value")
        if len(chunk) > size - offset:
            raise RuntimeError("Redis returned more bytes than requested")
        data[offset : offset + len(chunk)] = chunk
        offset += len(chunk)
    return bytes(data)


def _redis_int(line: bytes) -> int:
    try:
        return int(line)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Redis returned an invalid integer") from exc


def _redis_read_response(
    sock: socket.socket,
    *,
    max_bulk_bytes: int = REDIS_MAX_BULK_BYTES,
    max_array_items: int = REDIS_MAX_ARRAY_ITEMS,
    max_nesting: int = REDIS_MAX_NESTING,
    max_response_bytes: int = REDIS_MAX_RESPONSE_BYTES,
    _budget: Optional[_RedisResponseBudget] = None,
    _depth: int = 0,
) -> Any:
    limits = (max_bulk_bytes, max_array_items, max_nesting, max_response_bytes)
    if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in limits):
        raise ValueError("Redis response limits must be positive integers")
    budget = _budget or _RedisResponseBudget(max_response_bytes)
    prefix = _redis_read_exact(sock, 1, budget)
    if prefix == b"+":
        return _redis_read_line(sock, budget, REDIS_MAX_LINE_BYTES).decode("utf-8", "replace")
    if prefix == b"-":
        raise RuntimeError(_redis_read_line(sock, budget, REDIS_MAX_LINE_BYTES).decode("utf-8", "replace"))
    if prefix == b":":
        return _redis_int(_redis_read_line(sock, budget, REDIS_MAX_LINE_BYTES))
    if prefix == b"$":
        size = _redis_int(_redis_read_line(sock, budget, REDIS_MAX_LINE_BYTES))
        if size < 0:
            if size != -1:
                raise RuntimeError("Redis returned an invalid bulk length")
            return None
        if size > max_bulk_bytes:
            raise RuntimeError("Redis bulk value exceeded limit")
        data = _redis_read_exact(sock, size + 2, budget)
        if data[-2:] != b"\r\n":
            raise RuntimeError("Redis bulk value was not CRLF terminated")
        return data[:size].decode("utf-8", "replace")
    if prefix == b"*":
        count = _redis_int(_redis_read_line(sock, budget, REDIS_MAX_LINE_BYTES))
        if count < 0:
            if count != -1:
                raise RuntimeError("Redis returned an invalid array length")
            return None
        if count > max_array_items:
            raise RuntimeError("Redis array exceeded item limit")
        if _depth >= max_nesting:
            raise RuntimeError("Redis response nesting exceeded limit")
        return [
            _redis_read_response(
                sock,
                max_bulk_bytes=max_bulk_bytes,
                max_array_items=max_array_items,
                max_nesting=max_nesting,
                max_response_bytes=max_response_bytes,
                _budget=budget,
                _depth=_depth + 1,
            )
            for _ in range(count)
        ]
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


@dataclass(frozen=True)
class NotificationRows:
    handler: List[Mapping[str, Any]]
    slack: List[Mapping[str, Any]]
    complete: bool


class WatchdogCollector:
    def __init__(self, config: WatchdogConfig, api_client: Optional[N8nApiClient] = None) -> None:
        self.config = config
        self.api = api_client or N8nApiClient(
            config.n8n_api_url,
            config.n8n_api_key,
            config.probe_timeout_seconds,
            max_response_bytes=config.max_response_bytes,
        )
        if api_client is not None and hasattr(self.api, "max_response_bytes"):
            self.api.max_response_bytes = config.max_response_bytes
        self.redis = RedisProbe(config)
        self.failures: "OrderedDict[str, FailureState]" = OrderedDict()
        self.failure_totals: Dict[Tuple[str, str], int] = defaultdict(int)
        self.execution_detail_failures_total = 0
        self.execution_detail_failures_last_cycle = 0
        self.execution_coverage_complete = False
        self.notification_correlation_attempted = False
        self.notification_correlation_up = False
        self.notification_correlation_failures_total = 0
        self.last_body = ""
        self.last_collection = 0.0
        self.lock = threading.Lock()

    def _list_recent_executions(
        self,
        workflow_id: str = "",
        include_data: bool = False,
        since: Optional[float] = None,
        status: str = "",
    ) -> ExecutionPage:
        list_recent = getattr(self.api, "list_recent_executions", None)
        if callable(list_recent):
            return list_recent(
                workflow_id=workflow_id,
                limit=self.config.execution_limit,
                include_data=include_data,
                max_pages=self.config.max_pages,
                since=since,
                status=status,
            )
        rows = self.api.list_executions(
            workflow_id,
            self.config.execution_limit,
            include_data=include_data,
            status=status,
        )
        return ExecutionPage(rows, "")

    def _source_executions(self, now: float) -> Tuple[bool, List[Mapping[str, Any]], bool]:
        self.execution_detail_failures_last_cycle = 0
        if not self.api.configured:
            return False, [], False
        try:
            page = self._list_recent_executions(
                include_data=False,
                since=now - self.config.source_lookback_seconds,
                status="error",
            )
        except Exception as exc:  # noqa: BLE001 - a failed read must become an observable unhealthy cycle
            LOGGER.warning("n8n execution collection failed (%s)", type(exc).__name__)
            return False, [], False
        rows: List[Mapping[str, Any]] = []
        if self.config.workflow_ids:
            page_rows = [row for row in page.rows if _workflow_id(row) in self.config.workflow_ids]
        else:
            page_rows = page.rows
        for row in page_rows:
            if is_queue_failure(row):
                rows.append(row)
                continue
            # Public API v1 metadata omits execution.data and may omit the
            # error text. Never discard a status=error row merely because it
            # has startedAt: the detail payload may be the only place where a
            # zero-node Bull timeout is visible. A row is safe to skip only
            # when inline metadata already proves it ran at least one node
            # and includes an error payload we can classify.
            node_count = executed_node_count(row)
            if (
                row.get("startedAt") not in (None, "")
                and node_count is not None
                and node_count > 0
                and bool(execution_error_text(row))
            ):
                continue
            execution_id = str(row.get("id") or "")
            if not execution_id:
                self.execution_detail_failures_last_cycle += 1
                self.execution_detail_failures_total += 1
                continue
            try:
                detail = self.api.get_execution(execution_id)
            except Exception as exc:  # noqa: BLE001 - preserve an observable coverage failure
                LOGGER.warning("n8n execution detail read failed (%s)", type(exc).__name__)
                self.execution_detail_failures_last_cycle += 1
                self.execution_detail_failures_total += 1
                continue
            expanded = dict(row)
            expanded.update(detail)
            if is_queue_failure(expanded):
                rows.append(expanded)
        complete = not page.next_last_id and self.execution_detail_failures_last_cycle == 0
        return True, rows, complete

    def _notification_rows(
        self,
        now: float,
    ) -> Optional[NotificationRows]:
        """Read each notification workflow once per scrape, not once per failure."""

        self.notification_correlation_attempted = True
        if not (self.config.error_workflow_id and self.config.slack_workflow_id):
            # Handler and Slack workflow IDs are a pair. Treat either missing
            # value as an explicit unknown/unhealthy dependency; callers must
            # retain the core unnotified failure signal.
            self.notification_correlation_up = False
            return None
        try:
            handler_page = self._list_recent_executions(
                self.config.error_workflow_id,
                include_data=True,
                since=now - self.config.source_lookback_seconds,
            )
            slack_page = self._list_recent_executions(
                self.config.slack_workflow_id,
                include_data=True,
                since=now - self.config.source_lookback_seconds,
            )
            complete = not handler_page.next_last_id and not slack_page.next_last_id
            self.notification_correlation_up = complete
            if not complete:
                self.notification_correlation_failures_total += 1
            return NotificationRows(handler_page.rows, slack_page.rows, complete)
        except Exception as exc:  # noqa: BLE001 - preserve the core unnotified signal on dependency failure
            LOGGER.warning("n8n notification correlation failed (%s)", type(exc).__name__)
            self.notification_correlation_up = False
            self.notification_correlation_failures_total += 1
            return None

    def _notification_state(
        self,
        source_id: str,
        notification_rows: Optional[NotificationRows],
    ) -> Optional[bool]:
        if not (self.config.error_workflow_id and self.config.slack_workflow_id):
            return False
        if notification_rows is None or not notification_rows.complete:
            return False
        handler_rows, slack_rows = notification_rows.handler, notification_rows.slack
        handler_ok = any(_successful(row) and handler_references_execution(row, source_id) for row in handler_rows)
        if not handler_ok:
            return False
        if not self.config.slack_workflow_id:
            return True
        slack_ok = any(_successful(row) and handler_references_execution(row, source_id) for row in slack_rows)
        return slack_ok

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

            self.notification_correlation_attempted = False
            self.notification_correlation_up = False
            if not (self.config.error_workflow_id and self.config.slack_workflow_id):
                self.notification_correlation_attempted = True
                self.notification_correlation_failures_total += 1
            api_up, executions, self.execution_coverage_complete = self._source_executions(now)
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
            notification_rows = self._notification_rows(now) if self.failures else None
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
                "Whether the most recent n8n execution collection was complete and healthy.",
                [metric_line("n8n_watchdog_collection_success", int(api_up and self.execution_coverage_complete))],
            )
            lines += _metric_block(
                "n8n_watchdog_last_collection_timestamp_seconds",
                "gauge",
                "Unix timestamp of the most recent watchdog collection cycle.",
                [metric_line("n8n_watchdog_last_collection_timestamp_seconds", now)],
            )
            lines += _metric_block(
                "n8n_watchdog_execution_coverage_complete",
                "gauge",
                "Whether the bounded n8n execution listing reached its time boundary without pagination ambiguity.",
                [metric_line("n8n_watchdog_execution_coverage_complete", int(self.execution_coverage_complete))],
            )
            lines += _metric_block(
                "n8n_watchdog_execution_detail_failures_total",
                "counter",
                "Execution detail requests that failed while classifying a queue candidate.",
                [metric_line("n8n_watchdog_execution_detail_failures_total", self.execution_detail_failures_total)],
            )
            lines += _metric_block(
                "n8n_watchdog_notification_correlation_configured",
                "gauge",
                "Whether both the n8n error-handler and Slack workflow IDs are configured for correlation.",
                [
                    metric_line(
                        "n8n_watchdog_notification_correlation_configured",
                        int(bool(self.config.error_workflow_id and self.config.slack_workflow_id)),
                    )
                ],
            )
            lines += _metric_block(
                "n8n_watchdog_notification_correlation_attempted",
                "gauge",
                "Whether notification correlation was attempted during the most recent collection.",
                [metric_line("n8n_watchdog_notification_correlation_attempted", int(self.notification_correlation_attempted))],
            )
            lines += _metric_block(
                "n8n_watchdog_notification_correlation_up",
                "gauge",
                "Whether all required notification correlation reads completed without pagination ambiguity.",
                [metric_line("n8n_watchdog_notification_correlation_up", int(self.notification_correlation_up))],
            )
            lines += _metric_block(
                "n8n_watchdog_notification_correlation_failures_total",
                "counter",
                "Notification correlation reads that failed or were incomplete.",
                [metric_line("n8n_watchdog_notification_correlation_failures_total", self.notification_correlation_failures_total)],
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
