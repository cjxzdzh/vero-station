"""Cross-platform control for the Vero / Amorlink BLE device.

Runs on Windows, Linux and macOS through bleak, which selects the native
backend per platform (WinRT, BlueZ, CoreBluetooth). Nothing here shells out to
a platform tool, with one documented exception: the GATT-cache refresh during
recovery is Linux-only and reports that it did nothing elsewhere.

Entry points -- use the top-level scripts, not `python -m`, because `-m`
resolves the package name before any bootstrap code can run:

    python start_web.py        web control UI
    python run_diagnose.py     link lifetime measurement
    python run_cli.py scan     command line
    python selftest.py         offline check, no hardware needed

Modules:
    protocol    frame construction, CRC, parsers (no I/O, no dependencies)
    session     link ownership, the 20 ms frame stream, recovery
    automotion  sweep between two positions
    monitor     rolling telemetry statistics
    pressure    0x29 telemetry frame decoding
    simulator   an in-process stand-in device for testing without hardware
    cli         command line implementation
    webapp      browser control surface
    diagnose    link-lifetime measurement for cross-platform comparison
"""

__version__ = "1.0.0"
