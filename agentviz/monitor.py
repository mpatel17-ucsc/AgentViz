import asyncio
import sys
import time
import socketio

from .adapters.base import BaseAdapter
from .adapters.gemini_adapter import GeminiAdapter
from .adapters.claude_adapter import ClaudeAdapter
from .adapters.codex_adapter import CodexAdapter

class Monitor:
    def __init__(self, agent_id, agent_type, agent_command, workspace):
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.agent_command = agent_command
        self.workspace = workspace
        self.sio = socketio.Client()
        self.lock = asyncio.Lock()

        self.adapter_map = {
            "gemini": GeminiAdapter,
            "gemini-cli": GeminiAdapter,
            "claude": ClaudeAdapter,
            "claude-code": ClaudeAdapter,
            "codex": CodexAdapter,
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

        try:
            await adapter.run()
        except Exception as e:
            print(f"Error running adapter: {e}", file=sys.stderr)
            # Emit an error event if an adapter fails unexpectedly
            await self.emit_event(
                agent_id=self.agent_id,
                agent_type=self.agent_type,
                event_type="error",
                working_dir=self.workspace,
                metadata={"error": str(e)}
            )
        finally:
            if self.sio.connected:
                print(f"[AgentViz Debug] Attempting SocketIO disconnect.", file=sys.stderr)
                self.sio.disconnect()
                print(f"[AgentViz Debug] SocketIO disconnected.", file=sys.stderr)

