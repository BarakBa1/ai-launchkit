import importlib.util
import pathlib
import unittest
from time import time
from urllib.error import URLError
from urllib.request import Request

from monitoring.n8n_queue_watchdog import (
    ExecutionPage,
    N8nApiClient,
    WatchdogCollector,
    WatchdogConfig,
    _SameOriginRedirectHandler,
    _successful,
    _read_limited,
    _redis_encode,
    _redis_read_response,
    handler_references_execution,
    is_queue_failure,
)


MODULE_NAME = "monitoring.n8n_queue_watchdog"


class N8nQueueWatchdogTests(unittest.TestCase):
    ROOT = pathlib.Path(__file__).resolve().parents[1]

    def test_watchdog_module_is_available(self):
        try:
            module_spec = importlib.util.find_spec(MODULE_NAME)
        except ModuleNotFoundError:
            module_spec = None
        self.assertIsNotNone(
            module_spec,
            "the queue watchdog collector has not been implemented",
        )

    def test_queue_failure_requires_bull_signature_and_zero_node_boundary(self):
        self.assertTrue(
            is_queue_failure(
                {
                    "id": "921111",
                    "status": "error",
                    "startedAt": None,
                    "error": {"message": "Error: timeout exceeded when trying to connect"},
                    "data": {"resultData": {"runData": {}}},
                }
            )
        )
        self.assertFalse(
            is_queue_failure(
                {
                    "id": "in-workflow",
                    "status": "error",
                    "startedAt": "2026-09-17T03:30:00Z",
                    "error": {"message": "Error: timeout exceeded when trying to connect"},
                    "data": {"resultData": {"runData": {"start": []}}},
                }
            )
        )
        self.assertTrue(
            is_queue_failure(
                {
                    "id": "bull-versioned",
                    "status": "error",
                    "startedAt": None,
                    "error": {"message": "bull@4.16.4 queue failure"},
                }
            )
        )
        self.assertFalse(
            is_queue_failure(
                {
                    "id": "substring",
                    "status": "error",
                    "startedAt": None,
                    "error": {"message": "Bull queue infrastructure error"},
                }
            )
        )
        self.assertFalse(
            is_queue_failure(
                {
                    "id": "ordinary",
                    "status": "error",
                    "startedAt": None,
                    "error": {"message": "database system is not yet accepting connections"},
                }
            )
        )

    def test_handler_reference_searches_nested_execution_payload(self):
        self.assertTrue(
            handler_references_execution(
                {
                    "data": {
                        "resultData": {
                            "runData": {
                                "Error Trigger": [
                                    {"data": {"main": [[{"json": {"execution": {"id": "921111"}}}]]}}
                                ]
                            }
                        }
                    }
                },
                "921111",
            )
        )
        self.assertFalse(handler_references_execution({"id": "other"}, "921111"))

    def test_public_api_execution_shape_can_prove_success_without_status_field(self):
        self.assertTrue(
            _successful(
                {"finished": True, "data": {"resultData": {"runData": {"main": []}}}}
            )
        )
        self.assertFalse(
            _successful(
                {
                    "finished": True,
                    "data": {
                        "resultData": {
                            "runData": {},
                            "error": {"message": "handler failed"},
                        }
                    },
                }
            )
        )
        self.assertFalse(
            _successful({"finished": True, "data": {"resultData": {"error": "handler failed"}}})
        )

    def test_redis_command_is_framed_without_mutating_redis(self):
        self.assertEqual(
            _redis_encode(["PING"]),
            b"*1\r\n$4\r\nPING\r\n",
        )

    def test_redis_response_parser_handles_arrays_and_bulk_values(self):
        class FakeSocket:
            def __init__(self, payload):
                self.payload = payload

            def recv(self, size):
                chunk, self.payload = self.payload[:size], self.payload[size:]
                return chunk

        response = _redis_read_response(
            FakeSocket(b"*2\r\n$4\r\nPONG\r\n:1\r\n")
        )
        self.assertEqual(response, ["PONG", 1])

    def test_missing_api_configuration_is_an_unhealthy_collection(self):
        for config in (
            WatchdogConfig(),
            WatchdogConfig(n8n_api_url="https://n8n.example/api/v1"),
            WatchdogConfig(n8n_api_key="secret"),
        ):
            with self.subTest(config=config):
                metrics = WatchdogCollector(config).collect(force=True)
                self.assertIn("n8n_watchdog_collection_success 0", metrics)
                self.assertIn('n8n_watchdog_component_configured{component="n8n_api"} 0', metrics)

    def test_notification_query_failure_keeps_unnotified_alert_and_exposes_correlation_failure(self):
        now = time()
        source = {
            "id": "921111",
            "workflowId": "jarvis-id",
            "workflowName": "jarvis",
            "status": "error",
            "startedAt": None,
            "stoppedAt": now - 120,
            "error": {"message": "timeout exceeded when trying to connect"},
        }

        class FakeApi:
            configured = True

            def list_executions(self, workflow_id="", limit=100, **_kwargs):
                if workflow_id == "error-id":
                    raise Exception("handler unavailable")
                return [source]

        class FakeRedis:
            def collect(self):
                return True, {}

        config = WatchdogConfig(
            n8n_api_url="https://n8n.example/api/v1",
            n8n_api_key="test-only",
            error_workflow_id="error-id",
            notification_grace_seconds=1,
            scrape_cache_seconds=0,
        )
        collector = WatchdogCollector(config, api_client=FakeApi())
        collector.redis = FakeRedis()

        metrics = collector.collect(force=True)

        self.assertIn('n8n_queue_failure_unnotified{workflow_id="jarvis-id",workflow_name="jarvis"} 1', metrics)
        self.assertIn("n8n_watchdog_notification_correlation_up 0", metrics)
        self.assertIn("n8n_watchdog_notification_correlation_failures_total 1", metrics)

    def test_api_requires_https_for_api_key_transport(self):
        self.assertFalse(N8nApiClient("http://n8n.example/api/v1", "secret", 1).configured)
        self.assertTrue(N8nApiClient("https://n8n.example/api/v1", "secret", 1).configured)

    def test_redirect_handler_rejects_cross_origin_redirect(self):
        handler = _SameOriginRedirectHandler()
        request = Request("https://n8n.example/api/v1/executions")
        with self.assertRaises(URLError):
            handler.redirect_request(request, None, 302, "found", {}, "https://evil.example/collect")
        with self.assertRaises(URLError):
            handler.redirect_request(request, None, 302, "found", {}, "http://n8n.example/collect")

    def test_api_response_body_is_bounded(self):
        class FakeResponse:
            headers = {}

            def read(self, size=-1):
                return b"12345" if size != 4 else b"1234"

        with self.assertRaises(ValueError):
            _read_limited(FakeResponse(), 4)

    def test_api_listing_omits_execution_payload_and_detail_read_is_explicit(self):
        class FakeResponse:
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                return b'{"data": []}'

        class FakeOpener:
            def __init__(self):
                self.requests = []

            def open(self, request, timeout):
                self.requests.append((request, timeout))
                return FakeResponse()

        opener = FakeOpener()
        client = N8nApiClient("https://n8n.example/api/v1", "secret", 1, opener=opener)
        client.list_recent_executions(limit=10, status="error")
        self.assertIn("includeData=false", opener.requests[-1][0].full_url)
        self.assertIn("status=error", opener.requests[-1][0].full_url)
        client.get_execution("921111")
        self.assertIn("includeData=true", opener.requests[-1][0].full_url)

    def test_api_paginates_to_time_boundary_and_marks_page_cap_unknown(self):
        now = time()

        class FakeResponse:
            status = 200
            headers = {}

            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                import json

                return json.dumps(self.payload).encode("utf-8")

        class FakeOpener:
            def __init__(self):
                self.requests = []
                self.payloads = [
                    {"data": [{"id": "new", "startedAt": now}], "nextCursor": "opaque"},
                    {"data": [{"id": "old", "startedAt": now - 120}], "nextCursor": None},
                ]

            def open(self, request, timeout):
                self.requests.append((request, timeout))
                return FakeResponse(self.payloads.pop(0))

        opener = FakeOpener()
        client = N8nApiClient("https://n8n.example/api/v1", "secret", 1, opener=opener)
        page = client.list_recent_executions(limit=1, since=now - 60)

        self.assertEqual([row["id"] for row in page.rows], ["new", "old"])
        self.assertEqual(page.next_cursor, "")
        self.assertIn("cursor=opaque", opener.requests[1][0].full_url)

        capped_opener = FakeOpener()
        capped_client = N8nApiClient("https://n8n.example/api/v1", "secret", 1, opener=capped_opener)
        capped = capped_client.list_recent_executions(limit=1, max_pages=1, since=now - 60)
        self.assertEqual(capped.next_cursor, "unknown")

    def test_incomplete_notification_pagination_is_exposed_as_unknown(self):
        now = time()
        source = {
            "id": "921111",
            "workflowId": "jarvis-id",
            "workflowName": "jarvis",
            "status": "error",
            "startedAt": None,
            "stoppedAt": now - 120,
            "error": {"message": "timeout exceeded when trying to connect"},
        }

        class FakeApi:
            configured = True

            def list_recent_executions(self, workflow_id="", **_kwargs):
                if workflow_id == "error-id":
                    return ExecutionPage([], "unknown")
                return ExecutionPage([source], "")

        class FakeRedis:
            def collect(self):
                return True, {}

        config = WatchdogConfig(
            n8n_api_url="https://n8n.example/api/v1",
            n8n_api_key="test-only",
            error_workflow_id="error-id",
            notification_grace_seconds=1,
            scrape_cache_seconds=0,
        )
        collector = WatchdogCollector(config, api_client=FakeApi())
        collector.redis = FakeRedis()

        metrics = collector.collect(force=True)

        self.assertIn('n8n_queue_failure_unnotified{workflow_id="jarvis-id",workflow_name="jarvis"} 1', metrics)
        self.assertIn("n8n_watchdog_notification_correlation_up 0", metrics)
        self.assertIn("n8n_watchdog_notification_correlation_failures_total 1", metrics)

    def test_unstarted_error_metadata_is_confirmed_with_bounded_detail_read(self):
        now = time()
        metadata = {
            "id": "921041",
            "workflowId": "main-id",
            "workflowName": "main",
            "startedAt": None,
            "stoppedAt": now - 120,
        }
        detail = {
            **metadata,
            "data": {
                "resultData": {
                    "runData": {},
                    "error": {"message": "timeout exceeded when trying to connect"},
                }
            },
        }

        class FakeApi:
            configured = True

            def __init__(self):
                self.detail_ids = []

            def list_recent_executions(self, workflow_id="", **kwargs):
                self.assert_source_options(kwargs)
                return ExecutionPage([metadata], "")

            def assert_source_options(self, kwargs):
                self.source_options = kwargs
                if kwargs.get("include_data") or kwargs.get("status") != "error":
                    raise AssertionError("source listing must be metadata-only and error-filtered")

            def get_execution(self, execution_id):
                self.detail_ids.append(execution_id)
                return detail

        class FakeRedis:
            def collect(self):
                return True, {}

        collector = WatchdogCollector(
            WatchdogConfig(
                n8n_api_url="https://n8n.example/api/v1",
                n8n_api_key="test-only",
                notification_grace_seconds=1,
                scrape_cache_seconds=0,
            ),
            api_client=FakeApi(),
        )
        collector.redis = FakeRedis()

        metrics = collector.collect(force=True)

        self.assertIn('n8n_queue_failure_events_total{workflow_id="main-id",workflow_name="main"} 1', metrics)
        self.assertEqual(collector.api.detail_ids, ["921041"])

    def test_prometheus_target_has_an_atomic_compose_watchdog_service(self):
        compose = (self.ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        prometheus = (self.ROOT / "prometheus" / "prometheus.yml").read_text(encoding="utf-8")
        rules = (self.ROOT / "prometheus" / "rules" / "n8n_queue_watchdog.yml").read_text(encoding="utf-8")
        self.assertIn("  n8n-queue-watchdog:", compose)
        self.assertIn('profiles: ["monitoring"]', compose)
        self.assertIn("./monitoring:/app/monitoring:ro", compose)
        self.assertIn('job_name: "n8n-queue-watchdog"', prometheus)
        self.assertIn('targets: ["n8n-queue-watchdog:9105"]', prometheus)
        self.assertIn("N8nQueueWatchdogCollectionUnhealthy", rules)
        self.assertIn("N8nQueueWatchdogNotificationCorrelationDown", rules)

    def test_collector_deduplicates_source_execution_and_reports_missing_notification(self):
        now = time()
        source = {
            "id": "921111",
            "workflowId": "jarvis-id",
            "workflowName": "jarvis",
            "status": "error",
            "startedAt": None,
            "stoppedAt": now - 120,
            "error": {"message": "timeout exceeded when trying to connect"},
            "data": {"resultData": {"runData": {}}},
        }

        class FakeApi:
            configured = True

            def __init__(self):
                self.calls = []

            def list_executions(self, workflow_id="", limit=100, include_data=False, status=""):
                self.calls.append(workflow_id)
                if workflow_id == "error-id":
                    return []
                return [source]

        class FakeRedis:
            def collect(self):
                return True, {}

        config = WatchdogConfig(
            n8n_api_url="http://n8n/api/v1",
            n8n_api_key="test-only",
            error_workflow_id="error-id",
            notification_grace_seconds=1,
            scrape_cache_seconds=0,
        )
        fake_api = FakeApi()
        collector = WatchdogCollector(config, api_client=fake_api)
        collector.redis = FakeRedis()

        first = collector.collect(force=True)
        second = collector.collect(force=True)

        self.assertIn('n8n_queue_failure_events_total{workflow_id="jarvis-id",workflow_name="jarvis"} 1', first)
        self.assertIn('n8n_queue_failure_unnotified{workflow_id="jarvis-id",workflow_name="jarvis"} 1', first)
        self.assertIn('n8n_queue_failure_events_total{workflow_id="jarvis-id",workflow_name="jarvis"} 1', second)
        self.assertEqual(fake_api.calls.count("error-id"), 2)


if __name__ == "__main__":
    unittest.main()
