import sys
print(f"[AgentViz Debug] cli.py is being executed!", file=sys.stderr)
import argparse
import asyncio
import os
import subprocess
from agentviz.monitor import Monitor

def server(args):
    print("Starting AgentViz server...")
    try:
        backend_dir = os.path.join(os.path.dirname(__file__), '..', 'backend')
        # Note: Using absolute path for cross-platform compatibility
        uvicorn_path = os.path.join(os.path.dirname(sys.executable), 'uvicorn')
        
        # Start server in the background
        subprocess.Popen(
            [uvicorn_path, 'main:socket_app', '--host', args.bind, '--port', '8787'],
            cwd=backend_dir
        )
        print(f"AgentViz server started at http://{args.bind}:8787")
    except Exception as e:
        print(f"Failed to start server: {e}", file=sys.stderr)
        sys.exit(1)

def run(args):
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

    monitor = Monitor(agent_id, agent_type, args.agent_command, workspace)
    
    try:
        print(f"[AgentViz Debug] Starting asyncio run for monitor.", file=sys.stderr)
        asyncio.run(monitor.run())
        print(f"[AgentViz Debug] Asyncio run for monitor completed.", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nAgent monitoring interrupted by user.")
    finally:
        print(f"\nAgentViz finished monitoring {agent_id}.", file=sys.stderr)
        # Attempt to explicitly stop the event loop as a workaround for hanging
        try:
            loop = asyncio.get_event_loop()
            if not loop.is_closed():
                print(f"[AgentViz Debug] Attempting to stop asyncio loop.", file=sys.stderr)
                loop.stop()
                loop.close()
                print(f"[AgentViz Debug] Asyncio loop stopped and closed.", file=sys.stderr)
        except Exception as e:
            print(f"[AgentViz Debug] Error stopping asyncio loop: {e}", file=sys.stderr)

def main():
    print(f"[AgentViz Debug] sys.path: {sys.path}", file=sys.stderr)
    parser = argparse.ArgumentParser(description="AgentViz: Unified Visualization for Coding Agents.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Server Command
    parser_server = subparsers.add_parser("server", help="Start the AgentViz backend server.")
    parser_server.add_argument("--bind", default="127.0.0.1", help="Address to bind the server to.")
    parser_server.set_defaults(func=server)

    # Run Command
    parser_run = subparsers.add_parser("run", help="Run a coding agent and monitor it.")
    parser_run.add_argument("-w", required=True, help="Workspace directory for the agent.")
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
