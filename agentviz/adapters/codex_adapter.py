import asyncio
import os
import sys
import socket
from contextlib import closing, contextmanager
import toml
from pathlib import Path

from .base import BaseAdapter

# Import OpenTelemetry protobuf definitions
try:
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


class CodexAdapter(BaseAdapter):
    """
    Adapter for Codex CLI with HYBRID monitoring:
    - OTLP for API requests, tool approvals, session events
    - Base adapter for file changes, subprocess monitoring, thinking detection
    
    This gives complete coverage of all Codex activities.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.otel_queue = asyncio.Queue()
        self.otel_server_task = None
        self.otel_processor_task = None
        self.port = None
        self._seen_otel_events = set()  # Dedup OTLP events
        self.codex_config_path = Path.home() / ".codex" / "config.toml"
        self.codex_config_backup_path = None

    def _create_otel_app(self):
        app = FastAPI()

        @app.get("/")
        async def root():
            print(f"[OTEL] Health check received", file=sys.stderr)
            return {"status": "ok", "service": "agentviz-otlp-receiver"}

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
                if data_type == 'logs':
                    await self._process_logs(data)
            except Exception as e:
                print(f"[OTEL] Error processing {data_type}: {e}", file=sys.stderr)
                import traceback
                traceback.print_exc()
            self.otel_queue.task_done()

    async def _process_logs(self, logs_req):
        """
        Process log events from Codex CLI OTLP
        
        Focus on events that BaseAdapter can't capture:
        - API requests (tokens, model, cost)
        - Tool approval decisions
        - Session lifecycle
        - Structured tool metadata
        """
        for resource_log in logs_req.resource_logs:
            for scope_log in resource_log.scope_logs:
                for log_record in scope_log.log_records:
                    attrs = {attr.key: self._get_attr_value(attr.value) for attr in log_record.attributes}
                    
                    # Extract log body
                    body = ""
                    if log_record.body.HasField('string_value'):
                        body = log_record.body.string_value
                    
                    # Get event name (if structured)
                    event_name = attrs.get('event.name', '').lower()
                    
                    timestamp = log_record.time_unix_nano
                    
                    # API Request events (token usage, model info)
                    if 'api.request' in event_name or attrs.get('conversation.id'):
                        model = attrs.get('model', 'gpt-4')
                        input_tokens = int(attrs.get('token.input', 0) or 0)
                        output_tokens = int(attrs.get('token.output', 0) or 0)
                        duration_ms = float(attrs.get('duration_ms', 0) or 0)
                        
                        if input_tokens > 0 or output_tokens > 0:
                            dedup_key = f"token:{timestamp}"
                            if dedup_key not in self._seen_otel_events:
                                self._seen_otel_events.add(dedup_key)
                                
                                print(f"[OTEL] API request: {model} ({input_tokens} in, {output_tokens} out, {duration_ms}ms)", file=sys.stderr)
                                await self.emit_event("token_usage", {
                                    "model": model,
                                    "input_tokens": input_tokens,
                                    "output_tokens": output_tokens,
                                    "total": input_tokens + output_tokens,
                                    "duration_ms": duration_ms,
                                    "conversation_id": attrs.get('conversation.id'),
                                    "attributes": attrs
                                })

                    # Tool approval events (unique to Codex OTLP)
                    if 'tool.approval' in event_name or 'approval' in event_name:
                        tool_name = attrs.get('tool.name', 'unknown')
                        approved = attrs.get('approved', False)
                        
                        print(f"[OTEL] Tool approval: {tool_name} -> {'approved' if approved else 'denied'}", file=sys.stderr)
                        await self.emit_event("tool_approval", {
                            "tool_name": tool_name,
                            "approved": approved,
                            "attributes": attrs
                        })

                    # Streaming events (SSE chunks)
                    if 'sse' in event_name or 'stream' in event_name:
                        chunk_type = attrs.get('chunk.type', 'unknown')
                        # These are frequent, so we don't emit individual events
                        # But we could track streaming progress if needed
                        pass

                    # Session lifecycle
                    if 'session' in event_name or 'conversation' in event_name:
                        if 'start' in event_name or 'create' in event_name:
                            await self.emit_event("session_started", {
                                "conversation_id": attrs.get('conversation.id'),
                                "attributes": attrs
                            })
                        elif 'end' in event_name or 'stop' in event_name:
                            await self.emit_event("session_ended", {
                                "conversation_id": attrs.get('conversation.id'),
                                "attributes": attrs
                            })

                    # User prompts (if log_user_prompt enabled)
                    if 'prompt' in event_name or attrs.get('user.prompt'):
                        prompt_text = attrs.get('user.prompt', '') or body
                        dedup_key = f"prompt:{timestamp}"
                        if dedup_key not in self._seen_otel_events:
                            self._seen_otel_events.add(dedup_key)
                            
                            await self.emit_event("user_prompt", {
                                "prompt": prompt_text[:300],  # Truncate
                                "attributes": attrs
                            })

                    # Tool execution metadata from OTLP (complements subprocess monitoring)
                    if 'tool.execute' in event_name or 'tool.result' in event_name:
                        tool_name = attrs.get('tool.name', 'unknown')
                        exit_code = attrs.get('exit_code')
                        
                        # Only emit if we have meaningful metadata (not captured by subprocess monitor)
                        if exit_code is not None:
                            await self.emit_event("tool_result_metadata", {
                                "tool_name": tool_name,
                                "exit_code": exit_code,
                                "attributes": attrs
                            })

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

    @contextmanager
    def _manage_codex_config(self):
        """
        Temporarily modify Codex config.toml to enable OTLP telemetry
        pointing to our local receiver
        """
        original_config = None
        config_existed = self.codex_config_path.exists()
        
        try:
            # Backup original config
            if config_existed:
                with open(self.codex_config_path, 'r') as f:
                    original_config = f.read()
                    
                # Create backup
                self.codex_config_backup_path = self.codex_config_path.with_suffix('.toml.backup')
                with open(self.codex_config_backup_path, 'w') as f:
                    f.write(original_config)
            
            # Load or create config
            if config_existed:
                try:
                    config = toml.load(self.codex_config_path)
                except Exception as e:
                    print(f"[OTEL] Warning: Could not parse existing config: {e}", file=sys.stderr)
                    config = {}
            else:
                config = {}
                # Create config directory if needed
                self.codex_config_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Add OTLP configuration
            config['otel'] = {
                'environment': 'dev',
                'exporter': 'otlp-http',
                'log_user_prompt': False,  # Respect privacy by default
                'exporter': {
                    'otlp-http': {
                        'endpoint': f'http://127.0.0.1:{self.port}/v1/logs',
                        'protocol': 'binary'
                    }
                }
            }
            
            # Write modified config
            with open(self.codex_config_path, 'w') as f:
                toml.dump(config, f)
            
            print(f"[OTEL] Configured Codex telemetry to http://127.0.0.1:{self.port}/v1/logs", file=sys.stderr)
            yield
            
        except Exception as e:
            print(f"[OTEL] Failed to manage Codex config: {e}", file=sys.stderr)
            yield
            
        finally:
            # Restore original config
            try:
                if config_existed and original_config:
                    with open(self.codex_config_path, 'w') as f:
                        f.write(original_config)
                    print(f"[OTEL] Restored original Codex config", file=sys.stderr)
                    
                    # Remove backup
                    if self.codex_config_backup_path and self.codex_config_backup_path.exists():
                        self.codex_config_backup_path.unlink()
                elif not config_existed and self.codex_config_path.exists():
                    # Remove temp config if it didn't exist before
                    self.codex_config_path.unlink()
                    print(f"[OTEL] Removed temporary Codex config", file=sys.stderr)
            except Exception as cleanup_error:
                print(f"[OTEL] Config cleanup failed: {cleanup_error}", file=sys.stderr)

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
        """
        Main run loop with HYBRID monitoring:
        
        OTLP provides:
        - API requests (tokens, cost, model)
        - Tool approvals (unique to Codex)
        - Session lifecycle events
        
        BaseAdapter provides (via super().run()):
        - Real-time file monitoring (file_created, file_modified, file_deleted)
        - Subprocess tracking (tool_call events via psutil)
        - Thinking detection (thinking_start, thinking_end)
        - PTY management for interactive sessions
        """
        
        # If OTLP dependencies available, use hybrid approach
        if PROTOBUF_AVAILABLE and FASTAPI_AVAILABLE:
            self.port = find_free_port()
            print(f"[OTEL] Starting HYBRID mode: OTLP + BaseAdapter", file=sys.stderr)
            print(f"[OTEL] OTLP receiver on port {self.port}", file=sys.stderr)

            # Start the OTLP receiver server
            self.otel_server_task = asyncio.create_task(self._run_otel_server())

            # Start the queue processor
            self.otel_processor_task = asyncio.create_task(self._process_otel_queue())

            # Give server time to start
            await asyncio.sleep(0.5)
            print(f"[OTEL] OTLP receiver ready at http://127.0.0.1:{self.port}", file=sys.stderr)

            # Modify Codex config to enable telemetry
            with self._manage_codex_config():
                try:
                    # Run BaseAdapter which handles:
                    # - File monitoring (watchdog)
                    # - Subprocess monitoring (psutil)
                    # - Thinking detection
                    # - PTY management
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
        else:
            # Fallback to base adapter only (no OTLP)
            print("[OTEL] OTLP dependencies missing, using BaseAdapter only", file=sys.stderr)
            print("[OTEL] Install with: pip install opentelemetry-proto fastapi uvicorn toml", file=sys.stderr)
            print("[OTEL] File monitoring and subprocess tracking still active", file=sys.stderr)
            await super().run()