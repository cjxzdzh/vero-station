#!/usr/bin/env bash
# ============================================================
#  Vero control - Linux / macOS launcher (thin shim)
#
#  Usage:
#    ./run.sh            start the web UI on http://127.0.0.1:8800
#    ./run.sh install    prepare this machine and install the launcher
#    ./run.sh service    the same, and register the boot service
#    ./run.sh check      report what this machine is missing (changes nothing)
#    ./run.sh uninstall  remove the launcher and the background service
#    ./run.sh start      start the web UI without preparing anything
#    ./run.sh test       offline self-test (no hardware needed)
#    ./run.sh scan       look for the device
#    ./run.sh diag       link-lifetime measurement, 5 minutes
#    ./run.sh cli ...    raw CLI, e.g. ./run.sh cli frame --percent 50
#
#  The verbs are bootstrap.py's, and bootstrap.py owns the list:
#  install / check / uninstall / service are handled there before the
#  venv exists, and test / scan / diag / web / start are dispatched to
#  a script.
#
#  VERO_HOST=0.0.0.0 ./run.sh   to reach the UI from other machines
#
#  THE PIP SOURCE. The dependencies come from PyPI, which is slow from
#  mainland China. The source is never switched silently: the
#  operator's own PIP_INDEX_URL always wins, and this project's
#  VERO_PIP_INDEX is used when there is none.
#
#    VERO_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple ./run.sh install
#
#  A PROXY in the environment is honoured as it is -- pip reads
#  http_proxy / https_proxy itself, and nothing here overrides them.
#
#  All real logic lives in bootstrap.py so it can be tested on the machine
#  that writes it -- shell logic cannot. This file only picks a Python.
# ============================================================
set -uo pipefail
cd "$(dirname "$0")"

# Any working Python is fine to start with: bootstrap.py detects an old
# interpreter and re-executes itself under a newer one.
# THE VERSIONED CANDIDATES COME FIRST. "python3" alone is not enough:
# on a machine with pyenv, the "python3" on PATH is a SHIM, and which
# version it points at depends on the SHELL. So the same checkout built
# its virtualenv with 3.10 in a login shell and with 3.13 in a non-login
# one -- and the virtualenv is then fixed to whichever interpreter
# built it, for good.
#
# The order mirrors run.bat's: the newest versioned interpreter first,
# then the generic ones. And the version is CHECKED, because a shim
# pointing at 3.8 would otherwise be picked and the run would fail with
# a message about a Python that is not on PATH.
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
      PY="$cand"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo
  echo "  ERROR: no Python found on PATH."
  echo "  Install Python 3.10 or newer, then run this again."
  echo "    Debian/Ubuntu: sudo apt install python3 python3-venv"
  echo "    macOS        : brew install python@3.12"
  echo
  exit 1
fi

"$PY" bootstrap.py run "$@"
RC=$?
if [ "$RC" -ne 0 ]; then
  echo
  echo "  Exited with code $RC."
fi
exit $RC