import asyncio
import os
import sys
import termios
import time
import socketio

from .adapters.base import BaseAdapter
from .adapters.gemini_adapter import GeminiAdapter
from .adapters.claude_adapter import ClaudeAdapter
from .adapters.codex_adapter import CodexAdapter

class Monitor:
    def __init__(self, agent_id, agent_type, agent_command, workspace, wrapper="none"):
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.agent_command = agent_command
        self.workspace = workspace
        self.wrapper = wrapper
        self.sio = socketio.Client()
        self.lock = asyncio.Lock()
        # Track whether we've sent agent_stopped (used by cli.py for fallback)
        self._agent_stopped_sent = False

        self.adapter_map = {
            "gemini": GeminiAdapter,
            "gemini-cli": GeminiAdapter,
            "claude": ClaudeAdapter,
            "claude-code": ClaudeAdapter,
            "codex": CodexAdapter,
            "codex-cli": CodexAdapter,
            "openai-codex": CodexAdapter,
        }

    async def emit_event(self, agent_id, agent_type, event_type, working_dir, metadata):
        event_data = {
            "agent_id": agent_id,
            "agent_type": agent_type,
            "event_type": event_type,
            "timestamp": time.time(),
            "working_dir": working_dir,
            "metadata": metadata,
        }
        try:
            if self.sio.connected:
                self.sio.emit("agent_event", event_data)
        except Exception as e:
            print(f"Error emitting event: {e}", file=sys.stderr)

    async def run(self):
        try:
            self.sio.connect("http://localhost:8787")
        except socketio.exceptions.ConnectionError:
            print("Error: Could not connect to AgentViz server at http://localhost:8787.", file=sys.stderr)
            sys.exit(1)

        adapter_class = self.adapter_map.get(self.agent_type)
        if not adapter_class:
            print(f"Warning: No specific adapter found for agent type '{self.agent_type}'. Using generic adapter.", file=sys.stderr)
            adapter_class = BaseAdapter # Default to BaseAdapter if no specific adapter is found

        adapter = adapter_class(
            monitor=self,
            agent_id=self.agent_id,
            agent_type=self.agent_type,
            working_dir=self.workspace,
            command=self.agent_command
        )

        # Listen for control events from the dashboard (used by agentapi wrapper).
        # socketio.Client callbacks run in a background thread — must schedule
        # coroutines via call_soon_threadsafe.
        loop = asyncio.get_running_loop()

        def on_agent_control(data):
            try:
                if data.get("agent_id") != self.agent_id:
                    return
                action = data.get("action")
                print(
                    f"[AgentViz Debug] agent_control received action={action} "
                    f"append_enter={data.get('append_enter')} "
                    f"enter_sequence={data.get('enter_sequence')}",
                    file=sys.stderr,
                )
                if hasattr(adapter, 'handle_agent_control'):
                    loop.call_soon_threadsafe(
                        asyncio.ensure_future,
                        adapter.handle_agent_control(action, data),
                    )
                else:
                    # Control-only fallback: write to PTY directly.
                    # This preserves original state-transition behavior because it
                    # does not emit/force any state events.
                    pty_fd = getattr(adapter, "pty_master_fd", None)
                    if pty_fd is None:
                        return

                    def _enter_bytes(seq):
                        seq = str(seq or "cr").lower()
                        if seq == "lf":
                            return b"\n"
                        if seq == "crlf":
                            return b"\r\n"
                        return b"\r"

                    if action == "send_input":
                        text = data.get("text", "")
                        if text:
                            os.write(pty_fd, str(text).encode("utf-8"))
                            if data.get("append_enter", True):
                                os.write(pty_fd, _enter_bytes(data.get("enter_sequence", "cr")))
                    elif action == "select_option":
                        selected = data.get("selected") or {}
                        input_val = selected.get("input") or data.get("input") or str(data.get("index", ""))
                        os.write(pty_fd, str(input_val).encode("utf-8") + _enter_bytes(data.get("enter_sequence", "cr")))
                    elif action == "simulate_enter":
                        os.write(pty_fd, _enter_bytes(data.get("enter_sequence", "cr")))

                    try:
                        termios.tcdrain(pty_fd)
                    except Exception:
                        pass

                # Mirror local-enter semantics for remote control:
                # emit user_prompt on "submitted" input actions so backend state
                # transitions follow the same event path as non-controllable mode.
                should_emit_user_prompt = (
                    action == "simulate_enter" or
                    action == "select_option" or
                    (action == "send_input" and data.get("append_enter", True))
                )
                if should_emit_user_prompt:
                    prompt_text = "[user input]"
                    if action in ("simulate_enter", "select_option"):
                        prompt_text = "[user response to prompt]"
                    loop.call_soon_threadsafe(
                        asyncio.ensure_future,
                        self.emit_event(
                            agent_id=self.agent_id,
                            agent_type=self.agent_type,
                            event_type="user_prompt",
                            working_dir=self.workspace,
                            metadata={"prompt": prompt_text, "source": "dashboard_control"},
                        ),
                    )
            except Exception as e:
                print(f"[AgentViz Debug] Failed to handle agent_control: {e}", file=sys.stderr)

        self.sio.on("agent_control", on_agent_control)

        try:
            await adapter.run()
            # If we get here, the adapter completed normally
            # The adapter should have emitted agent_stopped already
            self._agent_stopped_sent = adapter._agent_stopped_emitted
        except (asyncio.CancelledError, KeyboardInterrupt) as e:
            # Emit agent_stopped before re-raising
            if not self._agent_stopped_sent:
                try:
                    await self.emit_event(
                        agent_id=self.agent_id,
                        agent_type=self.agent_type,
                        event_type="agent_stopped",
                        working_dir=self.workspace,
                        metadata={"return_code": -2, "reason": "interrupted"}
                    )
                    # Also emit state_change for proper frontend update
                    await self.emit_event(
                        agent_id=self.agent_id,
                        agent_type=self.agent_type,
                        event_type="state_change",
                        working_dir=self.workspace,
                        metadata={"state": "stopped", "source": "user_interrupt", "return_code": -2}
                    )
                    self._agent_stopped_sent = True
                    # Give the event time to be sent
                    await asyncio.sleep(0.1)
                except Exception as emit_error:
                    print(f"[AgentViz Debug] Failed to emit agent_stopped: {emit_error}", file=sys.stderr)
            raise
        except Exception as e:
            print(f"Error running adapter: {e}", file=sys.stderr)
            # Emit an error event if an adapter fails unexpectedly
            try:
                await self.emit_event(
                    agent_id=self.agent_id,
                    agent_type=self.agent_type,
                    event_type="error",
                    working_dir=self.workspace,
                    metadata={"error": str(e)}
                )
                # Also emit agent_stopped so it moves to completed/error
                await self.emit_event(
                    agent_id=self.agent_id,
                    agent_type=self.agent_type,
                    event_type="agent_stopped",
                    working_dir=self.workspace,
                    metadata={"return_code": 1, "reason": "error", "error": str(e)}
                )
                self._agent_stopped_sent = True
            except Exception:
                pass
        finally:
            # Final attempt to emit agent_stopped if not already done
            if not self._agent_stopped_sent and hasattr(adapter, '_agent_stopped_emitted') and not adapter._agent_stopped_emitted:
                try:
                    # Use synchronous emit since we may be shutting down
                    if self.sio.connected:
                        self.sio.emit("agent_event", {
                            "agent_id": self.agent_id,
                            "agent_type": self.agent_type,
                            "event_type": "agent_stopped",
                            "timestamp": time.time(),
                            "working_dir": self.workspace,
                            "metadata": {"return_code": -2, "reason": "cleanup"}
                        })
                        self.sio.emit("agent_event", {
                            "agent_id": self.agent_id,
                            "agent_type": self.agent_type,
                            "event_type": "state_change",
                            "timestamp": time.time(),
                            "working_dir": self.workspace,
                            "metadata": {"state": "stopped", "source": "cleanup", "return_code": -2}
                        })
                        self._agent_stopped_sent = True
                        # Small sync sleep to allow message to be sent
                        import time as time_module
                        time_module.sleep(0.1)
                except Exception as e:
                    print(f"[AgentViz Debug] Final agent_stopped emit failed: {e}", file=sys.stderr)

            if self.sio.connected:
                print(f"[AgentViz Debug] Attempting SocketIO disconnect.", file=sys.stderr)
                self.sio.disconnect()
                print(f"[AgentViz Debug] SocketIO disconnected.", file=sys.stderr)
