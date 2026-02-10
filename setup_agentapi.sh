#!/usr/bin/env bash
#
# setup_agentapi.sh — Download (or build) the AgentAPI binary
# Usage: ./setup_agentapi.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXTERNAL_DIR="${SCRIPT_DIR}/external"
AGENTAPI_DIR="${EXTERNAL_DIR}/agentapi"
BINARY="${AGENTAPI_DIR}/agentapi-bin"

AGENTAPI_VERSION="v0.11.8"

mkdir -p "${AGENTAPI_DIR}"

# If binary already exists and is executable, nothing to do
if [ -x "${BINARY}" ]; then
    echo "[setup_agentapi] Binary already exists at ${BINARY}"
    echo "[setup_agentapi] Done."
    exit 0
fi

# Detect OS and architecture for pre-built binary download
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"   # darwin / linux
ARCH="$(uname -m)"

case "${ARCH}" in
    x86_64|amd64)  ARCH="amd64" ;;
    arm64|aarch64)  ARCH="arm64" ;;
    *)
        echo "[setup_agentapi] Unsupported architecture: ${ARCH}"
        exit 1
        ;;
esac

DOWNLOAD_URL="https://github.com/coder/agentapi/releases/download/${AGENTAPI_VERSION}/agentapi-${OS}-${ARCH}"

# Try pre-built binary first (no Go required)
echo "[setup_agentapi] Downloading pre-built binary from ${DOWNLOAD_URL}..."
if curl -fSL --progress-bar -o "${BINARY}" "${DOWNLOAD_URL}"; then
    chmod +x "${BINARY}"
    echo "[setup_agentapi] Binary downloaded to ${BINARY}"
    echo "[setup_agentapi] Done."
    exit 0
else
    echo "[setup_agentapi] Download failed. Trying to build from source..."
fi

# Fallback: build from source if Go is available
if [ ! -d "${AGENTAPI_DIR}/.git" ] && [ ! -f "${AGENTAPI_DIR}/go.mod" ]; then
    echo "[setup_agentapi] Cloning coder/agentapi into ${AGENTAPI_DIR}..."
    # Clone into a temp dir first, then move contents (dir may already have our binary attempt)
    TMPDIR="$(mktemp -d)"
    git clone --depth=1 https://github.com/coder/agentapi "${TMPDIR}/agentapi"
    rm -rf "${TMPDIR}/agentapi/.git"
    cp -R "${TMPDIR}/agentapi/"* "${AGENTAPI_DIR}/"
    rm -rf "${TMPDIR}"
fi

if command -v go &>/dev/null; then
    echo "[setup_agentapi] Building agentapi binary with Go..."
    (cd "${AGENTAPI_DIR}" && go build -o agentapi-bin .)
    echo "[setup_agentapi] Binary built at ${BINARY}"
    echo "[setup_agentapi] Done."
else
    echo "[setup_agentapi] ERROR: Download failed and Go is not installed."
    echo "[setup_agentapi] Install Go (https://go.dev/dl/) or check your network and re-run."
    exit 1
fi
