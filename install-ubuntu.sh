#!/usr/bin/env bash
# ============================================================
#  Vero control -- Ubuntu / Debian installer and launcher.
#
#  COVERS the three platforms -- ubuntu / mac / windows -- with a start
#  entry point and an install entry point for each, and it handles the
#  awkward cases: various Bluetooth adapters, and dependency installs
#  through a domestic proxy or through the foreign one already set.
#
#  THIS IS THE ENTRY POINT an operator uses. It does three things and
#  then gets out of the way:
#
#    1. makes sure a usable Python is on PATH (and says exactly what
#       to install if there is not one),
#    2. hands over to install.py, which prepares the machine -- BlueZ,
#       the adapter, the pip source -- and installs the launcher,
#    3. starts the web UI.
#
#  Usage:
#    ./install-ubuntu.sh              prepare the machine, then start the UI
#    ./install-ubuntu.sh check        report what is missing, change nothing
#    ./install-ubuntu.sh service      also install it as a boot service
#    ./install-ubuntu.sh uninstall  remove the launcher and the background service
#    ./install-ubuntu.sh start        just start it (no preparation)
#
#  THE PIP SOURCE is never switched silently. The operator's own
#  PIP_INDEX_URL wins; VERO_PIP_INDEX is used when there is none:
#
#    VERO_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
#      ./install-ubuntu.sh
#
#  A proxy in the environment is honoured as it is.
# ============================================================
set -uo pipefail
cd "$(dirname "$0")"

VERB="${1:-install}"
shift || true

# ---------------------------------------------------------------- python
#
# Ubuntu 22.04 ships Python 3.10 and 24.04 ships 3.12, so the system's
# python3 is usually fine. But an image with 3.8 is not, and bleak 3.x
# needs 3.10+. So the version is checked, and the remedy is named.
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
            PY="$cand"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo
    echo "  需要 Python 3.10 或更新版本，但没找到合适的。"
    echo
    echo "  Ubuntu 22.04 及以后："
    echo "      sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
    echo
    echo "  如果 apt 给的版本太老（比如 20.04 只有 3.8）："
    echo "      sudo add-apt-repository ppa:deadsnakes/ppa"
    echo "      sudo apt update && sudo apt install -y python3.12 python3.12-venv"
    echo
    echo "  蓝牙相关的包（顺便装上）："
    echo "      sudo apt install -y bluez bluez-tools libbluetooth3"
    echo
    exit 1
fi

# ---------------------------------------------------------------- the deps
#
# The system packages that the BLE stack and the runtime need. Without
# them the venv builds and then the first scan fails with an error
# about a missing shared library, which is a much worse place to find
# out.
#
# Installing them needs sudo, so it is only attempted when sudo works
# without a password. Otherwise the message says what is missing.
need_pkgs=""
if ! dpkg -s bluez >/dev/null 2>&1; then need_pkgs="$need_pkgs bluez"; fi
if ! dpkg -s libbluetooth3 >/dev/null 2>&1; then need_pkgs="$need_pkgs libbluetooth3"; fi
if [ -n "$need_pkgs" ]; then
    if sudo -n true 2>/dev/null; then
        echo "安装系统包：$need_pkgs"
        sudo apt-get update -qq >/dev/null 2>&1 || true
        sudo apt-get install -y $need_pkgs >/dev/null 2>&1 || true
    else
        echo
        echo "  注意：缺少系统包：$need_pkgs"
        echo "  请运行：sudo apt install -y$need_pkgs"
        echo
    fi
fi

# ---------------------------------------------------------------- the go
# "start" is the bootstrap's own verb, so it is passed straight
# through instead of being interpreted here. It used to be a
# separate `if` in this script AND in install-macos.sh AND a `goto`
# in install-windows.bat -- the same word, the same meaning, four
# copies of the rule, and the copies could drift.
if [ "$VERB" = "start" ]; then
    exec "$PY" bootstrap.py run start "$@"
fi

"$PY" install.py "$VERB" "$@"
RC=$?

if [ "$VERB" = "install" ] && [ $RC -eq 0 ]; then
    echo
    echo "  正在启动……"
    echo
    exec "$PY" bootstrap.py run web
fi
exit $RC
