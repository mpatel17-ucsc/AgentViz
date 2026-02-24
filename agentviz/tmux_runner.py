import asyncio
import errno
import fcntl
import importlib
import json
import os
import pty
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import time
import tty

from .utils import find_free_port


class TmuxRunner:
    """
    Runs an agent command inside an isolated tmux session with a TTYD web terminal.

    Uses the REAL adapter (GeminiAdapter, ClaudeAdapter, etc.) for hooks setup
    and state monitoring — identical state tracking to non-tmux mode.

    The adapter handles:
      - Writing hooks config to workspace (e.g. .gemini/settings.json)
      - Monitoring the state file for hook events
      - Emitting state_change events to the backend

    TmuxRunner handles:
      - Creating the tmux session (instead of a PTY)
      - Starting ttyd for web terminal access
      - Attaching the user's terminal to the tmux session
      - Cleanup of tmux + ttyd on exit
    """

    def __init__(self, monitor, agent_id, agent_type, workspace, command, remote_host=None):
        self.monitor = monitor
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.workspace = workspace
        self.command = command
        self.remote_host = remote_host
        self.session_name = f"agentviz-{agent_id}"
        self.ttyd_process = None
        self.ttyd_port = None
        self.adapter = None
        self._state_monitor_task = None
        self._subprocess_monitor_task = None
        self._activity_monitor_task = None
        self._tmux_output_task = None
        self._tmux_input_task = None
        self._otel_server_task = None
        self._otel_processor_task = None
        self._tmux_io_dir = None
        self._tmux_output_path = None
        self._ttyd_input_path = None
        self._ttyd_input_task = None

    # ------------------------------------------------------------------
    # Adapter lifecycle (hooks + state monitoring)
    # ------------------------------------------------------------------

    def _create_adapter(self):
        """Instantiate the real adapter for hooks + state monitoring."""
        adapter_class = self.monitor.adapter_map.get(self.agent_type)
        if not adapter_class:
            print(f"[TMUX] No adapter for '{self.agent_type}', state tracking disabled", file=sys.stderr)
            return

        self.adapter = adapter_class(
            monitor=self.monitor,
            agent_id=self.agent_id,
            agent_type=self.agent_type,
            working_dir=self.workspace,
            command=self.command,
        )
        print(f"[TMUX] Created {adapter_class.__name__} for hooks + state monitoring", file=sys.stderr)

    def _start_adapter_hooks(self):
        """Call adapter's hooks setup and start its state file monitor."""
        if not self.adapter:
            return

        # Setup hooks config (writes to workspace or CODEX_HOME)
        if hasattr(self.adapter, '_setup_hooks_config'):
            # Claude, Gemini — hooks written to workspace config file
            self.adapter._setup_hooks_config()
        elif hasattr(self.adapter, '_setup_codex_config'):
            # Codex — hooks written to CODEX_HOME/config.toml
            self.adapter._setup_codex_config()

        # Start state file monitor (reads hook events, emits state_change)
        if hasattr(self.adapter, '_monitor_state_file'):
            self._state_monitor_task = asyncio.create_task(
                self.adapter._monitor_state_file()
            )
            print(f"[TMUX] Started adapter state monitor", file=sys.stderr)

    async def _prepare_adapter_runtime(self):
        """
        Prepare adapter runtime pieces normally done in adapter.run():
        - OTEL port/tasks setup
        - adapter env construction
        """
        if not self.adapter:
            return

        module = importlib.import_module(self.adapter.__class__.__module__)
        protobuf_available = bool(getattr(module, "PROTOBUF_AVAILABLE", False))
        fastapi_available = bool(getattr(module, "FASTAPI_AVAILABLE", False))

        # Start OTEL receiver/processor if this adapter supports it
        if (
            hasattr(self.adapter, "_run_otel_server")
            and hasattr(self.adapter, "_process_otel_queue")
            and protobuf_available
            and fastapi_available
        ):
            self.adapter.port = find_free_port()
            self._otel_server_task = asyncio.create_task(self.adapter._run_otel_server())
            self._otel_processor_task = asyncio.create_task(self.adapter._process_otel_queue())
            await asyncio.sleep(0.5)
            print(f"[TMUX] Started adapter OTEL receiver on port {self.adapter.port}", file=sys.stderr)

        # Build environment exactly like adapter.run() would
        self.adapter.env = os.environ.copy()

        adapter_name = self.adapter.__class__.__name__
        if adapter_name == "ClaudeAdapter" and getattr(self.adapter, "port", None):
            self.adapter.env["CLAUDE_CODE_ENABLE_TELEMETRY"] = "1"
            self.adapter.env["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
            self.adapter.env["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://127.0.0.1:{self.adapter.port}"
            self.adapter.env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = f"http://127.0.0.1:{self.adapter.port}/v1/traces"
            self.adapter.env["OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"] = f"http://127.0.0.1:{self.adapter.port}/v1/metrics"
            self.adapter.env["OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"] = f"http://127.0.0.1:{self.adapter.port}/v1/logs"
            self.adapter.env["OTEL_METRICS_EXPORTER"] = "otlp"
            self.adapter.env["OTEL_LOGS_EXPORTER"] = "otlp"
        elif adapter_name == "GeminiAdapter" and getattr(self.adapter, "port", None):
            self.adapter.env["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http"
            self.adapter.env["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://127.0.0.1:{self.adapter.port}"
            self.adapter.env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = f"http://127.0.0.1:{self.adapter.port}/v1/traces"
            self.adapter.env["OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"] = f"http://127.0.0.1:{self.adapter.port}/v1/metrics"

        # Codex config path must always be present in the tmux environment
        if hasattr(self.adapter, "_codex_home") and self.adapter._codex_home:
            self.adapter.env["CODEX_HOME"] = self.adapter._codex_home

    def _get_adapter_env(self):
        """
        Collect env vars the agent needs inside the tmux session.

        - Claude/Gemini: hooks live in workspace config files, no env vars needed.
        - Codex: notify config lives in CODEX_HOME, must be set as env var.
        """
        env_vars = {}
        if not self.adapter:
            return env_vars

        # If adapter prepared a full env, inject only keys that differ
        # from the current process environment.
        if getattr(self.adapter, "env", None):
            for key, value in self.adapter.env.items():
                if os.environ.get(key) != value:
                    env_vars[key] = value
            return env_vars

        # Fallback: Codex needs CODEX_HOME pointing to the temp config dir
        if hasattr(self.adapter, "_codex_home") and self.adapter._codex_home:
            env_vars["CODEX_HOME"] = self.adapter._codex_home

        return env_vars

    def _stop_adapter_hooks(self):
        """Signal adapter to stop monitoring and clean up hooks config."""
        if not self.adapter:
            return

        # Signal the adapter's monitor loop to exit.
        # Set _process_exited FIRST so the monitor breaks out of its loop,
        # then set shutdown_event as a backup signal.
        self.adapter._process_exited = True
        self.adapter.shutdown_event.set()

        # Cancel the state monitor task
        if self._state_monitor_task and not self._state_monitor_task.done():
            self._state_monitor_task.cancel()
            try:
                # Use a short sync wait — we're in cleanup
                pass
            except Exception:
                pass
        if self._subprocess_monitor_task and not self._subprocess_monitor_task.done():
            self._subprocess_monitor_task.cancel()
        if self._activity_monitor_task and not self._activity_monitor_task.done():
            self._activity_monitor_task.cancel()
        if self._tmux_output_task and not self._tmux_output_task.done():
            self._tmux_output_task.cancel()
        if self._tmux_input_task and not self._tmux_input_task.done():
            self._tmux_input_task.cancel()
        if self._ttyd_input_task and not self._ttyd_input_task.done():
            self._ttyd_input_task.cancel()
        if self._otel_server_task and not self._otel_server_task.done():
            self._otel_server_task.cancel()
        if self._otel_processor_task and not self._otel_processor_task.done():
            self._otel_processor_task.cancel()

        # Cleanup hooks config (restores original settings file)
        if hasattr(self.adapter, '_cleanup_hooks_config'):
            self.adapter._cleanup_hooks_config()
        elif hasattr(self.adapter, '_cleanup_codex_config'):
            self.adapter._cleanup_codex_config()

        print(f"[TMUX] Stopped adapter hooks", file=sys.stderr)

    def _get_tmux_pane_pid(self):
        """Return tmux pane PID for this session, or None if unavailable."""
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", self.session_name, "#{pane_pid}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        if not value.isdigit():
            return None
        return int(value)

    def _setup_tmux_io_capture(self):
        """
        Capture tmux pane output/input into files for adapter parity.
        This lets tmux mode reuse the same BaseAdapter parsers.
        """
        self._tmux_io_dir = tempfile.mkdtemp(prefix=f"agentviz-tmux-io-{self.agent_id}-")
        self._tmux_output_path = os.path.join(self._tmux_io_dir, "pane-output.log")
        open(self._tmux_output_path, "ab").close()

        out_cmd = f"cat >> {shlex.quote(self._tmux_output_path)}"
        out_res = subprocess.run(
            ["tmux", "pipe-pane", "-O", "-t", self.session_name, out_cmd],
            capture_output=True,
            text=True,
        )
        if out_res.returncode != 0:
            raise RuntimeError(f"Failed to setup tmux output capture: {out_res.stderr.strip()}")
        # NOTE: pipe-pane -I does NOT capture user keystrokes (it injects input).
        # User input detection is handled by PTY stdin interception in _attach_with_stdin_intercept().
        print("[TMUX] Enabled pane output capture", file=sys.stderr)

    async def _tail_tmux_output(self):
        """Tail captured pane output and feed adapter terminal parser."""
        if not self.adapter or not hasattr(self.adapter, "_ingest_terminal_output"):
            return
        path = self._tmux_output_path
        position = 0
        while not self.adapter.shutdown_event.is_set():
            if not path or not os.path.exists(path):
                await asyncio.sleep(0.1)
                continue
            try:
                with open(path, "rb") as f:
                    f.seek(position)
                    chunk = f.read()
                    position = f.tell()
                if chunk:
                    self.adapter._ingest_terminal_output(chunk.decode("utf-8", errors="ignore"))
            except Exception:
                pass
            await asyncio.sleep(0.1)

    def _create_ttyd_wrapper_script(self):
        """
        Create a Python script that wraps 'tmux attach-session' in a PTY,
        capturing stdin to a file. This lets us intercept keystrokes from
        ttyd (dashboard) the same way _attach_with_stdin_intercept() does
        for the local terminal.

        Data flow:
          ttyd → wrapper stdin → capture file + PTY master → tmux attach (slave PTY)
          tmux attach (slave PTY) → PTY master → wrapper stdout → ttyd
        """
        self._ttyd_input_path = os.path.join(self._tmux_io_dir, "ttyd-input.log")
        open(self._ttyd_input_path, "ab").close()

        script_path = os.path.join(self._tmux_io_dir, "ttyd-wrapper.py")
        script_content = f'''#!/usr/bin/env python3
"""Wrap tmux attach in a PTY, capturing stdin for state tracking."""
import errno, fcntl, os, pty, select, signal, struct, sys, termios, tty

SESSION = {self.session_name!r}
CAPTURE = {self._ttyd_input_path!r}

master_fd, slave_fd = pty.openpty()

# Propagate initial terminal size from ttyd
try:
    ws = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\\x00" * 8)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, ws)
except Exception:
    pass

pid = os.fork()
if pid == 0:
    os.close(master_fd)
    os.setsid()
    os.dup2(slave_fd, 0)
    os.dup2(slave_fd, 1)
    os.dup2(slave_fd, 2)
    if slave_fd > 2:
        os.close(slave_fd)
    os.execvp("tmux", ["tmux", "attach-session", "-t", SESSION])
    sys.exit(1)

os.close(slave_fd)

# Propagate SIGWINCH from ttyd to the PTY + child
def _winch(signum, frame):
    try:
        ws = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\\x00" * 8)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, ws)
        os.kill(pid, signal.SIGWINCH)
    except Exception:
        pass

signal.signal(signal.SIGWINCH, _winch)

capture_fd = os.open(CAPTURE, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
old_stdin_settings = None
try:
    # Match local attach path: forward exact bytes (arrows/esc/ctrl combos)
    # instead of line-buffered/cooked input from the terminal frontend.
    old_stdin_settings = termios.tcgetattr(sys.stdin.fileno())
    tty.setraw(sys.stdin.fileno())
except Exception:
    old_stdin_settings = None

try:
    while True:
        try:
            fds = select.select([sys.stdin.fileno(), master_fd], [], [], 1.0)[0]
        except (select.error, InterruptedError):
            continue
        if sys.stdin.fileno() in fds:
            try:
                data = os.read(sys.stdin.fileno(), 1024)
            except OSError:
                break
            if not data:
                break
            os.write(master_fd, data)
            os.write(capture_fd, data)
        if master_fd in fds:
            try:
                data = os.read(master_fd, 4096)
            except OSError as e:
                if e.errno == errno.EIO:
                    break
                raise
            if not data:
                break
            os.write(sys.stdout.fileno(), data)
finally:
    if old_stdin_settings is not None:
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_stdin_settings)
        except Exception:
            pass
    os.close(capture_fd)
    os.close(master_fd)
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
'''
        with open(script_path, 'w') as f:
            f.write(script_content)
        os.chmod(script_path, 0o755)
        print(f"[TMUX] Created ttyd wrapper script at {script_path}", file=sys.stderr)
        return script_path

    async def _tail_ttyd_input(self):
        """Tail captured ttyd stdin and feed adapter's stdin parser."""
        if not self.adapter or not hasattr(self.adapter, "_ingest_stdin_bytes"):
            return
        path = self._ttyd_input_path
        if not path:
            return
        position = 0
        while not self.adapter.shutdown_event.is_set():
            if not os.path.exists(path):
                await asyncio.sleep(0.1)
                continue
            try:
                with open(path, "rb") as f:
                    f.seek(position)
                    chunk = f.read()
                    position = f.tell()
                if chunk:
                    # side_effects=False: ttyd/tmux already handled the actual I/O;
                    # we only want state tracking (Enter detection, Escape, etc.)
                    self.adapter._ingest_stdin_bytes(chunk, side_effects=False)
            except Exception:
                pass
            await asyncio.sleep(0.1)

    async def _attach_with_stdin_intercept(self):
        """
        Attach to the tmux session via a PTY wrapper so we can intercept stdin.

        This is the same pattern as BaseAdapter.run():
        - Real stdin → PTY master → tmux attach reads it as its stdin
        - PTY master output (tmux rendering) → real stdout
        - Intercepted stdin bytes → adapter._ingest_stdin_bytes(side_effects=False)

        This ensures _ingest_stdin_bytes fires on every keystroke, giving us
        the exact same state transitions as non-tmux mode (Enter → in_progress,
        Escape detection, waiting_for_input response tracking, etc.).
        """
        attach_master_fd, attach_slave_fd = pty.openpty()

        # Propagate current terminal size to the PTY so tmux renders correctly
        try:
            winsize = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b'\x00' * 8)
            fcntl.ioctl(attach_slave_fd, termios.TIOCSWINSZ, winsize)
        except Exception:
            pass

        attach_pid = os.fork()

        if attach_pid == 0:
            # Child: run tmux attach with PTY as stdio.
            # CRITICAL: os.setsid() creates a new session so the slave PTY
            # becomes this process's controlling terminal. Without this,
            # /dev/tty still points to the original terminal, and tmux
            # (which opens /dev/tty directly) would read from the real
            # terminal — splitting keystrokes between our parent reader
            # and tmux, breaking stdin interception entirely.
            os.close(attach_master_fd)
            os.setsid()
            os.dup2(attach_slave_fd, sys.stdin.fileno())
            os.dup2(attach_slave_fd, sys.stdout.fileno())
            os.dup2(attach_slave_fd, sys.stderr.fileno())
            if attach_slave_fd > 2:
                os.close(attach_slave_fd)
            try:
                os.execvp("tmux", ["tmux", "attach-session", "-t", self.session_name])
            except Exception:
                sys.exit(1)
        else:
            # Parent: proxy stdin ↔ PTY and intercept for state tracking
            os.close(attach_slave_fd)
            old_stdin_settings = termios.tcgetattr(sys.stdin.fileno())
            # Raw mode is required (not cbreak) because tmux expects raw terminal I/O:
            # - cbreak keeps ICRNL active (CR→NL), so Enter sends \n instead of \r
            # - cbreak keeps ISIG active, so Ctrl+C kills parent instead of forwarding
            # Raw mode disables all input processing, forwarding exact bytes to tmux.
            tty.setraw(sys.stdin.fileno())

            loop = asyncio.get_running_loop()

            # Forward SIGWINCH (terminal resize) to the PTY
            def _handle_sigwinch():
                try:
                    winsize = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b'\x00' * 8)
                    fcntl.ioctl(attach_master_fd, termios.TIOCSWINSZ, winsize)
                    # Also notify the child process
                    os.kill(attach_pid, signal.SIGWINCH)
                except (OSError, ProcessLookupError):
                    pass

            loop.add_signal_handler(signal.SIGWINCH, _handle_sigwinch)

            def _stdin_to_tmux():
                """Read real stdin → forward to PTY + feed adapter state tracking."""
                try:
                    data = os.read(sys.stdin.fileno(), 1024)
                    if data:
                        os.write(attach_master_fd, data)
                        # Feed adapter's stdin parser (same as BaseAdapter._stdin_read_callback)
                        # side_effects=False: don't write to adapter.pty_master_fd or kill agent
                        # (tmux handles all of that; we only want state tracking)
                        if self.adapter:
                            self.adapter._ingest_stdin_bytes(data, side_effects=False)
                except OSError:
                    pass

            def _tmux_to_stdout():
                """Read PTY output (tmux rendering) → forward to real stdout."""
                try:
                    data = os.read(attach_master_fd, 4096)
                    if data:
                        os.write(sys.stdout.fileno(), data)
                except OSError as e:
                    if e.errno == errno.EIO:
                        # PTY closed — tmux attach exited
                        try:
                            loop.remove_reader(attach_master_fd)
                        except (ValueError, RuntimeError):
                            pass

            loop.add_reader(sys.stdin.fileno(), _stdin_to_tmux)
            loop.add_reader(attach_master_fd, _tmux_to_stdout)

            try:
                # Wait for tmux attach process to exit
                while True:
                    try:
                        wpid, status = os.waitpid(attach_pid, os.WNOHANG)
                        if wpid != 0:
                            rc = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
                            print(f"[TMUX] Detached/exited (rc={rc})", file=sys.stderr)
                            break
                    except ChildProcessError:
                        break
                    await asyncio.sleep(0.2)
            finally:
                # Cleanup: remove readers, close PTY, restore terminal
                try:
                    loop.remove_signal_handler(signal.SIGWINCH)
                except Exception:
                    pass
                try:
                    loop.remove_reader(sys.stdin.fileno())
                except (ValueError, RuntimeError):
                    pass
                try:
                    loop.remove_reader(attach_master_fd)
                except (ValueError, RuntimeError):
                    pass
                try:
                    os.close(attach_master_fd)
                except OSError:
                    pass
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_stdin_settings)

    def _start_adapter_background_monitors(self):
        """
        Start adapter background monitors used by non-tmux mode.

        This keeps subprocess/activity-driven transitions consistent with
        the original adapter behavior.
        """
        if not self.adapter:
            return

        pane_pid = self._get_tmux_pane_pid()
        if pane_pid:
            self.adapter.agent_proc_pid = pane_pid
            self.adapter.last_activity_time = time.time()
            print(f"[TMUX] Bound adapter monitors to pane pid={pane_pid}", file=sys.stderr)

        if hasattr(self.adapter, "monitor_subprocesses"):
            self._subprocess_monitor_task = asyncio.create_task(
                self.adapter.monitor_subprocesses()
            )
            print("[TMUX] Started adapter subprocess monitor", file=sys.stderr)

        if hasattr(self.adapter, "activity_monitor_loop"):
            self._activity_monitor_task = asyncio.create_task(
                self.adapter.activity_monitor_loop()
            )
            print("[TMUX] Started adapter activity monitor", file=sys.stderr)

    # ------------------------------------------------------------------
    # Clean stale hooks from previous interrupted runs
    # ------------------------------------------------------------------

    def _clean_stale_hooks(self):
        """Remove stale agentviz hook configs pointing to deleted temp dirs."""
        for dirname, filename, marker in [
            (".gemini", "settings.json", "agentviz-"),
            (".claude", "settings.local.json", "agentviz-"),
        ]:
            path = os.path.join(self.workspace, dirname, filename)
            if not os.path.exists(path):
                continue
            try:
                with open(path, 'r') as f:
                    config = json.load(f)
                if "hooks" not in config:
                    continue
                hooks_str = json.dumps(config["hooks"])
                if marker not in hooks_str:
                    continue
                del config["hooks"]
                config.pop("hooksConfig", None)
                if config:
                    with open(path, 'w') as f:
                        json.dump(config, f, indent=2)
                else:
                    os.remove(path)
                    parent = os.path.join(self.workspace, dirname)
                    if os.path.isdir(parent) and not os.listdir(parent):
                        os.rmdir(parent)
                print(f"[TMUX] Cleaned stale hooks from {path}", file=sys.stderr)
            except (json.JSONDecodeError, IOError) as e:
                print(f"[TMUX] Warning: could not clean {path}: {e}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------

    async def run(self):
        if not shutil.which("tmux"):
            raise RuntimeError("tmux not found. Install with: brew install tmux")
        if not shutil.which("ttyd"):
            raise RuntimeError("ttyd not found. Install with: brew install ttyd")

        tmux_history_limit = "20000"

        # 1. Clean stale hooks from previous interrupted runs
        self._clean_stale_hooks()

        # 2. Create the real adapter and set up hooks + state monitor
        self._create_adapter()
        self._start_adapter_hooks()
        await self._prepare_adapter_runtime()

        try:
            # 3. Create tmux session with agent command
            #    If the adapter needs env vars (e.g. Codex CODEX_HOME),
            #    prefix the command with "env VAR=VAL ..."
            env_vars = self._get_adapter_env()
            cmd_str = shlex.join(self.command)
            if env_vars:
                env_prefix = " ".join(
                    f"{k}={shlex.quote(str(v))}" for k, v in env_vars.items()
                )
                tmux_cmd_str = f"env {env_prefix} {cmd_str}"
                print(f"[TMUX] Injecting env: {env_prefix}", file=sys.stderr)
            else:
                tmux_cmd_str = cmd_str

            create_cmd = [
                "tmux", "new-session", "-d",
                "-s", self.session_name,
                "-c", self.workspace,
                tmux_cmd_str,
            ]
            result = subprocess.run(create_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"Failed to create tmux session: {result.stderr.strip()}")
            print(f"[TMUX] Created session '{self.session_name}' running: {cmd_str}", file=sys.stderr)

            # Increase scrollback for the AgentViz tmux window so dashboard terminals
            # can scroll further back into prior agent output.
            history_res = subprocess.run(
                ["tmux", "set-option", "-t", self.session_name, "history-limit", tmux_history_limit],
                capture_output=True,
                text=True,
            )
            if history_res.returncode != 0:
                print(
                    f"[TMUX] Warning: could not set history-limit={tmux_history_limit} "
                    f"for session '{self.session_name}': {history_res.stderr.strip()}",
                    file=sys.stderr,
                )
            else:
                print(f"[TMUX] Set history-limit={tmux_history_limit} for session '{self.session_name}'", file=sys.stderr)

            # Enable tmux mouse mode so wheel scrolling in ttyd can scroll the tmux pane/copy-mode.
            mouse_res = subprocess.run(
                ["tmux", "set-option", "-t", self.session_name, "mouse", "on"],
                capture_output=True,
                text=True,
            )
            if mouse_res.returncode != 0:
                print(
                    f"[TMUX] Warning: could not enable mouse mode for session '{self.session_name}': "
                    f"{mouse_res.stderr.strip()}",
                    file=sys.stderr,
                )
            else:
                print(f"[TMUX] Enabled mouse mode for session '{self.session_name}'", file=sys.stderr)

            # 3a. Mirror tmux pane IO into adapter's existing parsers
            self._setup_tmux_io_capture()
            self._tmux_output_task = asyncio.create_task(self._tail_tmux_output())

            # 3b. Start adapter background monitors (subprocess + activity)
            self._start_adapter_background_monitors()

            # 4. Emit agent_started
            await self.monitor.emit_event(
                agent_id=self.agent_id, agent_type=self.agent_type,
                event_type="agent_started", working_dir=self.workspace,
                metadata={"source": "tmux_runner"},
            )

            # 5. Start ttyd for web terminal (with stdin capture wrapper)
            #    The wrapper wraps 'tmux attach' in a PTY so we can capture
            #    keystrokes from the dashboard and feed _ingest_stdin_bytes().
            ttyd_wrapper = self._create_ttyd_wrapper_script()
            self.ttyd_port = find_free_port()
            ttyd_cmd = [
                "ttyd",
                "--port", str(self.ttyd_port),
                "--writable",
                "-t", f"scrollback={tmux_history_limit}",
            ]
            if self.remote_host:
                ttyd_cmd += ["--interface", "0.0.0.0"]
            ttyd_cmd += ["python3", ttyd_wrapper]
            self.ttyd_process = subprocess.Popen(
                ttyd_cmd,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            print(f"[TMUX] Started ttyd on port {self.ttyd_port} (pid={self.ttyd_process.pid})", file=sys.stderr)

            # 5a. Tail ttyd input capture → adapter._ingest_stdin_bytes()
            self._ttyd_input_task = asyncio.create_task(self._tail_ttyd_input())

            # 6. Emit tmux_session_info for dashboard
            tmux_metadata = {
                "ttyd_port": self.ttyd_port,
                "tmux_session": self.session_name,
                "tmux_input_path": self._ttyd_input_path,
            }
            if self.remote_host:
                tmux_metadata["ttyd_url"] = f"http://{self.remote_host}:{self.ttyd_port}"
            await self.monitor.emit_event(
                agent_id=self.agent_id, agent_type=self.agent_type,
                event_type="tmux_session_info", working_dir=self.workspace,
                metadata=tmux_metadata,
            )

            # 7. Attach to tmux with PTY wrapper for stdin interception.
            #    This mirrors BaseAdapter's PTY approach: we intercept real stdin,
            #    forward it to tmux, and feed adapter._ingest_stdin_bytes() so
            #    state transitions (idle → in_progress, waiting_for_input response)
            #    work identically to non-tmux mode.
            print(f"[TMUX] Attaching to session '{self.session_name}' (with stdin interception)...", file=sys.stderr)
            await self._attach_with_stdin_intercept()

            # 8. If user detached (session still alive), poll until it ends
            while True:
                check = subprocess.run(
                    ["tmux", "has-session", "-t", self.session_name],
                    capture_output=True,
                )
                if check.returncode != 0:
                    break
                print(f"[TMUX] Session still alive (detached). Polling... (Ctrl+C to stop)", file=sys.stderr)
                await asyncio.sleep(2)

            # 9. Emit agent_stopped
            await self.monitor.emit_event(
                agent_id=self.agent_id, agent_type=self.agent_type,
                event_type="agent_stopped", working_dir=self.workspace,
                metadata={"return_code": 0, "reason": "tmux_session_ended"},
            )
            await self.monitor.emit_event(
                agent_id=self.agent_id, agent_type=self.agent_type,
                event_type="state_change", working_dir=self.workspace,
                metadata={"state": "stopped", "source": "tmux_runner", "return_code": 0},
            )
            self.monitor._agent_stopped_sent = True

        finally:
            # 10. Cleanup: stop adapter, kill ttyd + tmux
            self._stop_adapter_hooks()
            self._cleanup_tmux()

    # ------------------------------------------------------------------
    # Cleanup tmux + ttyd
    # ------------------------------------------------------------------

    def _cleanup_tmux(self):
        """Terminate ttyd and kill tmux session if still alive."""
        # Disable tmux pane output capture
        subprocess.run(["tmux", "pipe-pane", "-O", "-t", self.session_name], capture_output=True)

        if self.ttyd_process and self.ttyd_process.poll() is None:
            try:
                self.ttyd_process.terminate()
                self.ttyd_process.wait(timeout=5)
                print(f"[TMUX] Terminated ttyd (pid={self.ttyd_process.pid})", file=sys.stderr)
            except Exception:
                try:
                    self.ttyd_process.kill()
                except Exception:
                    pass

        check = subprocess.run(
            ["tmux", "has-session", "-t", self.session_name],
            capture_output=True,
        )
        if check.returncode == 0:
            subprocess.run(
                ["tmux", "kill-session", "-t", self.session_name],
                capture_output=True,
            )
            print(f"[TMUX] Killed session '{self.session_name}'", file=sys.stderr)

        if self._tmux_io_dir and os.path.isdir(self._tmux_io_dir):
            shutil.rmtree(self._tmux_io_dir, ignore_errors=True)
