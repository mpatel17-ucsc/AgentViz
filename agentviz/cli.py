import sys
import argparse
import asyncio
import os
import subprocess
from agentviz.monitor import Monitor

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
        subprocess.run(['git', '-C', install_dir, 'pull', '--ff-only'], check=True)
    except subprocess.CalledProcessError as e:
        print(f"git pull failed: {e}", file=sys.stderr)
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


def server(args):
    import time

    print("Starting AgentViz server...")

    bind_addr = '0.0.0.0' if args.remote else args.host

    # Kill any stale server still holding the port from a previous run
    _kill_stale_server(args.port)

    package_dir = os.path.dirname(__file__)  # .../agentviz/
    frontend_dir = os.path.abspath(os.path.join(package_dir, '..', 'frontend'))
    uvicorn_path = os.path.join(os.path.dirname(sys.executable), 'uvicorn')

    processes = []

    try:
        # --- Start backend ---
        backend_proc = subprocess.Popen(
            [uvicorn_path, 'agentviz.server:socket_app',
             '--host', bind_addr, '--port', str(args.port)],
        )
        processes.append(backend_proc)
        print(f"  Backend:  http://{bind_addr}:{args.port}")
        if args.remote:
            print("  Remote access enabled — listening on all interfaces.")

        # --- Start frontend ---
        static_dir = os.path.join(package_dir, 'static')
        has_static = os.path.isfile(os.path.join(static_dir, 'index.html'))

        if not args.dev and has_static:
            # Pre-built static files served by the backend — no extra process needed
            print(f"  Frontend: http://{bind_addr}:{args.port}  (pre-built static)")
        elif os.path.isdir(frontend_dir):
            # Dev mode or no static build: start npm dev server with hot-reload
            if not os.path.isdir(os.path.join(frontend_dir, 'node_modules')):
                print("  Installing frontend dependencies...")
                subprocess.run(['npm', 'install'], cwd=frontend_dir, check=True)

            env = os.environ.copy()
            env['HOST'] = bind_addr
            env['PORT'] = str(args.frontend_port)
            env['BROWSER'] = 'none'  # don't auto-open a browser

            frontend_proc = subprocess.Popen(
                ['npm', 'start'],
                cwd=frontend_dir,
                env=env,
            )
            processes.append(frontend_proc)
            print(f"  Frontend: http://{bind_addr}:{args.frontend_port}  (dev server)")
        else:
            print("  Warning: no frontend found. Run 'agentviz build' to build it.")

        print("  Press Ctrl+C to stop.\n")

        # Wait until any child exits or the user hits Ctrl+C
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
        print("AgentViz stopped.")

def run(args):
    import time
    import signal
    import socketio

    agent_type = args.agent
    agent_id = getattr(args, 'id', None) or f"{agent_type}-{os.getpid()}"
    workspace = os.path.abspath(args.w)

    if not os.path.isdir(workspace):
        print(f"Error: Workspace '{workspace}' does not exist or is not a directory.", file=sys.stderr)
        sys.exit(1)

    if not args.agent_command:
        print(f"Error: Missing agent command for agent '{args.agent}'.", file=sys.stderr)
        print("Usage: agentviz run -w <workspace> <agent_type> <command...>", file=sys.stderr)
        sys.exit(1)

    monitor = Monitor(agent_id, agent_type, args.agent_command, workspace, tmux_mode=getattr(args, 'tmux_start', False), remote_host=getattr(args, 'remote', None))
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

        except Exception as e:
            print(f"[AgentViz Debug] Failed to emit agent_stopped: {e}", file=sys.stderr)

    try:
        print(f"[AgentViz Debug] Starting asyncio run for monitor.", file=sys.stderr)
        asyncio.run(monitor.run())
        print(f"[AgentViz Debug] Asyncio run for monitor completed.", file=sys.stderr)
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
                print(f"[AgentViz Debug] SocketIO disconnected.", file=sys.stderr)
            except Exception:
                pass

        print(f"\nAgentViz finished monitoring {agent_id}.", file=sys.stderr)

def main():
    print(f"[AgentViz Debug] sys.path: {sys.path}", file=sys.stderr)
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
    parser_server.add_argument("--host", default="127.0.0.1", help="Host to bind both servers to (default: 127.0.0.1).")
    parser_server.add_argument("--port", default=8787, type=int, help="Backend port (default: 8787).")
    parser_server.add_argument("--frontend-port", default=3000, type=int, dest="frontend_port",
                               help="Frontend dev-server port (default: 3000). Only used with --dev.")
    parser_server.add_argument("--remote", action="store_true", help="Enable remote access (binds to 0.0.0.0 for Tailscale/LAN).")
    parser_server.add_argument("--dev", action="store_true",
                               help="Force npm start dev server even if a pre-built frontend exists.")
    parser_server.set_defaults(func=server)

    # Run Command
    parser_run = subparsers.add_parser("run", help="Run a coding agent and monitor it.")
    parser_run.add_argument("-w", required=True, help="Workspace directory for the agent.")
    parser_run.add_argument("-i", "--id", default=None, help="Custom agent ID (default: <agent_type>-<pid>).")
    parser_run.add_argument("--tmux-start", action="store_true", help="Run agent inside a tmux session with a TTYD web terminal.")
    parser_run.add_argument("--remote", metavar="HOSTNAME", default=None, help="Tailscale/LAN hostname for remote access (e.g. 'manav-macbook'). Makes ttyd URLs accessible from other devices.")
    parser_run.add_argument("agent", help="The agent to run (e.g., 'gemini-cli', 'claude-code').")
    parser_run.add_argument("agent_command", nargs=argparse.REMAINDER, help="The command to execute the agent.")
    parser_run.set_defaults(func=run)

    args = parser.parse_args()
    if hasattr(args, 'func'):
        args.func(args)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
