#!/usr/bin/env sh
# AgentViz one-command installer
#
# Usage:
#   curl -LsSf https://raw.githubusercontent.com/mpatel17-ucsc/AgentViz/main/scripts/install.sh | sh -s -- [INSTALL_DIR]
#
# INSTALL_DIR defaults to ~/.local/share/agentviz (XDG) when ~/.local exists,
# otherwise ~/agentviz. The agentviz wrapper is placed in ~/.local/bin or
# <INSTALL_DIR>/bin respectively.

set -eu

REPO_URL="https://github.com/mpatel17-ucsc/AgentViz.git"

# ---------------------------------------------------------------------------
# Resolve install and bin directories
# ---------------------------------------------------------------------------
if [ -z "${1:-}" ]; then
  INSTALL_DIR="$PWD/agentviz"
else
  INSTALL_DIR="$1"
fi
BIN_DIR="$HOME/.local/bin"

# Expand leading ~ manually in INSTALL_DIR (POSIX sh does not expand ~ in assignments)
case "$INSTALL_DIR" in
  "~"/*) INSTALL_DIR="$HOME/${INSTALL_DIR#\~/}" ;;
  "~")   INSTALL_DIR="$HOME" ;;
esac

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
# Unset UV env vars that redirect project/venv locations — these would cause
# uv to install into a different directory and break the wrapper script.
unset UV_PROJECT UV_PROJECT_ENVIRONMENT UV_VENV 2>/dev/null || true
uv sync

# ---------------------------------------------------------------------------
# 4. Ensure node/npm is available, install via nvm if not
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
  say "Installing frontend dependencies (npm install)..."
  npm install --prefix frontend --silent
fi

# ---------------------------------------------------------------------------
# 5. Write wrapper script so users never need to activate the venv manually
# ---------------------------------------------------------------------------
say "Writing agentviz wrapper to $BIN_DIR/agentviz ..."
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/agentviz" <<WRAPPER
#!/bin/sh
exec "$INSTALL_DIR/.venv/bin/agentviz" "\$@"
WRAPPER
chmod +x "$BIN_DIR/agentviz"

# ---------------------------------------------------------------------------
# 6. Offer to append BIN_DIR to PATH in the user's shell rc (ask first)
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
echo "Quick start (3 terminals):"
echo "    # Terminal 1 — backend"
echo "    agentviz server --remote"
echo ""
echo "    # Terminal 2 — frontend"
echo "    cd $INSTALL_DIR/frontend"
echo "    HOST=0.0.0.0 npm start"
echo ""
echo "    # Terminal 3 — agent"
echo "    agentviz run -w <WORKSPACE> --tmux-start --remote <TAILSCALE_IP> gemini-cli /opt/homebrew/bin/gemini"
echo ""
