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
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import errno
import subprocess  # for git diff
import subprocess as sp
import hashlib

# Set to True to enable debug output (will break TUI apps like Claude Code)
AGENTVIZ_DEBUG = os.environ.get("AGENTVIZ_DEBUG", "").lower() in ("1", "true", "yes")

# Set to True to disable file watcher entirely (useful when multiple agents share a directory)
# File events will come from OTEL telemetry instead (which is agent-specific)
DISABLE_FILE_WATCHER = os.environ.get("AGENTVIZ_DISABLE_FILE_WATCHER", "").lower() in ("1", "true", "yes")

# Global registry for file ownership across agents
# Maps absolute file path -> (agent_id, timestamp)
# This prevents cross-contamination when multiple agents watch the same directory
_file_ownership_registry = {}
_file_ownership_lock = threading.Lock()
_FILE_OWNERSHIP_TTL = 30.0  # Ownership expires after 30 seconds

# Global registry for subprocess ownership across agents
# Maps pid -> agent_id
# This prevents subprocesses from being attributed to wrong agents
_subprocess_ownership_registry = {}
_subprocess_ownership_lock = threading.Lock()

# Global registry for agent activity tracking
# Maps agent_id -> timestamp of last subprocess/tool activity
# Used to determine which agent should claim file events
_agent_activity_registry = {}
_agent_activity_lock = threading.Lock()
_ACTIVITY_WINDOW = 3.0  # Consider agent "active" for 3 seconds after subprocess activity

# Track which agents are watching which directories
# Maps directory path -> set of agent_ids
_directory_watchers = {}
_directory_watchers_lock = threading.Lock()

# Global cache for file content (for diff generation)
_file_content_cache = {}
_file_content_cache_lock = threading.Lock()

def debug_print(msg, **kwargs):
    """Print debug message only if AGENTVIZ_DEBUG is enabled. Accepts **kwargs for compatibility."""
    if AGENTVIZ_DEBUG:
        kwargs.setdefault("file", sys.stderr)
        print(f"[AgentViz Debug] {msg}", **kwargs)


def get_directory_snapshot(working_dir, recursive=True):
    """
    Create a snapshot of files in directory.
    Works without git - pure Python implementation.
    
    Returns dict: {
        file_path: {
            'mtime': modification_time,
            'size': file_size,
            'hash': content_hash (first 1KB for speed)
        }
    }
    """
    snapshot = {}
    
    try:
        if recursive:
            for root, dirs, files in os.walk(working_dir):
                # Skip hidden dirs and common ignore patterns
                dirs[:] = [d for d in dirs if not d.startswith('.') and
                          d not in ['node_modules', '__pycache__', 'venv', '.venv', 'dist', 'build']]
                
                for filename in files:
                    if filename.startswith('.'):
                        continue
                    
                    file_path = os.path.join(root, filename)
                    try:
                        stat = os.stat(file_path)
                        
                        # Quick hash of first 1KB for change detection
                        file_hash = None
                        if stat.st_size < 1024 * 100:  # Only hash files < 100KB
                            try:
                                with open(file_path, 'rb') as f:
                                    file_hash = hashlib.md5(f.read(1024)).hexdigest()
                            except:
                                pass
                        
                        snapshot[file_path] = {
                            'mtime': stat.st_mtime,
                            'size': stat.st_size,
                            'hash': file_hash
                        }
                    except (OSError, PermissionError):
                        continue
        
        return snapshot
        
    except Exception as e:
        debug_print(f"Error creating snapshot: {e}")
        return {}


def compare_snapshots(before, after):
    """
    Compare two directory snapshots.
    
    Returns dict: {
        'created': set of new files,
        'modified': set of modified files,
        'deleted': set of deleted files
    }
    """
    before_files = set(before.keys())
    after_files = set(after.keys())
    
    created = after_files - before_files
    deleted = before_files - after_files
    
    modified = set()
    common_files = before_files & after_files
    
    for file_path in common_files:
        before_info = before[file_path]
        after_info = after[file_path]
        
        if (before_info['mtime'] != after_info['mtime'] or
            before_info['size'] != after_info['size'] or
            (before_info['hash'] and after_info['hash'] and
             before_info['hash'] != after_info['hash'])):
            modified.add(file_path)
    
    return {
        'created': created,
        'modified': modified,
        'deleted': deleted
    }


