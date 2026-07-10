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
#
# One machine runs at most ONE worker: re-running this script takes over
# (stops any previous bootstrap loop, its installers, and its client) before
# starting fresh. The client additionally holds a lock file, so even a loop
# this script cannot see can never produce a second concurrent worker.
#
# Exit-code contract with client.py (worker.py):
#   65  another worker already owns this machine -> do not restart
#   66  worker key was disabled/deleted, or openbench.exit -> do not restart

PIDFILE="$HOME/.shogibench-worker.pgid"
BOOTLOCK="$HOME/.shogibench-boot.lock"

self_pgid() {
    ps -o pgid= -p $$ 2>/dev/null | tr -d ' '
}

acquire_boot_lock() {

    # 接続 POST が同時に2つ届くなどで bootstrap が並走すると、互いを
    # 「前回の起動」と見なして殺し合う。起動処理 (前回の掃除〜pidfile 記録)
    # を flock で直列化し、後から来た方は「既に起動中」として静かに終了する。
    # ロックは起動処理の間だけ保持するので、時間を置いた再接続による
    # takeover はこれまで通り機能する。保持者が死ねば自動で解放される
    command -v flock >/dev/null 2>&1 || return 0
    : >>"$BOOTLOCK" 2>/dev/null || return 0
    exec 9>>"$BOOTLOCK"
    if ! flock -n 9; then
        echo "[setup_worker] another bootstrap is starting right now; exiting"
        exit 0
    fi
}

release_boot_lock() {
    exec 9>&- 2>/dev/null || true
}

cleanup_pidfile() {

    # 自分が記録した pidfile だけを消す。takeover で殺された側の EXIT トラップ
    # が、新しい bootstrap の記録したばかりの pidfile を消してしまわないように
    [ "$(cat "$PIDFILE" 2>/dev/null || true)" = "$$" ] && rm -f "$PIDFILE"
    return 0
}

pgid_looks_like_worker() {

    # pidfile の pgid がまだ本当に shogibench 系のグループかを確かめる。
    # 再起動などで pid 番号が再利用されると、記録された番号が無関係な
    # プロセス群 (sshd や dockerd 等) を指すことがあり、無検証で kill
    # できない。メンバーのコマンドラインか作業ディレクトリで判定する。
    # 素の 'client.py' はパターンに入れない: 本物のワーカー群は必ず
    # shogibench 系のパス/名前を持つ (無関係な同名スクリプトを守るため)
    local pgid="$1" pid args cwd
    for pid in $(pgrep -g "$pgid" 2>/dev/null || true); do
        args=$(ps -o args= -p "$pid" 2>/dev/null || true)
        cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null || true)
        case "$args $cwd" in
            *shogibench*|*ShogiBench*|*setup_worker*)            return 0 ;;
            *"${SHOGIBENCH_DIR:-/nonexistent-shogibench-dir}"*)  return 0 ;;
        esac
    done
    return 1
}

shogibench_client_pid() {

    # cmdline か作業ディレクトリが shogibench 系の client.py だけを対象に
    # する。無関係なプロジェクトのたまたま同名の client.py を巻き込まない
    local pid="$1" args cwd
    args=$(ps -o args= -p "$pid" 2>/dev/null || true)
    cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null || true)
    case "$args $cwd" in
        *shogibench*|*ShogiBench*)                           return 0 ;;
        *"${SHOGIBENCH_DIR:-/nonexistent-shogibench-dir}"*)  return 0 ;;
    esac
    return 1
}

self_ancestors() {

    # 自分の祖先 PID の一覧 (sshd やログ収集シェルなど)。コマンドラインに
    # たまたま setup_worker.sh の文字列を含む祖先を巻き込み殺さないための除外リスト。
    # 以下の掃除経路の ps/cat は、対象 pid が pgrep との間に消えると失敗し得る。
    # set -e 下で bootstrap ごと静かに死なないよう、全て `|| true` で吸収する
    local pid=$$ ppid
    while [ -n "$pid" ] && [ "$pid" != "1" ] && [ "$pid" != "0" ]; do
        ppid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ' || true)
        [ -n "$ppid" ] || break
        echo "$ppid"
        pid="$ppid"
    done
}

