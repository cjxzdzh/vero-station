#!/usr/bin/env bash
# ============================================================
#  Vero control -- macOS installer and launcher.
#
#  COVERS the three platforms -- ubuntu / mac / windows -- with a start
#  entry point and an install entry point for each, and it handles the
#  awkward cases: various Bluetooth adapters, and dependency installs
#  through a domestic proxy or through the foreign one already set.
#
#  macOS IS THE EASY ONE FOR THE RADIO AND THE HARD ONE FOR THE
#  PERMISSION.
#
#  CoreBluetooth lives inside the process, so there is no adapter to
#  configure, no daemon to start, and no kernel timeout to tune. What
#  there IS is a permission: since macOS 11 (Big Sur) a process that
#  uses Bluetooth has to declare why, and the user has to allow it.
#  For a terminal that means System Settings -> Privacy & Security ->
#  Bluetooth, and the entry is the TERMINAL (or iTerm, or whatever
#  started it) -- not the script.
#
#  There is no command that grants that permission, so this script
#  SAYS SO rather than pretending it can fix it.
#
#  Usage:
#    ./install-macos.sh              prepare the machine, then start the UI
#    ./install-macos.sh check        report what is missing, change nothing
#    ./install-macos.sh service      also install it as a launch agent
#    ./install-macos.sh uninstall  remove the launcher and the background service
#    ./install-macos.sh start        just start it (no preparation)
#
#  THE PIP SOURCE is never switched silently. The operator's own
#  PIP_INDEX_URL wins; VERO_PIP_INDEX is used when there is none.
# ============================================================
set -uo pipefail
cd "$(dirname "$0")"

VERB="${1:-install}"
shift || true

if [ "$(uname -s)" != "Darwin" ]; then
    echo "这个脚本是给 macOS 用的；当前系统是 $(uname -s)。"
    exit 1
fi

# ---------------------------------------------------------------- python
#
# macOS ships NO python3 at all -- it ships python3 only when the
# Command Line Tools are installed, and that install is a popup that
# waits for a click. So the check is: is there a python3, and is it
# new enough? If there is none, Homebrew is the way -- and if Homebrew
# is not there either, the Command Line Tools prompt is the answer.
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
            PY="$cand"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo
    echo "  需要 Python 3.10 或更新版本。"
    echo
    if command -v brew >/dev/null 2>&1; then
        echo "  已找到 Homebrew，请运行："
        echo "      brew install python@3.12"
    else
        echo "  没找到 Homebrew。装 Python 的两条路："
        echo
        echo "  1. 装 Homebrew（推荐）："
        echo "      /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
        echo
        echo "  2. 装 Xcode 命令行工具（会弹窗，等它装完）："
        echo "      xcode-select --install"
        echo "     然后：brew install python@3.12"
        echo
        echo "  国内网络下载 Homebrew 慢的话，可以走镜像："
        echo "      export HOMEBREW_BREW_GIT_REMOTE=" \\
        echo"        https://mirrors.tuna.tsinghua.edu.cn/git/homebrew/brew.git"
        echo "      export HOMEBREW_CORE_GIT_REMOTE=" \\
        echo"        https://mirrors.tuna.tsinghua.edu.cn/git/homebrew/homebrew-core.git"
        echo "      /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    fi
    echo
    exit 1
fi

# ---------------------------------------------------------------- the go
# "start" is the bootstrap's own verb, so it is passed straight
# through instead of being interpreted here. It used to be a
# separate `if` in this script AND in install-ubuntu.sh AND a `goto`
# in install-windows.bat -- the same word, the same meaning, four
# copies of the rule, and the copies could drift.
if [ "$VERB" = "start" ]; then
    exec "$PY" bootstrap.py run start "$@"
fi

"$PY" install.py "$VERB" "$@"
RC=$?

if [ "$VERB" = "install" ] && [ $RC -eq 0 ]; then
    echo
    echo "  首次使用前，请在「系统设置 → 隐私与安全性 → 蓝牙」里"
    echo "  允许你的终端程序使用蓝牙。"
    echo
    echo "  正在启动……"
    echo
    exec "$PY" bootstrap.py run web
fi
exit $RC
