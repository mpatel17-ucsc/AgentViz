import asyncio
import os
import json
import sys
import tempfile
import shutil
from pathlib import Path

from .base import BaseAdapter, AGENTVIZ_DEBUG, debug_print, register_agent_activity, is_path_within_dir
from ..utils import find_free_port

# Import OpenTelemetry protobuf definitions
try:
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
    from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2
    PROTOBUF_AVAILABLE = True
except ImportError:
    debug_print("[OTEL] Warning: opentelemetry-proto not installed")
    PROTOBUF_AVAILABLE = False

try:
    from fastapi import FastAPI, Request
    import uvicorn
    FASTAPI_AVAILABLE = True
except ImportError:
    debug_print("[OTEL] Warning: fastapi/uvicorn not installed")
    FASTAPI_AVAILABLE = False



class GeminiAdapter(BaseAdapter):
    """
    Adapter for Gemini CLI with:
    1. Official Hooks API for state tracking
    2. OpenTelemetry for detailed telemetry

    State Detection via Hooks (per https://geminicli.com/docs/hooks/reference/):
    - SessionStart -> STARTING (session begins)
    - BeforeAgent -> THINKING (after user submission, before planning)
    - BeforeTool -> WORKING (tool about to execute)
    - AfterTool -> WORKING (tool completed)
    - AfterAgent -> READY (agent turn complete)
    - Notification -> WAITING_FOR_INPUT (permission needed)
    - SessionEnd -> STOPPED

    IMPORTANT: BeforeAgent fires "after user submission but before planning"
    This IS the "thinking" state - the agent is processing but hasn't started
    using tools yet.

    Hook Lifecycle:
    1. SessionStart - session begins
    2. BeforeAgent - user submits prompt, agent starts thinking
    3. BeforeTool - agent decides to use a tool
    4. AfterTool - tool execution completes
    5. AfterAgent - agent finishes responding
    6. Notification - system events (permissions, etc.)
    7. SessionEnd - session terminates

    Gemini CLI hooks are configured in .gemini/settings.json
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.otel_queue = asyncio.Queue()
        self.otel_server_task = None
        self.otel_processor_task = None
        self.state_monitor_task = None
        self.port = None
        self._seen_file_operations = set()
        self._seen_tool_calls = set()
        self._disable_file_watcher = True
        self._enable_subprocess_snapshot = False

        # IMPORTANT: Use hooks for state tracking, not screen-based detection
        # This prevents flickering when agent is thinking but not producing output
        self._use_hooks_for_state = True
        # Disable idle timeout fallback for Gemini (use hooks only)
        self._enable_idle_timeout_fallback = False

        # State tracking
        self._state_dir = None
        self._state_file = None
        self._gemini_settings_backup = None
        self._gemini_dir_existed = False
        self._current_state = "idle"

    def _get_state_hook_script(self):
        """
        Generate Python hook script for reliable JSON output.

        IMPORTANT: Gemini CLI hooks require "Silence is Mandatory" -
        scripts must output ONLY JSON to stdout.
        See: https://geminicli.com/docs/hooks/reference/

        Python scripts provide reliable, clean JSON output without
        shell initialization noise.
        """
        return f'''#!/usr/bin/env python3
# Gemini CLI state hook for agentviz
# This script is called by Gemini CLI hooks to track state changes
import sys
import json
import time

def main():
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"

    # Read stdin for hook input JSON (contains tool info, prompt, etc.)
    # Gemini passes structured JSON input to hooks via stdin
    stdin_data = None
    hook_input = None
    try:
        if not sys.stdin.isatty():
            stdin_data = sys.stdin.read().strip()
            if stdin_data:
                hook_input = json.loads(stdin_data)
    except (json.JSONDecodeError, IOError):
        pass

    # Build event record
    data = {{
        "event": event,
        "timestamp": int(time.time() * 1000),
        "agent_id": "{self.agent_id}"
    }}

    # Extract useful fields from hook input
    if hook_input and isinstance(hook_input, dict):
        # Tool name for BeforeTool/AfterTool
        if "tool_name" in hook_input:
            data["tool_name"] = hook_input["tool_name"]
        # Notification type for Notification events
        if "notification_type" in hook_input:
            data["notification_type"] = hook_input["notification_type"]
        # Prompt for BeforeAgent
        if "prompt" in hook_input:
            data["prompt_preview"] = str(hook_input["prompt"])[:100]
        # Session source for SessionStart
        if "source" in hook_input:
            data["source"] = hook_input["source"]
        # Store full input for debugging (truncated)
        data["input"] = hook_input

    # Append to state file
    try:
        with open("{self._state_file}", "a") as f:
            f.write(json.dumps(data) + "\\n")
            f.flush()
    except IOError as e:
        sys.stderr.write(f"Failed to write state: {{e}}\\n")
        sys.exit(1)

    # Exit 0 = success, allow the operation to proceed
    # Output nothing to stdout (Silence is Mandatory)
    sys.exit(0)

if __name__ == "__main__":
    main()
'''

    def _setup_hooks_config(self):
        """
        Set up Gemini CLI hooks configuration for state tracking.
        Creates/modifies .gemini/settings.json in the working directory.
        """
        # Create unique state directory for this agent instance
        self._state_dir = tempfile.mkdtemp(prefix=f"agentviz-gemini-{self.agent_id}-")
        self._state_file = os.path.join(self._state_dir, "state.jsonl")

        # Create state file
        Path(self._state_file).touch()

        # Create hook script (Python for reliable JSON output)
        hook_script_path = os.path.join(self._state_dir, "state-hook.py")
        with open(hook_script_path, 'w') as f:
            f.write(self._get_state_hook_script())
        os.chmod(hook_script_path, 0o755)

        debug_print(f"[HOOKS] Created Gemini hook script: {hook_script_path}")

        # Build hooks configuration for Gemini CLI
        # See: https://geminicli.com/docs/hooks/
        # See: https://geminicli.com/docs/hooks/reference/
        # See: https://geminicli.com/docs/get-started/configuration/
        #
        # Hook Lifecycle Order:
        # 1. SessionStart - session begins
        # 2. BeforeAgent - user submits prompt (THINKING state)
        # 3. BeforeTool - tool about to execute (WORKING state)
        # 4. AfterTool - tool execution completes
        # 5. AfterAgent - agent finishes responding (READY state)
        # 6. Notification - system events like tool permissions (WAITING_FOR_INPUT)
        # 7. SessionEnd - session terminates (STOPPED state)
        #
        # IMPORTANT: Use python3 explicitly to ensure the Python script runs correctly
        hooks_config = {
            # Primary toggle for hooks system
            # See: https://geminicli.com/docs/hooks/reference/
            "hooksConfig": {
                "enabled": True,
                "showIndicators": True  # Show visual indicators when hooks run (helps debugging)
            },
            "hooks": {
                "SessionStart": [{
                    "hooks": [{
                        "type": "command",
                        "command": f"python3 {hook_script_path} session_start",
                        "timeout": 5000
                    }]
                }],
                "BeforeAgent": [{
                    "hooks": [{
                        "type": "command",
                        "command": f"python3 {hook_script_path} before_agent",
                        "timeout": 5000
                    }]
                }],
                "AfterAgent": [{
                    "hooks": [{
                        "type": "command",
                        "command": f"python3 {hook_script_path} after_agent",
                        "timeout": 5000
                    }]
                }],
                "BeforeTool": [{
                    "matcher": "*",
                    "hooks": [{
                        "type": "command",
                        "command": f"python3 {hook_script_path} before_tool",
                        "timeout": 5000
                    }]
                }],
                "AfterTool": [{
                    "matcher": "*",
                    "hooks": [{
                        "type": "command",
                        "command": f"python3 {hook_script_path} after_tool",
                        "timeout": 5000
                    }]
                }],
                "Notification": [{
                    "hooks": [{
                        "type": "command",
                        "command": f"python3 {hook_script_path} notification",
                        "timeout": 5000
                    }]
                }],
                "SessionEnd": [{
                    "hooks": [{
                        "type": "command",
                        "command": f"python3 {hook_script_path} session_end",
                        "timeout": 5000
                    }]
                }]
            }
        }

        # Add telemetry config if we have OTEL
        if self.port:
            hooks_config["telemetry"] = {
                "enabled": True,
                "target": "local",
                "otlpEndpoint": f"http://127.0.0.1:{self.port}",
                "otlpProtocol": "http"
            }

        # Write to project .gemini/settings.json
        gemini_dir = os.path.join(self.working_dir, ".gemini")
        settings_path = os.path.join(gemini_dir, "settings.json")

        self._gemini_dir_existed = os.path.exists(gemini_dir)
        os.makedirs(gemini_dir, exist_ok=True)

        # Backup existing settings
        if os.path.exists(settings_path):
            with open(settings_path, 'r') as f:
                self._gemini_settings_backup = f.read()
            # Merge with existing settings
            try:
                existing = json.loads(self._gemini_settings_backup)
                # Always enable hooks
                existing["hooksConfig"] = hooks_config["hooksConfig"]
                existing["hooks"] = hooks_config["hooks"]
                if "telemetry" in hooks_config:
                    existing["telemetry"] = hooks_config["telemetry"]
                hooks_config = existing
            except json.JSONDecodeError:
                pass

        with open(settings_path, 'w') as f:
            json.dump(hooks_config, f, indent=2)

        debug_print(f"[HOOKS] Configured Gemini CLI hooks in {settings_path}")
        debug_print(f"[HOOKS] State file: {self._state_file}")

    def _cleanup_hooks_config(self):
        """Clean up hooks configuration"""
        settings_path = os.path.join(self.working_dir, ".gemini", "settings.json")
        gemini_dir = os.path.join(self.working_dir, ".gemini")

        try:
            if self._gemini_settings_backup is not None:
                with open(settings_path, 'w') as f:
                    f.write(self._gemini_settings_backup)
                debug_print("[HOOKS] Restored original .gemini/settings.json")
            elif os.path.exists(settings_path):
                os.remove(settings_path)
                debug_print("[HOOKS] Removed temporary .gemini/settings.json")
                # Remove .gemini dir if we created it and it's empty
                if not self._gemini_dir_existed and os.path.exists(gemini_dir) and not os.listdir(gemini_dir):
                    os.rmdir(gemini_dir)
        except Exception as e:
            debug_print(f"[HOOKS] Cleanup warning: {e}")

        # Clean up state directory
        if self._state_dir and os.path.exists(self._state_dir):
            try:
                shutil.rmtree(self._state_dir)
                debug_print(f"[HOOKS] Removed state directory: {self._state_dir}")
            except Exception as e:
                debug_print(f"[HOOKS] Could not remove state dir: {e}")

    async def _monitor_state_file(self):
        """Monitor the state file for hook events and emit state changes"""
        debug_print("[HOOKS] State file monitor started")

        last_position = 0

        while not self.shutdown_event.is_set():
            # CRITICAL: Check if process has exited before processing any events
            # This prevents race conditions where hook events arrive after Ctrl+C
            if self._process_exited:
                debug_print("[HOOKS] Process exited, stopping state monitor")
                break

            try:
                if os.path.exists(self._state_file):
                    with open(self._state_file, 'r') as f:
                        f.seek(last_position)
                        new_lines = f.readlines()
                        last_position = f.tell()

                    for line in new_lines:
                        # Check again before each event to minimize race window
                        if self._process_exited:
                            debug_print("[HOOKS] Process exited mid-batch, aborting")
                            break

                        line = line.strip()
                        if not line:
                            continue

                        try:
                            event_data = json.loads(line)
                            event_type = event_data.get("event", "")
                            hook_input = event_data.get("input", {})

                            debug_print(f"[HOOKS] Received Gemini event: {event_type}")

                            # Map hook events to state transitions
                            # See: https://geminicli.com/docs/hooks/reference/
                            if event_type == "session_start":
                                self._current_state = "starting"
                                source_type = event_data.get("source", "startup")
                                await self.emit_event("state_change", {
                                    "state": "starting",
                                    "source": "hook",
                                    "hook_event": event_type,
                                    "session_source": source_type  # startup, resume, or clear
                                })

                            elif event_type == "before_agent":
                                # BeforeAgent fires "after user submission but before planning"
                                # See: https://geminicli.com/docs/hooks/reference/
                                #
                                # IMPORTANT: This hook fires for BOTH:
                                # 1. New tasks (user types a request and presses Enter)
                                # 2. Permission responses (user presses Enter to accept/deny)
                                #
                                # We need to distinguish between these cases:
                                # - If we're in waiting_for_input state, this is a RESPONSE to a permission
                                #   prompt. We should NOT transition to in_progress because the user might
                                #   have denied the permission, and we should wait for the actual outcome
                                #   (AfterAgent hook will fire when Gemini finishes responding to denial).
                                # - If we're NOT in waiting_for_input, this is a new task submission.
                                #
                                if self._current_state == "waiting_for_input":
                                    debug_print(f"[HOOKS] before_agent while waiting_for_input - marking response received, awaiting outcome")
                                    # Don't change state - wait for AfterAgent (denial) or BeforeTool (approval)
                                    # But DO mark that the user responded, so the AfterAgent hook won't be ignored.
                                    # This is critical for inputs that bypass _ingest_stdin_bytes (e.g. tmux send-keys).
                                    self._waiting_for_input_response_received = True
                                else:
                                    # New task submission - transition to thinking/in_progress
                                    self._current_state = "thinking"
                                    self._task_in_progress = True
                                    await self.emit_event("state_change", {
                                        "state": "in_progress",  # Maps to in_progress for frontend
                                        "source": "hook",
                                        "hook_event": event_type,
                                        "detail": "thinking"  # Distinguish from "working"
                                    })

                            elif event_type == "before_tool":
                                # BeforeTool fires before tool invocation
                                # This transitions from "thinking" to "working"
                                self._current_state = "working"
                                register_agent_activity(self.agent_id)

                                # Extract tool info from input
                                tool_name = "unknown"
                                if isinstance(hook_input, dict):
                                    tool_name = hook_input.get("tool_name", "unknown")

                                await self.emit_event("state_change", {
                                    "state": "working",
                                    "source": "hook",
                                    "hook_event": event_type,
                                    "detail": "tool_executing",
                                    "tool_name": tool_name
                                })

                            elif event_type == "after_tool":
                                # Tool completed
                                register_agent_activity(self.agent_id)
                                await self.emit_event("tool_completed", {
                                    "source": "hook"
                                })

                            elif event_type == "after_agent":
                                # If we're waiting for input and the user has NOT responded yet,
                                # ignore a spurious AfterAgent/ready transition.
                                # This prevents flicker to READY while the CLI is still prompting.
                                if self._current_state == "waiting_for_input" and not self._waiting_for_input_response_received:
                                    debug_print("[HOOKS] after_agent received while still waiting for input (no user response) - ignoring", file=sys.stderr)
                                    continue
                                # AfterAgent fires after agent loop completes
                                # Agent finished responding - ready for next input
                                self._current_state = "ready"
                                self._task_in_progress = False

                                await self.emit_event("task_completed", {
                                    "reason": "after_agent",
                                    "source": "hook"
                                })
                                await self.emit_event("state_change", {
                                    "state": "ready",
                                    "source": "hook",
                                    "hook_event": event_type,
                                    "detail": "turn_complete"
                                })

                            elif event_type == "notification":
                                # Agent needs user attention (permission, etc.)
                                self._enter_waiting_for_input_state()
                                await self.emit_event("waiting_for_input", {
                                    "prompt": "Notification from Gemini",
                                    "source": "hook"
                                })
                                await self.emit_event("state_change", {
                                    "state": "waiting_for_input",
                                    "source": "hook",
                                    "hook_event": event_type
                                })

                            elif event_type == "session_end":
                                self._current_state = "stopped"
                                await self.emit_event("state_change", {
                                    "state": "stopped",
                                    "source": "hook",
                                    "hook_event": event_type
                                })

                        except json.JSONDecodeError as e:
                            debug_print(f"[HOOKS] Invalid JSON in state file: {e}")

                await asyncio.sleep(0.1)

            except Exception as e:
                debug_print(f"[HOOKS] State monitor error: {e}")
                await asyncio.sleep(0.5)

        debug_print("[HOOKS] State file monitor stopped")

    def _create_otel_app(self):
        app = FastAPI()

        @app.get("/")
        async def root():
            return {"status": "ok", "service": "agentviz-gemini-otlp"}

        @app.post("/v1/traces")
        async def receive_traces(request: Request):
            try:
                body = await request.body()
                debug_print(f"[OTEL] Received traces ({len(body)} bytes)")
                if PROTOBUF_AVAILABLE:
                    traces_req = trace_service_pb2.ExportTraceServiceRequest()
                    traces_req.ParseFromString(body)
                    await self.otel_queue.put(('traces', traces_req))
                return {"status": "ok"}
            except Exception as e:
                debug_print(f"[OTEL] Error: {e}")
                return {"status": "error"}

        @app.post("/v1/metrics")
        async def receive_metrics(request: Request):
            try:
                body = await request.body()
                debug_print(f"[OTEL] Received metrics ({len(body)} bytes)")
                if PROTOBUF_AVAILABLE:
                    metrics_req = metrics_service_pb2.ExportMetricsServiceRequest()
                    metrics_req.ParseFromString(body)
                    await self.otel_queue.put(('metrics', metrics_req))
                return {"status": "ok"}
            except Exception as e:
                debug_print(f"[OTEL] Error: {e}")
                return {"status": "error"}

        @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
        async def catch_all(path: str, request: Request):
            body = await request.body()
            debug_print(f"[OTEL] Caught /{path} ({len(body)} bytes)")
            return {"status": "ok"}

        return app

    async def _process_otel_queue(self):
        debug_print("[OTEL] Queue processor started")
        while True:
            data_type, data = await self.otel_queue.get()
            try:
                if data_type == 'traces':
                    await self._process_traces(data)
                elif data_type == 'metrics':
                    await self._process_metrics(data)
            except Exception as e:
                debug_print(f"[OTEL] Error processing {data_type}: {e}")
            self.otel_queue.task_done()

    async def _process_traces(self, traces_req):
        """Process trace spans from Gemini CLI"""
        for resource_span in traces_req.resource_spans:
            for scope_span in resource_span.scope_spans:
                for span in scope_span.spans:
                    attrs = {attr.key: self._get_attr_value(attr.value) for attr in span.attributes}
                    span_name = span.name.lower()

                    # File operations
                    if any(kw in span_name for kw in ['file', 'write', 'read', 'edit', 'create', 'code_execution']):
                        file_path = (attrs.get('file.path') or attrs.get('file_path') or
                                    attrs.get('path') or attrs.get('gen_ai.file.path') or
                                    attrs.get('code_execution.file_path'))

                        if file_path:
                            from .base import register_file_ownership

                            if not os.path.isabs(file_path):
                                file_path = os.path.join(self.working_dir, file_path)

                            if not is_path_within_dir(file_path, self.working_dir):
                                continue

                            register_file_ownership(file_path, self.agent_id)
                            register_agent_activity(self.agent_id)

                            content = None
                            if os.path.exists(file_path) and os.path.isfile(file_path):
                                try:
                                    with open(file_path, 'r', encoding='utf-8') as f:
                                        content = f.read()
                                except:
                                    pass

                            operation = (attrs.get('operation', 'modified') or 'modified').lower()
                            if operation in ('create', 'created', 'new'):
                                file_event_type = "file_created"
                            elif operation in ('delete', 'deleted'):
                                file_event_type = "file_deleted"
                            else:
                                file_event_type = "file_modified"

                            dedup_key = f"{file_path}:{operation}:{span.start_time_unix_nano}"
                            if dedup_key not in self._seen_file_operations:
                                self._seen_file_operations.add(dedup_key)
                                await self.emit_event(file_event_type, {
                                    "file_path": file_path,
                                    "content": content,
                                    "source": "otel"
                                })

                    # Tool calls
                    if 'tool' in span_name or 'gen_ai' in span_name or 'tool_use' in span_name:
                        tool_name = attrs.get('gen_ai.operation.name') or attrs.get('tool.name', 'unknown')
                        tool_input = attrs.get('tool.input', '') or attrs.get('gen_ai.tool.input', '')

                        start_time = span.start_time_unix_nano
                        dedup_key = f"{tool_name}:{start_time}"

                        if dedup_key not in self._seen_tool_calls:
                            self._seen_tool_calls.add(dedup_key)
                            register_agent_activity(self.agent_id)

                            tool_type = "local"
                            if "search" in tool_name.lower() or "google" in tool_name.lower():
                                tool_type = "google_search"
                            elif "browse" in tool_name.lower():
                                tool_type = "browse_page"

                            await self.emit_event("tool_call", {
                                "tool_name": tool_name,
                                "command": tool_input,
                                "type": tool_type,
                                "source": "otel"
                            })

                    # Code generation
                    if 'gen_ai.generate_content' in span_name or 'code_generation' in span_name:
                        await self.emit_event("code_generation", {
                            "model": attrs.get('gen_ai.model', 'gemini'),
                            "input_tokens": int(attrs.get('gen_ai.input.tokens', 0)),
                            "output_tokens": int(attrs.get('gen_ai.output.tokens', 0))
                        })

    async def _process_metrics(self, metrics_req):
        """Process metrics from Gemini CLI"""
        for resource_metric in metrics_req.resource_metrics:
            for scope_metric in resource_metric.scope_metrics:
                for metric in scope_metric.metrics:
                    metric_name = metric.name.lower()

                    data_points = []
                    if metric.HasField('sum'):
                        data_points = metric.sum.data_points
                    elif metric.HasField('histogram'):
                        data_points = metric.histogram.data_points
                    elif metric.HasField('gauge'):
                        data_points = metric.gauge.data_points

                    # Token usage
                    if 'token' in metric_name:
                        for dp in data_points:
                            attrs = {attr.key: self._get_attr_value(attr.value) for attr in dp.attributes}
                            token_type = attrs.get('gen_ai.token.type') or attrs.get('type', 'unknown')
                            model = attrs.get('gen_ai.request.model') or attrs.get('model', 'gemini')

                            total = 0
                            if hasattr(dp, 'value'):
                                if hasattr(dp.value, 'sum'):
                                    total = int(dp.value.sum)
                                else:
                                    total = int(dp.value)
                            elif hasattr(dp, 'as_int'):
                                total = dp.as_int

                            await self.emit_event("token_usage", {
                                "type": token_type,
                                "model": model,
                                "total": total,
                                "input_tokens": total if token_type == 'input' else 0,
                                "output_tokens": total if token_type == 'output' else 0
                            })

                    # File operations from metrics
                    if "file.operation" in metric_name or "file_operation" in metric_name:
                        for dp in data_points:
                            attrs = {attr.key: self._get_attr_value(attr.value) for attr in dp.attributes}
                            operation_type = attrs.get('operation', 'unknown')
                            lines = int(attrs.get('lines', 0))
                            extension = attrs.get('extension', '')

                            start_time = getattr(dp, 'start_time_unix_nano', 0)
                            dedup_key = f"{operation_type}:{extension}:{lines}:{start_time}"

                            if dedup_key not in self._seen_file_operations:
                                self._seen_file_operations.add(dedup_key)
                                register_agent_activity(self.agent_id)

                                await self.emit_event("file_operation", {
                                    "extension": extension,
                                    "lines": lines,
                                    "operation_type": operation_type,
                                    "source": "otel"
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

    async def _run_otel_server(self):
        if not FASTAPI_AVAILABLE:
            return

        app = self._create_otel_app()
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None
        await server.serve()

    async def run(self):
        """Main run loop with hooks-based state tracking + OTEL telemetry"""

        # Set up OTEL port first
        if PROTOBUF_AVAILABLE and FASTAPI_AVAILABLE:
            self.port = find_free_port()
            debug_print(f"[OTEL] Starting OTLP receiver on port {self.port}")

        # Set up hooks configuration
        self._setup_hooks_config()

        try:
            if self.port:
                self.otel_server_task = asyncio.create_task(self._run_otel_server())
                self.otel_processor_task = asyncio.create_task(self._process_otel_queue())
                await asyncio.sleep(0.5)

                # Set environment for Gemini CLI OTEL
                self.env = os.environ.copy()
                self.env["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http"
                self.env["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://127.0.0.1:{self.port}"
                self.env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = f"http://127.0.0.1:{self.port}/v1/traces"
                self.env["OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"] = f"http://127.0.0.1:{self.port}/v1/metrics"
            else:
                self.env = os.environ.copy()

            # Start state file monitor
            self.state_monitor_task = asyncio.create_task(self._monitor_state_file())

            # Run base adapter
            await super().run()

        finally:
            if self.state_monitor_task:
                self.state_monitor_task.cancel()
                try:
                    await self.state_monitor_task
                except asyncio.CancelledError:
                    pass

            if self.otel_server_task:
                self.otel_server_task.cancel()
                try:
                    await self.otel_server_task
                except asyncio.CancelledError:
                    pass

            if self.otel_processor_task:
                self.otel_processor_task.cancel()
                try:
                    await self.otel_processor_task
                except asyncio.CancelledError:
                    pass

            self._cleanup_hooks_config()
