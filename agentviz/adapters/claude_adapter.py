import asyncio
import os
import sys
import socket
from contextlib import closing, contextmanager
import json

from .base import BaseAdapter

# Import OpenTelemetry protobuf definitions
try:
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
    from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2
    from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
    PROTOBUF_AVAILABLE = True
except ImportError:
    print("[OTEL] Warning: opentelemetry-proto not installed. Run: pip install opentelemetry-proto", file=sys.stderr)
    PROTOBUF_AVAILABLE = False

# Import FastAPI/uvicorn for OTLP receiver
try:
    from fastapi import FastAPI, Request
    import uvicorn
    FASTAPI_AVAILABLE = True
except ImportError:
    print("[OTEL] Warning: fastapi/uvicorn not installed. Run: pip install fastapi uvicorn", file=sys.stderr)
    FASTAPI_AVAILABLE = False


def find_free_port():
    """Find a free port for the OTLP receiver"""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(('', 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


class ClaudeAdapter(BaseAdapter):
    """
    Adapter for Claude Code with OpenTelemetry OTLP support
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.otel_queue = asyncio.Queue()
        self.otel_server_task = None
        self.otel_processor_task = None
        self.port = None
        self._seen_tool_calls = set()  # Dedup tool calls

    def _create_otel_app(self):
        app = FastAPI()

        @app.get("/")
        async def root():
            print(f"[OTEL] Health check received", file=sys.stderr)
            return {"status": "ok", "service": "agentviz-otlp-receiver"}

        @app.post("/v1/traces")
        async def receive_traces(request: Request):
            try:
                body = await request.body()
                print(f"[OTEL] *** RECEIVED TRACES ({len(body)} bytes) ***", file=sys.stderr)

                if PROTOBUF_AVAILABLE:
                    traces_req = trace_service_pb2.ExportTraceServiceRequest()
                    traces_req.ParseFromString(body)
                    num_spans = sum(
                        len(scope_span.spans)
                        for rs in traces_req.resource_spans
                        for scope_span in rs.scope_spans
                    )
                    print(f"[OTEL] Parsed {num_spans} trace spans", file=sys.stderr)
                    await self.otel_queue.put(('traces', traces_req))
                else:
                    print("[OTEL] Cannot parse - opentelemetry-proto not installed", file=sys.stderr)

                return {"status": "ok"}
            except Exception as e:
                print(f"[OTEL] Error parsing traces: {e}", file=sys.stderr)
                import traceback
                traceback.print_exc()
                return {"status": "error", "message": str(e)}

        @app.post("/v1/metrics")
        async def receive_metrics(request: Request):
            try:
                body = await request.body()
                print(f"[OTEL] *** RECEIVED METRICS ({len(body)} bytes) ***", file=sys.stderr)

                if PROTOBUF_AVAILABLE:
                    metrics_req = metrics_service_pb2.ExportMetricsServiceRequest()
                    metrics_req.ParseFromString(body)
                    metric_names = []
                    for rm in metrics_req.resource_metrics:
                        for sm in rm.scope_metrics:
                            for m in sm.metrics:
                                metric_names.append(m.name)
                    print(f"[OTEL] Parsed {len(metric_names)} metrics: {metric_names}", file=sys.stderr)
                    await self.otel_queue.put(('metrics', metrics_req))
                else:
                    print("[OTEL] Cannot parse - opentelemetry-proto not installed", file=sys.stderr)

                return {"status": "ok"}
            except Exception as e:
                print(f"[OTEL] Error parsing metrics: {e}", file=sys.stderr)
                import traceback
                traceback.print_exc()
                return {"status": "error", "message": str(e)}

        @app.post("/v1/logs")
        async def receive_logs(request: Request):
            try:
                body = await request.body()
                print(f"[OTEL] *** RECEIVED LOGS ({len(body)} bytes) ***", file=sys.stderr)

                if PROTOBUF_AVAILABLE:
                    logs_req = logs_service_pb2.ExportLogsServiceRequest()
                    logs_req.ParseFromString(body)
                    num_logs = sum(
                        len(scope_log.log_records)
                        for rl in logs_req.resource_logs
                        for scope_log in rl.scope_logs
                    )
                    print(f"[OTEL] Parsed {num_logs} log records", file=sys.stderr)
                    await self.otel_queue.put(('logs', logs_req))
                else:
                    print("[OTEL] Cannot parse - opentelemetry-proto not installed", file=sys.stderr)

                return {"status": "ok"}
            except Exception as e:
                print(f"[OTEL] Error parsing logs: {e}", file=sys.stderr)
                import traceback
                traceback.print_exc()
                return {"status": "error", "message": str(e)}

        # Catch-all for debugging
        @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
        async def catch_all(path: str, request: Request):
            body = await request.body()
            print(f"[OTEL] Caught request to /{path} ({request.method}, {len(body)} bytes)", file=sys.stderr)
            return {"status": "ok"}

        return app

    async def _process_otel_queue(self):
        print("[OTEL] Queue processor started, waiting for data...", file=sys.stderr)
        while True:
            data_type, data = await self.otel_queue.get()
            print(f"[OTEL] Processing {data_type} from queue", file=sys.stderr)
            try:
                if data_type == 'traces':
                    await self._process_traces(data)
                elif data_type == 'metrics':
                    await self._process_metrics(data)
                elif data_type == 'logs':
                    await self._process_logs(data)
            except Exception as e:
                print(f"[OTEL] Error processing {data_type}: {e}", file=sys.stderr)
                import traceback
                traceback.print_exc()
            self.otel_queue.task_done()

    async def _process_traces(self, traces_req):
        """Process trace spans from Claude Code"""
        for resource_span in traces_req.resource_spans:
            for scope_span in resource_span.scope_spans:
                for span in scope_span.spans:
                    attrs = {attr.key: self._get_attr_value(attr.value) for attr in span.attributes}
                    
                    span_name = span.name.lower()
                    print(f"[OTEL] Trace span: {span_name} | attrs: {list(attrs.keys())}", file=sys.stderr)
                    
                    # Tool execution spans (bash, edit, create, etc.)
                    if 'tool' in span_name or 'execute' in span_name:
                        tool_name = attrs.get('tool.name') or attrs.get('operation', 'unknown')
                        tool_input = attrs.get('tool.input', '')
                        
                        # Create dedup key
                        start_time = span.start_time_unix_nano
                        dedup_key = f"{tool_name}:{start_time}"
                        
                        if dedup_key not in self._seen_tool_calls:
                            self._seen_tool_calls.add(dedup_key)
                            
                            print(f"[OTEL] Tool call: {tool_name}", file=sys.stderr)
                            await self.emit_event("tool_call", {
                                "tool_name": tool_name,
                                "command": tool_input,
                                "type": "local",
                                "attributes": attrs
                            })

                    # API request spans (token usage, model info)
                    if 'api' in span_name or 'request' in span_name or 'claude' in span_name:
                        model = attrs.get('model', 'claude-sonnet-4-5')
                        input_tokens = int(attrs.get('input_tokens', 0))
                        output_tokens = int(attrs.get('output_tokens', 0))
                        
                        if input_tokens > 0 or output_tokens > 0:
                            print(f"[OTEL] API request: {model} ({input_tokens} in, {output_tokens} out)", file=sys.stderr)
                            await self.emit_event("token_usage", {
                                "model": model,
                                "input_tokens": input_tokens,
                                "output_tokens": output_tokens,
                                "total": input_tokens + output_tokens,
                                "attributes": attrs
                            })

                    # Session lifecycle
                    if 'session' in span_name:
                        if 'start' in span_name:
                            await self.emit_event("session_started", {"attributes": attrs})
                        elif 'end' in span_name or 'stop' in span_name:
                            await self.emit_event("session_ended", {"attributes": attrs})

    async def _process_metrics(self, metrics_req):
        """Process metrics from Claude Code"""
        for resource_metric in metrics_req.resource_metrics:
            for scope_metric in resource_metric.scope_metrics:
                for metric in scope_metric.metrics:
                    metric_name = metric.name.lower()
                    print(f"[OTEL] Metric: {metric_name}", file=sys.stderr)

                    # Get data points
                    data_points = []
                    if metric.HasField('sum'):
                        data_points = metric.sum.data_points
                    elif metric.HasField('histogram'):
                        data_points = metric.histogram.data_points
                    elif metric.HasField('gauge'):
                        data_points = metric.gauge.data_points

                    # Token usage metrics
                    if 'token' in metric_name:
                        for dp in data_points:
                            attrs = {attr.key: self._get_attr_value(attr.value) for attr in dp.attributes}
                            token_type = attrs.get('type', 'unknown')
                            model = attrs.get('model', 'claude')
                            
                            total = 0
                            if hasattr(dp, 'as_int'):
                                total = dp.as_int
                            elif hasattr(dp, 'value'):
                                total = int(dp.value)
                            
                            print(f"[OTEL] Token metric: {token_type} = {total}", file=sys.stderr)
                            await self.emit_event("token_usage", {
                                "type": token_type,
                                "model": model,
                                "total": total,
                                "input_tokens": total if token_type == 'input' else 0,
                                "output_tokens": total if token_type == 'output' else 0,
                                "attributes": attrs
                            })

                    # Cost metrics
                    if 'cost' in metric_name:
                        for dp in data_points:
                            attrs = {attr.key: self._get_attr_value(attr.value) for attr in dp.attributes}
                            cost = getattr(dp, 'as_double', 0.0) or float(getattr(dp, 'value', 0.0))
                            
                            print(f"[OTEL] Cost metric: ${cost}", file=sys.stderr)
                            await self.emit_event("cost_update", {
                                "cost": cost,
                                "attributes": attrs
                            })

                    # Active sessions
                    if 'session' in metric_name and 'active' in metric_name:
                        for dp in data_points:
                            count = getattr(dp, 'as_int', 0)
                            print(f"[OTEL] Active sessions: {count}", file=sys.stderr)

    async def _process_logs(self, logs_req):
        """Process log events from Claude Code.

        NOTE: File operations are handled by the file watcher in base.py
        which provides actual code content via git diff or file reading.
        OTEL logs don't include the actual code changes, so we only log them for debugging.
        """
        for resource_log in logs_req.resource_logs:
            for scope_log in resource_log.scope_logs:
                for log_record in scope_log.log_records:
                    # Extract log body for debugging
                    body = ""
                    if log_record.body.HasField('string_value'):
                        body = log_record.body.string_value

                    print(f"[OTEL] Log: {body[:100]}", file=sys.stderr)

    def _get_attr_value(self, value):
        """Extract value from OTLP AnyValue"""
        if value.HasField('string_value'):
            return value.string_value
        elif value.HasField('int_value'):
            return value.int_value
        elif value.HasField('double_value'):
            return value.double_value
        elif value.HasField('bool_value'):
            return value.bool_value
        return None

    async def _run_otel_server(self):
        """Run the FastAPI OTLP receiver server"""
        if not FASTAPI_AVAILABLE:
            print("[OTEL] FastAPI not available, cannot start OTLP receiver", file=sys.stderr)
            return

        app = self._create_otel_app()
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            access_log=False
        )
        server = uvicorn.Server(config)
        await server.serve()

    async def run(self):
        """Main run loop with OTLP support"""
        if not PROTOBUF_AVAILABLE or not FASTAPI_AVAILABLE:
            print("[OTEL] Missing dependencies, falling back to base adapter", file=sys.stderr)
            print("[OTEL] Install with: pip install opentelemetry-proto fastapi uvicorn", file=sys.stderr)
            await super().run()
            return

        self.port = find_free_port()
        print(f"[OTEL] Starting OTLP receiver on port {self.port}", file=sys.stderr)

        # Start the OTLP receiver server
        self.otel_server_task = asyncio.create_task(self._run_otel_server())

        # Start the queue processor
        self.otel_processor_task = asyncio.create_task(self._process_otel_queue())

        # Give server time to start
        await asyncio.sleep(0.5)
        print(f"[OTEL] OTLP receiver ready at http://127.0.0.1:{self.port}", file=sys.stderr)

        # Set environment variables for Claude Code
        self.env = os.environ.copy()
        self.env["CLAUDE_CODE_ENABLE_TELEMETRY"] = "1"
        self.env["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
        self.env["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://127.0.0.1:{self.port}"
        self.env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = f"http://127.0.0.1:{self.port}/v1/traces"
        self.env["OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"] = f"http://127.0.0.1:{self.port}/v1/metrics"
        self.env["OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"] = f"http://127.0.0.1:{self.port}/v1/logs"
        self.env["OTEL_METRICS_EXPORTER"] = "otlp"
        self.env["OTEL_LOGS_EXPORTER"] = "otlp"
        self.env["OTEL_LOG_USER_PROMPTS"] = "1"  # Enable user prompt logging

        print(f"[OTEL] Environment configured for Claude Code telemetry", file=sys.stderr)

        try:
            await super().run()
        finally:
            # Cancel the server and processor tasks
            if self.otel_server_task:
                self.otel_server_task.cancel()
                try:
                    await self.otel_server_task
                except asyncio.CancelledError:
                    pass
                print("[OTEL] OTLP receiver stopped", file=sys.stderr)

            if self.otel_processor_task:
                self.otel_processor_task.cancel()
                try:
                    await self.otel_processor_task
                except asyncio.CancelledError:
                    pass
                print("[OTEL] Queue processor stopped", file=sys.stderr)