def get_file_content_diff(file_path, before_content=None):
    """
    Get a simple diff representation for a file.
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            after_content = f.read()
    except:
        return None
    
    if before_content is None:
        # New file - return with line numbers
        lines = after_content.split('\n')
        diff_lines = [f"+{i+1}: {line}" for i, line in enumerate(lines[:100])]  # First 100 lines
        if len(lines) > 100:
            diff_lines.append(f"... ({len(lines) - 100} more lines)")
        return '\n'.join(diff_lines)
    
    # Modified file - simple line diff
    before_lines = before_content.split('\n')
    after_lines = after_content.split('\n')
    
    diff_lines = []
    max_lines = max(len(before_lines), len(after_lines))
    
    for i in range(min(max_lines, 100)):  # First 100 lines only
        before_line = before_lines[i] if i < len(before_lines) else None
        after_line = after_lines[i] if i < len(after_lines) else None
        
        if before_line != after_line:
            if before_line is not None:
                diff_lines.append(f"-{i+1}: {before_line}")
            if after_line is not None:
                diff_lines.append(f"+{i+1}: {after_line}")
    
    if max_lines > 100:
        diff_lines.append(f"... (diff truncated, {max_lines - 100} more lines)")
    
    return '\n'.join(diff_lines) if diff_lines else None


def is_path_within_dir(path, directory):
    """Return True if path is within directory (after normalization)."""
    try:
        abs_path = os.path.abspath(path)
        abs_dir = os.path.abspath(directory)
        return os.path.commonpath([abs_path, abs_dir]) == abs_dir
    except Exception:
        return False


def cache_file_content(file_path):
    """Cache file content before subprocess modifies it"""
    try:
        if os.path.exists(file_path) and os.path.isfile(file_path):
            # Only cache small text files
            stat = os.stat(file_path)
            if stat.st_size > 1024 * 1024:  # Skip files > 1MB
                return
                
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            with _file_content_cache_lock:
                _file_content_cache[file_path] = content
    except:
        pass


def get_cached_content(file_path):
    """Get cached content for file"""
    with _file_content_cache_lock:
        return _file_content_cache.get(file_path)


def clear_cached_content(file_path):
    """Clear cached content for file"""
    with _file_content_cache_lock:
        if file_path in _file_content_cache:
            del _file_content_cache[file_path]


def register_file_ownership_from_subprocess(command_args, agent_id, working_dir):
    """
    Pre-register file ownership based on subprocess command.
    Helps catch file operations before file watcher sees them.
    """
    editor_commands = ['vim', 'nvim', 'nano', 'emacs', 'code', 'subl', 'vi', 'ed']
    write_commands = ['tee', 'dd', 'cp', 'mv', 'touch', 'cat']
    
    command_str = ' '.join(command_args).lower()
    is_file_command = any(cmd in command_str for cmd in editor_commands + write_commands)
    
    if not is_file_command:
        return
    
    # Extract filenames from command arguments
    for arg in command_args:
        if arg.startswith('-'):
            continue
        
        potential_path = arg if os.path.isabs(arg) else os.path.join(working_dir, arg)
        
        if (os.path.exists(potential_path) or potential_path.startswith(working_dir)) and is_path_within_dir(potential_path, working_dir):
            register_file_ownership(potential_path, agent_id)
            debug_print(f"📝 Pre-registered: {os.path.basename(potential_path)} → {agent_id}")


def get_modified_files_via_git(working_dir):
    """
    Get list of modified files using git (OPTIONAL - fallback if git available).
    Returns set of absolute file paths that have uncommitted changes.
    Returns None if not in a git repo or git command fails.
    """
    try:
        result = sp.run(
            ['git', 'rev-parse', '--git-dir'],
            cwd=working_dir,
            capture_output=True,
            timeout=2
        )
        if result.returncode != 0:
            return None
        
        result = sp.run(
            ['git', 'ls-files', '-m', '-o', '--exclude-standard'],
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=2
        )
        
        if result.returncode == 0:
            files = result.stdout.strip().split('\n')
            files = [f for f in files if f]
            abs_files = {os.path.abspath(os.path.join(working_dir, f)) for f in files}
            return abs_files
        
        return None
    except:
        return None


def register_file_ownership(file_path, agent_id):
    """
    Register that a specific agent instance owns/created a file.
    Used for STRICT INSTANCE-LEVEL ISOLATION.

    The agent_id is the full unique ID (e.g., "gemini-cli-12345"),
    so this ensures only that specific terminal/instance can claim this file.
    """
    abs_path = os.path.abspath(file_path)
    with _file_ownership_lock:
        _file_ownership_registry[abs_path] = (agent_id, time.time())
        debug_print(f"File ownership registered: {abs_path} -> {agent_id} (instance-level)")


def register_agent_activity(agent_id):
    """Mark an agent as having recent subprocess/tool activity."""
    with _agent_activity_lock:
        _agent_activity_registry[agent_id] = time.time()
        debug_print(f"Registered activity for agent: {agent_id}")


def register_directory_watcher(directory, agent_id):
    """Register that an agent instance is watching a directory."""
    abs_dir = os.path.abspath(directory)
    with _directory_watchers_lock:
        if abs_dir not in _directory_watchers:
            _directory_watchers[abs_dir] = set()
        _directory_watchers[abs_dir].add(agent_id)
        watchers = _directory_watchers[abs_dir]
        debug_print(f"Agent instance {agent_id} now watching directory: {abs_dir}")
        if len(watchers) > 1:
            debug_print(f"WARNING: {len(watchers)} agent instances sharing {abs_dir}: {watchers}")
            debug_print(f"STRICT ISOLATION active: file watcher events will be suppressed")


def unregister_directory_watcher(directory, agent_id):
    """Unregister an agent from watching a directory."""
    abs_dir = os.path.abspath(directory)
    with _directory_watchers_lock:
        if abs_dir in _directory_watchers:
            _directory_watchers[abs_dir].discard(agent_id)
            if not _directory_watchers[abs_dir]:
                del _directory_watchers[abs_dir]


def get_agents_watching_directory(directory):
    """Get all agents watching a directory."""
    abs_dir = os.path.abspath(directory)
    with _directory_watchers_lock:
        return _directory_watchers.get(abs_dir, set()).copy()


def get_most_recently_active_agent(agent_ids=None):
    """
    Get the agent with most recent activity within the activity window.
    If agent_ids is provided, only consider those agents.
    Returns (agent_id, timestamp) or (None, 0) if no agent is active.
    """
    with _agent_activity_lock:
        now = time.time()
        candidates = _agent_activity_registry.items()
        if agent_ids:
            candidates = [(aid, ts) for aid, ts in candidates if aid in agent_ids]

        active_agents = [(aid, ts) for aid, ts in candidates
                         if now - ts < _ACTIVITY_WINDOW]

        if not active_agents:
            return (None, 0)

        # Return the most recently active
        return max(active_agents, key=lambda x: x[1])


def should_agent_claim_file_event(file_path, agent_id, working_dir):
    """
    STRICT attribution: Only claim if 100% certain this is our event.
    Better to miss an event than to wrongly attribute it.
    """
    abs_path = os.path.abspath(file_path)
    abs_dir = os.path.abspath(working_dir)

    # Rule 1: Explicit ownership
    with _file_ownership_lock:
        now = time.time()
        expired = [k for k, (_, ts) in _file_ownership_registry.items()
                   if now - ts > _FILE_OWNERSHIP_TTL]
        for k in expired:
            del _file_ownership_registry[k]

        if abs_path in _file_ownership_registry:
            owner_id, ownership_time = _file_ownership_registry[abs_path]
            if owner_id == agent_id:
                debug_print(f"✓ CERTAIN: {os.path.basename(abs_path)} owned by {agent_id}")
                return True
            else:
                debug_print(f"✗ CERTAIN: {os.path.basename(abs_path)} owned by {owner_id}, NOT {agent_id}")
                return False

    # Rule 2: Single watcher
    watchers = get_agents_watching_directory(abs_dir)
    
    if len(watchers) == 0:
        return True
    
    if len(watchers) == 1 and agent_id in watchers:
        debug_print(f"✓ SAFE: {agent_id} is sole watcher")
        return True

    # Rule 3: Multiple watchers - STRICT activity check
    if len(watchers) > 1:
        debug_print(f"⚙ SHARED: {len(watchers)} agents watching")
        
        most_active_agent, activity_timestamp = get_most_recently_active_agent(watchers)
        
        STRICT_WINDOW = 1.0  # 1 second window
        
        now = time.time()
        time_since_activity = now - activity_timestamp if activity_timestamp else float('inf')
        
        if most_active_agent == agent_id and time_since_activity < STRICT_WINDOW:
            debug_print(f"✓ PROBABLE: {agent_id} active {time_since_activity:.2f}s ago")
            return True
        else:
            if most_active_agent and most_active_agent != agent_id:
                debug_print(f"✗ SKIP: Belongs to {most_active_agent}")
            elif time_since_activity >= STRICT_WINDOW:
                debug_print(f"✗ SKIP: Stale activity ({time_since_activity:.2f}s)")
            else:
                debug_print(f"✗ SKIP: No active agent")
            return False
    
    debug_print(f"✗ UNCERTAIN: Cannot determine ownership")
    return False

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

            # Check if this agent should claim this file event
            # Uses activity-based attribution when multiple agents share a directory
            if not should_agent_claim_file_event(file_path, self.adapter.agent_id, self.adapter.working_dir):
                debug_print(f" Skipping file event (another agent is more active): {file_path}")
                if event_key in self.timers:
                    del self.timers[event_key]
                return

            try:
                rel_path = os.path.relpath(file_path, self.adapter.working_dir)
            except ValueError:
                rel_path = file_path

            debug_print(f" File event: {event_type} - {rel_path} (agent: {self.adapter.agent_id})")

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
        self.last_activity_time = time.time()
        self.agent_proc = None
        self.agent_proc_pid = None
        self.agent_proc_returncode = None
        self._agent_tty = None
        self._agent_stopped_emitted = False
        self._user_interrupt_requested = False
        self.pty_master_fd = None
        self.lock = asyncio.Lock()
        self.old_stdin_settings = None
        self.shutdown_event = asyncio.Event()
        self.env = None  # Allow subclasses to set custom environment
        # Deduplication for waiting_for_input: track prompts with timestamps
        self._seen_approval_prompts = {}  # {cleaned_prompt: timestamp}
        self._approval_dedup_window = 10.0  # Ignore same prompt for 10 seconds
        self._last_task_completed_at = 0.0
        # Track if a task is currently in progress (user has provided input)
        self._task_in_progress = False
        # Track when user last provided input
        self._last_user_input_at = 0.0
        # Track waiting_for_input timing and user response
        self._waiting_for_input_since = 0.0
        self._waiting_for_input_response_received = False

        # OTEL-based state detection
        # Track when we last received OTEL work activity (tool_call, token_usage, etc.)
        self._last_otel_activity_at = 0.0
        # How long without OTEL activity before considering agent READY (seconds)
        self._otel_idle_threshold = 5.0
        # Track if we've emitted task_completed for the current idle period
        self._idle_task_completed_emitted = False

        # Screen-based state detection (like AgentAPI)
        # Track recent terminal output for stability detection
        self._screen_buffer = ""  # Recent terminal output
        self._screen_buffer_max = 2000  # Max characters to track
        self._last_screen_change_at = 0.0  # When screen last changed
        # IMPORTANT: Set high duration to avoid flickering - agents may pause while thinking
        self._screen_stability_duration = 15.0  # 15 seconds of no change = stable
        self._last_stable_state_emitted = False  # Track if we emitted stable state
        self._terminal_activity_detected = False  # Track if any terminal activity since last check

        # Process exit flag - prevents ANY events after process exits
        self._process_exited = False

        # Subclasses can set this to True to disable file watcher
        # (when file events come from OTEL instead)
        self._disable_file_watcher = False
        # Subclasses can disable snapshot-based file detection
        # (to avoid cross-agent contamination when using OTEL)
        self._enable_subprocess_snapshot = True

        # Subclasses using hooks for state tracking should set this to True
        # This disables screen-based "ready" detection which causes flickering
        # when agents are thinking but not producing output
        self._use_hooks_for_state = False
        # Allow disabling idle timeout fallback for hook-based adapters
        self._enable_idle_timeout_fallback = True

        # Current internal state (used by idle timeout fallback)
        # Possible values: idle, starting, thinking, in_progress, working, ready, waiting_for_input, stopped
        self._current_state = "idle"

    def _enter_waiting_for_input_state(self):
        """Transition to waiting_for_input and reset response tracking."""
        self._current_state = "waiting_for_input"
        self._waiting_for_input_since = time.time()
        self._waiting_for_input_response_received = False

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
                # Cache controlling TTY (used to detect process handoff)
                try:
                    self._agent_tty = psutil.Process(pid).terminal()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    self._agent_tty = None

                # Save original stdin settings and set cbreak mode
                # cbreak mode passes characters immediately but preserves more terminal behavior
                # than raw mode (like Ctrl+C handling)
                # NOTE: Skip when stdin is not a TTY (e.g. benchmark harness with stdin=DEVNULL)
                loop = asyncio.get_running_loop()
                if sys.stdin.isatty():
                    self.old_stdin_settings = termios.tcgetattr(sys.stdin.fileno())
                    tty.setcbreak(sys.stdin.fileno())
                    loop.add_reader(sys.stdin.fileno(), self._stdin_read_callback)
                else:
                    self.old_stdin_settings = None
                    debug_print(" stdin is not a TTY, skipping cbreak/reader setup", file=sys.stderr)

                # Add PTY reader
                loop.add_reader(self.pty_master_fd, self._pty_read_callback)

                # Start monitors (file watcher disabled globally)
                asyncio.create_task(self.monitor_subprocesses())
                asyncio.create_task(self.activity_monitor_loop())

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
                # Check if user requested interrupt
                if self._user_interrupt_requested and self.shutdown_event.is_set():
                    debug_print(f" User interrupt detected, breaking wait loop", file=sys.stderr)
                    break

                pid, status = os.waitpid(self.agent_proc_pid, os.WNOHANG)
                if pid == 0:
                    await asyncio.sleep(0.2)
                    continue
                self.agent_proc_returncode = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -os.WTERMSIG(status)
                debug_print(f" Process {pid} exited with code {self.agent_proc_returncode}", file=sys.stderr)

                # Some CLIs (e.g., Claude) may hand off to a new process that keeps the same TTY.
                # If we can find another live process on the same TTY, treat it as the new agent PID.
                replacement_pid = None
                if self._agent_tty:
                    try:
                        candidates = []
                        for proc in psutil.process_iter(['pid', 'terminal', 'create_time']):
                            try:
                                if proc.info.get('pid') == pid:
                                    continue
                                if proc.info.get('terminal') == self._agent_tty:
                                    candidates.append(proc)
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                continue
                        if candidates:
                            # Choose the newest process on this TTY
                            candidates.sort(key=lambda p: p.info.get('create_time', 0.0), reverse=True)
                            replacement_pid = candidates[0].info.get('pid')
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        replacement_pid = None

                if replacement_pid:
                    debug_print(f" Detected TTY handoff: adopting PID {replacement_pid} for agent", file=sys.stderr)
                    self.agent_proc_pid = replacement_pid
                    try:
                        self._agent_tty = psutil.Process(replacement_pid).terminal()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                    continue

                # CRITICAL: Set process_exited BEFORE emitting events
                # This blocks all other events from background tasks/hooks
                self._process_exited = True

                if not self._agent_stopped_emitted:
                    await self.emit_event("agent_stopped", {"return_code": self.agent_proc_returncode})
                    # Also emit state_change for consistency with hook-based state tracking
                    await self.emit_event("state_change", {
                        "state": "stopped",
                        "source": "process_exit",
                        "return_code": self.agent_proc_returncode
                    })
                    self._agent_stopped_emitted = True
                break
            except ChildProcessError:
                # Process already reaped or doesn't exist
                debug_print(f" ChildProcessError - process already gone", file=sys.stderr)
                self._process_exited = True
                break

        # Always emit agent_stopped if we didn't already
        if not self._agent_stopped_emitted:
            # Set process_exited flag
            self._process_exited = True
            # Use interrupt return code if user requested it, otherwise use actual return code
            return_code = -signal.SIGINT if self._user_interrupt_requested else (self.agent_proc_returncode or 0)
            debug_print(f" Emitting agent_stopped (fallback) with return_code={return_code}", file=sys.stderr)
            await self.emit_event("agent_stopped", {"return_code": return_code})
            # Also emit state_change for consistency with hook-based state tracking
            await self.emit_event("state_change", {
                "state": "stopped",
                "source": "process_exit",
                "return_code": return_code
            })
            self._agent_stopped_emitted = True
            # Give the event time to be sent over the socket
            await asyncio.sleep(0.1)

        debug_print(f" wait_for_process completed", file=sys.stderr)

    def _pty_read_callback(self):
        try:
            data = os.read(self.pty_master_fd, 1024)
            if data:
                os.write(sys.stdout.fileno(), data)

                output_str = data.decode('utf-8', errors='ignore')
                self._ingest_terminal_output(output_str)

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

    def _ingest_terminal_output(self, output_str):
        """
        Process terminal output for state transitions.
        Used by PTY mode and tmux pane-capture mode.
        """
        if not output_str:
            return

        self.last_activity_time = time.time()
        self._last_screen_change_at = time.time()
        self._terminal_activity_detected = True
        self._last_stable_state_emitted = False  # Reset stable state on new output

        # Update screen buffer for stability detection
        self._screen_buffer += output_str
        if len(self._screen_buffer) > self._screen_buffer_max:
            self._screen_buffer = self._screen_buffer[-self._screen_buffer_max:]

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
            "press enter",
            "continue? (y/n)",
            "continue? [y/n]",
            "proceed? (y/n)",
            "proceed? [y/n]",
        ]

        # NOTE: Task completion is now detected via OTEL idle detection
        # in activity_monitor_loop(), not by parsing terminal output.
        # This is more reliable as OTEL events are the source of truth.

        # Only trigger waiting_for_input for EXPLICIT prompts, not general output.
        # Skip screen-based detection entirely for hook-based adapters (e.g. Gemini):
        # the hooks are the source of truth and screen detection causes spurious
        # waiting_for_input events that snap the state back after the user accepts.
        is_explicit_prompt = (not self._use_hooks_for_state) and any(kw in lower_output for kw in approval_keywords)
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
                # Keep internal state in sync so idle fallback doesn't misclassify this as active work.
                self._enter_waiting_for_input_state()
                asyncio.create_task(self.emit_event("waiting_for_input", {
                    "prompt": output_str.strip()[:300]
                }))
                self._seen_approval_prompts[cleaned_prompt] = current_time
            # Note: Don't reset on non-approval output - that causes duplicates

        # ============================================================
        # REJECTION/CANCELLATION DETECTION
        # ============================================================
        rejection_keywords = [
            "cancelled", "canceled", "rejected", "denied", "aborted",
            "skipping", "skipped", "stopping", "stopped",
            "permission denied", "request cancelled", "request canceled",
            "user denied", "operation cancelled", "operation canceled",
            "action cancelled", "action canceled", "not allowed",
            "won't proceed", "will not proceed", "won't do that",
            "will not do that", "won't make", "will not make",
            "i won't", "i will not", "i'll skip", "i will skip",
            "okay, i won't", "ok, i won't", "okay, i will not",
            "understood, i won't", "understood, i will not",
            "alright, i won't", "alright, i will not",
            "got it, i won't", "got it, i will not",
            "no problem, i won't", "sure, i won't",
            "tool was denied", "tool denied", "action denied",
            "user chose not to", "user declined", "declined by user",
            "approval denied", "changes rejected", "not approved",
            "interrupted", "task cancelled", "task canceled",
            "request denied", "operation stopped",
        ]

        is_rejection = any(kw in lower_output for kw in rejection_keywords)
        if is_rejection and self._current_state == "waiting_for_input":
            # Only treat as rejection AFTER the user has responded to the prompt.
            # This avoids false positives when the prompt itself contains words like "deny".
            if not self._waiting_for_input_response_received:
                debug_print(" Detected rejection keywords in output while waiting_for_input, but no user response yet - ignoring", file=sys.stderr)
                return
            debug_print(f" Detected REJECTION in output while waiting_for_input -> transitioning to READY", file=sys.stderr)
            self._current_state = "ready"
            self._task_in_progress = False
            asyncio.create_task(self.emit_event("task_completed", {
                "reason": "user_rejected",
                "source": "screen_rejection"
            }))
            asyncio.create_task(self.emit_event("state_change", {
                "state": "ready",
                "source": "screen_rejection",
                "detail": "user_rejected_permission"
            }))

    def _stdin_read_callback(self):
        try:
            data = os.read(sys.stdin.fileno(), 1024)
            if data:
                self._ingest_stdin_bytes(data, side_effects=True)
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

    def _ingest_stdin_bytes(self, data, side_effects=True):
        """Process stdin bytes for user-input-driven state transitions."""
        if not data:
            return

        if side_effects and b"\x03" in data:
            self._user_interrupt_requested = True
            # Mark task as no longer in progress
            self._task_in_progress = False
            if self.agent_proc_pid:
                try:
                    os.kill(self.agent_proc_pid, signal.SIGINT)
                except OSError:
                    pass
            # Don't emit here - let wait_for_process handle it
            # This ensures the event is actually sent before cleanup
            self.shutdown_event.set()
            return

        # Detect Escape key press (task cancellation/interruption)
        # Escape = \x1b (but \x1b[ is start of ANSI sequence for arrow keys)
        # Only treat as interrupt if it's bare Escape (not part of sequence)
        if data == b"\x1b" or (b"\x1b" in data and b"\x1b[" not in data):
            if self._current_state in ("waiting_for_input", "in_progress", "working", "thinking"):
                debug_print(f" User pressed Escape (current_state={self._current_state}) - potential cancellation", file=sys.stderr)
                # Don't immediately transition - let the agent handle it
                # But reset the screen change timestamp so idle detection kicks in faster
                self._last_screen_change_at = time.time()
                # Emit event to track the interrupt attempt
                asyncio.create_task(self.emit_event("user_interrupt_attempt", {
                    "key": "escape",
                    "previous_state": self._current_state
                }))

        if side_effects and self.pty_master_fd is not None:
            # Normalize Enter to CR for TUIs that expect carriage return on PTY input.
            if b"\n" in data and b"\r" not in data:
                data = data.replace(b"\n", b"\r")
            os.write(self.pty_master_fd, data)

        # Update activity time
        self.last_activity_time = time.time()

        # When user presses Enter (CR or LF), emit user_prompt event
        # This signals that user has provided input (for state transitions)
        if b"\r" in data or b"\n" in data:
            debug_print(f" User pressed Enter (current_state={self._current_state})", file=sys.stderr)
            now = time.time()

            # IMPORTANT: Distinguish between starting a new task vs responding to a prompt
            # If we're waiting for input (permission dialog), the user is RESPONDING, not starting a new task
            # In this case, we should NOT transition to in_progress - let hooks/idle detect the outcome
            if self._current_state == "waiting_for_input":
                debug_print(f" User responding to permission prompt - NOT transitioning to in_progress", file=sys.stderr)
                # Just update activity timestamps, but don't change state
                self._last_user_input_at = now
                self._waiting_for_input_response_received = True
                self._last_screen_change_at = now
                self._terminal_activity_detected = True
                asyncio.create_task(self.emit_event("user_prompt", {
                    "prompt": "[user response to prompt]"
                }))
            else:
                # User is starting a new task (from ready/idle state)
                debug_print(f" User starting new task - transitioning to in_progress", file=sys.stderr)
                self._task_in_progress = True
                self._current_state = "in_progress"
                self._last_user_input_at = now
                self._last_otel_activity_at = now  # Treat user input as activity
                self._idle_task_completed_emitted = False
                self._last_screen_change_at = now
                self._last_stable_state_emitted = False
                self._terminal_activity_detected = True
                register_agent_activity(self.agent_id)
                asyncio.create_task(self.emit_event("state_change", {
                    "state": "in_progress",
                    "source": "user_input"
                }))
                asyncio.create_task(self.emit_event("user_prompt", {
                    "prompt": "[user input]"
                }))

    async def emit_event(self, event_type, metadata=None):
        metadata = metadata or {}

        # CRITICAL: Block ALL events after process has exited, EXCEPT agent_stopped and state_change(stopped)
        # This prevents late hook events or background tasks from overwriting the "completed" state
        if self._process_exited:
            # Only allow final exit events
            if event_type == 'agent_stopped':
                pass  # Allow
            elif event_type == 'state_change' and metadata.get('state') == 'stopped':
                pass  # Allow
            else:
                debug_print(f" Blocking post-exit event: {event_type}", file=sys.stderr)
                return  # Block all other events

        # OTEL work activity events - these are the SOURCE OF TRUTH for state detection
        # When we receive these, the agent is definitely working
        # Note: state_change with certain states also counts as activity
        otel_work_events = {
            'tool_call', 'subprocess_started', 'token_usage', 'code_generation',
            'file_created', 'file_modified', 'file_operation', 'work_activity',
            'tool_approval', 'tool_result_metadata', 'session_started'
        }

        # Check if this is a state_change indicating active work
        is_active_state_change = (
            event_type == 'state_change' and
            metadata.get('state') in ('in_progress', 'working', 'starting') and
            metadata.get('detail') in ('thinking', 'tool_executing', None)
        )

        if event_type in otel_work_events or is_active_state_change:
            now = time.time()
            self._last_otel_activity_at = now
            self._idle_task_completed_emitted = False  # Reset idle detection

            # IMPORTANT: Do NOT automatically transition to in_progress when in waiting_for_input state
            # The user might have just responded to a permission prompt (denied it), and we should
            # wait for the idle timeout to transition to ready, not assume we're working.
            # Only transition if we see ACTUAL work activity (not just state_change events).
            if self._current_state == "waiting_for_input":
                # When in waiting_for_input, only mark as in_progress for ACTUAL work events
                # (tool execution, file operations) - not just state_change events
                actual_work_events = {'tool_call', 'subprocess_started', 'file_created', 'file_modified', 'file_operation'}
                if event_type in actual_work_events:
                    debug_print(f"[ACTIVITY] Actual work ({event_type}) while waiting_for_input, transitioning to IN_PROGRESS", file=sys.stderr)
                    self._task_in_progress = True
                    self._current_state = "in_progress"
                    # Agent is doing work after approval prompt — user must have responded.
                    # Set this so the notify hook can process agent-turn-complete correctly
                    # (without this, the hook ignores turn-complete when response not detected).
                    self._waiting_for_input_response_received = True
                else:
                    debug_print(f"[ACTIVITY] Ignoring {event_type} while in waiting_for_input (waiting for actual work or idle)", file=sys.stderr)
                    # Don't change state - let idle timeout handle transition to ready
            else:
                if not self._task_in_progress:
                    detail = metadata.get('detail', event_type)
                    debug_print(f"[ACTIVITY] Work activity ({detail}), marking IN_PROGRESS", file=sys.stderr)
                self._task_in_progress = True

        # For file events, check the source to determine if we should emit
        if event_type in ('file_created', 'file_modified', 'file_deleted', 'file_operation'):
            source = metadata.get('source', '')
            # OTEL-based adapters: only allow OTEL-sourced file events
            if self._disable_file_watcher:
                if not str(source).startswith('otel'):
                    debug_print(f" Skipping non-OTEL file event: {event_type} (source={source})", file=sys.stderr)
                    return
            # Non-OTEL adapters: allow all file events (from file watcher)
            # No blocking needed

        # Register file ownership when this agent modifies files
        # This prevents cross-contamination when multiple agents share a directory
        if event_type in ('file_created', 'file_modified', 'file_deleted'):
            file_path = metadata.get('absolute_path') or metadata.get('file_path')
            if file_path:
                abs_path = os.path.join(self.working_dir, file_path) if not os.path.isabs(file_path) else file_path
                if is_path_within_dir(abs_path, self.working_dir):
                    register_file_ownership(abs_path, self.agent_id)

        # Also register ownership from tool_call events that touch files
        if event_type == 'tool_call':
            command = metadata.get('command', '')
            tool_name = metadata.get('tool_name', '').lower()

            # Common file operation patterns
            if tool_name in ('write', 'edit', 'create', 'bash') or \
               any(op in command.lower() for op in ['touch ', 'echo ', '> ', 'cat ', 'cp ', 'mv ', 'mkdir ']):
                # Try to extract file paths from command
                # This is a heuristic - not perfect but helps
                import shlex
                try:
                    parts = shlex.split(command)
                    for part in parts:
                        if '/' in part or '.' in part:
                            # Looks like a file path
                            if not part.startswith('-'):
                                abs_path = os.path.join(self.working_dir, part) if not os.path.isabs(part) else part
                                if is_path_within_dir(abs_path, self.working_dir):
                                    register_file_ownership(abs_path, self.agent_id)
                except ValueError:
                    pass  # shlex parsing failed, ignore

        await self.monitor.emit_event(
            agent_id=self.agent_id,
            agent_type=self.agent_type,
            event_type=event_type,
            working_dir=self.working_dir,
            metadata=metadata,
        )

    async def monitor_workspace(self):
        """
        Monitor workspace for file changes.

        For OTEL-based adapters (Claude, Gemini, Codex), file watcher is DISABLED.
        File events come from OTEL telemetry only, which guarantees perfect isolation
        when multiple agents share a directory (each agent only reports its own actions).
        """
        # Check instance flag (set by subclass) or environment variable
        if self._disable_file_watcher or DISABLE_FILE_WATCHER:
            reason = "adapter uses OTEL" if self._disable_file_watcher else "AGENTVIZ_DISABLE_FILE_WATCHER=1"
            debug_print(f" File watcher DISABLED ({reason})", file=sys.stderr)
            debug_print(f" File events will come from OTEL telemetry only (perfect isolation)", file=sys.stderr)
            # Still need to wait for agent to exit
            while not self.shutdown_event.is_set():
                if self.agent_proc_pid is None or not psutil.pid_exists(self.agent_proc_pid):
                    break
                await asyncio.sleep(1)
            return

        debug_print(f" Starting workspace monitor for: {self.working_dir}", file=sys.stderr)

        # Register this agent as watching this directory
        register_directory_watcher(self.working_dir, self.agent_id)

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
            # Unregister this agent from watching
            unregister_directory_watcher(self.working_dir, self.agent_id)
            debug_print(f" Stopped workspace monitor for: {self.working_dir}")

    async def monitor_subprocesses(self):
        """
        Track subprocess lifecycle with state changes.
        Emits subprocess_started and subprocess_ended events.
        Builds a process tree for visualization.
        Uses ownership registry to prevent cross-contamination between agents.
        """
        tracked_procs = {}  # pid -> {state, started_at, command, parent_pid, process}
        if not self._enable_subprocess_snapshot and AGENTVIZ_DEBUG:
            debug_print(" Subprocess snapshots DISABLED for this adapter", file=sys.stderr)

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
                            # Check if another agent already owns this subprocess
                            with _subprocess_ownership_lock:
                                existing_owner = _subprocess_ownership_registry.get(child.pid)
                                if existing_owner and existing_owner != self.agent_id:
                                    debug_print(f" Skipping subprocess {child.pid} (owned by {existing_owner})")
                                    continue
                                # Register ownership
                                _subprocess_ownership_registry[child.pid] = self.agent_id

                            cmd = child.cmdline()
                            command_str = " ".join(cmd) if cmd else ""

                            # Skip empty commands or very short-lived processes
                            if not command_str:
                                continue

                            # === NEW: Take snapshot before subprocess ===
                            snapshot_before = None
                            if self._enable_subprocess_snapshot:
                                snapshot_before = get_directory_snapshot(self.working_dir, recursive=True)

                                # Cache existing files for diff
                                for file_path in snapshot_before.keys():
                                    cache_file_content(file_path)
                            # === END NEW ===

                            # Verify this is actually a child of our agent process
                            # by checking the parent chain
                            try:
                                proc = psutil.Process(child.pid)
                                parent_chain = []
                                current = proc
                                while current.pid != 1:  # Stop at init
                                    parent_chain.append(current.pid)
                                    if current.pid == self.agent_proc_pid:
                                        break
                                    current = current.parent()
                                    if current is None:
                                        break

                                if self.agent_proc_pid not in parent_chain:
                                    debug_print(f" Subprocess {child.pid} not in our parent chain, skipping")
                                    with _subprocess_ownership_lock:
                                        if child.pid in _subprocess_ownership_registry:
                                            del _subprocess_ownership_registry[child.pid]
                                    continue
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                pass  # Process may have exited, that's ok

                            proc_info = {
                                "pid": child.pid,
                                "parent_pid": child.ppid(),
                                "command": command_str,
                                "state": "running",
                                "started_at": time.time(),
                                "ended_at": None,
                                "exit_code": None,
                                "snapshot_before": snapshot_before,
                            }
                            tracked_procs[child.pid] = proc_info

                            # Register this agent as active (for file attribution)
                            register_agent_activity(self.agent_id)

                            # === NEW: Pre-register file ownership ===
                            register_file_ownership_from_subprocess(cmd, self.agent_id, self.working_dir)
                            # === END NEW ===

                            debug_print(f" Subprocess started: {child.pid} - {command_str[:50]} (agent: {self.agent_id})")

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

                        # === NEW: Check what files changed ===
                        if self._enable_subprocess_snapshot and proc_info.get("snapshot_before") is not None:
                            snapshot_after = get_directory_snapshot(self.working_dir, recursive=True)
                            snapshot_before = proc_info.get("snapshot_before", {})
                            
                            changes = compare_snapshots(snapshot_before, snapshot_after)
                            all_changed_files = changes['created'] | changes['modified']
                            
                            if all_changed_files:
                                debug_print(f"📝 Subprocess {pid} changed {len(all_changed_files)} files")
                                
                                # Register ownership (100% certain)
                                for file_path in all_changed_files:
                                    register_file_ownership(file_path, self.agent_id)
                                    debug_print(f"  ✓ {os.path.basename(file_path)}")
                                
                                # Emit file events
                                for file_path in all_changed_files:
                                    try:
                                        operation = 'created' if file_path in changes['created'] else 'modified'
                                        before_content = None if operation == 'created' else get_cached_content(file_path)
                                        
                                        content = None
                                        if os.path.exists(file_path) and os.path.isfile(file_path):
                                            try:
                                                with open(file_path, 'r', encoding='utf-8') as f:
                                                    content = f.read()
                                            except:
                                                pass
                                        
                                        diff = get_file_content_diff(file_path, before_content)
                                        
                                        await self.emit_event("file_modified", {
                                            "file_path": file_path,
                                            "content": content,
                                            "diff": diff,
                                            "operation": operation,
                                            "modified_by_subprocess": pid,
                                            "command": proc_info["command"],
                                            "source": "filesystem_snapshot",
                                            "certainty": "high"
                                        })
                                        
                                        clear_cached_content(file_path)
                                        
                                    except Exception as e:
                                        debug_print(f"Error emitting file event: {e}")
                                
                                proc_info["modified_files"] = list(all_changed_files)
                        # === END NEW ===

                        debug_print(f"■ Subprocess ended: {pid}")

                        # Emit subprocess_ended event
                        await self.emit_event("subprocess_ended", proc_info)
                        completed_pids.append(pid)

                        # Clean up ownership registry
                        with _subprocess_ownership_lock:
                            if pid in _subprocess_ownership_registry:
                                del _subprocess_ownership_registry[pid]

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
                # Clean up ownership
                with _subprocess_ownership_lock:
                    if pid in _subprocess_ownership_registry:
                        del _subprocess_ownership_registry[pid]

    async def _idle_timeout_fallback_loop(self):
        """
        Lightweight idle detection for hook-based adapters.

        This is a FALLBACK mechanism that catches cases where hooks don't fire:
        - User cancels/denies a permission request (NO HOOK EXISTS FOR THIS)
        - User interrupts a task (Ctrl+C doesn't fire Stop hook)
        - Agent goes back to idle without explicit hook event
        - Hook system fails for some reason

        IMPORTANT RESEARCH FINDINGS:
        - Claude Code: Stop hook does NOT fire on user interrupt (GitHub #9516)
        - Gemini CLI: Ctrl+C terminates entire session (GitHub #3385)
        - Codex CLI: No interrupt hook exists (GitHub #5905)
        - NONE of the agents emit a dedicated event when user denies permission

        Logic:
        - Use SHORTER timeout for waiting_for_input state (user response is quick)
        - Use LONGER timeout for working states (avoid false positives during thinking)
        - When idle detected, transition to "ready"
        """
        if not self._enable_idle_timeout_fallback:
            debug_print("[IDLE_FALLBACK] Disabled by adapter", file=sys.stderr)
            return
        # Different timeouts based on state
        WAITING_FOR_INPUT_TIMEOUT = 3.0  # Short - agent responds quickly after user input
        WORKING_STATE_TIMEOUT = 8.0  # Longer - avoid false positives during thinking

        debug_print(f"[IDLE_FALLBACK] Started (waiting_for_input={WAITING_FOR_INPUT_TIMEOUT}s, working={WORKING_STATE_TIMEOUT}s)", file=sys.stderr)

        while not self.shutdown_event.is_set():
            if self.agent_proc_pid is None or not psutil.pid_exists(self.agent_proc_pid):
                break

            now = time.time()

            # Determine which timeout to use based on current state
            if self._current_state == "waiting_for_input":
                # Only apply the waiting_for_input timeout AFTER the user has responded.
                # If the user has not responded yet, stay in waiting_for_input indefinitely.
                if self._waiting_for_input_response_received:
                    # User just responded (yes or no) - agent will either:
                    # 1. Continue working (approval) -> hooks will fire
                    # 2. Go idle (denial/cancel) -> need fast detection
                    idle_timeout = WAITING_FOR_INPUT_TIMEOUT
                    should_check_idle = True
                else:
                    should_check_idle = False
                    idle_timeout = WAITING_FOR_INPUT_TIMEOUT
            elif self._task_in_progress and self._current_state in ("thinking", "working", "in_progress"):
                idle_timeout = WORKING_STATE_TIMEOUT
                should_check_idle = True
            else:
                should_check_idle = False
                idle_timeout = WORKING_STATE_TIMEOUT

            if should_check_idle:
                time_since_screen = now - self._last_screen_change_at
                time_since_otel = now - self._last_otel_activity_at if self._last_otel_activity_at > 0 else float('inf')
                time_since_activity = min(time_since_screen, time_since_otel)

                # Check if we've been idle for longer than the timeout
                if time_since_activity > idle_timeout:
                    # Double-check: make sure no subprocesses are running
                    has_running_subprocess = False
                    try:
                        if self.agent_proc_pid and psutil.pid_exists(self.agent_proc_pid):
                            parent = psutil.Process(self.agent_proc_pid)
                            children = parent.children(recursive=True)
                            has_running_subprocess = len(children) > 0
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass

                    if not has_running_subprocess:
                        prev_state = self._current_state
                        reason = "user_cancelled" if prev_state == "waiting_for_input" else "idle_timeout_fallback"

                        debug_print(f"[IDLE_FALLBACK] No activity for {time_since_activity:.1f}s (state={prev_state}), transitioning to READY", file=sys.stderr)
                        self._task_in_progress = False
                        self._current_state = "ready"

                        await self.emit_event("task_completed", {
                            "reason": reason,
                            "previous_state": prev_state,
                            "idle_duration": time_since_activity
                        })
                        await self.emit_event("state_change", {
                            "state": "ready",
                            "source": reason,
                            "previous_state": prev_state,
                            "idle_duration": time_since_activity
                        })

            await asyncio.sleep(0.5)  # Check more frequently (was 1.0s)

        debug_print("[IDLE_FALLBACK] Loop ended", file=sys.stderr)

    async def activity_monitor_loop(self):
        """
        Monitor agent activity using screen-based state detection (like AgentAPI).

        For agents using hooks (_use_hooks_for_state=True):
        - Hooks are the PRIMARY source of truth for state transitions
        - BUT we still run a lightweight idle timeout detection as a FALLBACK
        - This catches cases where hooks don't fire (e.g., user cancels/denies permission)

        For non-hook agents:
        1. Track terminal output changes
        2. If output hasn't changed for stability_duration, agent is "stable" (ready)
        3. If output is changing, agent is "running" (in_progress)
        """
        # For hook-based adapters, run a lightweight idle detection as fallback
        if self._use_hooks_for_state:
            if self._enable_idle_timeout_fallback:
                debug_print("[ACTIVITY] Hooks are primary, idle timeout fallback is ENABLED", file=sys.stderr)
            else:
                debug_print("[ACTIVITY] Hooks are primary, idle timeout fallback is DISABLED", file=sys.stderr)
            await self._idle_timeout_fallback_loop()
            return

        debug_print("[ACTIVITY] Screen-based state detection ENABLED (no hooks)", file=sys.stderr)

        while not self.shutdown_event.is_set():
            if self.agent_proc_pid is None or not psutil.pid_exists(self.agent_proc_pid):
                break

            now = time.time()
            time_since_screen_change = now - self._last_screen_change_at

            # Screen-based state detection (fallback for non-hook agents)
            # If terminal output has been stable for the stability duration, agent is ready
            if (self._terminal_activity_detected and
                time_since_screen_change > self._screen_stability_duration and
                not self._last_stable_state_emitted and
                self._task_in_progress):

                # Check for running subprocesses before declaring stable
                has_running_subprocess = False
                try:
                    if self.agent_proc_pid and psutil.pid_exists(self.agent_proc_pid):
                        parent = psutil.Process(self.agent_proc_pid)
                        children = parent.children(recursive=True)
                        # Filter out very short-lived processes
                        has_running_subprocess = len(children) > 0
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

                if not has_running_subprocess:
                    debug_print(f"[SCREEN] Terminal stable for {time_since_screen_change:.1f}s, transitioning to READY", file=sys.stderr)
                    self._task_in_progress = False
                    self._last_stable_state_emitted = True
                    self._idle_task_completed_emitted = True

                    # Emit both task_completed and state_change for compatibility
                    await self.emit_event("task_completed", {"reason": "screen_stable"})
                    await self.emit_event("state_change", {
                        "state": "ready",
                        "source": "screen_stable"
                    })

            # Also check OTEL idle as a secondary signal (only for non-hook agents)
            time_since_otel = now - self._last_otel_activity_at
            if (self._task_in_progress and
                time_since_otel > self._otel_idle_threshold and
                self._last_otel_activity_at > 0 and
                not self._idle_task_completed_emitted):

                # Only use OTEL if screen detection hasn't triggered
                if not self._last_stable_state_emitted:
                    debug_print(f"[OTEL] Idle for {time_since_otel:.1f}s, transitioning to READY", file=sys.stderr)
                    self._task_in_progress = False
                    self._idle_task_completed_emitted = True
                    await self.emit_event("task_completed", {"reason": "otel_idle"})
                    await self.emit_event("state_change", {
                        "state": "ready",
                        "source": "otel_idle"
                    })

            await asyncio.sleep(0.3)  # Check more frequently for responsiveness
