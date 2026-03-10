import sys
import argparse
import asyncio
import os
import subprocess
from agentviz.monitor import Monitor

def _resolve_agent(name):
    """
    Resolve a shorthand agent name to (agent_type, command).
    Tries shutil.which first, then falls back to known install paths.
    Returns None if name is not a known shorthand.
    """
    import shutil
    _KNOWN = {
        'claude': ('claude-code', ['claude']),
        'gemini': ('gemini-cli',  ['gemini', '/opt/homebrew/bin/gemini']),
        'codex':  ('codex-cli',   ['codex']),
    }
    entry = _KNOWN.get(name.lower())
    if entry is None:
        return None
    agent_type, candidates = entry
    for cmd in candidates:
        if shutil.which(cmd) or os.path.isfile(cmd):
            return agent_type, cmd
    # Fall back to the first candidate even if not found (will fail with a clear error at runtime)
    return agent_type, candidates[0]


def _kill_stale_server(port=8787):
    """Kill any existing process listening on the given port."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=5
        )
        pids = result.stdout.strip().split('\n')
        for pid in pids:
            pid = pid.strip()
            if pid and pid.isdigit():
                try:
                    os.kill(int(pid), 9)
                    print(f"Killed stale process {pid} on port {port}")
                except ProcessLookupError:
                    pass
    except Exception:
        pass

def _build_frontend(frontend_dir, install_dir):
    """Build the React frontend and copy the output into agentviz/static/."""
    import shutil
    print("Building frontend...")
    env = os.environ.copy()
    env['CI'] = 'false'  # CRA treats warnings as errors when CI=true
    result = subprocess.run(['npm', 'run', 'build'], cwd=frontend_dir, env=env)
    if result.returncode != 0:
        print("Frontend build failed.", file=sys.stderr)
        return False

    build_dir = os.path.join(frontend_dir, 'build')
    static_dir = os.path.join(install_dir, 'agentviz', 'static')

    if os.path.isdir(static_dir):
        shutil.rmtree(static_dir)
    shutil.copytree(build_dir, static_dir)
    print(f"Frontend built and copied to {static_dir}")
    return True


def build(args):
    """Build the React frontend and embed it into the package for single-port serving."""
    install_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    frontend_dir = os.path.join(install_dir, 'frontend')

    if not os.path.isdir(frontend_dir):
        print("Error: frontend/ directory not found. This command must be run from the repo.", file=sys.stderr)
        sys.exit(1)

    if subprocess.run(['npm', '--version'], capture_output=True).returncode != 0:
        print("Error: npm not found. Install Node.js to build the frontend.", file=sys.stderr)
        sys.exit(1)

    if not os.path.isdir(os.path.join(frontend_dir, 'node_modules')):
        print("Installing frontend dependencies...")
        subprocess.run(['npm', 'install'], cwd=frontend_dir, check=True)

    if _build_frontend(frontend_dir, install_dir):
        print("Done. Run 'agentviz server' to start.")


def update(args):
    """Pull the latest code and reinstall Python + frontend dependencies."""
    # Resolve the repo root: this file lives at <root>/agentviz/cli.py
    install_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    print(f"Updating AgentViz at {install_dir} ...")

    env = os.environ.copy()
    for var in ('UV_PROJECT', 'UV_PROJECT_ENVIRONMENT', 'UV_VENV'):
        env.pop(var, None)

    try:
        # Clean generated files that would block the pull
        import glob as _glob
        for pyc in _glob.glob(os.path.join(install_dir, '**', '*.pyc'), recursive=True):
            try: os.remove(pyc)
            except OSError: pass
        subprocess.run(['git', '-C', install_dir, 'fetch', 'origin', 'main'], check=True)
        subprocess.run(['git', '-C', install_dir, 'reset', '--hard', 'origin/main'], check=True)
    except subprocess.CalledProcessError as e:
        print(f"git update failed: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        uv_path = os.path.join(os.path.dirname(sys.executable), 'uv')
        subprocess.run([uv_path, 'sync'], cwd=install_dir, env=env, check=True)
    except subprocess.CalledProcessError as e:
        print(f"uv sync failed: {e}", file=sys.stderr)
        sys.exit(1)

    frontend_dir = os.path.join(install_dir, 'frontend')
    if os.path.isdir(frontend_dir) and subprocess.run(
        ['npm', '--version'], capture_output=True
    ).returncode == 0:
        subprocess.run(['npm', 'install', '--prefix', frontend_dir, '--silent'], check=True)
        _build_frontend(frontend_dir, install_dir)

    print("AgentViz updated successfully.")


_STATE_DIR = os.path.expanduser("~/.agentviz")
_PID_FILE  = os.path.join(_STATE_DIR, "server.pid")


def _build_procs(args):
    """Resolve commands and URLs for backend + optional frontend."""
    bind_addr    = '0.0.0.0' if args.remote else args.host
    package_dir  = os.path.dirname(__file__)
    frontend_dir = os.path.abspath(os.path.join(package_dir, '..', 'frontend'))
    uvicorn_path = os.path.join(os.path.dirname(sys.executable), 'uvicorn')
    log_level    = 'info' if args.debug else 'error'

    backend_cmd = [uvicorn_path, 'agentviz.server:socket_app',
                   '--host', bind_addr, '--port', str(args.port),
                   '--log-level', log_level]

    static_dir = os.path.join(package_dir, 'static')
    has_static  = os.path.isfile(os.path.join(static_dir, 'index.html'))

    frontend_spec = None
    if not args.dev and has_static:
        frontend_url = f"http://{bind_addr}:{args.port}  (pre-built static)"
    elif os.path.isdir(frontend_dir):
        if not os.path.isdir(os.path.join(frontend_dir, 'node_modules')):
            print("  Installing frontend dependencies...")
            subprocess.run(['npm', 'install'], cwd=frontend_dir, check=True)
        env = os.environ.copy()
        env['HOST']    = bind_addr
        env['PORT']    = str(args.frontend_port)
        env['BROWSER'] = 'none'
        frontend_spec = (['npm', 'start'], frontend_dir, env)
        frontend_url = f"http://{bind_addr}:{args.frontend_port}  (dev server)"
    else:
        print("  Warning: no frontend found. Run 'agentviz build' to build it.")
        frontend_url = None

    return backend_cmd, frontend_spec, bind_addr, frontend_url


def _server_stop():
    import json
    if not os.path.isfile(_PID_FILE):
        print("No running AgentViz server found.")
        return
    with open(_PID_FILE) as f:
        state = json.load(f)
    killed = []
    for name, pid in state.items():
        try:
            os.kill(pid, 15)  # SIGTERM
            killed.append(f"{name} (pid {pid})")
        except ProcessLookupError:
            pass
    os.remove(_PID_FILE)
    print("Stopped: " + ", ".join(killed) if killed else "Server processes already stopped.")


def _server_run(args):
    import json, time
    _kill_stale_server(args.port)
    backend_cmd, frontend_spec, bind_addr, frontend_url = _build_procs(args)

    quiet = not args.debug
    stdio = subprocess.DEVNULL if quiet else None
    processes = []

    os.makedirs(_STATE_DIR, exist_ok=True)

    try:
        backend_proc = subprocess.Popen(backend_cmd, stdout=stdio, stderr=stdio)
        processes.append(backend_proc)
        state = {'backend': backend_proc.pid}

        if frontend_spec:
            cmd, cwd, env = frontend_spec
            fp = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=stdio, stderr=stdio)
            processes.append(fp)
            state['frontend'] = fp.pid

        # Save PIDs so `agentviz server stop` can kill from another terminal
        with open(_PID_FILE, 'w') as f:
            json.dump(state, f)

        print(f"  Dashboard: http://{bind_addr}:{args.port}")
        if frontend_url:
            print(f"  Frontend:  {frontend_url}")
        if args.remote:
            print("  Remote access enabled — listening on all interfaces.")
        print("  Press Ctrl+C or run 'agentviz server stop' to stop.\n")

        while True:
            for p in processes:
                if p.poll() is not None:
                    print(f"\nA server process exited (code {p.returncode}). Shutting down...")
                    return
            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\nShutting down AgentViz...")
    except Exception as e:
        print(f"Failed to start server: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        for p in processes:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        if os.path.isfile(_PID_FILE):
            os.remove(_PID_FILE)
        print("AgentViz stopped.")


def server(args):
    if getattr(args, 'action', None) == 'stop':
        _server_stop()
    else:
        print("Starting AgentViz server...")
        _server_run(args)

def run(args):
    import time
    import signal
    import socketio

    workspace = os.path.abspath(args.w)

    if not os.path.isdir(workspace):
        print(f"Error: Workspace '{workspace}' does not exist or is not a directory.", file=sys.stderr)
        sys.exit(1)

    # Resolve shorthand (e.g. 'claude', 'gemini', 'codex') to type + command
    resolved = _resolve_agent(args.agent)
    if resolved:
        agent_type, default_cmd = resolved
        # Extra args after the shorthand name override the default command
        agent_command = list(args.agent_command) if args.agent_command else [default_cmd]
    else:
        agent_type = args.agent
        agent_command = list(args.agent_command)

    if not agent_command:
        print(f"Error: Missing agent command for '{args.agent}'.", file=sys.stderr)
        print("Usage: agentviz run -w <workspace> <agent>  (e.g. claude, gemini, codex)", file=sys.stderr)
        sys.exit(1)

    agent_id = getattr(args, 'id', None) or f"{agent_type}-{os.getpid()}"
    monitor = Monitor(agent_id, agent_type, agent_command, workspace, tmux_mode=getattr(args, 'tmux_start', False), remote_host=getattr(args, 'remote', None))
    interrupted = False
    error_occurred = False

    def emit_stopped_sync(reason="finished", return_code=0):
        """
        Emit agent_stopped event synchronously.

        CRITICAL: This is the MOST RELIABLE way to ensure the stopped event reaches
        the backend. Async emits during Ctrl+C can be interrupted before they're sent.
        This sync emit happens AFTER asyncio has shut down, using the raw socket.
        """
        try:
            # Create a fresh socket connection if needed
            sio = None
            if monitor.sio.connected:
                sio = monitor.sio
            else:
                # Try to reconnect
                try:
                    sio = socketio.Client()
                    sio.connect("http://localhost:8787", wait_timeout=2)
                except Exception:
                    return

            if sio and sio.connected:
                # Emit agent_stopped
                sio.emit("agent_event", {
                    "agent_id": agent_id,
                    "agent_type": agent_type,
                    "event_type": "agent_stopped",
                    "timestamp": time.time(),
                    "working_dir": workspace,
                    "metadata": {"return_code": return_code, "reason": reason}
                })

                # Also emit state_change for proper frontend update
                sio.emit("agent_event", {
                    "agent_id": agent_id,
                    "agent_type": agent_type,
                    "event_type": "state_change",
                    "timestamp": time.time(),
                    "working_dir": workspace,
                    "metadata": {"state": "stopped", "source": reason, "return_code": return_code}
                })

                # Wait for events to be sent before disconnecting
                time.sleep(0.3)

                # Disconnect if we created a new connection
                if sio != monitor.sio:
                    sio.disconnect()

        except Exception:
            pass

    try:
        asyncio.run(monitor.run())
    except KeyboardInterrupt:
        interrupted = True
        print("\nAgent monitoring interrupted by user.")
    except Exception as e:
        error_occurred = True
        print(f"Error: {e}", file=sys.stderr)
    finally:
        # ALWAYS emit agent_stopped in finally block
        # The async emit during adapter shutdown might not have reached the backend
        # This sync emit is the most reliable way to ensure the state updates
        if interrupted:
            emit_stopped_sync(reason="interrupted", return_code=-2)
        elif error_occurred:
            emit_stopped_sync(reason="error", return_code=1)
        else:
            # Normal completion - still emit to ensure backend knows
            emit_stopped_sync(reason="finished", return_code=0)

        # Disconnect socket
        if monitor.sio.connected:
            try:
                monitor.sio.disconnect()
            except Exception:
                pass

def main():
    parser = argparse.ArgumentParser(description="AgentViz: Unified Visualization for Coding Agents.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Build Command
    parser_build = subparsers.add_parser("build", help="Build the React frontend and embed it in the package (for single-port serving).")
    parser_build.set_defaults(func=build)

    # Update Command
    parser_update = subparsers.add_parser("update", help="Pull the latest AgentViz code and reinstall dependencies.")
    parser_update.set_defaults(func=update)

    # Server Command
    parser_server = subparsers.add_parser("server", help="Start the AgentViz backend + frontend.")
    parser_server.add_argument("action", nargs="?", choices=["start", "stop"], default="start",
                               help="'start' (default) starts the server; 'stop' stops it from another terminal.")
    parser_server.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1).")
    parser_server.add_argument("--port", default=8787, type=int, help="Backend port (default: 8787).")
    parser_server.add_argument("--frontend-port", default=3000, type=int, dest="frontend_port",
                               help="Frontend dev-server port (default: 3000). Only used with --dev.")
    parser_server.add_argument("--remote", action="store_true", help="Bind to 0.0.0.0 for Tailscale/LAN access.")
    parser_server.add_argument("--dev", action="store_true",
                               help="Use npm start dev server instead of the pre-built frontend.")
    parser_server.add_argument("--debug", action="store_true",
                               help="Show verbose uvicorn and subprocess output.")
    parser_server.set_defaults(func=server)

    # Run Command
    parser_run = subparsers.add_parser("run", help="Run a coding agent and monitor it.")
    parser_run.add_argument("-w", required=True, help="Workspace directory for the agent.")
    parser_run.add_argument("-i", "--id", default=None, help="Custom agent ID (default: <agent_type>-<pid>).")
    parser_run.add_argument("--tmux-start", action="store_true", help="Run agent inside a tmux session with a TTYD web terminal.")
    parser_run.add_argument("--remote", metavar="HOSTNAME", default=None, help="Tailscale/LAN hostname for remote access (e.g. 'manav-macbook'). Makes ttyd URLs accessible from other devices.")
    parser_run.add_argument("agent", help="Agent shorthand: claude, gemini, codex. Or a custom type with an explicit command after it.")
    parser_run.add_argument("agent_command", nargs=argparse.REMAINDER, help="Override the default command (optional for known agents).")
    parser_run.set_defaults(func=run)

    args = parser.parse_args()
    if hasattr(args, 'func'):
        args.func(args)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
