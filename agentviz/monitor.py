import asyncio
import sys
import time
import socketio

from .adapters.base import BaseAdapter
from .adapters.gemini_adapter import GeminiAdapter
from .adapters.claude_adapter import ClaudeAdapter
from .adapters.codex_adapter import CodexAdapter
from .tmux_runner import TmuxRunner

class Monitor:
    def __init__(self, agent_id, agent_type, agent_command, workspace, tmux_mode=False, remote_host=None):
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.agent_command = agent_command
        self.workspace = workspace
        self.tmux_mode = tmux_mode
        self.remote_host = remote_host
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

        # ---- tmux-mode: skip adapter, use TmuxRunner instead ----
        if self.tmux_mode:
            runner = TmuxRunner(
                monitor=self,
                agent_id=self.agent_id,
                agent_type=self.agent_type,
                workspace=self.workspace,
                command=self.agent_command,
                remote_host=self.remote_host,
            )
            try:
                await runner.run()
            except (asyncio.CancelledError, KeyboardInterrupt):
                if not self._agent_stopped_sent:
                    try:
                        await self.emit_event(
                            agent_id=self.agent_id,
                            agent_type=self.agent_type,
                            event_type="agent_stopped",
                            working_dir=self.workspace,
                            metadata={"return_code": -2, "reason": "interrupted"}
                        )
                        await self.emit_event(
                            agent_id=self.agent_id,
                            agent_type=self.agent_type,
                            event_type="state_change",
                            working_dir=self.workspace,
                            metadata={"state": "stopped", "source": "user_interrupt", "return_code": -2}
                        )
                        self._agent_stopped_sent = True
                        await asyncio.sleep(0.1)
                    except Exception:
                        pass
                raise
            except Exception as e:
                print(f"Error running tmux runner: {e}", file=sys.stderr)
                try:
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
                if self.sio.connected:
                    self.sio.disconnect()
            return

        # ---- Normal adapter path ----
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
