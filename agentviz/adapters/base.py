import asyncio
import time
import sys
import psutil
import json
import pty
import os
import select
import termios
import tty
import signal
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import errno
import subprocess  # for git diff

class DebouncedFileSystemEventHandler(FileSystemEventHandler):
    def __init__(self, adapter, loop, debounce_interval=0.3):
        self.adapter = adapter
        self.loop = loop  # Store the event loop reference
        self.debounce_interval = debounce_interval
        self.timers = {}
        self._lock = asyncio.Lock()

    def on_any_event(self, event):
        if event.is_directory:
            return

        # Ignore hidden/temp files
        filename = os.path.basename(event.src_path)
        if filename.startswith('.') or filename.endswith('~') or filename.endswith('.swp'):
            return

        event_key = (event.event_type, event.src_path)

        # Schedule the debounced handling on the event loop thread
        self.loop.call_soon_threadsafe(
            lambda: self._schedule_debounced(event_key, event)
        )

    def _schedule_debounced(self, event_key, event):
        """Called from the event loop thread to handle debouncing"""
        if event_key in self.timers:
            self.timers[event_key].cancel()

        self.timers[event_key] = self.loop.call_later(
            self.debounce_interval,
            lambda: asyncio.create_task(self._handle_event(event))
        )

    async def _handle_event(self, event):
        event_key = (event.event_type, event.src_path)
        event_map = {
            'created': 'file_created',
            'modified': 'file_modified',
            'deleted': 'file_deleted',
        }
        event_type = event_map.get(event.event_type)
        if event_type:
            try:
                rel_path = os.path.relpath(event.src_path, self.adapter.working_dir)
            except ValueError:
                rel_path = event.src_path
            
            print(f"[AgentViz Debug] File event: {event_type} - {rel_path}", file=sys.stderr)
            
            metadata = {"file_path": rel_path, "absolute_path": event.src_path}
            if event.event_type in ['created', 'modified'] and os.path.exists(event.src_path):
                try:
                    metadata['size_bytes'] = os.path.getsize(event.src_path)
                    # Add git diff for line changes (like Vibe Kanban)
                    metadata.update(self._get_git_diff(rel_path))
                except OSError:
                    pass
            
            await self.adapter.emit_event(event_type, metadata)

        if event_key in self.timers:
            del self.timers[event_key]

    def _get_git_diff(self, rel_path):
        try:
            # Assume workspace is git repo — run git diff for lines added/removed
            cmd = ["git", "--no-pager", "diff", "--numstat", rel_path]
            result = subprocess.run(cmd, cwd=self.adapter.working_dir, capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split()
                if len(parts) == 3:
                    added, removed, _ = parts
                    return {"lines_added": int(added), "lines_removed": int(removed)}
        except Exception as e:
            print(f"[GIT DIFF DEBUG] Failed to get diff for {rel_path}: {e}", file=sys.stderr)
        return {"lines_added": 0, "lines_removed": 0}

class BaseAdapter:
    def __init__(self, monitor, agent_id, agent_type, working_dir, command):
        self.monitor = monitor
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.working_dir = working_dir
        self.command = command
        self.thinking = False
        self.last_activity_time = time.time()
        self.agent_proc = None
        self.agent_proc_pid = None
        self.agent_proc_returncode = None
        self.pty_master_fd = None
        self.lock = asyncio.Lock()
        self.old_stdin_settings = None
        self.shutdown_event = asyncio.Event()
        self.env = None  # Allow subclasses to set custom environment
        # Deduplication for waiting_for_input: track prompts with timestamps
        self._seen_approval_prompts = {}  # {cleaned_prompt: timestamp}
        self._approval_dedup_window = 10.0  # Ignore same prompt for 10 seconds

    async def run(self):
        await self.emit_event("agent_started", {"command": " ".join(self.command)})

        cmd = list(self.command)

        print(f"[AgentViz Debug] Launching agent with command: {' '.join(cmd)} using PTY...", file=sys.stderr)

        try:
            # Create a pseudo-terminal
            master_fd, slave_fd = pty.openpty()

            pid = os.fork()

            if pid == 0:  # Child process
                os.close(master_fd)
                # Redirect stdin, stdout, stderr to the slave side of the PTY
                os.dup2(slave_fd, sys.stdin.fileno())
                os.dup2(slave_fd, sys.stdout.fileno())
                os.dup2(slave_fd, sys.stderr.fileno())
                os.close(slave_fd)

                try:
                    os.chdir(self.working_dir)
                    # Use custom environment if provided by subclass
                    if self.env is not None:
                        print(f"[AgentViz Debug] Using custom environment with {len(self.env)} variables", file=sys.stderr)
                        os.execvpe(cmd[0], cmd, self.env)
                    else:
                        os.execvp(cmd[0], cmd)
                except Exception as e:
                    print(f"[AgentViz Debug] Failed to exec agent: {e}", file=sys.stderr)
                    sys.exit(1)

            else:  # Parent process
                os.close(slave_fd)
                self.pty_master_fd = master_fd
                self.agent_proc_pid = pid
                print(f"[AgentViz Debug] Agent process started with PID: {pid} via PTY", file=sys.stderr)

                # Save original stdin settings and set raw mode
                self.old_stdin_settings = termios.tcgetattr(sys.stdin.fileno())
                tty.setraw(sys.stdin.fileno())

                # Add stdin reader to pass input to PTY
                loop = asyncio.get_running_loop()
                loop.add_reader(sys.stdin.fileno(), self._stdin_read_callback)

                # Add PTY reader
                loop.add_reader(self.pty_master_fd, self._pty_read_callback)

                # Start monitors
                asyncio.create_task(self.monitor_workspace())
                asyncio.create_task(self.monitor_subprocesses())
                asyncio.create_task(self.thinking_monitor_loop())

                # Wait for process exit
                await self.wait_for_process()

        except Exception as e:
            print(f"[AgentViz Debug] Error launching agent: {e}", file=sys.stderr)
            await self.emit_event("error", {"message": str(e)})
            raise

        finally:
            print(f"[AgentViz Debug] Starting cleanup", file=sys.stderr)
            self.shutdown_event.set()

            # Restore terminal settings
            if self.old_stdin_settings:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old_stdin_settings)
                print(f"[AgentViz Debug] Terminal settings restored", file=sys.stderr)

            # Remove stdin reader
            loop = asyncio.get_running_loop()
            try:
                loop.remove_reader(sys.stdin.fileno())
            except (ValueError, RuntimeError):
                pass

            # Close PTY
            if self.pty_master_fd:
                try:
                    os.close(self.pty_master_fd)
                except OSError:
                    pass

            print(f"[AgentViz Debug] Cleanup completed", file=sys.stderr)

    async def wait_for_process(self):
        print(f"[AgentViz Debug] Starting wait_for_process for PID {self.agent_proc_pid}", file=sys.stderr)
        while True:
            try:
                pid, status = os.waitpid(self.agent_proc_pid, os.WNOHANG)
                if pid == 0:
                    await asyncio.sleep(0.2)
                    continue
                self.agent_proc_returncode = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -os.WTERMSIG(status)
                print(f"[AgentViz Debug] Process {pid} exited with code {self.agent_proc_returncode}", file=sys.stderr)
                await self.emit_event("agent_stopped", {"return_code": self.agent_proc_returncode})
                break
            except ChildProcessError:
                break
        print(f"[AgentViz Debug] wait_for_process completed", file=sys.stderr)

    def _pty_read_callback(self):
        try:
            data = os.read(self.pty_master_fd, 1024)
            if data:
                self.last_activity_time = time.time()
                os.write(sys.stdout.fileno(), data)

                output_str = data.decode('utf-8', errors='ignore')
                lower_output = output_str.lower()

                approval_keywords = [
                    "approve?", "y/n", "confirmation", "waiting for user confirmation",
                    "allow once", "allow for this session", "no, suggest changes",
                    "modify with external editor"
                ]
                if any(kw in lower_output for kw in approval_keywords):
                    # Normalize prompt for comparison
                    cleaned_prompt = ' '.join(output_str.strip().split()).lower()[:250]
                    current_time = time.time()

                    # Clean up old entries (older than 2x the dedup window)
                    stale_cutoff = current_time - (self._approval_dedup_window * 2)
                    self._seen_approval_prompts = {
                        k: v for k, v in self._seen_approval_prompts.items()
                        if v > stale_cutoff
                    }

                    # Check if we've seen this prompt recently
                    last_seen = self._seen_approval_prompts.get(cleaned_prompt, 0)
                    if current_time - last_seen > self._approval_dedup_window:
                        print(f"[AgentViz Debug] Detected NEW user approval request in output", file=sys.stderr)
                        asyncio.create_task(self.emit_event("waiting_for_input", {
                            "prompt": output_str.strip()[:300]
                        }))
                        self._seen_approval_prompts[cleaned_prompt] = current_time
                    # Note: Don't reset on non-approval output - that causes duplicates

            else:
                print(f"[AgentViz Debug] PTY master detected EOF", file=sys.stderr)
                loop = asyncio.get_running_loop()
                try:
                    loop.remove_reader(self.pty_master_fd)
                except (ValueError, RuntimeError):
                    pass

        except OSError as e:
            if e.errno != errno.EIO:
                print(f"[AgentViz Debug] Error reading from PTY: {e}", file=sys.stderr)
            loop = asyncio.get_running_loop()
            if self.pty_master_fd:
                try:
                    loop.remove_reader(self.pty_master_fd)
                except (ValueError, RuntimeError):
                    pass

    def _stdin_read_callback(self):
        try:
            data = os.read(sys.stdin.fileno(), 1024)
            if data:
                if self.pty_master_fd is not None:
                    os.write(self.pty_master_fd, data)
                else:
                    loop = asyncio.get_running_loop()
                    try:
                        loop.remove_reader(sys.stdin.fileno())
                    except (ValueError, RuntimeError):
                        pass
            else:
                loop = asyncio.get_running_loop()
                try:
                    loop.remove_reader(sys.stdin.fileno())
                except (ValueError, RuntimeError):
                    pass
        except OSError as e:
            print(f"[AgentViz Debug] Error reading from stdin: {e}", file=sys.stderr)
            loop = asyncio.get_running_loop()
            try:
                loop.remove_reader(sys.stdin.fileno())
            except (ValueError, RuntimeError):
                pass

    async def emit_event(self, event_type, metadata=None):
        await self.monitor.emit_event(
            agent_id=self.agent_id,
            agent_type=self.agent_type,
            event_type=event_type,
            working_dir=self.working_dir,
            metadata=metadata or {},
        )

    async def update_thinking_status(self, is_thinking):
        async with self.lock:
            if is_thinking and not self.thinking:
                self.thinking = True
                await self.emit_event("thinking_start", {})
            elif not is_thinking and self.thinking:
                self.thinking = False
                await self.emit_event("thinking_end", {})

    async def monitor_workspace(self):
        print(f"[AgentViz Debug] Starting workspace monitor for: {self.working_dir}", file=sys.stderr)
        loop = asyncio.get_running_loop()
        observer = Observer()
        event_handler = DebouncedFileSystemEventHandler(self, loop)
        observer.schedule(event_handler, self.working_dir, recursive=True)
        observer.start()
        try:
            while not self.shutdown_event.is_set():
                if self.agent_proc_pid is None or not psutil.pid_exists(self.agent_proc_pid):
                    break
                await asyncio.sleep(1)
        finally:
            observer.stop()
            observer.join()

    async def monitor_subprocesses(self):
        seen_pids = set()
        while not self.shutdown_event.is_set():
            if self.agent_proc_pid is None or not psutil.pid_exists(self.agent_proc_pid):
                break
            try:
                parent = psutil.Process(self.agent_proc_pid)
                children = parent.children(recursive=True)
                for child in children:
                    if child.pid not in seen_pids:
                        seen_pids.add(child.pid)
                        try:
                            cmd = child.cmdline()
                            await self.emit_event("tool_call", {
                                "command": " ".join(cmd) if cmd else "",
                                "pid": child.pid
                            })
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
            except psutil.NoSuchProcess:
                break
            except psutil.AccessDenied:
                pass
            await asyncio.sleep(0.5)

    async def thinking_monitor_loop(self):
        while not self.shutdown_event.is_set():
            if self.agent_proc_pid is None or not psutil.pid_exists(self.agent_proc_pid):
                break
            if time.time() - self.last_activity_time > 1.0:
                await self.update_thinking_status(True)
            await asyncio.sleep(0.5)