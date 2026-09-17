import importlib.util
import unittest
from time import time

from monitoring.n8n_queue_watchdog import (
    WatchdogCollector,
    WatchdogConfig,
    _redis_encode,
    _redis_read_response,
    handler_references_execution,
    is_queue_failure,
)


MODULE_NAME = "monitoring.n8n_queue_watchdog"


class N8nQueueWatchdogTests(unittest.TestCase):
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

            def list_executions(self, workflow_id="", limit=100):
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
