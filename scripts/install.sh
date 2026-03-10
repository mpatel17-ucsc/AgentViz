#!/usr/bin/env sh
# AgentViz one-command installer
#
# Usage:
#   curl -LsSf https://raw.githubusercontent.com/mpatel17-ucsc/AgentViz/main/scripts/install.sh | sh -s -- [INSTALL_DIR]
#
# INSTALL_DIR defaults to ~/.local/share/agentviz (XDG standard).
# The agentviz wrapper is always placed in ~/.local/bin.

set -eu

REPO_URL="https://github.com/mpatel17-ucsc/AgentViz.git"

# ---------------------------------------------------------------------------
# Resolve install and bin directories
# ---------------------------------------------------------------------------
XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
if [ -z "${1:-}" ]; then
  INSTALL_DIR="$XDG_DATA_HOME/agentviz"
else
  # Expand leading ~ manually (POSIX sh does not expand ~ in assignments)
  case "$1" in
    "~"/*) INSTALL_DIR="$HOME/${1#\~/}" ;;
    "~")   INSTALL_DIR="$HOME" ;;
    *)     INSTALL_DIR="$1" ;;
  esac
fi
BIN_DIR="$HOME/.local/bin"

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
# 2. Ensure git is available
# ---------------------------------------------------------------------------
if ! command -v git >/dev/null 2>&1; then
  err "git not found. Install it first:
  macOS:  xcode-select --install  (or: brew install git)
  Ubuntu/WSL: sudo apt-get install -y git"
fi

# ---------------------------------------------------------------------------
# 3. Clone or update the repo
# ---------------------------------------------------------------------------
if [ -d "$INSTALL_DIR/.git" ]; then
  say "Updating existing repo at $INSTALL_DIR ..."
  # Clean generated files that confuse git (pyc cache, build artifacts)
  find "$INSTALL_DIR" -name "*.pyc" -delete 2>/dev/null || true
  find "$INSTALL_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
  git -C "$INSTALL_DIR" fetch origin main
  git -C "$INSTALL_DIR" reset --hard origin/main
else
  say "Cloning repo to $INSTALL_DIR ..."
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

# ---------------------------------------------------------------------------
# 4. Install Python deps + agentviz CLI into a project-local venv
# ---------------------------------------------------------------------------
say "Installing Python dependencies (uv sync)..."
cd "$INSTALL_DIR"
# Unset UV env vars that redirect project/venv locations — these would cause
# uv to install into a different directory and break the wrapper script.
unset UV_PROJECT UV_PROJECT_ENVIRONMENT UV_VENV 2>/dev/null || true
uv sync

# ---------------------------------------------------------------------------
# 5. Ensure node/npm is available, install via nvm if not
# ---------------------------------------------------------------------------
if ! command -v npm >/dev/null 2>&1; then
  say "npm not found — installing Node.js via nvm..."
  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | sh
  # Source nvm so it's available in this shell session
  NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
  if command -v nvm >/dev/null 2>&1; then
    nvm install --lts
    nvm use --lts
    say "Node.js installed: $(node --version), npm: $(npm --version)"
  else
    printf "    [skip] nvm installation failed — install Node.js manually (https://nodejs.org) then run: npm install --prefix %s/frontend\n" "$INSTALL_DIR"
  fi
fi

if command -v npm >/dev/null 2>&1; then
  say "Building frontend (npm install + npm run build)..."
  "$INSTALL_DIR/.venv/bin/agentviz" build
fi

# ---------------------------------------------------------------------------
# 6. Write wrapper script so users never need to activate the venv manually
# ---------------------------------------------------------------------------
say "Writing agentviz wrapper to $BIN_DIR/agentviz ..."
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/agentviz" <<WRAPPER
#!/bin/sh
exec "$INSTALL_DIR/.venv/bin/agentviz" "\$@"
WRAPPER
chmod +x "$BIN_DIR/agentviz"

# ---------------------------------------------------------------------------
# 7. Offer to append BIN_DIR to PATH in the user's shell rc (ask first)
# ---------------------------------------------------------------------------
SHELL_RC=""
case "${SHELL:-}" in
  */zsh)  SHELL_RC="$HOME/.zshrc" ;;
  */bash) SHELL_RC="$HOME/.bashrc" ;;
  *)      SHELL_RC="your shell rc file" ;;
esac

case ":${PATH}:" in
  *":$BIN_DIR:"*) ;; # already on PATH — nothing to do
  *)
    printf "\nAdd '%s' to PATH in %s? [y/N] " "$BIN_DIR" "$SHELL_RC"
    read -r _answer
    case "$_answer" in
      [yY]*)
        printf '\n# Added by AgentViz installer\nexport PATH="%s:$PATH"\n' "$BIN_DIR" >> "$SHELL_RC"
        say "Appended to $SHELL_RC — run: source $SHELL_RC"
        ;;
      *)
        echo "Skipped. Add manually: export PATH=\"$BIN_DIR:\$PATH\""
        ;;
    esac
    ;;
esac

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "======================================================"
echo "  AgentViz installed successfully!"
echo "======================================================"
echo ""
echo "Verify:"
echo "    agentviz --help"
echo ""
echo "Quick start (2 terminals):"
echo "    # Terminal 1 — server (backend + frontend)"
echo "    agentviz server                        # local only"
echo "    agentviz server --remote               # expose to Tailscale/LAN"
echo ""
echo "    # Terminal 2 — agent"
echo "    agentviz run -w <WORKSPACE> --tmux-start --remote <TAILSCALE_IP> gemini-cli /opt/homebrew/bin/gemini"
echo ""
