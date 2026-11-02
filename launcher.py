#!/usr/bin/env python3
"""Start the Vero web UI. Installed by install.py."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VP = os.path.join(HERE, ".venv", "bin", "python")
if os.name == "nt":
    VP = os.path.join(HERE, ".venv", "Scripts", "python.exe")

if not os.path.exists(VP):
    sys.stderr.write(
        "The virtual environment is missing. Run install.py again.\n")
    raise SystemExit(1)

host = os.environ.get("VERO_HOST", "127.0.0.1")
port = os.environ.get("VERO_PORT", "8800")
raise SystemExit(subprocess.call(
    [VP, "-u", os.path.join(HERE, "start_web.py"),
     "--host", host, "--port", port]))
