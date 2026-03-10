# AgentViz

AgentViz is a local dashboard for visualizing coding agents (Gemini CLI, Claude Code, Codex CLI, and others) while they run in a workspace. It includes a Python backend (FastAPI + Socket.IO), a React frontend dashboard, per-agent adapters, optional `tmux` + `ttyd` web terminals, and remote viewing support over Tailscale/LAN.

![AgentViz Dashboard](images/AgentVizDashboard.png)
![AgentViz Agent Detail View](images/AgentVizLaunchAgent.png)
![AgentViz Tmux Session](images/AgentVizTmux.png)

## Windows Users

**Windows is not natively supported.** AgentViz relies on Unix-only system modules (`pty`, `termios`, `tty`, `fcntl`) for its PTY-based terminal architecture, which do not exist on Windows.

**Use WSL2 (Windows Subsystem for Linux):**

1. Open PowerShell as Administrator and run:
   ```powershell
   wsl --install
   ```
2. Restart your machine, then open the WSL terminal (Ubuntu by default).
3. Follow the Quick Install steps below from inside WSL.

The backend and frontend run normally inside WSL, and you can access the dashboard from your Windows browser at `http://localhost:8787`.

---

## Quick Install (one command)

```bash
curl -LsSf https://raw.githubusercontent.com/mpatel17-ucsc/AgentViz/main/scripts/install.sh | sh
```

**Requires `git`** — install it first if needed (`xcode-select --install` on macOS, `sudo apt-get install -y git` on Ubuntu/WSL).

This single command:
- Installs `uv` if not already present
- Clones the repo to `~/.local/share/agentviz` (standard XDG location — nothing written to your working directory)
- Creates a Python venv, installs all dependencies, and builds the frontend
- Writes an `agentviz` wrapper to `~/.local/bin/` so you can run `agentviz` from **any directory**
- Asks if you want `~/.local/bin` added to `PATH` in your shell rc automatically

To install to a custom path:

```bash
curl -LsSf https://raw.githubusercontent.com/mpatel17-ucsc/AgentViz/main/scripts/install.sh | sh -s -- ~/my-agentviz
```

If the installer didn't update your shell rc, add this manually:

```bash
export PATH="$HOME/.local/bin:$PATH"   # add to ~/.zshrc or ~/.bashrc
```

