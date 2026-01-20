import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import json

from agentviz.adapters.gemini_adapter import GeminiAdapter, TracesData, MetricsData

class TestGeminiAdapterWithOTLP(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        """Set up a mock monitor and default adapter arguments."""
        self.mock_monitor = MagicMock()
        self.adapter_args = {
            "monitor": self.mock_monitor,
            "agent_id": "test_agent",
            "agent_type": "gemini",
            "working_dir": ".",
            "command": ["echo", "hello"]
        }

    async def test_adapter_initialization(self):
        """Tests that the GeminiAdapter initializes correctly."""
        adapter = GeminiAdapter(**self.adapter_args)
        self.assertIsInstance(adapter, GeminiAdapter)
        self.assertIsInstance(adapter.otel_queue, asyncio.Queue)

    @patch('agentviz.adapters.base.BaseAdapter.run')
    async def test_run_starts_and_stops_servers(self, mock_super_run):
        """Tests that the run method starts servers and sets the env var."""
        adapter = GeminiAdapter(**self.adapter_args)
        
        async def run_and_cancel():
            run_task = asyncio.create_task(adapter.run())
            await asyncio.sleep(0.1)
            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass

        await run_and_cancel()

        self.assertIsNotNone(adapter.port)
        self.assertIn("OTEL_EXPORTER_OTLP_ENDPOINT", adapter.env)
        self.assertTrue(f"127.0.0.1:{adapter.port}" in adapter.env["OTEL_EXPORTER_OTLP_ENDPOINT"])
        
        self.assertIsNotNone(adapter.otel_server_task)
        self.assertTrue(adapter.otel_server_task.done())

        self.assertIsNotNone(adapter.otel_processor_task)
        self.assertTrue(adapter.otel_processor_task.done())

    async def test_otel_trace_processing(self):
        """Tests the flow for OTLP trace data (e.g., tool call)."""
        adapter = GeminiAdapter(**self.adapter_args)
        adapter.emit_event = AsyncMock()
        
        processor_task = asyncio.create_task(adapter._process_otel_queue())

        tool_call_trace_data = {
            "resourceSpans": [{"scopeSpans": [{"spans": [{
                "name": "gen_ai.client.tool.call",
                "attributes": [
                    {"key": "gen_ai.client.tool.name", "value": {"stringValue": "run_shell_command"}},
                    {"key": "gen_ai.client.tool.input", "value": {"stringValue": "ls -l"}}
                ]
            }]}]}]
        }
        
        traces = TracesData.model_validate(tool_call_trace_data)
        await adapter.otel_queue.put(traces)
        await adapter.otel_queue.join()

        adapter.emit_event.assert_called_once_with(
            "tool_call",
            {
                'gen_ai.client.tool.name': 'run_shell_command',
                'tool_name': 'run_shell_command',
                'gen_ai.client.tool.input': 'ls -l',
                'command': 'ls -l'
            }
        )
        
        processor_task.cancel()
        try:
            await processor_task
        except asyncio.CancelledError:
            pass
            
    async def test_otel_metric_processing(self):
        """Tests the flow for OTLP metrics data (e.g., file operation)."""
        adapter = GeminiAdapter(**self.adapter_args)
        adapter.emit_event = AsyncMock()
        
        processor_task = asyncio.create_task(adapter._process_otel_queue())

        file_op_metric_data = {
            "resourceMetrics": [{"scopeMetrics": [{"metrics": [{
                "name": "gemini_cli.file.operation.count",
                "sum": {"dataPoints": [{
                    "attributes": [
                        {"key": "file.path", "value": {"stringValue": "src/main.py"}},
                        {"key": "file.lines.added", "value": {"intValue": "10"}},
                        {"key": "file.lines.removed", "value": {"intValue": "2"}}
                    ],
                    "asInt": "1"
                }]}
            }]}]}]
        }
        
        metrics = MetricsData.model_validate(file_op_metric_data)
        await adapter.otel_queue.put(metrics)
        await adapter.otel_queue.join()

        adapter.emit_event.assert_called_once_with(
            "file_operation",
            {
                'file_path': 'src/main.py',
                'lines_added': 10,
                'lines_removed': 2
            }
        )
        
        processor_task.cancel()
        try:
            await processor_task
        except asyncio.CancelledError:
            pass


if __name__ == '__main__':
    unittest.main()
