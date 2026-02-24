#!/usr/bin/env sh
# AgentViz one-command installer
#
# Usage:
#   curl -LsSf https://raw.githubusercontent.com/mpatel17-ucsc/CSE247B_VisualizationCodingAgents/main/scripts/install.sh | sh -s -- [INSTALL_DIR]
#
# INSTALL_DIR defaults to ~/agentviz if not provided.
# The agentviz binary is placed in <INSTALL_DIR>/bin/agentviz.
# Add that directory to PATH (printed at the end) to use the CLI.

set -eu

REPO_URL="https://github.com/mpatel17-ucsc/CSE247B_VisualizationCodingAgents.git"
INSTALL_DIR="${1:-$HOME/agentviz}"

# Expand leading ~ manually (POSIX sh does not expand ~ in assignments)
case "$INSTALL_DIR" in
  "~"/*) INSTALL_DIR="$HOME/${INSTALL_DIR#\~/}" ;;
  "~")   INSTALL_DIR="$HOME" ;;
esac

BIN_DIR="$INSTALL_DIR/bin"

say() { printf "==> %s\n" "$*"; }
err() { printf "ERROR: %s\n" "$*" >&2; exit 1; }

echo ""
echo "  AgentViz installer"
echo "  Install directory : $INSTALL_DIR"
echo "  Bin directory     : $BIN_DIR"
echo ""

# ---------------------------------------------------------------------------
# 1. Ensure uv is available
# ---------------------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (fast Python package manager)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installs to ~/.local/bin on Linux/macOS
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  command -v uv >/dev/null 2>&1 || err "uv installation failed. Install manually: https://docs.astral.sh/uv/getting-started/installation/"
  say "uv installed: $(uv --version)"
else
  say "uv already installed: $(uv --version)"
fi

# ---------------------------------------------------------------------------
# 2. Clone or update the repo
# ---------------------------------------------------------------------------
if [ -d "$INSTALL_DIR/.git" ]; then
  say "Updating existing repo at $INSTALL_DIR ..."
  git -C "$INSTALL_DIR" pull --ff-only
else
  say "Cloning repo to $INSTALL_DIR ..."
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

# ---------------------------------------------------------------------------
# 3. Install Python deps + agentviz CLI into a project-local venv
# ---------------------------------------------------------------------------
say "Installing Python dependencies (uv sync)..."
cd "$INSTALL_DIR"
uv sync

# ---------------------------------------------------------------------------
# 4. Install frontend npm dependencies (optional — skip if npm missing)
# ---------------------------------------------------------------------------
if command -v npm >/dev/null 2>&1; then
  say "Installing frontend dependencies (npm install)..."
  npm install --prefix frontend --silent
else
  printf "    [skip] npm not found — install Node.js to use the frontend dashboard.\n"
fi

# ---------------------------------------------------------------------------
# 5. Expose agentviz binary in <INSTALL_DIR>/bin/
# ---------------------------------------------------------------------------
say "Creating bin symlink at $BIN_DIR/agentviz ..."
mkdir -p "$BIN_DIR"
ln -sf "$INSTALL_DIR/.venv/bin/agentviz" "$BIN_DIR/agentviz"

# ---------------------------------------------------------------------------
# 6. Detect shell config file for PATH hint
# ---------------------------------------------------------------------------
SHELL_RC=""
case "${SHELL:-}" in
  */zsh)  SHELL_RC="$HOME/.zshrc" ;;
  */bash) SHELL_RC="$HOME/.bashrc" ;;
  *)      SHELL_RC="your shell rc file" ;;
esac

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "======================================================"
echo "  AgentViz installed successfully!"
echo "======================================================"
echo ""
echo "Add agentviz to your PATH by appending this line"
echo "to $SHELL_RC:"
echo ""
echo "    export PATH=\"$BIN_DIR:\$PATH\""
echo ""
echo "Then reload your shell:"
echo "    source $SHELL_RC"
echo ""
echo "Verify:"
echo "    agentviz --help"
echo ""
echo "Quick start (3 terminals):"
echo "    # Terminal 1 — backend"
echo "    agentviz server --remote"
echo ""
echo "    # Terminal 2 — frontend"
echo "    HOST=0.0.0.0 npm start --prefix $INSTALL_DIR/frontend"
echo ""
echo "    # Terminal 3 — agent"
echo "    agentviz run -w <WORKSPACE> --tmux-start --remote <TAILSCALE_IP> gemini-cli /path/to/gemini"
echo ""
