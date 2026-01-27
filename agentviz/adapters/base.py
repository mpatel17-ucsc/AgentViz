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

# Set to True to enable debug output (will break TUI apps like Claude Code)
AGENTVIZ_DEBUG = os.environ.get("AGENTVIZ_DEBUG", "").lower() in ("1", "true", "yes")

def debug_print(msg, **kwargs):
    """Print debug message only if AGENTVIZ_DEBUG is enabled. Accepts **kwargs for compatibility."""
    if AGENTVIZ_DEBUG:
        print(f"[AgentViz Debug] {msg}", file=sys.stderr)

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

        # For moved events, check destination (this is how Claude atomic writes work)
        if event.event_type == 'moved':
            dest_path = getattr(event, 'dest_path', None)
            if dest_path:
                dest_filename = os.path.basename(dest_path)
                # Skip if destination is hidden/temp
                if dest_filename.startswith('.') or dest_filename.endswith('~') or dest_filename.endswith('.swp'):
                    return
                if '.tmp.' in dest_filename:
                    return
                # Allow this event - it's a temp file being renamed to a real file
                event_key = (event.event_type, dest_path)
                self.loop.call_soon_threadsafe(
                    lambda: self._schedule_debounced(event_key, event)
                )
                return

        # Ignore hidden/temp files
        filename = os.path.basename(event.src_path)
        if filename.startswith('.') or filename.endswith('~') or filename.endswith('.swp'):
            return

        # Ignore Claude Code temp files (pattern: filename.tmp.XXXX.XXXXX)
        if '.tmp.' in filename:
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
            'moved': 'file_modified',  # Treat moved/renamed as modified
        }
        event_type = event_map.get(event.event_type)
        if event_type:
            # For moved events, use dest_path as the actual file path
            if event.event_type == 'moved':
                file_path = getattr(event, 'dest_path', event.src_path)
                # Skip if destination is a temp file
                dest_filename = os.path.basename(file_path)
                if '.tmp.' in dest_filename:
                    return
            else:
                file_path = event.src_path

            try:
                rel_path = os.path.relpath(file_path, self.adapter.working_dir)
            except ValueError:
                rel_path = file_path

            debug_print(f" File event: {event_type} - {rel_path}", file=sys.stderr)

            metadata = {"file_path": rel_path, "absolute_path": file_path}
            if event.event_type in ['created', 'modified', 'moved'] and os.path.exists(file_path):
                try:
                    metadata['size_bytes'] = os.path.getsize(file_path)
                    # Get git diff with actual code changes
                    metadata.update(self._get_git_diff_with_content(rel_path, file_path))
                except OSError:
                    pass

            await self.adapter.emit_event(event_type, metadata)

        if event_key in self.timers:
            del self.timers[event_key]

    def _get_git_diff_with_content(self, rel_path, abs_path):
        """Get git diff with actual code changes"""
        result = {"lines_added": 0, "lines_removed": 0, "diff": None, "content_preview": None}

        try:
            # First try to get the unified diff (actual code changes)
            diff_cmd = ["git", "--no-pager", "diff", "--no-color", "-U3", rel_path]
            diff_result = subprocess.run(
                diff_cmd,
                cwd=self.adapter.working_dir,
                capture_output=True,
                text=True,
                timeout=5
            )

            if diff_result.returncode == 0 and diff_result.stdout.strip():
                diff_text = diff_result.stdout.strip()
                # Limit diff size to prevent huge payloads
                if len(diff_text) > 5000:
                    diff_text = diff_text[:5000] + "\n... (truncated)"
                result["diff"] = diff_text

                # Count added/removed lines from the diff
                lines_added = 0
                lines_removed = 0
                for line in diff_text.split('\n'):
                    if line.startswith('+') and not line.startswith('+++'):
                        lines_added += 1
                    elif line.startswith('-') and not line.startswith('---'):
                        lines_removed += 1
                result["lines_added"] = lines_added
                result["lines_removed"] = lines_removed
            else:
                # No staged diff - might be a new untracked file, read content directly
                numstat_cmd = ["git", "--no-pager", "diff", "--numstat", rel_path]
                numstat_result = subprocess.run(
                    numstat_cmd,
                    cwd=self.adapter.working_dir,
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if numstat_result.returncode == 0 and numstat_result.stdout.strip():
                    parts = numstat_result.stdout.strip().split()
                    if len(parts) >= 2:
                        try:
                            result["lines_added"] = int(parts[0]) if parts[0] != '-' else 0
                            result["lines_removed"] = int(parts[1]) if parts[1] != '-' else 0
                        except ValueError:
                            pass

            # For new/untracked files or if no git diff, read the file content
            if not result["diff"] and os.path.exists(abs_path):
                try:
                    # Check if it's a text file by extension
                    text_extensions = {'.py', '.js', '.ts', '.tsx', '.jsx', '.java', '.c', '.cpp',
                                       '.h', '.hpp', '.go', '.rs', '.rb', '.php', '.swift', '.kt',
                                       '.scala', '.sh', '.bash', '.zsh', '.json', '.yaml', '.yml',
                                       '.toml', '.xml', '.html', '.css', '.scss', '.md', '.txt',
                                       '.sql', '.graphql', '.proto', '.dockerfile', '.env'}

                    _, ext = os.path.splitext(abs_path.lower())
                    if ext in text_extensions or not ext:
                        with open(abs_path, 'r', encoding='utf-8', errors='ignore') as f:
                            content = f.read(3000)  # Read first 3KB
                            if len(content) == 3000:
                                content += "\n... (truncated)"
                            result["content_preview"] = content
                            if result["lines_added"] == 0:
                                result["lines_added"] = content.count('\n') + 1
                except Exception as e:
                    debug_print(f" Could not read file content: {e}", file=sys.stderr)

        except subprocess.TimeoutExpired:
            debug_print(f" Git diff timed out for {rel_path}", file=sys.stderr)
        except Exception as e:
            debug_print(f" Failed to get diff for {rel_path}: {e}", file=sys.stderr)

        return result

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

        debug_print(f" Launching agent with command: {' '.join(cmd)} using PTY...", file=sys.stderr)

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
                        debug_print(f" Using custom environment with {len(self.env)} variables", file=sys.stderr)
                        os.execvpe(cmd[0], cmd, self.env)
                    else:
                        os.execvp(cmd[0], cmd)
                except Exception as e:
                    debug_print(f" Failed to exec agent: {e}", file=sys.stderr)
                    sys.exit(1)

            else:  # Parent process
                os.close(slave_fd)
                self.pty_master_fd = master_fd
                self.agent_proc_pid = pid
                debug_print(f" Agent process started with PID: {pid} via PTY", file=sys.stderr)

                # Save original stdin settings and set cbreak mode
                # cbreak mode passes characters immediately but preserves more terminal behavior
                # than raw mode (like Ctrl+C handling)
                self.old_stdin_settings = termios.tcgetattr(sys.stdin.fileno())
                tty.setcbreak(sys.stdin.fileno())

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
            debug_print(f" Error launching agent: {e}", file=sys.stderr)
            await self.emit_event("error", {"message": str(e)})
            raise

        finally:
            debug_print(f" Starting cleanup", file=sys.stderr)
            self.shutdown_event.set()

            # Restore terminal settings
            if self.old_stdin_settings:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old_stdin_settings)
                debug_print(f" Terminal settings restored", file=sys.stderr)

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

            debug_print(f" Cleanup completed", file=sys.stderr)

    async def wait_for_process(self):
        debug_print(f" Starting wait_for_process for PID {self.agent_proc_pid}", file=sys.stderr)
        while True:
            try:
                pid, status = os.waitpid(self.agent_proc_pid, os.WNOHANG)
                if pid == 0:
                    await asyncio.sleep(0.2)
                    continue
                self.agent_proc_returncode = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -os.WTERMSIG(status)
                debug_print(f" Process {pid} exited with code {self.agent_proc_returncode}", file=sys.stderr)
                await self.emit_event("agent_stopped", {"return_code": self.agent_proc_returncode})
                break
            except ChildProcessError:
                break
        debug_print(f" wait_for_process completed", file=sys.stderr)

    def _pty_read_callback(self):
        try:
            data = os.read(self.pty_master_fd, 1024)
            if data:
                self.last_activity_time = time.time()
                os.write(sys.stdout.fileno(), data)

                output_str = data.decode('utf-8', errors='ignore')
                lower_output = output_str.lower()

                # Keywords for detecting EXPLICIT questions/prompts from the agent
                # Be conservative - only trigger for clear approval/question prompts
                approval_keywords = [
                    # Explicit approval prompts
                    "approve?", "(y/n)", "[y/n]", "yes/no?",
                    "allow once", "allow for this session",
                    "do you want to proceed", "do you want to continue",
                    # Claude Code tool approval
                    "allow once", "allow for this session", "no, suggest changes",
                    "deny", "always allow",
                    # Explicit questions requiring response
                    "waiting for user confirmation",
                    "waiting for your input",
                    "please confirm",
                    "press enter to continue",
                ]

                # Keywords indicating agent finished a task and is ready for next one
                task_complete_keywords = [
                    # Claude Code task completion indicators
                    "what else can i help",
                    "what would you like me to do",
                    "anything else",
                    "let me know if you",
                    "is there anything else",
                    "task complete",
                    "completed successfully",
                    "i've finished",
                    "i have finished",
                    "done!",
                    # Gemini CLI completion indicators
                    "how can i assist you",
                    "what can i help you with",
                ]

                # Check for task completion (agent ready for next task)
                is_task_complete = any(kw in lower_output for kw in task_complete_keywords)
                if is_task_complete:
                    debug_print(f" Detected task completion in output", file=sys.stderr)
                    asyncio.create_task(self.emit_event("task_completed", {}))

                # Only trigger waiting_for_input for EXPLICIT prompts, not general output
                is_explicit_prompt = any(kw in lower_output for kw in approval_keywords)
                if is_explicit_prompt:
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
                        debug_print(f" Detected NEW user approval request in output", file=sys.stderr)
                        asyncio.create_task(self.emit_event("waiting_for_input", {
                            "prompt": output_str.strip()[:300]
                        }))
                        self._seen_approval_prompts[cleaned_prompt] = current_time
                    # Note: Don't reset on non-approval output - that causes duplicates

            else:
                debug_print(f" PTY master detected EOF", file=sys.stderr)
                loop = asyncio.get_running_loop()
                try:
                    loop.remove_reader(self.pty_master_fd)
                except (ValueError, RuntimeError):
                    pass

        except OSError as e:
            if e.errno != errno.EIO:
                debug_print(f" Error reading from PTY: {e}", file=sys.stderr)
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
                    # Update activity time but DON'T emit event on every keystroke
                    # State changes should come from agent output, not user typing
                    self.last_activity_time = time.time()
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
            debug_print(f" Error reading from stdin: {e}", file=sys.stderr)
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
        debug_print(f" Starting workspace monitor for: {self.working_dir}", file=sys.stderr)
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
        """
        Track subprocess lifecycle with state changes.
        Emits subprocess_started and subprocess_ended events.
        Builds a process tree for visualization.
        """
        tracked_procs = {}  # pid -> {state, started_at, command, parent_pid, process}

        while not self.shutdown_event.is_set():
            if self.agent_proc_pid is None or not psutil.pid_exists(self.agent_proc_pid):
                break

            try:
                parent = psutil.Process(self.agent_proc_pid)
                current_children = parent.children(recursive=True)
                current_pids = {child.pid for child in current_children}

                # Detect new subprocesses
                for child in current_children:
                    if child.pid not in tracked_procs:
                        try:
                            cmd = child.cmdline()
                            command_str = " ".join(cmd) if cmd else ""

                            # Skip empty commands or very short-lived processes
                            if not command_str:
                                continue

                            proc_info = {
                                "pid": child.pid,
                                "parent_pid": child.ppid(),
                                "command": command_str,
                                "state": "running",
                                "started_at": time.time(),
                                "ended_at": None,
                                "exit_code": None,
                            }
                            tracked_procs[child.pid] = proc_info

                            debug_print(f" Subprocess started: {child.pid} - {command_str[:50]}", file=sys.stderr)

                            # Emit subprocess_started event
                            await self.emit_event("subprocess_started", proc_info)

                            # Also emit legacy tool_call for backward compatibility
                            await self.emit_event("tool_call", {
                                "command": command_str,
                                "pid": child.pid,
                                "tool_name": "subprocess"
                            })

                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass

                # Detect completed subprocesses
                completed_pids = []
                for pid, proc_info in tracked_procs.items():
                    if pid not in current_pids and proc_info["state"] == "running":
                        proc_info["state"] = "completed"
                        proc_info["ended_at"] = time.time()

                        # Try to get exit code if we can
                        try:
                            # Check if process exists to determine if it ended normally
                            if not psutil.pid_exists(pid):
                                proc_info["exit_code"] = 0  # Assume success if not available
                        except:
                            proc_info["exit_code"] = 0

                        debug_print(f" Subprocess ended: {pid}", file=sys.stderr)

                        # Emit subprocess_ended event
                        await self.emit_event("subprocess_ended", proc_info)
                        completed_pids.append(pid)

                # Clean up completed processes after a delay (keep for tree display)
                # Actually, let's keep them for the session to show in the tree

            except psutil.NoSuchProcess:
                break
            except psutil.AccessDenied:
                pass
            except Exception as e:
                debug_print(f" Error monitoring subprocesses: {e}", file=sys.stderr)

            await asyncio.sleep(0.3)

        # When agent exits, mark all remaining subprocesses as completed
        for pid, proc_info in tracked_procs.items():
            if proc_info["state"] == "running":
                proc_info["state"] = "completed"
                proc_info["ended_at"] = time.time()
                await self.emit_event("subprocess_ended", proc_info)

    async def thinking_monitor_loop(self):
        while not self.shutdown_event.is_set():
            if self.agent_proc_pid is None or not psutil.pid_exists(self.agent_proc_pid):
                break
            if time.time() - self.last_activity_time > 1.0:
                await self.update_thinking_status(True)
            await asyncio.sleep(0.5)