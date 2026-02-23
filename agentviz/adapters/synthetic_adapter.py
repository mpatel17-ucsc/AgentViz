import asyncio
import os
import tempfile
from pathlib import Path

from .base import BaseAdapter, debug_print
from .claude_adapter import ClaudeAdapter


class SyntheticAdapter(ClaudeAdapter):
    """
    Adapter for synthetic benchmark agents.

    Reuses ClaudeAdapter's _monitor_state_file() for JSONL-based state tracking.
    Skips OTEL setup and Claude-specific hooks configuration.
    The synthetic agent script writes directly to the state file.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Use hooks (state file) for state tracking
        self._use_hooks_for_state = True
        # Disable features not needed for synthetic agents
        self._disable_file_watcher = True
        self._enable_subprocess_snapshot = False
        self._enable_idle_timeout_fallback = False

    def _setup_hooks_config(self):
        """
        Create temp state file and pass path via env var.
        No .claude/settings.local.json needed since the synthetic agent
        writes directly to the state file.
        """
        self._state_dir = tempfile.mkdtemp(prefix=f"agentviz-synthetic-{self.agent_id}-")
        self._state_file = os.path.join(self._state_dir, "state.jsonl")
        Path(self._state_file).touch()

        # Pass state file path to the synthetic agent via environment
        if self.env is None:
            self.env = os.environ.copy()
        self.env["AGENTVIZ_STATE_FILE"] = self._state_file

        debug_print(f"[SYNTHETIC] State file: {self._state_file}")

    def _cleanup_hooks_config(self):
        """Clean up temp state directory only (no settings.local.json to restore)."""
        import shutil
        if self._state_dir and os.path.exists(self._state_dir):
            try:
                shutil.rmtree(self._state_dir)
                debug_print(f"[SYNTHETIC] Removed state directory: {self._state_dir}")
            except Exception as e:
                debug_print(f"[SYNTHETIC] Could not remove state dir: {e}")

    async def run(self):
        """
        Start state file monitor and run the base adapter.
        Skips OTEL server setup entirely.
        """
        self._setup_hooks_config()

        try:
            # Start state file monitor (reused from ClaudeAdapter)
            self.state_monitor_task = asyncio.create_task(self._monitor_state_file())

            # Run the base adapter directly (PTY, subprocess monitoring)
            await BaseAdapter.run(self)
        finally:
            if self.state_monitor_task:
                self.state_monitor_task.cancel()
                try:
                    await self.state_monitor_task
                except asyncio.CancelledError:
                    pass

            self._cleanup_hooks_config()