> **Note:** `tmux` and `ttyd` are system tools the installer does not manage — install them separately if you plan to use `--tmux-start`:
> - **macOS:** `brew install tmux ttyd`
> - **Ubuntu/WSL:** `sudo apt-get install -y tmux` — for `ttyd`, download from [github.com/tsl0922/ttyd/releases](https://github.com/tsl0922/ttyd/releases): `curl -LO https://github.com/tsl0922/ttyd/releases/latest/download/ttyd.x86_64 && chmod +x ttyd.x86_64 && sudo mv ttyd.x86_64 /usr/local/bin/ttyd`

## Features

- Live agent state tracking (`ready`, `in_progress`, `waiting_for_input`, `completed`, `error`)
- Hooks-based integration for Gemini CLI and Claude Code
- Codex CLI notify-hook integration (via temporary `CODEX_HOME`)
- File activity / subprocess monitoring
- `tmux` + `ttyd` web terminal access for each agent session
- Remote dashboard + web terminal access (Tailscale/LAN)

## Running AgentViz

Open **2 terminal tabs**.

**Tab 1 — Server**
```bash
agentviz server           # local only  → open http://localhost:8787
agentviz server --remote  # Tailscale   → open http://<TAILSCALE_IP>:8787
```

This starts the backend and frontend together on port `8787`. No separate `npm start` needed.

**Tab 2 — Agent**

Replace `<WORKSPACE>` with the directory the agent should work in.

```bash
# Claude Code
agentviz run -w <WORKSPACE> claude
agentviz run -w <WORKSPACE> --tmux-start claude

# Gemini CLI
agentviz run -w <WORKSPACE> gemini
agentviz run -w <WORKSPACE> --tmux-start gemini

# Codex CLI
agentviz run -w <WORKSPACE> codex
agentviz run -w <WORKSPACE> --tmux-start codex
```

> For Tailscale/LAN phone access, add `--remote <TAILSCALE_IP>` to the run command so ttyd terminal links use the right host.

## CLI Reference

### `agentviz server`

Starts the backend and frontend together on a single port.

```bash
agentviz server           # foreground — Ctrl+C to stop
agentviz server start     # background daemon
agentviz server stop      # stop the background daemon
```

| Flag | Default | Description |
|------|---------|-------------|
| `--host <ip>` | `127.0.0.1` | IP address to bind to. |
| `--port <n>` | `8787` | Port for the server. |
| `--remote` | off | Bind to `0.0.0.0` so other devices (Tailscale/LAN) can reach it. |
| `--debug` | off | Show verbose uvicorn and subprocess output. Without it the server runs quietly. |
| `--dev` | off | Use the React dev server (`npm start`) instead of the pre-built frontend. Enables hot-reload for frontend development. |
| `--frontend-port <n>` | `3000` | Dev server port. Only used with `--dev`. |

When running as a daemon (`start`), logs are written to `~/.agentviz/server.log`.

### `agentviz run`

Runs and monitors one coding agent process.

```bash
agentviz run -w <workspace> <agent> [custom-command]
```

| Argument / Flag | Required | Description |
|-----------------|----------|-------------|
| `-w <workspace>` | **Yes** | Directory the agent will work inside. |
| `<agent>` | **Yes** | `claude`, `gemini`, or `codex`. AgentViz resolves the agent type and binary automatically. Pass a custom command after to override (e.g. a full path). |
| `--tmux-start` | No | Run the agent inside a `tmux` session and expose a `ttyd` web terminal. |
| `--remote <ip-or-hostname>` | No | Host embedded in ttyd terminal URLs — needed for phone/Tailscale access with `--tmux-start`. |
| `-i, --id <agent-id>` | No | Custom agent ID shown in the dashboard (default: `<agent>-<pid>`). |

**Examples:**

```bash
# Shorthand — type and command resolved automatically
agentviz run -w ~/myproject claude
agentviz run -w ~/myproject gemini
agentviz run -w ~/myproject codex

# Custom binary path (overrides the default)
agentviz run -w ~/myproject gemini /opt/homebrew/bin/gemini

# With tmux terminal + Tailscale phone access
agentviz run -w ~/myproject --tmux-start --remote 100.x.x.x claude
```

> **`--remote` on `server` vs `run` are different:**
> - `agentviz server --remote` — exposes the dashboard to other devices
> - `agentviz run --remote <ip>` — sets the host in each agent's ttyd terminal URL

### `agentviz update`

Pulls the latest code and rebuilds everything in-place.

```bash
agentviz update
```

### `agentviz build`

Builds the React frontend and bundles it into the package. Run this if you modify the frontend source and want the changes reflected without `--dev`.

```bash
agentviz build
```

## Tailscale Setup (Phone Access)

### On your laptop

1. Install and sign in to Tailscale.
2. Get your Tailscale IP:
```bash
tailscale ip -4
```

### On your phone

1. Install Tailscale and sign into the same tailnet.
2. Open the dashboard:
```
http://<TAILSCALE_IP>:8787
```

Start AgentViz with:
```bash
agentviz server --remote
agentviz run -w <WORKSPACE> --tmux-start --remote <TAILSCALE_IP> claude
```

> Per-agent `ttyd` terminals use dynamically assigned ports — AgentViz shows the links in the dashboard.

## Manual Setup (alternative to Quick Install)

> Skip this section if you used the quick installer — the wrapper handles the venv automatically.

**System requirements:** `python` 3.10+, `node` + `npm`, `git`

```bash
# Python deps
uv sync

# Frontend
agentviz build

# Verify
agentviz --help
```

Or with pip instead of uv:
```bash
python3 -m venv venv && source venv/bin/activate
pip install -e .
agentviz build
```

## Benchmarks

AgentViz was benchmarked against [TmuxCC](https://github.com/nyanko3141592/tmuxcc) and [Agent of Empires](https://github.com/njbrake/agent-of-empires) on approval-detection latency and memory overhead. See [`benchmarks/`](benchmarks/) for the full methodology, results, and reproduction instructions.

## Troubleshooting

- **`nvm installation failed` / frontend build skipped during install**
  - Install Node.js manually from https://nodejs.org (LTS), then run `agentviz build`

- **`Error: Could not connect to AgentViz server at http://localhost:8787`**
  - Start `agentviz server` first — it must run on the same machine as `agentviz run`

- **`tmux not found` / `ttyd not found`**
  - `brew install tmux ttyd` (macOS) — both required for `--tmux-start`

- **Phone shows disconnected**
  - Start the server with `agentviz server --remote`
  - Confirm port `8787` is reachable on your Tailscale IP

- **Phone can't open `http://<ip>:8787`**
  - Start the server with `agentviz server --remote`
  - Confirm both devices are on the same Tailscale tailnet

- **Agent settings files modified unexpectedly**
  - AgentViz writes temporary hook config into the workspace (`.gemini/settings.json` or `.claude/settings.local.json`) and restores it on exit
  - Codex uses a temporary `CODEX_HOME` and does not touch your global config

## Notes

- `agentviz run` connects to `http://localhost:8787` — the server must run on the same machine as the agent.
- For remote access, you view the dashboard and ttyd terminals remotely; the agent process itself still runs on your laptop.
