import asyncio
import os
import socket
import json
import tempfile
import shutil
import toml
from pathlib import Path
from contextlib import closing

from .base import BaseAdapter, AGENTVIZ_DEBUG, debug_print, register_agent_activity, is_path_within_dir

# Import OpenTelemetry protobuf definitions
try:
    from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
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


def find_free_port():
    """Find a free port"""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(('', 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


class CodexAdapter(BaseAdapter):
    """
    Adapter for Codex CLI with:
    1. Official notify hook for state tracking
    2. OpenTelemetry OTLP for detailed telemetry
    3. Screen-based detection for states not available via hooks

    IMPORTANT LIMITATION (per official docs):
    The Codex CLI notify system ONLY supports ONE event: "agent-turn-complete"
    See: https://developers.openai.com/codex/config-advanced/

    - agent-turn-complete -> READY (task complete) ✅ Available via notify
    - approval-requested -> NOT available via notify, only via tui.notifications (visual only)
    - thinking state -> NOT available (must use screen detection or OTEL)
    - tool execution -> NOT available (must use subprocess monitoring)

    State Detection Strategy:
    1. notify hook: Captures agent-turn-complete reliably
    2. Screen-based detection: Captures approval prompts, thinking indicators
    3. Subprocess monitoring: Detects tool execution activity
    4. OTEL telemetry: Captures token usage and detailed events

    The notify system calls an external script with JSON payload containing:
    - type: event type (only "agent-turn-complete" currently)
    - thread-id: session identifier
    - turn-id: turn identifier
    - last-assistant-message: assistant's response
    - input-messages: user prompts
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.otel_queue = asyncio.Queue()
        self.otel_server_task = None
        self.otel_processor_task = None
        self.state_monitor_task = None
        self.port = None
        self._seen_otel_events = set()
        self._seen_file_operations = set()
        self._disable_file_watcher = True
        self._enable_subprocess_snapshot = False

        # IMPORTANT: Codex notify only fires on agent-turn-complete
        # We MUST use screen-based detection as supplement for other states
        # (approval prompts, thinking indicators, etc.)
        self._use_hooks_for_state = False  # Enable screen-based detection

        # State tracking
        self._state_dir = None
        self._state_file = None
        self._codex_home = None
        self._original_codex_config = None
        self._current_state = "idle"

        # Codex-specific approval keywords for screen detection
        # These are used when the agent is waiting for user approval
        self._codex_approval_keywords = [
            "approve",
            "allow",
            "deny",
            "(y/n)",
            "[y/n]",
            "continue?",
            "proceed?",
            "confirm",
        ]

    def _get_notify_script(self):
        """Generate the notify script that writes state to our state file"""
        # Codex CLI passes JSON payload as command-line argument (after the command itself)
        # See: https://developers.openai.com/codex/config-advanced/
        return f'''#!/usr/bin/env python3
import sys
import json
import os
import time

STATE_FILE = "{self._state_file}"

def main():
    event_data = None

    # Try to read JSON from command line arguments
    # Codex appends JSON payload after the notify command
    for i, arg in enumerate(sys.argv[1:], 1):
        try:
            event_data = json.loads(arg)
            break
        except (json.JSONDecodeError, TypeError):
            continue

    # If no JSON in args, try stdin
    if event_data is None:
        try:
            if not sys.stdin.isatty():
                stdin_data = sys.stdin.read().strip()
                if stdin_data:
                    event_data = json.loads(stdin_data)
        except (json.JSONDecodeError, TypeError, IOError):
            pass

    # If still no data, create a minimal event
    if event_data is None:
        event_data = {{"type": "unknown", "args": sys.argv[1:]}}

    # Add metadata
    event_data["timestamp"] = int(time.time() * 1000)
    event_data["agent_id"] = "{self.agent_id}"

    # Append to state file
    try:
        with open(STATE_FILE, "a") as f:
            f.write(json.dumps(event_data) + "\\n")
            f.flush()
    except IOError as e:
        sys.stderr.write(f"Failed to write to state file: {{e}}\\n")
        sys.exit(1)

if __name__ == "__main__":
    main()
'''

    def _setup_codex_config(self):
        """
        Set up Codex CLI configuration with notify hook for state tracking.
        Uses a per-instance CODEX_HOME to avoid conflicts.
        """
        # Create unique directories for this agent instance
        self._state_dir = tempfile.mkdtemp(prefix=f"agentviz-codex-{self.agent_id}-")
        self._state_file = os.path.join(self._state_dir, "state.jsonl")
        self._codex_home = tempfile.mkdtemp(prefix=f"agentviz-codex-home-{self.agent_id}-")

        # Create state file
        Path(self._state_file).touch()

        # Create notify script
        notify_script_path = os.path.join(self._state_dir, "notify-hook.py")
        with open(notify_script_path, 'w') as f:
            f.write(self._get_notify_script())
        os.chmod(notify_script_path, 0o755)

        # Build Codex config with notify hook
        # See: https://developers.openai.com/codex/config-advanced/
        # Format: notify = ["command", "arg1", ...]
        # Format: [tui] notifications = true | ["event1", "event2"]
        config = {
            # Top-level notify: command receives JSON payload on stdin
            "notify": ["python3", notify_script_path],
        }

        # Write config to CODEX_HOME using manual TOML format
        # to ensure correct structure (Python toml lib can produce incorrect format)
        config_path = os.path.join(self._codex_home, "config.toml")
        with open(config_path, 'w') as f:
            # Write notify array
            f.write(f'notify = ["python3", "{notify_script_path}"]\n\n')
            # Write TUI section with notifications enabled
            f.write('[tui]\n')
            f.write('notifications = true\n')

        debug_print(f"[HOOKS] Configured Codex notify in {config_path}")
        debug_print(f"[HOOKS] State file: {self._state_file}")
        debug_print(f"[HOOKS] CODEX_HOME: {self._codex_home}")

    def _cleanup_codex_config(self):
        """Clean up Codex configuration"""
        # Clean up state directory
        if self._state_dir and os.path.exists(self._state_dir):
            try:
                shutil.rmtree(self._state_dir)
                debug_print(f"[HOOKS] Removed state directory: {self._state_dir}")
            except Exception as e:
                debug_print(f"[HOOKS] Could not remove state dir: {e}")

        # Clean up CODEX_HOME
        if self._codex_home and os.path.exists(self._codex_home):
            try:
                shutil.rmtree(self._codex_home)
                debug_print(f"[HOOKS] Removed CODEX_HOME: {self._codex_home}")
            except Exception as e:
                debug_print(f"[HOOKS] Could not remove CODEX_HOME: {e}")

    async def _monitor_state_file(self):
        """
        Monitor the state file for notify events and emit state changes.

        IMPORTANT LIMITATION:
        Per official Codex docs (https://developers.openai.com/codex/config-advanced/),
        the notify system ONLY fires for "agent-turn-complete" events.

        "approval-requested" is NOT available via notify - it only works via
        tui.notifications for visual display, not for scripting.

        For approval detection, we rely on screen-based detection in base.py
        (enabled by setting _use_hooks_for_state = False).
        """
        debug_print("[HOOKS] State file monitor started")
        debug_print("[HOOKS] NOTE: Codex notify only supports 'agent-turn-complete' event")

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
                            event_type = event_data.get("type", "")

                            debug_print(f"[HOOKS] Received Codex event: {event_type}")

                            # Map Codex events to state transitions
                            # NOTE: Only agent-turn-complete is reliably available via notify
                            if event_type == "agent-turn-complete":
                                # Agent finished a turn - task complete, ready for input
                                self._current_state = "ready"
                                self._task_in_progress = False

                                # Extract useful info from the event
                                last_message = event_data.get("last-assistant-message", "")
                                thread_id = event_data.get("thread-id", "")
                                turn_id = event_data.get("turn-id", "")

                                await self.emit_event("task_completed", {
                                    "reason": "agent_turn_complete",
                                    "source": "hook",
                                    "thread_id": thread_id,
                                    "turn_id": turn_id,
                                    "last_message_preview": last_message[:200] if last_message else ""
                                })
                                await self.emit_event("state_change", {
                                    "state": "ready",
                                    "source": "hook",
                                    "hook_event": event_type
                                })

                            # NOTE: approval-requested is NOT available via notify
                            # This handler is kept for potential future support,
                            # but currently approval detection relies on screen-based
                            # detection in base.py
                            elif event_type == "approval-requested":
                                debug_print("[HOOKS] WARNING: approval-requested received but this event is not officially supported via notify")
                                self._current_state = "waiting_for_input"

                                await self.emit_event("waiting_for_input", {
                                    "prompt": "Approval requested",
                                    "source": "hook"
                                })
                                await self.emit_event("state_change", {
                                    "state": "waiting_for_input",
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
            return {"status": "ok", "service": "agentviz-codex-otlp"}

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
                if data_type == 'logs':
                    await self._process_logs(data)
            except Exception as e:
                debug_print(f"[OTEL] Error processing {data_type}: {e}")
            self.otel_queue.task_done()

    async def _process_logs(self, logs_req):
        """Process log events from Codex CLI OTLP"""
        for resource_log in logs_req.resource_logs:
            for scope_log in resource_log.scope_logs:
                for log_record in scope_log.log_records:
                    attrs = {attr.key: self._get_attr_value(attr.value) for attr in log_record.attributes}

                    body = ""
                    if log_record.body.HasField('string_value'):
                        body = log_record.body.string_value

                    event_name = attrs.get('event.name', '').lower()
                    timestamp = log_record.time_unix_nano

                    # API Request events (token usage)
                    if 'api.request' in event_name or attrs.get('conversation.id'):
                        model = attrs.get('model', 'gpt-4')
                        input_tokens = int(attrs.get('token.input', 0) or 0)
                        output_tokens = int(attrs.get('token.output', 0) or 0)

                        if input_tokens > 0 or output_tokens > 0:
                            dedup_key = f"token:{timestamp}"
                            if dedup_key not in self._seen_otel_events:
                                self._seen_otel_events.add(dedup_key)
                                await self.emit_event("token_usage", {
                                    "model": model,
                                    "input_tokens": input_tokens,
                                    "output_tokens": output_tokens,
                                    "total": input_tokens + output_tokens
                                })

                    # Tool execution
                    if 'tool.execute' in event_name or 'tool.result' in event_name:
                        tool_name = attrs.get('tool.name', 'unknown')
                        register_agent_activity(self.agent_id)

                        # Mark as working when tool starts
                        if 'execute' in event_name and self._current_state != "working":
                            self._current_state = "working"
                            self._task_in_progress = True
                            await self.emit_event("state_change", {
                                "state": "working",
                                "source": "otel",
                                "tool": tool_name
                            })

                        await self.emit_event("tool_call", {
                            "tool_name": tool_name,
                            "source": "otel"
                        })

                    # File operations
                    if any(kw in event_name for kw in ['file', 'write', 'read', 'edit']):
                        file_path = attrs.get('file.path') or attrs.get('file_path') or attrs.get('path')

                        if file_path:
                            await self._handle_file_operation(file_path, attrs)

    async def _handle_file_operation(self, file_path, attrs):
        """Handle a file operation from OTEL"""
        from .base import register_file_ownership

        if not os.path.isabs(file_path):
            file_path = os.path.join(self.working_dir, file_path)

        if not is_path_within_dir(file_path, self.working_dir):
            return

        register_file_ownership(file_path, self.agent_id)
        register_agent_activity(self.agent_id)

        operation = attrs.get('operation', 'modified')
        dedup_key = f"file:{file_path}:{operation}"

        if dedup_key in self._seen_file_operations:
            return
        self._seen_file_operations.add(dedup_key)

        content = None
        if os.path.exists(file_path) and os.path.isfile(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
            except:
                pass

        if operation in ('create', 'created', 'new'):
            event_type = "file_created"
        elif operation in ('delete', 'deleted'):
            event_type = "file_deleted"
        else:
            event_type = "file_modified"

        await self.emit_event(event_type, {
            "file_path": file_path,
            "content": content,
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
        """Main run loop with notify-based state tracking + OTEL telemetry"""

        # Set up OTEL port first (needed for config)
        if PROTOBUF_AVAILABLE and FASTAPI_AVAILABLE:
            self.port = find_free_port()
            debug_print(f"[OTEL] Starting OTLP receiver on port {self.port}")

        # Set up Codex configuration with notify hook
        self._setup_codex_config()

        try:
            if self.port:
                self.otel_server_task = asyncio.create_task(self._run_otel_server())
                self.otel_processor_task = asyncio.create_task(self._process_otel_queue())
                await asyncio.sleep(0.5)

            # Set environment for Codex
            self.env = os.environ.copy()
            self.env["CODEX_HOME"] = self._codex_home

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

            self._cleanup_codex_config()
