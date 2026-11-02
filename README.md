# Vero Station

A cross-platform controller for **AIKE**'s **Vero** and **Mira** devices.

One tree runs on Windows, Linux and macOS. The Bluetooth layer is
[bleak](https://github.com/hbldh/bleak), which picks the native backend
per platform (Windows → WinRT, Linux → BlueZ, macOS → CoreBluetooth),
so there is no platform-specific code in the package.

The project speaks the devices' own BLE protocol directly — it does not
drive the vendor app, and it does not need it.

---

## What it does

* **Find and connect** to a device over BLE.
* **Read telemetry** — travel position, battery, pressure — live.
* **Drive the device** — set travel position, vibration intensity, and
  run automated motion patterns.
* **Record and replay** sessions; draw them as a chart and in 3D.
* **Expose all of it over HTTP**, so an agent or a script can drive the
  device without a browser.

There is a web UI and an HTTP API. The CLI is there too.

---

## For agents

The HTTP API is also published as a **separate skill package**:
**<https://github.com/cjxzdzh/vera-station-http-api>**

The two repositories are companions, and each one points at the other:

| | |
|---|---|
| **[cjxzdzh/vero-station](https://github.com/cjxzdzh/vero-station)** (this one) | The service: the BLE layer, the web UI, the CLI, the protocol documentation. |
| **[cjxzdzh/vera-station-http-api](https://github.com/cjxzdzh/vera-station-http-api)** | The skill: the HTTP API reference for an agent, plus a verifier that checks every documented claim against a running service. |

The skill is **documentation plus a verifier** — it is not a second copy of the
service, and it does not talk to a device by itself. Install the service
from this repository, then install the skill wherever your runtime looks for
skills:

```bash
git clone https://github.com/cjxzdzh/vera-station-http-api.git
cp -r vera-station-http-api ~/.hermes/skills/     # Hermes
cp -r vera-station-http-api ~/.claude/skills/     # Claude Code
cp -r vera-station-http-api ~/.codex/skills/      # Codex
```

**One copy, one name.** Two directories with the same skill name make the
name ambiguous and the skill unloadable. If you install it in more than one
place, rename the directory in one of them.

The skill's `INSTALL.md` walks through the whole thing, including the one
step that matters before you write any command: **check whether you are
pointing at a real device or at the simulator**, because a write against a
real device moves a real device.

---

## Install

### Requirements

| | |
|---|---|
| **Python** | 3.10 or newer. bleak 3.x requires it and will not install on anything older. |
| **Windows** | Windows 11, build 22000 or newer. bleak 3.x reaches the radio through WinRT, which needs it. No Bluetooth driver, no adapter setup. The radio must be **ON** (Settings → Bluetooth and devices → Bluetooth) and the Bluetooth Support Service (`bthserv`) must be running. |
| **Linux** | BlueZ 5.55 or newer, with the `bluetooth` and `dbus` services running. |
| **macOS** | 10.15 or newer, and your terminal needs Bluetooth permission (System Settings → Privacy & Security → Bluetooth). |

### Get the code

```bash
git clone https://github.com/cjxzdzh/vero-station.git
cd vera-station
```

Or download the archive and unpack it — **one archive serves all three
platforms**:

| Platform | Unpack |
|---|---|
| Windows | `tar -xf vera-station-universal-v1.0.0.tar.gz` |
| Linux | `tar -xzf vera-station-universal-v1.0.0.tar.gz` |
| macOS | `tar -xzf vera-station-universal-v1.0.0.tar.gz` |

Windows 10 and 11 both ship `tar` (bsdtar), so there is nothing to
install first — no 7-Zip, no extra download.

### Install

| Platform | Install and start | Check only (changes nothing) | Uninstall |
|---|---|---|---|
| Ubuntu / Debian | `./install-ubuntu.sh` | `./install-ubuntu.sh check` | `./install-ubuntu.sh uninstall` |
| macOS | `./install-macos.sh` | `./install-macos.sh check` | `./install-macos.sh uninstall` |
| Windows | double-click `install-windows.bat` | `install-windows.bat check` | `install-windows.bat uninstall` |

The installer picks a Python, builds a virtual environment, installs the
dependencies, leaves a launcher behind and starts the UI.

Add `--service` to also register it as a boot service (systemd on Linux,
launchd on macOS, the Task Scheduler on Windows).

**On macOS there is one thing you must click yourself.** macOS 11 and
later require a process to say why it wants Bluetooth, and the user has
to allow it. For a terminal that means **System Settings → Privacy &
Security → Bluetooth**, and the entry is the **terminal application**
(Terminal / iTerm / VS Code) — not Python. No command can grant it; if
it is refused, the only way back is that settings pane.

---

## Run

| | Windows | Linux / macOS |
|---|---|---|
| start the UI | `run.bat` | `./run.sh` |
| offline self-test | `run.bat test` | `./run.sh test` |
| find the device | `run.bat scan` | `./run.sh scan` |
| measure the link | `run.bat diag` | `./run.sh diag` |
| raw CLI | `run.bat cli ...` | `./run.sh cli ...` |

The UI comes up on <http://127.0.0.1:8800>.

`run.sh` / `run.bat` **only start the UI**. They never install
anything and never modify your environment. To install, use the
installer above.

---

## Use the web UI

Open <http://127.0.0.1:8800>.

**On the connect tab there is one button: Scan.** Press it, wait for the
device to appear in the list, then click the device to connect. There
is no separate "connect device" button — the scan list *is* the
connection picker.

Only devices this project can actually drive are listed. Anything else
in radio range — other people's phones, headphones, watches — is
counted and reported as "N devices this version cannot control", never
offered, and **their addresses are redacted** before they leave the
server. The connectable device's own address is passed through,
because connecting to it needs the real one; the page simply does not
print it.

Then the control tab gives you travel, vibration, the automated motion
patterns and a live 3D view of the mechanism.

---

## Use the HTTP API

```bash
# 1. connect
curl -s -X POST http://127.0.0.1:8800/api/v1/connection/connect

# 2. arm — the telemetry channels need this
curl -s -X POST http://127.0.0.1:8800/api/arm

# 3. drive
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"percent":45,"vibration":90}' \
     http://127.0.0.1:8800/api/v1/stroke

# 4. release
curl -s -X POST http://127.0.0.1:8800/api/v1/stroke/release
```

Two things that are easy to get wrong, both measured rather than
guessed:

* **Arming comes after connecting.** There is no `/api/v1/arm`; the arm
  route is `POST /api/arm`.
* **The body is validated before the link is checked.** A malformed body
  is a `422` whether or not a device is connected; only a *valid* body
  reaches the `409` "not connected". A `422` does not mean "not ready
  yet".

`SKILL.md` is the complete API reference — every route, what each one
needs first, the exact error shapes and the ordering rules. It was
produced by measuring a running service, not by reading the source.

### Holding a position

`POST /api/v1/stroke` **holds** the position it sets. The answer says
so: `"held": true`.

That matters because the service has a safety net: if nothing asks it to
keep a position, it retracts after a couple of seconds. A browser
sends a heartbeat, so the page's own commands are held that way. An API
caller does not, so the route claims the hold explicitly. The most
recent **explicit** command owns the position — a later command from
anywhere supersedes an earlier hold.

Pass `"hold": false` to opt out and get the old time-out behaviour.

---

## Try it without a device

The simulator is opt-in, and only the web entry point can reach it:

```bash
.venv\Scripts\python.exe start_web.py --simulate --host 127.0.0.1 --port 8807   # Windows
.venv/bin/python3 start_web.py --simulate --host 127.0.0.1 --port 8807         # Linux/macOS
```

The CLI has **no** `--simulate` flag and always drives real hardware.

---

## Access token

By default **no route requires a token**: anyone who can reach the port
can read the device's identity and its recorded sessions. On a network
you do not fully control, set `VERO_ACCESS_TOKEN` before starting.

```bash
set VERO_ACCESS_TOKEN=<a secret>          # Windows
export VERO_ACCESS_TOKEN=<a secret>       # Linux/macOS
```

The token is then sent as `X-Vero-Token` or `?token=`.

---

## Safety

**This service drives a physical device.** A command that moves it moves
it in the hand of whoever is wearing it.

* Connect before you arm, and arm before you drive.
* `POST /api/v1/stroke/release` and `POST /api/stop` both stop motion.
* If the UI reports the link as lost, it retracts on its own.
* `run.sh diag` measures the link for five minutes and writes
  `report.json` — that is the one to read when the device connects and
  then drops.

---

## From mainland China, use a pip mirror

The dependencies come from PyPI, which is slow from here. The source is
never switched silently: **your own `PIP_INDEX_URL` always wins**, and
this project's `VERO_PIP_INDEX` is used only when you have none.

```bash
set VERO_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple      # Windows
export VERO_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple  # Linux/macOS
```

The installer also **asks** which mirror to use, and the default is the
domestic (Tsinghua) one — press ENTER to take it. The choice is
remembered, so the next run uses it too. A read with nobody there takes
the default after 45 seconds rather than hanging.

A proxy already in your environment is honoured as it is.

---

## If it does not start

1. `run.sh check` / `run.bat check` — reports what is missing.
2. `run.sh test` / `run.bat test` — the offline self-test; no hardware
   needed.
3. `run.sh diag` / `run.bat diag` — measures the link for five minutes
   and writes `report.json`.

---

## Layout

```
run.bat, run.sh              the launchers, one per platform family
install-windows.bat          the installers, one per platform
install-ubuntu.sh
install-macos.sh
bootstrap.py                 finds a Python, builds the venv, installs the
                             dependencies, dispatches the verb
install.py                   the machine checks and the boot service
launcher.py                  what the installer puts on PATH
start_web.py, run_cli.py     the service and the command line
selftest.py, run_diagnose.py the offline self-test and the link measurement
vero/                        the package: BLE, protocol, telemetry,
                             recording, and the web UI
  web/index.html             the UI
  web/js/                    the record chart and the 3D replay
  web/vendor/                three.js and uPlot, vendored
docs/                        the protocol documentation
SKILL.md                     the HTTP API reference
requirements.txt             the dependencies
```

### The documentation

| File | What it covers |
|---|---|
| `SKILL.md` | The complete HTTP API, for a caller outside the browser. |
| `docs/01-protocol.md` | The BLE protocol: frames, CRC, the two transports, the capabilities table. |
| `docs/02-telemetry.md` | The telemetry channels and what each one carries. |
| `docs/03-battery.md` | Battery reporting. |
| `docs/04-link.md` | Link lifetime, supervision timeout, and the disconnect behaviour. |
| `docs/05-app-behaviour.md` | What the vendor app does, and what this project mirrors. |
| `docs/06-reverse-method.md` | How the protocol was worked out, and the traps. |
| `docs/07-hardware.md` | The hardware: MCU, radio, sensors. |
| `docs/08-platforms.md` | The per-platform preparation checklist. |
| `docs/INSTALL.md` | Installation in detail. |

---

## License

MIT — see [LICENSE](LICENSE).

This project is not affiliated with, endorsed by, or connected to AIKE
or any of its product lines. It is an independent implementation that
speaks the devices' BLE protocol.