protected_pids() {

    # サーバーの SSH 起動用シェルなど、親子関係から外れて見えることがある
    # プロセスも明示的に保護する
    printf '%s\n' ${SHOGIBENCH_PROTECTED_PIDS:-}
    self_ancestors
}

is_protected_pid() {
    local pid="$1"
    [ "$pid" = "$$" ] && return 0
    case " $PROTECTED_PIDS " in
        *" $pid "*) return 0 ;;
    esac
    return 1
}

is_protected_group() {
    local pgid="$1" pid ppid
    [ "$pgid" = "$(self_pgid)" ] && return 0
    for pid in $PROTECTED_PIDS; do
        [ -n "$pid" ] || continue
        ppid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)
        [ "$ppid" = "$pgid" ] && return 0
    done
    return 1
}

kill_group() {

    # Terminate every member of a process group. The plain group signal
    # comes first, backed up by pkill: some sandboxed/containerized
    # environments deliver kill(-pgid) to the leader only. Callers ensure
    # the group is neither ours nor an ancestor's, so pkill -g is safe
    local pgid="$1"
    kill -TERM -- "-$pgid" 2>/dev/null || true
    pkill -TERM -g "$pgid" 2>/dev/null || true
}

kill_group_or_pid() {

    # Kill a process group if we can identify it (takes out installers and
    # engines too); fall back to the single pid. Never touch pgid 1, our own
    # group, or a group led by one of our ancestors
    local pid="$1" pgid
    pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)

    case "$pgid" in
        ''|*[!0-9]*) pgid="" ;;
    esac

    if [ -n "$pgid" ] && [ "$pgid" != "1" ] \
            && ! is_protected_group "$pgid" && ! is_protected_pid "$pgid"; then
        kill_group "$pgid"
    else
        kill -TERM "$pid" 2>/dev/null || true
    fi
}

stop_previous_workers() {

    # 自分と自分の祖先は絶対に殺さない
    PROTECTED_PIDS="$(protected_pids | tr '\n' ' ' || true)"

    # 1) Modern bootstraps record their process group here; killing the
    #    group stops the loop, its installers, the client, and any engines.
    #    再起動後の pid 再利用で無関係なグループを指している場合は殺さず、
    #    stale な記録として捨てる
    if [ -f "$PIDFILE" ]; then
        local oldpgid
        oldpgid=$(cat "$PIDFILE" 2>/dev/null | tr -d ' ' || true)
        case "$oldpgid" in
            ''|*[!0-9]*) : ;;
            1) : ;;
            *)
                if ! pgid_looks_like_worker "$oldpgid"; then
                    rm -f "$PIDFILE" 2>/dev/null || true
                elif ! is_protected_group "$oldpgid" && ! is_protected_pid "$oldpgid"; then
                    kill_group "$oldpgid"
                fi ;;
        esac
    fi

    # 2) Older bootstraps, found by script name in the command line
    local pid
    for pid in $(pgrep -f '[s]hogibench_setup.sh|[s]etup_worker.sh' 2>/dev/null || true); do
        is_protected_pid "$pid" && continue
        kill_group_or_pid "$pid"
    done

    # 3) Bootstraps started via `curl | bash` show up as a bare "bash" and
    #    are invisible to (2); find their restart loop through the running
    #    client's parent shell instead, then stop the client itself.
    #    無関係なプロジェクトの同名 client.py は対象にしない
    local cpid ppid pcomm
    for cpid in $(pgrep -f '[c]lient.py' 2>/dev/null || true); do
        is_protected_pid "$cpid" && continue
        shogibench_client_pid "$cpid" || continue
        ppid=$(ps -o ppid= -p "$cpid" 2>/dev/null | tr -d ' ' || true)
        if [ -n "$ppid" ] && [ "$ppid" != "1" ] && ! is_protected_pid "$ppid"; then
            pcomm=$(ps -o comm= -p "$ppid" 2>/dev/null | tr -d ' ' || true)
            case "$pcomm" in
                bash|sh|dash) kill_group_or_pid "$ppid" ;;
            esac
        fi
        kill -TERM "$cpid" 2>/dev/null || true
    done

    # 4) Give everything a moment to exit, then finish off stragglers, so
    #    the new run never races an old apt/dpkg lock or a half-dead client
    local i stray
    for i in 1 2 3 4 5 6 7 8 9 10; do
        stray=""
        for cpid in $(pgrep -f '[c]lient.py' 2>/dev/null || true); do
            is_protected_pid "$cpid" && continue
            if shogibench_client_pid "$cpid"; then
                stray="$cpid"
                break
            fi
        done
        [ -n "$stray" ] || break
        sleep 1
    done
    for cpid in $(pgrep -f '[c]lient.py' 2>/dev/null || true); do
        is_protected_pid "$cpid" && continue
        if shogibench_client_pid "$cpid"; then
            kill -KILL "$cpid" 2>/dev/null || true
        fi
    done
}

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

