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

# Stream the client's output into the log as it happens. Piped Python
# buffers stdout otherwise, which makes a healthy worker look silent
export PYTHONUNBUFFERED=1

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

# A single failed apt/rustup call must not abort the whole bootstrap and
# leave nothing registered; from here we handle errors ourselves and
# retry, so a transient network hiccup self-heals instead of wedging.
set +e

clang_major() {
    command -v clang++ >/dev/null || { echo 0; return; }
    clang++ --version | grep -oE 'version [0-9]+' | grep -oE '[0-9]+' | head -1
}

install_toolchain() {

    # Install only the packages that are missing
    PKGS=""
    command -v git     >/dev/null || PKGS="$PKGS git"
    command -v curl    >/dev/null || PKGS="$PKGS curl"
    command -v make    >/dev/null || PKGS="$PKGS make"
    command -v g++     >/dev/null || PKGS="$PKGS g++"
    command -v python3 >/dev/null || PKGS="$PKGS python3"
    command -v pip3    >/dev/null || PKGS="$PKGS python3-pip"
    command -v pgrep   >/dev/null || PKGS="$PKGS procps"
    command -v python  >/dev/null || PKGS="$PKGS python-is-python3"

    if [ -n "$PKGS" ]; then
        $SUDO apt-get update -y
        $SUDO apt-get install -y --no-install-recommends $PKGS
    fi

    # The engines require clang++ >= 16, newer than many distro defaults
    # (Ubuntu 22.04 ships clang 14). Pull a modern one from apt.llvm.org
    # and shadow the distro binaries via /usr/local/bin, which precedes them
    if [ "$(clang_major)" -lt 16 ]; then
        echo "[setup_worker] clang++ >= 16 required (found: $(clang_major)), installing clang-18"
        $SUDO apt-get update -y
        $SUDO apt-get install -y --no-install-recommends lsb-release wget gnupg software-properties-common
        curl -sSf https://apt.llvm.org/llvm.sh | $SUDO bash -s -- 18
        [ -x "$(command -v clang++-18)" ] && $SUDO ln -sf "$(command -v clang++-18)" /usr/local/bin/clang++
        [ -x "$(command -v clang-18)"   ] && $SUDO ln -sf "$(command -v clang-18)"   /usr/local/bin/clang
    fi

    # Rust toolchain, required to build the shogitest match runner. Distro
    # packages are often too old, so install via rustup when missing.
    [ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
    if ! command -v cargo >/dev/null; then
        curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable
        [ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
    fi
    export PATH="$HOME/.cargo/bin:$PATH"
}

# Everything the client hard-requires at startup. If any is missing the
# client exits before it ever registers, so verify before launching it.
toolchain_ready() {
    command -v make  >/dev/null || { echo "make missing";        return 1; }
    command -v cargo >/dev/null || { echo "cargo missing";        return 1; }
    { command -v g++ >/dev/null || command -v clang++ >/dev/null; } \
                                || { echo "C++ compiler missing"; return 1; }
    [ "$(clang_major)" -ge 16 ] || { echo "clang++ >= 16 missing (engines need it)"; return 1; }
    return 0
}

# Install, retrying with backoff. A first attempt often fails on a slow
# mirror; without this the worker would spin forever on a broken toolchain.
ATTEMPT=1
while :; do
    install_toolchain
    if reason=$(toolchain_ready); then
        echo "[setup_worker] Toolchain ready"
        break
    fi
    echo "[setup_worker] Toolchain incomplete ($reason); retry $ATTEMPT in 15s"
    ATTEMPT=$((ATTEMPT + 1))
    sleep 15
done

# Normalize the server URL: an http:// URL gets 301-redirected to https,
# which turns the client's POSTs into empty GETs and breaks authentication.
# Follow one redirect and adopt the corrected base URL.
REDIRECT=$(curl -s -o /dev/null -w '%{redirect_url}' "${OPENBENCH_SERVER%/}/clientGetBuildInfo/" || true)
if [ -n "$REDIRECT" ]; then
    OPENBENCH_SERVER="${REDIRECT%clientGetBuildInfo/}"
    export OPENBENCH_SERVER
    echo "[setup_worker] Server redirected; using $OPENBENCH_SERVER instead"
fi

# Fail fast with the server's actual message if the credentials are wrong,
# instead of looping forever inside the client
CHECK=$(curl -s -X POST "${OPENBENCH_SERVER%/}/clientVersionRef/" \
    --data-urlencode "username=$OPENBENCH_USERNAME" \
    --data-urlencode "password=$OPENBENCH_PASSWORD" || true)

case "$CHECK" in
    *client_version*)
        echo "[setup_worker] Credentials verified against $OPENBENCH_SERVER" ;;
    *)
        echo "[setup_worker] Credential check against $OPENBENCH_SERVER failed: ${CHECK:-no response}"
        echo "[setup_worker] Check OPENBENCH_SERVER / OPENBENCH_USERNAME / OPENBENCH_PASSWORD"
        exit 1 ;;
esac

if [ ! -d "$SHOGIBENCH_DIR" ]; then
    git clone --depth 1 -b "$SHOGIBENCH_REPO_REF" "$SHOGIBENCH_REPO_URL" "$SHOGIBENCH_DIR"
else
    git -C "$SHOGIBENCH_DIR" pull --ff-only || true
fi

cd "$SHOGIBENCH_DIR/Client"

# Newer Debian/Ubuntu images mark the system Python as externally managed
pip3 install --break-system-packages -r requirements.txt 2>/dev/null \
    || pip3 install -r requirements.txt

# Keep the worker alive across transient failures. If the client dies
# almost immediately it is a misconfiguration (a missing tool it checks
# at startup), not a transient error, so re-run the toolchain install to
# self-heal instead of spinning forever on the same broken state.
while true; do
    STARTED=$(date +%s)
    python3 client.py -T "$SHOGIBENCH_THREADS" -N "$SHOGIBENCH_SOCKETS" || true
    RAN=$(( $(date +%s) - STARTED ))

    if [ "$RAN" -lt 10 ]; then
        echo "[setup_worker] client exited after ${RAN}s (startup failure); re-checking toolchain"
        install_toolchain
        toolchain_ready || echo "[setup_worker] toolchain still incomplete: $(toolchain_ready)"
    fi

    echo "[setup_worker] client exited, restarting in 15s"
    sleep 15
done
