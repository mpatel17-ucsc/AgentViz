import sys
print(f"[AgentViz Debug] cli.py is being executed!", file=sys.stderr)
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

def server(args):
    print("Starting AgentViz server...")

    # Kill any stale server still holding the port from a previous run
    _kill_stale_server(8787)

    try:
        backend_dir = os.path.join(os.path.dirname(__file__), '..', 'backend')
        uvicorn_path = os.path.join(os.path.dirname(sys.executable), 'uvicorn')

        # --remote overrides bind to 0.0.0.0 so other devices on Tailscale/LAN can connect
        bind_addr = '0.0.0.0' if args.remote else args.bind

        print(f"AgentViz server running at http://{bind_addr}:8787 (Ctrl+C to stop)")
        if args.remote:
            print("Remote access enabled — server listening on all interfaces.")

        # Run server in the foreground so Ctrl+C kills it cleanly
        subprocess.run(
            [uvicorn_path, 'main:socket_app', '--host', bind_addr, '--port', '8787'],
            cwd=backend_dir
        )
    except KeyboardInterrupt:
        print("\nAgentViz server stopped.")
    except Exception as e:
        print(f"Failed to start server: {e}", file=sys.stderr)
        sys.exit(1)

def run(args):
    import time
    import signal
    import socketio

    agent_type = args.agent
    agent_id = f"{agent_type}-{os.getpid()}"
    workspace = os.path.abspath(args.w)

    if not os.path.isdir(workspace):
        print(f"Error: Workspace '{workspace}' does not exist or is not a directory.", file=sys.stderr)
        sys.exit(1)

    if not args.agent_command:
        print(f"Error: Missing agent command for agent '{args.agent}'.", file=sys.stderr)
        print("Usage: agentviz run -w <workspace> <agent_type> <command...>", file=sys.stderr)
        sys.exit(1)

    monitor = Monitor(agent_id, agent_type, args.agent_command, workspace, tmux_mode=getattr(args, 'tmux_mode', False), remote_host=getattr(args, 'remote', None))
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
                    print(f"[AgentViz Debug] Could not reconnect socket", file=sys.stderr)
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

                # CRITICAL: Wait for events to be sent before disconnecting
                time.sleep(0.3)
                print(f"[AgentViz Debug] Emitted agent_stopped for {agent_id} (reason={reason})", file=sys.stderr)

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

    # Server Command
    parser_server = subparsers.add_parser("server", help="Start the AgentViz backend server.")
    parser_server.add_argument("--bind", default="127.0.0.1", help="Address to bind the server to.")
    parser_server.add_argument("--remote", action="store_true", help="Enable remote access (binds to 0.0.0.0 for Tailscale/LAN).")
    parser_server.set_defaults(func=server)

    # Run Command
    parser_run = subparsers.add_parser("run", help="Run a coding agent and monitor it.")
    parser_run.add_argument("-w", required=True, help="Workspace directory for the agent.")
    parser_run.add_argument("--tmux-mode", action="store_true", help="Run agent inside a tmux session with a TTYD web terminal.")
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
