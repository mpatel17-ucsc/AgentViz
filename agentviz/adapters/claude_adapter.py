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
    from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
    PROTOBUF_AVAILABLE = True
except ImportError:
    debug_print("[OTEL] Warning: opentelemetry-proto not installed. Run: pip install opentelemetry-proto")
    PROTOBUF_AVAILABLE = False

# Import FastAPI/uvicorn for OTLP receiver
try:
    from fastapi import FastAPI, Request
    import uvicorn
    FASTAPI_AVAILABLE = True
except ImportError:
    debug_print("[OTEL] Warning: fastapi/uvicorn not installed. Run: pip install fastapi uvicorn")
    FASTAPI_AVAILABLE = False



class ClaudeAdapter(BaseAdapter):
    """
    Adapter for Claude Code with:
    1. Official Hooks API for state tracking (SessionStart, Stop, PreToolUse, etc.)
    2. OpenTelemetry OTLP for file events and tool details

    State Detection via Hooks:
    - SessionStart -> IN_PROGRESS
    - PreToolUse/PostToolUse -> WORKING (tool execution)
    - Stop -> READY (task complete)
    - Notification[idle_prompt] -> IDLE
    - Notification[permission_prompt] -> WAITING_FOR_INPUT
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.otel_queue = asyncio.Queue()
        self.otel_server_task = None
        self.otel_processor_task = None
        self.state_monitor_task = None
        self.port = None
        self._seen_tool_calls = set()
        self._seen_file_operations = set()
        self._disable_file_watcher = True
        self._enable_subprocess_snapshot = False

        # IMPORTANT: Use hooks for state tracking, not screen-based detection
        # This prevents flickering when agent is thinking but not producing output
        self._use_hooks_for_state = True
        # Disable idle timeout fallback for Claude (use hooks only)
        self._enable_idle_timeout_fallback = False

        # State tracking via hooks
        self._state_file = None
        self._state_dir = None
        self._claude_settings_backup = None
        self._current_state = "idle"

    def _get_state_hook_script(self):
        """
        Generate Python hook script for reliable JSON output.

        IMPORTANT: Claude Code hooks require PURE JSON output on stdout.
        Bash scripts can have shell initialization output that corrupts JSON parsing.
        Python scripts provide reliable, clean JSON output.
        """
        return f'''#!/usr/bin/env python3
# Claude Code state hook for agentviz
# This script is called by Claude Code hooks to track state changes
import sys
import json
import time

def main():
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"

    # Read any stdin data (hook input JSON) - but we mainly care about the event name
    stdin_data = None
    try:
        if not sys.stdin.isatty():
            stdin_data = sys.stdin.read().strip()
    except:
        pass

    # Build event record
    data = {{
        "event": event,
        "timestamp": int(time.time() * 1000),
        "agent_id": "{self.agent_id}"
    }}

    # If we got stdin data, try to extract useful fields
    if stdin_data:
        try:
            hook_input = json.loads(stdin_data)
            # Extract tool name if this is a tool-related hook
            if "tool_name" in hook_input:
                data["tool_name"] = hook_input["tool_name"]
            # Extract notification type if this is a notification
            if "notification_type" in hook_input:
                data["notification_type"] = hook_input["notification_type"]
        except json.JSONDecodeError:
            pass

    # Append to state file (atomic write)
    try:
        with open("{self._state_file}", "a") as f:
            f.write(json.dumps(data) + "\\n")
            f.flush()
    except IOError as e:
        sys.stderr.write(f"Failed to write state: {{e}}\\n")
        sys.exit(1)

    # Exit 0 = success, allow the action to proceed
    sys.exit(0)

if __name__ == "__main__":
    main()
'''

    def _setup_hooks_config(self):
        """
        Set up Claude Code hooks configuration for state tracking.
        Creates temporary settings that configure hooks to report state changes.
        """
        # Create a unique state directory for this agent instance
        self._state_dir = tempfile.mkdtemp(prefix=f"agentviz-claude-{self.agent_id}-")
        self._state_file = os.path.join(self._state_dir, "state.jsonl")

        # Create the state file
        Path(self._state_file).touch()

        # Create hook script (Python for reliable JSON output)
        # Using .py extension for clarity
        hook_script_path = os.path.join(self._state_dir, "state-hook.py")
        with open(hook_script_path, 'w') as f:
            f.write(self._get_state_hook_script())
        os.chmod(hook_script_path, 0o755)

        debug_print(f"[HOOKS] Created hook script: {hook_script_path}")

        # Build hooks configuration
        # See: https://code.claude.com/docs/en/hooks
        #
        # Hook lifecycle order:
        # 1. SessionStart - session begins
        # 2. UserPromptSubmit - user submits prompt (THINKING state)
        # 3. PreToolUse - before tool executes (WORKING state)
        # 4. PermissionRequest - permission dialog shown (WAITING_FOR_INPUT)
        # 5. PostToolUse - after tool completes
        # 6. Stop - Claude finishes responding (READY state)
        # 7. SessionEnd - session terminates (STOPPED state)
        #
        # Note: Use python3 explicitly to ensure the Python script runs correctly
        hooks_config = {
            "hooks": {
                "SessionStart": [{
                    "hooks": [{
                        "type": "command",
                        "command": f"python3 {hook_script_path} session_start"
                    }]
                }],
                "Stop": [{
                    "hooks": [{
                        "type": "command",
                        "command": f"python3 {hook_script_path} stop"
                    }]
                }],
                "PreToolUse": [{
                    "matcher": "*",
                    "hooks": [{
                        "type": "command",
                        "command": f"python3 {hook_script_path} pre_tool_use"
                    }]
                }],
                "PostToolUse": [{
                    "matcher": "*",
                    "hooks": [{
                        "type": "command",
                        "command": f"python3 {hook_script_path} post_tool_use"
                    }]
                }],
                # PermissionRequest fires when user sees permission dialog
                # This is the PRIMARY hook for detecting "waiting for input"
                # See: https://code.claude.com/docs/en/hooks#permissionrequest
                "PermissionRequest": [{
                    "matcher": "*",
                    "hooks": [{
                        "type": "command",
                        "command": f"python3 {hook_script_path} permission_request"
                    }]
                }],
                # Notification hooks for different notification types
                # Matchers: permission_prompt, idle_prompt, auth_success, elicitation_dialog
                "Notification": [
                    {
                        "matcher": "idle_prompt",
                        "hooks": [{
                            "type": "command",
                            "command": f"python3 {hook_script_path} idle_prompt"
                        }]
                    },
                    {
                        "matcher": "permission_prompt",
                        "hooks": [{
                            "type": "command",
                            "command": f"python3 {hook_script_path} permission_prompt"
                        }]
                    }
                ],
                "UserPromptSubmit": [{
                    "hooks": [{
                        "type": "command",
                        "command": f"python3 {hook_script_path} user_prompt_submit"
                    }]
                }],
                "SessionEnd": [{
                    "hooks": [{
                        "type": "command",
                        "command": f"python3 {hook_script_path} session_end"
                    }]
                }]
            }
        }

        # Write hooks config to project-local settings
        project_settings_dir = os.path.join(self.working_dir, ".claude")
        project_settings_path = os.path.join(project_settings_dir, "settings.local.json")

        os.makedirs(project_settings_dir, exist_ok=True)

        # Backup existing settings if present
        if os.path.exists(project_settings_path):
            with open(project_settings_path, 'r') as f:
                self._claude_settings_backup = f.read()
            # Merge with existing settings
            try:
                existing = json.loads(self._claude_settings_backup)
                existing["hooks"] = hooks_config["hooks"]
                hooks_config = existing
            except json.JSONDecodeError:
                pass

        with open(project_settings_path, 'w') as f:
            json.dump(hooks_config, f, indent=2)

        debug_print(f"[HOOKS] Configured Claude Code hooks in {project_settings_path}")
        debug_print(f"[HOOKS] State file: {self._state_file}")

        return project_settings_path

    def _cleanup_hooks_config(self):
        """Clean up hooks configuration"""
        project_settings_path = os.path.join(self.working_dir, ".claude", "settings.local.json")

        try:
            if self._claude_settings_backup is not None:
                with open(project_settings_path, 'w') as f:
                    f.write(self._claude_settings_backup)
                debug_print("[HOOKS] Restored original settings.local.json")
            elif os.path.exists(project_settings_path):
                os.remove(project_settings_path)
                debug_print("[HOOKS] Removed temporary settings.local.json")
                # Remove .claude dir if empty
                claude_dir = os.path.join(self.working_dir, ".claude")
                if os.path.exists(claude_dir) and not os.listdir(claude_dir):
                    os.rmdir(claude_dir)
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

                            debug_print(f"[HOOKS] Received event: {event_type}")

                            # Map hook events to state transitions
                            # See: https://code.claude.com/docs/en/hooks for lifecycle
                            if event_type == "session_start":
                                self._current_state = "starting"
                                await self.emit_event("state_change", {
                                    "state": "starting",
                                    "source": "hook",
                                    "hook_event": event_type
                                })

                            elif event_type == "user_prompt_submit":
                                # UserPromptSubmit fires when user submits a prompt,
                                # BEFORE Claude starts processing.
                                #
                                # IMPORTANT: This hook fires for BOTH:
                                # 1. New tasks (user types a request and presses Enter)
                                # 2. Permission responses (user presses Enter to accept/deny)
                                #
                                # We need to distinguish between these cases:
                                # - If we're in waiting_for_input state, this is a RESPONSE to a permission
                                #   prompt. We should NOT transition to in_progress because the user might
                                #   have denied the permission, and we should wait for the actual outcome
                                #   (Stop hook will fire when Claude finishes responding to the denial).
                                # - If we're NOT in waiting_for_input, this is a new task submission.
                                #
                                if self._current_state == "waiting_for_input":
                                    debug_print(f"[HOOKS] user_prompt_submit while waiting_for_input - marking response received, awaiting outcome")
                                    # Don't change state - wait for Stop (denial) or PreToolUse (approval)
                                    # But DO mark that the user responded, so the Stop hook won't be ignored.
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

                            elif event_type == "pre_tool_use":
                                # PreToolUse fires before a tool executes.
                                # This transitions from "thinking" to "working".
                                self._current_state = "working"
                                register_agent_activity(self.agent_id)

                                # Extract tool name from event data if available
                                tool_name = event_data.get("tool_name", "unknown")

                                await self.emit_event("state_change", {
                                    "state": "working",
                                    "source": "hook",
                                    "hook_event": event_type,
                                    "detail": "tool_executing",
                                    "tool_name": tool_name
                                })

                            elif event_type == "post_tool_use":
                                # Still working, but tool finished
                                register_agent_activity(self.agent_id)
                                await self.emit_event("tool_completed", {
                                    "source": "hook"
                                })

                            elif event_type == "stop":
                                # If we're waiting for input and the user has NOT responded yet,
                                # ignore a spurious Stop/ready transition.
                                if self._current_state == "waiting_for_input" and not self._waiting_for_input_response_received:
                                    debug_print("[HOOKS] stop received while still waiting for input (no user response) - ignoring", file=sys.stderr)
                                    continue
                                # Claude finished responding - task complete
                                self._current_state = "ready"
                                self._task_in_progress = False
                                await self.emit_event("task_completed", {
                                    "reason": "hook_stop",
                                    "source": "hook"
                                })
                                await self.emit_event("state_change", {
                                    "state": "ready",
                                    "source": "hook",
                                    "hook_event": event_type
                                })

                            elif event_type == "idle_prompt":
                                self._current_state = "idle"
                                await self.emit_event("state_change", {
                                    "state": "idle",
                                    "source": "hook",
                                    "hook_event": event_type
                                })

                            elif event_type == "permission_request":
                                # PermissionRequest hook - fires when permission dialog appears
                                # This is the primary hook for detecting waiting for user input
                                self._enter_waiting_for_input_state()
                                await self.emit_event("waiting_for_input", {
                                    "prompt": "Permission required",
                                    "source": "hook"
                                })
                                await self.emit_event("state_change", {
                                    "state": "waiting_for_input",
                                    "source": "hook",
                                    "hook_event": event_type
                                })

                            elif event_type == "permission_prompt":
                                # Notification[permission_prompt] - backup for permission detection
                                self._enter_waiting_for_input_state()
                                await self.emit_event("waiting_for_input", {
                                    "prompt": "Permission required",
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

                await asyncio.sleep(0.1)  # Check every 100ms

            except Exception as e:
                debug_print(f"[HOOKS] State monitor error: {e}")
                await asyncio.sleep(0.5)

        debug_print("[HOOKS] State file monitor stopped")

    def _create_otel_app(self):
        app = FastAPI()

        @app.get("/")
        async def root():
            return {"status": "ok", "service": "agentviz-otlp-receiver"}

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
                debug_print(f"[OTEL] Error parsing traces: {e}")
                return {"status": "error", "message": str(e)}

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
                debug_print(f"[OTEL] Error parsing metrics: {e}")
                return {"status": "error", "message": str(e)}

        @app.post("/v1/logs")
        async def receive_logs(request: Request):
            try:
                body = await request.body()
                debug_print(f"[OTEL] Received logs ({len(body)} bytes)")
                if PROTOBUF_AVAILABLE:
                    logs_req = logs_service_pb2.ExportLogsServiceRequest()
                    logs_req.ParseFromString(body)
                    await self.otel_queue.put(('logs', logs_req))
                return {"status": "ok"}
            except Exception as e:
                debug_print(f"[OTEL] Error parsing logs: {e}")
                return {"status": "error", "message": str(e)}

        @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
        async def catch_all(path: str, request: Request):
            body = await request.body()
            debug_print(f"[OTEL] Caught request to /{path} ({len(body)} bytes)")
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
                elif data_type == 'logs':
                    await self._process_logs(data)
            except Exception as e:
                debug_print(f"[OTEL] Error processing {data_type}: {e}")
            self.otel_queue.task_done()

    async def _process_traces(self, traces_req):
        """Process trace spans from Claude Code"""
        for resource_span in traces_req.resource_spans:
            for scope_span in resource_span.scope_spans:
                for span in scope_span.spans:
                    attrs = {attr.key: self._get_attr_value(attr.value) for attr in span.attributes}
                    span_name = span.name.lower()

                    # File operations
                    if any(kw in span_name for kw in ['file', 'write', 'read', 'edit', 'create']):
                        file_path = (attrs.get('file.path') or attrs.get('file_path') or
                                    attrs.get('path') or attrs.get('tool.file_path'))

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
                            elif operation in ('delete', 'deleted', 'remove', 'removed'):
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

                    # Tool execution spans
                    if 'tool' in span_name or 'execute' in span_name:
                        tool_name = attrs.get('tool.name') or attrs.get('operation', 'unknown')
                        tool_input = attrs.get('tool.input', '')

                        start_time = span.start_time_unix_nano
                        dedup_key = f"{tool_name}:{start_time}"

                        if dedup_key not in self._seen_tool_calls:
                            self._seen_tool_calls.add(dedup_key)
                            register_agent_activity(self.agent_id)
                            await self.emit_event("tool_call", {
                                "tool_name": tool_name,
                                "command": tool_input,
                                "type": "local",
                                "source": "otel"
                            })

                    # API request spans (token usage)
                    if 'api' in span_name or 'request' in span_name or 'claude' in span_name:
                        model = attrs.get('model', 'claude-sonnet-4-5')
                        input_tokens = int(attrs.get('input_tokens', 0))
                        output_tokens = int(attrs.get('output_tokens', 0))

                        if input_tokens > 0 or output_tokens > 0:
                            await self.emit_event("token_usage", {
                                "model": model,
                                "input_tokens": input_tokens,
                                "output_tokens": output_tokens,
                                "total": input_tokens + output_tokens
                            })

    async def _process_metrics(self, metrics_req):
        """Process metrics from Claude Code"""
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

                    if 'token' in metric_name:
                        for dp in data_points:
                            attrs = {attr.key: self._get_attr_value(attr.value) for attr in dp.attributes}
                            token_type = attrs.get('type', 'unknown')
                            model = attrs.get('model', 'claude')

                            total = getattr(dp, 'as_int', 0) or int(getattr(dp, 'value', 0))

                            await self.emit_event("token_usage", {
                                "type": token_type,
                                "model": model,
                                "total": total,
                                "input_tokens": total if token_type == 'input' else 0,
                                "output_tokens": total if token_type == 'output' else 0
                            })

    async def _process_logs(self, logs_req):
        """Process log events from Claude Code"""
        for resource_log in logs_req.resource_logs:
            for scope_log in resource_log.scope_logs:
                for log_record in scope_log.log_records:
                    body = ""
                    if log_record.body.HasField('string_value'):
                        body = log_record.body.string_value
                    debug_print(f"[OTEL] Log: {body[:100]}")

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

        # Set up hooks configuration FIRST
        self._setup_hooks_config()

        try:
            # Set up OTEL if available
            if PROTOBUF_AVAILABLE and FASTAPI_AVAILABLE:
                self.port = find_free_port()
                debug_print(f"[OTEL] Starting OTLP receiver on port {self.port}")

                self.otel_server_task = asyncio.create_task(self._run_otel_server())
                self.otel_processor_task = asyncio.create_task(self._process_otel_queue())
                await asyncio.sleep(0.5)

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
            else:
                self.env = os.environ.copy()

            # Start state file monitor
            self.state_monitor_task = asyncio.create_task(self._monitor_state_file())

            # Run the base adapter (PTY, subprocess monitoring, etc.)
            await super().run()

        finally:
            # Clean up
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
