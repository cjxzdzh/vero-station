#!/usr/bin/env python3
"""Command line control."""
import os
import sys

# This launcher lives inside the package directory, so the directory that must
# be importable is its PARENT: the one containing the `vero` package.
# `python -m vero.webapp` cannot be the documented entry point, because with the
# shell inside the package folder Python resolves the module name before any of
# our code runs.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vero.cli import main   # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
