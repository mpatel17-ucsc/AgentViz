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


class GeminiAdapter(BaseAdapter):
    """
    Adapter for Gemini CLI with OpenTelemetry protobuf support + project-local config management
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.otel_queue = asyncio.Queue()
        self.otel_server_task = None
        self.otel_processor_task = None
        self.port = None
        self.otel_proc = None
        self._seen_file_operations = set()  # Dedup file operations
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

        # Catch-all for any other paths (must be last!)
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
            except Exception as e:
                print(f"[OTEL] Error processing {data_type}: {e}", file=sys.stderr)
                import traceback
                traceback.print_exc()
            self.otel_queue.task_done()

    async def _process_traces(self, traces_req):
        for resource_span in traces_req.resource_spans:
            for scope_span in resource_span.scope_spans:
                for span in scope_span.spans:
                    attrs = {attr.key: self._get_attr_value(attr.value) for attr in span.attributes}
                    
                    span_name = span.name.lower()
                    print(f"[OTEL] Trace span: {span_name}", file=sys.stderr)
                    
                    # Tool calls (local, Google search, browse, etc.)
                    if 'tool' in span_name or 'gen_ai' in span_name or 'tool_use' in span_name:
                        tool_name = attrs.get('gen_ai.operation.name') or attrs.get('tool.name', 'unknown')
                        tool_input = attrs.get('tool.input', '') or attrs.get('gen_ai.tool.input', '')
                        tool_type = "local"
                        if "search" in tool_name.lower() or "google" in tool_name.lower():
                            tool_type = "google_search"
                        elif "browse" in tool_name.lower():
                            tool_type = "browse_page"
                        
                        await self.emit_event("tool_call", {
                            "tool_name": tool_name,
                            "command": tool_input,
                            "type": tool_type,
                            "attributes": attrs
                        })

                    # Code generation / content generation
                    if 'gen_ai.generate_content' in span_name or 'code_generation' in span_name:
                        await self.emit_event("code_generation", {
                            "model": attrs.get('gen_ai.model', 'gemini'),
                            "input_tokens": int(attrs.get('gen_ai.input.tokens', 0)),
                            "output_tokens": int(attrs.get('gen_ai.output.tokens', 0)),
                            "attributes": attrs
                        })

    async def _process_metrics(self, metrics_req):
        for resource_metric in metrics_req.resource_metrics:
            for scope_metric in resource_metric.scope_metrics:
                for metric in scope_metric.metrics:
                    metric_name = metric.name.lower()
                    print(f"[OTEL] Metric: {metric_name}", file=sys.stderr)

                    # Handle both sum and histogram metrics
                    data_points = []
                    if metric.HasField('sum'):
                        data_points = metric.sum.data_points
                    elif metric.HasField('histogram'):
                        data_points = metric.histogram.data_points
                    elif metric.HasField('gauge'):
                        data_points = metric.gauge.data_points

                    # File operations: gemini_cli.file.operation.count
                    # Gemini CLI attributes: operation, lines, extension, programming_language
                    if "file.operation" in metric_name or "file_operation" in metric_name:
                        for dp in data_points:
                            attrs = {attr.key: self._get_attr_value(attr.value) for attr in dp.attributes}
                            operation_type = attrs.get('operation', 'unknown')
                            lines = int(attrs.get('lines', 0))
                            extension = attrs.get('extension', '')
                            language = attrs.get('programming_language', '')

                            # Create dedup key based on operation, lines, extension, and timestamp
                            start_time = getattr(dp, 'start_time_unix_nano', 0)
                            dedup_key = f"{operation_type}:{extension}:{lines}:{start_time}"

                            if dedup_key not in self._seen_file_operations:
                                self._seen_file_operations.add(dedup_key)

                                # Determine lines added/removed based on operation type
                                lines_added = lines if operation_type in ('create', 'update') else 0
                                lines_removed = 0  # Gemini doesn't report removed lines separately

                                print(f"[OTEL] File operation: {operation_type} {extension} ({lines} lines)", file=sys.stderr)
                                await self.emit_event("file_operation", {
                                    "file_path": f"<file>{extension}" if extension else "unknown",
                                    "extension": extension,
                                    "programming_language": language,
                                    "lines_added": lines_added,
                                    "lines_removed": lines_removed,
                                    "operation_type": operation_type,
                                    "total_changes": lines,
                                    "attributes": attrs
                                })

                    # Lines changed: gemini_cli.lines.changed
                    # Attributes: type (added/removed), function_name
                    if "lines.changed" in metric_name or "lines_changed" in metric_name:
                        for dp in data_points:
                            attrs = {attr.key: self._get_attr_value(attr.value) for attr in dp.attributes}
                            change_type = attrs.get('type', 'unknown')
                            function_name = attrs.get('function_name', 'unknown')
                            count = getattr(dp, 'as_int', None) or int(getattr(dp, 'value', 0))

                            print(f"[OTEL] Lines changed: {change_type} {count} lines ({function_name})", file=sys.stderr)
                            await self.emit_event("lines_changed", {
                                "type": change_type,
                                "count": count,
                                "function_name": function_name,
                                "attributes": attrs
                            })

                    # Tool call latency: gemini_cli.tool.call.latency (histogram)
                    # Attributes: function_name
                    if "tool.call" in metric_name or "tool_call" in metric_name:
                        for dp in data_points:
                            attrs = {attr.key: self._get_attr_value(attr.value) for attr in dp.attributes}
                            function_name = attrs.get('function_name', 'unknown')
                            start_time = getattr(dp, 'start_time_unix_nano', 0)

                            # Dedup based on function name and start time
                            dedup_key = f"{function_name}:{start_time}"

                            if dedup_key not in self._seen_tool_calls:
                                self._seen_tool_calls.add(dedup_key)

                                # Extract count from histogram if available
                                count = 1
                                if hasattr(dp, 'value') and hasattr(dp.value, 'count'):
                                    count = dp.value.count
                                elif hasattr(dp, 'count'):
                                    count = dp.count

                                print(f"[OTEL] Tool call: {function_name} (count: {count})", file=sys.stderr)
                                await self.emit_event("tool_call", {
                                    "tool_name": function_name,
                                    "command": function_name,
                                    "type": "local",
                                    "count": count,
                                    "attributes": attrs
                                })

                    # Token usage: gen_ai.client.token.usage or gemini_cli.token
                    # Attributes: gen_ai.token.type, gen_ai.request.model, type, model
                    if "token" in metric_name:
                        for dp in data_points:
                            attrs = {attr.key: self._get_attr_value(attr.value) for attr in dp.attributes}

                            # Handle gen_ai.client.token.usage format
                            token_type = attrs.get('gen_ai.token.type') or attrs.get('type', 'unknown')
                            model = attrs.get('gen_ai.request.model') or attrs.get('model', 'gemini')

                            # Get the value (could be histogram or sum)
                            total = 0
                            if hasattr(dp, 'value'):
                                if hasattr(dp.value, 'sum'):
                                    total = int(dp.value.sum)
                                else:
                                    total = int(dp.value)
                            elif hasattr(dp, 'as_int'):
                                total = dp.as_int

                            print(f"[OTEL] Token usage: {token_type} {total} ({model})", file=sys.stderr)
                            await self.emit_event("token_usage", {
                                "type": token_type,
                                "model": model,
                                "total": total,
                                "input_tokens": total if token_type == 'input' else 0,
                                "output_tokens": total if token_type == 'output' else 0,
                                "attributes": attrs
                            })

    def _get_attr_value(self, value):
        if value.HasField('string_value'):
            return value.string_value
        elif value.HasField('int_value'):
            return value.int_value
        elif value.HasField('double_value'):
            return value.double_value
        elif value.HasField('bool_value'):
            return value.bool_value
        return None

    @contextmanager
    def _manage_project_telemetry_config(self):
        settings_dir = os.path.join(self.working_dir, ".gemini")
        settings_path = os.path.join(settings_dir, "settings.json")

        original_content = None
        file_existed = os.path.exists(settings_path)
        dir_existed = os.path.exists(settings_dir)

        try:
            os.makedirs(settings_dir, exist_ok=True)

            if file_existed:
                with open(settings_path, 'r') as f:
                    original_content = f.read()

            # Point telemetry to our OTLP receiver
            # Note: Gemini CLI only accepts "grpc" or "http" for otlpProtocol
            telemetry_config = {
                "telemetry": {
                    "enabled": True,
                    "target": "local",
                    "otlpEndpoint": f"http://127.0.0.1:{self.port}",
                    "otlpProtocol": "http"
                }
            }
            print(f"[OTEL] Configuring Gemini telemetry to send to http://127.0.0.1:{self.port}", file=sys.stderr)

            if not file_existed:
                config_to_write = telemetry_config
            else:
                try:
                    existing = json.loads(original_content)
                    existing["telemetry"] = telemetry_config["telemetry"]
                    config_to_write = existing
                except json.JSONDecodeError:
                    config_to_write = telemetry_config

            with open(settings_path, 'w') as f:
                json.dump(config_to_write, f, indent=2)

            print(f"[AgentViz] Set project telemetry config: {settings_path}", file=sys.stderr)
            yield

        except Exception as e:
            print(f"[AgentViz] Failed to manage project config: {e}", file=sys.stderr)
            yield

        finally:
            try:
                if not file_existed:
                    if os.path.exists(settings_path):
                        os.remove(settings_path)
                        print(f"[AgentViz] Removed temporary .gemini/settings.json", file=sys.stderr)
                    if not dir_existed and os.path.exists(settings_dir) and not os.listdir(settings_dir):
                        os.rmdir(settings_dir)
                        print(f"[AgentViz] Removed empty .gemini directory", file=sys.stderr)
                else:
                    if original_content is not None:
                        with open(settings_path, 'w') as f:
                            f.write(original_content)
                        print(f"[AgentViz] Restored original .gemini/settings.json", file=sys.stderr)
            except Exception as cleanup_error:
                print(f"[AgentViz] Cleanup failed: {cleanup_error}", file=sys.stderr)

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
        if not PROTOBUF_AVAILABLE or not FASTAPI_AVAILABLE:
            print("[OTEL] Falling back to base (missing protobuf or fastapi)", file=sys.stderr)
            await super().run()
            return

        self.port = find_free_port()
        print(f"[OTEL] Starting OTLP receiver on HTTP port {self.port}", file=sys.stderr)

        # Start the OTLP receiver server
        self.otel_server_task = asyncio.create_task(self._run_otel_server())

        # Start the queue processor
        self.otel_processor_task = asyncio.create_task(self._process_otel_queue())

        # Give the server a moment to start
        await asyncio.sleep(0.5)
        print(f"[OTEL] OTLP receiver ready at http://127.0.0.1:{self.port}", file=sys.stderr)

        # Set environment variables for Gemini CLI
        # Note: Gemini CLI only accepts "grpc" or "http" for protocol
        self.env = os.environ.copy()
        self.env["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http"
        self.env["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://127.0.0.1:{self.port}"
        self.env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = f"http://127.0.0.1:{self.port}/v1/traces"
        self.env["OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"] = f"http://127.0.0.1:{self.port}/v1/metrics"

        with self._manage_project_telemetry_config():
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