# For tests: expose the functions above without running the bootstrap
if [ "${SHOGIBENCH_SOURCE_ONLY:-0}" = "1" ]; then
    return 0 2>/dev/null || exit 0
fi

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

# Become a session/process-group leader, so that "this bootstrap and every
# process it ever starts" is exactly one process group. That is what makes
# the takeover above complete: killing the recorded group cannot miss an
# installer, the client, or an engine
if [ "$(self_pgid)" != "$$" ] && command -v setsid >/dev/null && [ -f "$0" ]; then
    exec setsid bash "$0" "$@"
fi

# Serialize the startup section: two bootstraps launched at nearly the same
# moment (double-clicked connect button, duplicated POST) must not both run
# the takeover below, or they treat each other as "previous" and kill each
# other. The loser exits here; the winner proceeds alone
acquire_boot_lock

# Stop any worker started by an earlier run (or an earlier failed attempt),
# so re-running this script never leaves two loops behind
stop_previous_workers

echo "[setup_worker] starting bootstrap pid=$$ pgid=$(self_pgid)"

# Record our process group for the next takeover. Only useful when we truly
# lead our own group; otherwise the name-based sweep still covers us
if [ "$(self_pgid)" = "$$" ]; then
    echo "$$" > "$PIDFILE"
    trap cleanup_pidfile EXIT
fi

# Startup is serialized up to here. From now on a newer bootstrap may take
# over at any time: it finds us via the pidfile or the name-based sweeps
release_boot_lock

# A single failed apt/rustup call must not abort the whole bootstrap and
# leave nothing registered; from here we handle errors ourselves and
# retry, so a transient network hiccup self-heals instead of wedging.
set +e

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
    python3 client.py -T "$SHOGIBENCH_THREADS" -N "$SHOGIBENCH_SOCKETS"
    CODE=$?
    RAN=$(( $(date +%s) - STARTED ))

    # Deliberate shutdowns must not be "healed" by restarting:
    #   65 = another worker owns this machine (duplicate-launch protection)
    #   66 = the worker key was disabled/deleted, or openbench.exit was used
    case "$CODE" in
        65) echo "[setup_worker] another worker is already running here; exiting"; exit 0 ;;
        66) echo "[setup_worker] worker was shut down (key revoked or openbench.exit); exiting"; exit 0 ;;
    esac

    if [ "$RAN" -lt 10 ]; then
        echo "[setup_worker] client exited after ${RAN}s (startup failure); re-checking toolchain"
        install_toolchain
        toolchain_ready || echo "[setup_worker] toolchain still incomplete: $(toolchain_ready)"
    fi

    echo "[setup_worker] client exited with code $CODE, restarting in 15s"
    sleep 15
done
