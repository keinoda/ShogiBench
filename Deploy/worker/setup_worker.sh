#!/usr/bin/env bash

# ShogiBench worker bootstrap, for vast.ai instances or any Debian/Ubuntu box.
#
# Required environment variables:
#   OPENBENCH_SERVER    Server URL, e.g. https://shogibench.fly.dev
#   OPENBENCH_USERNAME  Your ShogiBench username
#   OPENBENCH_PASSWORD  A Worker Key token, created at <server>/workers/
#
# Optional environment variables:
#   SHOGIBENCH_THREADS   Threads for the worker  (default: all cores)
#   SHOGIBENCH_SOCKETS   Number of CPU sockets   (default: 1)
#   SHOGIBENCH_REPO_URL  Repo to fetch the client from
#   SHOGIBENCH_REPO_REF  Branch / tag of the repo
#   SHOGIBENCH_DIR       Where to place the client checkout

set -euo pipefail

: "${OPENBENCH_SERVER:?OPENBENCH_SERVER is required (e.g. https://shogibench.fly.dev)}"
: "${OPENBENCH_USERNAME:?OPENBENCH_USERNAME is required}"
: "${OPENBENCH_PASSWORD:?OPENBENCH_PASSWORD is required (use a Worker Key token)}"

SHOGIBENCH_REPO_URL="${SHOGIBENCH_REPO_URL:-https://github.com/keinoda/ShogiBench}"
SHOGIBENCH_REPO_REF="${SHOGIBENCH_REPO_REF:-shogi}"
SHOGIBENCH_DIR="${SHOGIBENCH_DIR:-$HOME/shogibench-worker}"
SHOGIBENCH_THREADS="${SHOGIBENCH_THREADS:-$(nproc)}"
SHOGIBENCH_SOCKETS="${SHOGIBENCH_SOCKETS:-1}"

export DEBIAN_FRONTEND=noninteractive

SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null; then
    SUDO="sudo"
fi

# Stop any worker started by an earlier run of this script, so re-running
# it (e.g. from the /workers/ page) never leaves two loops behind
for pid in $(pgrep -f '[s]hogibench_setup.sh|[s]etup_worker.sh' 2>/dev/null || true); do
    [ "$pid" != "$$" ] && [ "$pid" != "$PPID" ] && kill "$pid" 2>/dev/null || true
done
pkill -f '[c]lient.py' 2>/dev/null || true

# Install only the packages that are missing
PKGS=""
command -v git     >/dev/null || PKGS="$PKGS git"
command -v curl    >/dev/null || PKGS="$PKGS curl"
command -v make    >/dev/null || PKGS="$PKGS make"
command -v clang++ >/dev/null || PKGS="$PKGS clang"
command -v g++     >/dev/null || PKGS="$PKGS g++"
command -v python3 >/dev/null || PKGS="$PKGS python3"
command -v pip3    >/dev/null || PKGS="$PKGS python3-pip"
command -v pgrep   >/dev/null || PKGS="$PKGS procps"

if [ -n "$PKGS" ]; then
    $SUDO apt-get update -y
    $SUDO apt-get install -y --no-install-recommends $PKGS
fi

# Rust toolchain, required to build the shogitest match runner. Distro
# packages are often too old, so install via rustup when missing.
if [ -f "$HOME/.cargo/env" ]; then
    . "$HOME/.cargo/env"
fi

if ! command -v cargo >/dev/null; then
    curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable
    . "$HOME/.cargo/env"
fi

export PATH="$HOME/.cargo/bin:$PATH"

if [ ! -d "$SHOGIBENCH_DIR" ]; then
    git clone --depth 1 -b "$SHOGIBENCH_REPO_REF" "$SHOGIBENCH_REPO_URL" "$SHOGIBENCH_DIR"
else
    git -C "$SHOGIBENCH_DIR" pull --ff-only || true
fi

cd "$SHOGIBENCH_DIR/Client"

# Newer Debian/Ubuntu images mark the system Python as externally managed
pip3 install --break-system-packages -r requirements.txt 2>/dev/null \
    || pip3 install -r requirements.txt

# Keep the worker alive across transient failures
while true; do
    python3 client.py -T "$SHOGIBENCH_THREADS" -N "$SHOGIBENCH_SOCKETS" || true
    echo "[setup_worker] client exited, restarting in 15s"
    sleep 15
done
