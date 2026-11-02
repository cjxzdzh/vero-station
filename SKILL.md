---
name: vera-station-http-api
description: Use when driving the Vero device over HTTP from a script, an agent, or a test — the live service on <lan-ip>:8800 — connecting, reading identity, holding a position, sweeping automatically, pulling battery and touch telemetry, binding the device's buttons, or diagnosing a 409/422. Covers the two route families (/api/v1 public and /api UI-internal), the arm step telemetry needs, the 422-before-409 ordering, and the simulator-vs-hardware boundary. Does not cover protocol reverse-engineering (see docs/) or frontend work (see index.html).
---

# Vero web API

This skill is for a **caller outside the browser**: a script, a test, or an agent.
Everything here was produced by running the real service on this repository and
recording what it actually returned. Where the prose in `README.md` and `docs/`
would give you a different impression, the measurement below wins — see
[Measured facts that contradict the docs](#measured-facts-that-contradict-the-docs).

## Two route families

The app serves two independent sets of endpoints. They look similar and are
not interchangeable.

| | `/api/v1/...` | `/api/...` |
|---|---|---|
| Purpose | public contract for external callers | the UI's own internal state |
| Source | `vero/api.py` | `vero/webapp.py` |
| Envelope | every response carries `ok` | no `ok` field |
| Count | **19** | **33** |

`/api/v1` is the one to build against. The `/api/...` family returns the whole
internal snapshot, so its field names exist for the page's benefit and carry no
stability promise.

**What each count counts.** Both are the number of route decorators the named
module declares — `vero/api.py` 19, `vero/webapp.py` 33 — which is what
`scripts/p47_regression.py` measures, decorator by decorator, rather than
repeating this page's own prose. The internal family's 33 is NOT 33 URLs under
`/api/`: 28 are, and 5 serve the page itself (`/`, `/index.html`, `/js/{name}`,
`/vendor/{name}`, `/icon/{name}`). The count is per module, not per prefix, so
a caller reading "33" should read it as "everything this app serves besides
`/api/v1`", which is also the only way the number can be checked without
deciding which prefixes count.

**There is no video feature.** The whole video-sync stack — the library, the
upload, the analysis, the stroke plan, the follower and its twenty-four
routes — was removed. Nothing in this document describes it, and
`scripts/p47_regression.py` asserts the absence, so it cannot come back
unnoticed.

**One endpoint is outside both families: `GET /js/{name}`.** It serves the
page's own ES modules — `recchart.js` (the interactive record chart),
`replay.js` (the 3D replay) — by exact name, from a
whitelist, with the same
gates the vendored `/vendor/{name}` route uses. It is not part of either
contract: a caller that wants the device's state or its telemetry uses the two
families above, and nothing else should be building a URL against `/js/`.

**One endpoint from the internal family is nonetheless required to get
telemetry: `POST /api/arm`.** There is no `/api/v1/arm`. See
[The arm step](#the-arm-step-telemetry-needs-it-and-it-is-not-in-api-v1).

**Three internal routes carry the session recording.** `GET /api/records`
lists every recording on disk (newest first); `GET /api/records/{name}`
returns one of them as plottable series; `POST /api/records/import` writes
an exported archive into the records directory, so an imported file becomes
a REAL history record -- one that is in the list, can be picked later, and
survives a restart, rather than a row that lives in the browser until the
page is reloaded. The recording itself needs no route and no permission:
the server starts writing when the link comes up and ends the file when it
drops, so a caller that wants the data reads it afterwards rather than
asking for it to be captured. The two reads are read-only and none of the
three is in `/api/v1`.

## Starting the service

Always confirm the port is free *before* starting, or you will measure
somebody else's server and report it as this one's behaviour.

```bash
ss -tlnp | grep 8807    # must print nothing
```

Then run it in the background and re-check that the listening PID is yours:

```bash
.venv/bin/python3 start_web.py --simulate --port 8807
ss -tlnp | grep 8807    # users:(("python3",pid=<your pid>,...))
```

Confirm you reached your own server rather than a squatter on the port — a
wrong process answers `/api/*` with 404s and looks like a broken build:

```bash
curl -s -o /dev/null -w '%{http_code} %{size_download}\n' http://127.0.0.1:8807/
# measured: 200 37603
```

Shut it down afterwards and prove the port went back:

```bash
ss -tlnp | grep 8807    # must print nothing
```

### On Windows

The commands above are POSIX. The **route names and the JSON are
identical** — only the shell is different, so an agent on Windows
translates these three things and nothing else:

| What | Linux / macOS | Windows |
|---|---|---|
| the interpreter in the venv | `.venv/bin/python3` | `.venv\Scripts\python.exe` |
| is the port free? | `ss -tlnp \| grep 8807` | `netstat -ano \| findstr 8807` |
| the launcher | `./run.sh` | `run.bat` |

```bat
netstat -ano | findstr 8807
.venv\Scripts\python.exe start_web.py --simulate --port 8807
netstat -ano | findstr 8807
```

`curl` may not be on a stock Windows PATH. PowerShell's equivalent, which
returns the same two numbers (`status` and `bytes`), is:

```powershell
$r = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8807/
"$($r.StatusCode) $($r.RawContentLength)"
```

The Windows package ships `scripts/verify_api_doc.py`, which is
cross-platform: it finds the venv interpreter in either layout
(`.venv/Scripts/python.exe` on Windows, `.venv/bin/python3` elsewhere) and
it skips the systemd-unit checks when there is no `systemctl` — which is
the case on every Windows machine, and is the same path a Linux machine
without a unit already took.


### The optional LAN gate

The service is started with `--host 0.0.0.0`, so every device on the LAN
can reach it — and by default **no route requires a token**. That is a
deliberate default, not an oversight: the panel is the operator's own,
on his own network.

For this project it is worth knowing what that means. MEASURED on the
live service with the gate off:

| request | answer |
|---|---|
| `GET /api/state` | the device's identity, its counters and its telemetry |
| `GET /api/records` | the recorded sessions, with their paths |
| `GET /api/v1/connection` | the link state and the device's identity |

The API KEY itself is never echoed: the public config reports
`has_api_key` and `api_key_length`, and the key lives in a file at mode
`600` outside the repository.

To close the gate, set one environment variable:

```bash
VERO_ACCESS_TOKEN=<a secret> .venv/bin/python3 start_web.py --host 0.0.0.0 --port 8800
```

Then every request must carry the token, as `X-Vero-Token: <secret>` or
`?token=<secret>`, and the page is handed the token by the server so it
can authenticate itself. The assets the page needs before it can
authenticate (`/`, `/js/*`, `/vendor/*`, `/icon/*`) are exempt.

MEASURED with the gate on:

```
GET /api/state                      -> 401 {"detail":"需要访问令牌。"}
GET /api/state   X-Vero-Token: ok  -> 200
GET /api/state   ?token=ok          -> 200
GET /api/state   X-Vero-Token: bad -> 401
GET /                              -> 200 (the page, with the token in it)
```

**MEASURED on the DEPLOYED service (2026-10-31, `http://<lan-ip>:8800`):
the gate is OFF.** `GET /api/state`, `/api/records` and
`/api/v1/connection` all answer `200` with no token at all. So on the live
box **every route is reachable by anyone on the LAN**, including the
device's identity and the recorded session data. That is the deliberate
default above, not a hole that was opened by accident — but a caller
writing a script against this service should know it, and the operator who
does not want that should set `VERO_ACCESS_TOKEN` and restart.


## The deployed service, and which tree it runs

**8800 does not run this repository's working tree.** It runs a DEPLOYED
COPY at `<install-dir>`, started by the systemd unit
`vera-preview.service`:

```
WorkingDirectory=<install-dir>
ExecStart=<install-dir>/.venv/bin/python -u start_web.py --host 0.0.0.0 --port 8800
```

The copy shares this repository's git history (same commits, same branch),
and `scripts/deploy_8800.py` is what pushes a tree across and restarts the
service. **So a change made in `<install-dir>` is NOT live on 8800
until that script has run** — a caller who edits the source and then reads
8800 is reading the OLD build.

Two consequences that are easy to get wrong:

1. **The service on 8800 runs in REAL-HARDWARE mode.** The unit's
   `ExecStart` has **no `--simulate` flag**, and `start_web.py`'s flag
   defaults to `False` — so 8800 drives a real device when one is in radio
   range. MEASURED on 8800: `/api/state` reports `"simulate": false`,
   `"connected": true`, `"serial": "UMVX"`. **A write to 8800 moves a
   real device.** Use a local simulator instance (`--simulate --port 8807`)
   for anything that writes.
2. **The deployed copy's data directory is its own.** MEASURED on 8800:
   `GET /api/records` answers `"records": [{"name": "<timestamp>.jsonl",
   "path": "<install-dir>/vero/data/records/<timestamp>.jsonl",
   ...}]` — the PREVIEW tree's records, not this tree's. A local simulator
   instance started from `<install-dir>` writes to THIS tree's
   `vero/data/`.

   **`current` is a TRANSIENT field.** It names the file being written
   RIGHT NOW, so it is `null` between recordings — MEASURED: right after a
   disconnect it answered `"current": null, "recording": false` while
   `records` still held the finished file. **Read `records[].path`, not
   `current`,** if you want to know where the recordings live.


## The simulator / hardware boundary

**This machine has one Bluetooth HCI controller.** Measured on 2026-10-20:

```
$ lsusb | grep -i -E "33fa|blue"
Bus 003 Device 006: ID 33fa:0012  UGREEN BT6.0 Adapter
Bus 003 Device 009: ID 1915:522a Nordic Semiconductor ASA nRF Sniffer for Bluetooth LE
```

**The filter is the trap.** `lsusb | grep -i blue` prints **only** the nRF
sniffer, because "blue" does not appear in the UGREEN adapter's vendor/product
string. An earlier draft of this file used that filter and concluded the host
had no adapter; it had one all along. Match on the USB id (`33fa`) or read the
full `lsusb` output.

`hciconfig` prints the controller:

```
$ hciconfig
hci0:	Type: Primary  Bus: USB
	BD Address: 04:7F:0E:XX:XX:XX  ACL MTU: 679:9  SCO MTU: 255:4
	UP RUNNING 
```

(The RX/TX byte counters that follow `UP RUNNING` are omitted on purpose —
they advance on every call, so they are a live counter rather than a property
of the adapter. The stable lines are the address, the MTUs and `UP RUNNING`.)

`hciconfig -a` adds the vendor identity — behind the UGREEN BT6.0 shell sits a
**Barrot Technology Limited (2279)** controller, `HCI Version (0xe) Revision
0x204`, driven by Linux `btusb` 0.8.

The nRF52840 on `/dev/ttyACM0` is still a passive capture tool. It is not the
HCI controller and never was.

**What actually defaults to the simulator — measured, and it is not what an
earlier draft of this file claimed.** An earlier draft said every command here
defaults to `--simulate`. That was wrong twice over: the flag is not a default
at all, and one of the two entry points has no such flag. Measured:

| Entry point | With no `--simulate` flag | Source |
|---|---|---|
| `start_web.py` | **real hardware** | `vero/webapp.py:1101` — `action="store_true"`, so the flag defaults to `False` |
| `start_web.py --simulate` | **the simulator** | same line — the flag is what turns it on |
| `run_cli.py` | **real hardware, always** | `vero/cli.py:19` — `DeviceSession(simulate=False, address=args.target)`, hardcoded, and there is no flag to change it |

So **neither entry point simulates by default.** The simulator is opt-in
everywhere, and only `start_web.py` can reach it at all.

**Why the ten-step run below still passes `--simulate`.** Not because it is the
default, and not because the hardware is missing — a real link takes the
device's single BLE central slot, and the documented flow must not take the
machine's real device away from whoever is using it. That run is written
against the simulator so that every command in it is safe to re-run without a
device in hand; **a result obtained that way is a statement about the
simulator, not about the device.**

`GET /api/v1/connection` reports this per response, so check it rather than
assuming:

```json
{"simulated": true, "connected": true, "device": {"serial": "sim-device", "firmware": "1.0.11"}}
```

To run against the real device **on this host**: the device powered and not
already paired to a phone, and `start_web.py` **without** `--simulate`. The
simulator implements the same transport contract, so command shape is
identical; only the identity fields differ.

## A complete run

Ten steps, all measured. The order matters: `arm` must follow `connect`, and
nothing else subsumes it.

```bash
U=http://127.0.0.1:8807
B=$U/api/v1

# 1 connect
curl -s -X POST $B/connection/connect
{"ok":true,"connected":true,"serial":"sim-device"}                                    # 200

# 2 arm the telemetry sensors  -- UI-family route, required for step 6
curl -s -X POST $U/api/arm; sleep 3

# 3 read identity and counters
curl -s $B/connection
{"ok":true,"connected":true,"link_ok":true,"reconnects":0,"connected_seconds":0.0,
 "frames_sent":1,"write_errors":0,"device":{"serial":"sim-device","firmware":"1.0.11",
 "power":15.0,"battery_percent":60,"battery_cell_volts":3.75,"rssi":-40,"mtu":255},
 "platform":"linux","simulated":true,"error":""}                                      # 200

# 4 hold a position; omitting `vibration` keeps the current level
#   the position is HELD until another action ends it -- see "Holding a
#   position" below
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"percent":45,"vibration":90}' $B/stroke
{"ok":true,"percent":45.0,"vibration":90,"held":true}                                 # 200

# 5 start the automatic sweep
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"min_percent":25,"max_percent":75,"traverse_seconds":0.6}' $B/auto/start
{"ok":true,"running":true,"min_percent":25.0,"max_percent":75.0,
 "traverse_seconds":0.6}                                                             # 200

# 6 read live touch telemetry (needs step 2)
curl -s $B/pressure
{"ok":true,"position":8000,"position_pct":80,"active_slot":4,"touch_raw":2886,
 "touch_span_pct":100,"resting_level":1533,"press_delta":400,"pressure":2886,
 "raw_pct":70,"peak":2899,"pressed":true,"press_events":1,"frames":521,
 "streaming":true,"age_s":0.05,"sample_hz":19.6,"interval_ms":50.95}                 # 200

# 7 battery, converted with the manufacturer's own curve
curl -s $B/battery
{"ok":true,"percent":60,"percent_shown":null,"samples":0,"median_cell_volts":null,
 "filter_reason":"waiting for the first reading","pack_volts":15.0,"cell_volts":3.75,
 "table_cell_volts":3.7524,"table_percent":60,"in_range":true,"cell_count":4,
 "connected":true}                                                                   # 200

# 8 the series window
curl -s "$B/pressure/series?seconds=10"
{"channel":0,"count":191,"requested_seconds":10.0,"window_seconds":26.8,
 "sample_hz":19.1,"position_pct":80}                                                 # 200

# 9 stop sweeping and let go
curl -s -X POST $B/auto/stop      # {"ok":true,"running":false}                        # 200
curl -s -X POST $B/stroke/release # {"ok":true,"percent":0.0}                          # 200

# 10 close the link
curl -s -X POST $B/connection/disconnect
{"ok":true,"connected":false}                                                        # 200
```

## Route reference

Nineteen public routes, each row measured. "Precondition" is what must already
be true.

### Connection

| Method | Path | Precondition | Measured |
|---|---|---|---|
| GET | `/api/v1/connection` | none | `200` `{"ok":true,"connected":false,"link_ok":false,"reconnects":0,"median_link_lifetime":null,"connected_seconds":0.0,"frames_sent":0,"write_errors":0,"device":{"serial":"","firmware":"","power":null,"battery_percent":null,"battery_cell_volts":null,"rssi":null,"mtu":null},"platform":"linux","simulated":true,"error":""}` |
| POST | `/api/v1/connection/connect` | none | `200` `{"ok":true,"connected":true,"serial":"sim-device"}` |
| POST | `/api/v1/connection/connect` (again) | already connected | `200` `{"ok":true,"connected":true,"already":true}` — idempotent, safe to retry |
| POST | `/api/v1/connection/disconnect` | none | `200` `{"ok":true,"connected":false}` |

### Stroke

| Method | Path | Precondition | Measured |
|---|---|---|---|
| GET | `/api/v1/stroke` | connected | `200` `{"ok":true,"percent":0.0,"active_percent":0.0,"reported_percent":null,"vibration":0,"streaming":false,"auto_running":false,"held":false}` |
| POST | `/api/v1/stroke` | connected | `200` `{"ok":true,"percent":45.0,"vibration":90,"held":true}` |
| POST | `/api/v1/stroke/release` | connected | `200` `{"ok":true,"percent":0.0}` |
| POST | `/api/v1/intensity` | connected | `200` `{"ok":true,"level":2}` — the device's three NATIVE steps |


`percent` is what you commanded; `active_percent` is what the send loop emits
right now. They differ while a sweep runs. `reported_percent` is `null` until
the device's own position telemetry arrives.

**An OMITTED `vibration` KEEPS the level in effect — it does not zero it.**
MEASURED: `POST /api/v1/stroke {"percent":45,"vibration":90}` then
`POST /api/v1/stroke {"percent":50}` answers
`{"ok":true,"percent":50.0,"vibration":90}` and the following
`GET /api/v1/stroke` still reports `"vibration": 90`. A caller that
adjusts position therefore does not silently stop the vibration it asked
for earlier.

#### Holding a position

**`POST /api/v1/stroke` HOLDS the position. It does not time out.**

MEASURED, on the live service: `POST /api/v1/stroke {"percent":100}`
answers `200 {"ok":true,"percent":100.0,"vibration":0,"held":true}`, and
the device is still at 100% four seconds later — and still at 100% a
minute later. Before this contract existed, the answer was the same
`200` and the device went straight back in after 2.5 s, because an
HTTP caller has no page and therefore sends no heartbeat: the server's
idle guard read the caller as absent and retracted the position.

That was a **lie in the API's own words**: the route's docstring said
"Applies immediately and holds until changed", and it did not hold. An
agent that asked for full travel got a success answer and a device that
went home on its own.

**What ends the hold** — each of these is an explicit action, and each
is checked:

| Action | Route |
|---|---|
| let go | `POST /api/v1/stroke/release` |
| a different position | `POST /api/v1/stroke {"percent":30}` (re-claims the hold) |
| a sweep takes over | `POST /api/v1/auto/start` |
| a gait takes over | `POST /api/v1/gait/start` |
| stop | `POST /api/v1/auto/stop`, `POST /api/v1/gait/stop`, `POST /api/stop` |
| the link closes | `POST /api/v1/connection/disconnect` |

**`held` tells you whether your command is still in force.** It is in
both the `POST /api/v1/stroke` answer and the `GET /api/v1/stroke` body.

**`hold: false` is the opt-out.** It restores the old behaviour — the
position is released automatically once the caller goes quiet, exactly
as the WebUI's safety net does — and it CLEARS a hold an earlier
command left set:

```bash
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"percent":45,"hold":false}' $B/stroke
{"ok":true,"percent":45.0,"vibration":90,"held":false}
```

**`percent: 0` is a let-go, not a hold** — the device goes home and
stays home, and no hold is claimed.

**The WebUI's safety net is untouched.** A browser tab that goes away
still retracts the position in 2.5 s, because the page never claims
this hold — only the API's stroke route does. The two are separate:
the page's hold is a heartbeat, this one is a command.

**THE MOST RECENT COMMAND OWNS THE POSITION.** If the WebUI then moves
the travel, the page's command takes over and the caller's hold ends —
so the page's safety net applies to the page's positions again. This
was a real bug, found on a Windows machine: an agent commanded 100%,
the page moved the travel to 30%, the tab closed, and the position
stayed out, because the agent's hold was still in force and the
page's own safety net never got to run.

So: **a caller that hands the device over must let go of it**, or the
next command from another surface inherits the caller's hold. The
release is that hand-back:

```bash
curl -s -X POST $B/stroke/release
```

**`POST /api/v1/intensity` is an ORDINAL, not a percentage.** `level` is
`0`=low, `1`=medium, `2`=high — the app's own three steps, each a
hardcoded frame. MEASURED: `{"level":2}` → `200 {"ok":true,"level":2}`;
`{"level":5}` → `422` with `"Input should be less than or equal to 2"`. There
is no fourth step to send and no way to scale one, so a caller that wants
a percentage has to use `vibration` (0–255) instead.

### Auto motion

| Method | Path | Precondition | Measured |
|---|---|---|---|
| GET | `/api/v1/auto` | none | `200` `{"ok":true,"running":false,"cycles":0,"current_percent":0.0,"min_percent":30.0,"max_percent":70.0,"traverse_seconds":0.5,"vibration":0,"dwell_seconds":0.0,"error":""}` |
| POST | `/api/v1/auto/params` | **none** | `200` — see the note below; this one works while disconnected |
| POST | `/api/v1/auto/start` | connected | `200` `{"ok":true,"running":true,"min_percent":25.0,"max_percent":75.0,"traverse_seconds":0.6}` |
| POST | `/api/v1/auto/params` | none | `200` `{"ok":true,"running":false,"min_percent":30.0,"max_percent":70.0,"traverse_seconds":0.5}` |
| POST | `/api/v1/auto/stop` | none | `200` `{"ok":true,"running":false}` |

#### The motion template (`preset`)

The automatic sweep can run one of three motion templates. The parameter is
called `preset`, and the values are:

| `preset` | shape | peak speed vs the sine | what it feels like |
|---|---|---|---|
| `sine` (default) | `sin(x·π/2)` | 1.00x | the baseline: comes to rest at both ends of the stroke |
| `surge` | `sin(x·π/2)·(1+0.3·cos(x·π/2)²)` | 1.30x | livelier through the middle, still at rest at the ends |
| `sag` | `sin(x·π/2)·(1−0.3·cos(x·π/2)²)` | 0.82x | gentler through the middle, so the ends take up more of the cycle |

Every template is strictly increasing over the stroke and maps its two ends
exactly onto the ends of the range, so a live range change remaps the phase
instead of jumping. The default is `sine`, and a caller that never mentions
the template gets exactly the behaviour that existed before it was added.

**`GET /api/v1/auto` does NOT report `preset`.** That endpoint builds an
explicit whitelist dictionary rather than passing the parameter set through,
so the template is invisible there. Do not write a caller that polls
`/api/v1/auto` and looks for a `preset` field — it is not there, and its
absence is not a bug.

**The `/api/v1` family does not express `preset` at all, and a call to it does
not change the template that is running.** The public family is unchanged by the
template work: `vero/api.py` is a protected path and was not touched, so
`AutoParamsIn` has no `preset` field and the body's extra key is dropped on the
way in. The response is `200`, no `preset` comes back, and **the template the
sweep is already running is left exactly as it was** — it is neither replaced by
the sine nor cleared:

```bash
# select the surge through the UI family
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"min_percent":10,"max_percent":70,"preset":"surge"}' $U/api/auto/start

# a v1 call that changes only the range: the surge keeps running
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"max_percent":60}' $B/auto/params
# -> {"ok":true,"running":true,"min_percent":10.0,"max_percent":60.0,...}
#    the template is still the surge
```

**This is worth stating precisely, because the two failure modes are opposite.**
A v1 call does not **discard** the template you asked for (the key never
reaches the generator at all), and it does not **clear** the template that is
already running either. Before the fix it did the second one: the public
family builds its parameters without naming a template, and the dataclass
default was the sine, so a v1 call that changed nothing but `max_percent`
silently swapped a running surge for the sine.

**A v1 `auto/start` cannot name a template, so it runs the default one
(`sine`).** The template is selectable through the **UI family**,
`/api/auto/start` and `/api/auto/params`, which do accept it:

```bash
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"min_percent":10,"max_percent":70,"preset":"surge"}' $U/api/auto/start
```

The UI family **refuses** a template it does not know with a `422` rather
than quietly running the default:

```
$ curl -s -X POST -H 'Content-Type: application/json' \
       -d '{"preset":"triangle"}' $U/api/auto/params
{"detail":[{"type":"value_error","loc":["body","preset"],
  "msg":"Value error, unknown motion template 'triangle'; expected one of sine, surge, sag", ...}]}
```

`GET /api/auto` (the UI family) **does** report the template, under
`params.preset`.

### Choosing the device, and the tick interval

Two UI-family routes were added for the operator who wants to pick a
device and choose how fast positions are sent.

**`GET /api/devices`** scans and lists the advertisers **this build can
drive** — a device that cannot be controlled is not offered. Each row carries
`address`, `name`, `rssi` (dBm), `product` (`Vero` / `Mira` / `Nexo` /
`Wyrh` / `Unknown`), `identified`, `has_vibration`,
`has_position_sensors`, `evidence`, `controllable` and a one-line `note`.

**The answer has three counts, and the third is the one that matters.** The
payload is `{"devices": [...], "count": N, "hidden_count": M, "hidden": [...],
"scan_seconds": 8.0}`. `devices`/`count` are the CONTROLLABLE ones;
`hidden`/`hidden_count` are the advertisers that were seen and are **not**
offered, each with a `reason`. The hidden ones are counted and reported
rather than silently dropped, because "there is nothing out there" and
"there is something out there and it cannot be controlled" are different
situations and the operator needs to tell them apart — a device that
broadcasts and is not offered looks like a missing device until it is
named. MEASURED on the live service with no device in radio range:
`{"devices":[],"count":0,"hidden_count":0,"hidden":[],"scan_seconds":8.0}`.

The filter reads the same `controllable` flag the row carries, so a device
that is offered is a device the send path can actually drive. `controllable`
means **this build can drive it**, which is not the same as "it is one of
ours": a Nexo is one of ours and the send path does not speak its
transport, so a Nexo is not offered; but a device that advertised the
vendor service UUID and no usable name is one of ours AND of an unknown
model — it might be a Vero, which IS drivable — so it is offered, with a
`note` saying the model could not be read.

**The scan takes 8 seconds** (`scan_seconds` is in the answer), and the radio
is a single shared thing: the scan is taken under a process-wide lock, so two
callers cannot have two BLE scans running at once.

**The addresses in the payload are redacted, and the one the connect call
needs is the real one.** The row carries the real `address` — the page hands
it back and the server connects to it — and also `address_masked` (the OUI,
with the device bytes masked), which is what is ever printed. A caller that
wants to print an address must print `address_masked`.

Two things a caller is most likely to get wrong here:

**The capability booleans come from the vendor app's source, not from the
hardware.** `has_vibration: null` means *the model was not recognised*, and
that is a different statement from `has_vibration: false` (the model is
known and has no vibration). The Vero in hand reports no capabilities over
the wire at all, so `evidence` is `vendor-app-source` for every row.

**BLE permits exactly one central at a time.** A device connected to a
phone does not broadcast and will not appear in the list. "Not in the
list" very often means "your phone has it", not "something is broken".

On a host with no Bluetooth adapter the route answers **503** with a
readable `detail`, not a 500. This host has an adapter, so that branch does
not fire here.

**`POST /api/connect`** accepts an optional body `{"address": "..."}` to
connect to a specific device. With no body it connects to the current
target, exactly as before. Switching devices stops any running sweep,
drops the old link and reconnects — the per-link state is reset by
`connect()` itself, so no state survives from the previous device.

**`GET /api/tick`** reports the interval actually in force:
`tick_ms` / `tick_hz` (the generator's period) and
`frame_interval_ms` / `frame_hz` (the BLE pump's write period), plus the
`min_tick_ms` / `max_tick_ms` band.

**`POST /api/tick`** with `{"tick_ms": N}` sets it, taking effect
immediately — no restart. The band is **8–30 ms**, the vendor app's own
(`GeneratedMotionPolicy.MIN_TICK_MS` / `MAX_TICK_MS`); a value outside it
is a **422**, not a silent clamp.

The pump follows the tick, and the reported tick is the one the
generator will actually use — it is not always the nominal, because the
generator scales the tick with the traverse when no tick was named. The
two clocks are separate on purpose: the pump writes a *variable*, not a
queue, so a pump slower than the generator overwrites the positions in
between before the device sees them.

### The other UI-family control routes

These are the UI's own commands. They return the whole snapshot (no `ok`
field), so a caller reads the fields it wants out of the answer.

| Method | Path | Body | Measured |
|---|---|---|---|
| POST | `/api/target` | `{"percent":40}` | `200` — set the target position; the answer is the snapshot, with `target_percent` set and `reported_percent` showing where the device actually is |
| POST | `/api/stop` | — | `200` — let go: `target_percent` goes to `0.0` |
| POST | `/api/keepalive` | — | `200` — keep the link alive |
| POST | `/api/connect` | `{"address": "..."}` (optional) | `200` — connect, or connect to a specific device |
| POST | `/api/disconnect` | — | `200` — close the link; **stops a running sweep first** |
| GET | `/api/frame/{percent}` | — | `200 {"percent":50,"drive":5000,"vibration":0,"hex":"A5 5A 0D A0 B0 00 A0 01 0F 88 13 B0 E6"}` — the raw BLE frame a position becomes, without sending it |
| GET | `/api/state` | — | `200` — the whole internal snapshot |
| GET | `/api/pressure` | — | `200` — the UI family's pressure view; **this one has `armed`**, and its `object` block carries the per-slot data |

**`GET /api/frame/{percent}` does not move anything.** It renders the frame
a position would be encoded into and hands back the bytes as `hex`, which is
the cheapest way to see what a position actually costs on the wire.

**`POST /api/target` and `POST /api/stop` are the UI-family way to move the
device without a sweep.** `/api/stop` sets `target_percent` to `0.0` — it is
a RELEASE, not a disconnect: the link stays up.

### Battery and touch

| Method | Path | Precondition | Measured |
|---|---|---|---|
| GET | `/api/v1/battery` | none | `200` `{"ok":true,"percent":60,"percent_shown":null,"samples":0,"median_cell_volts":null,"filter_reason":"waiting for the first reading","pack_volts":15.0,"cell_volts":3.75,"table_cell_volts":3.7524,"table_percent":60,"in_range":true,"cell_count":4,"connected":true}` |
| GET | `/api/v1/battery/curve` | none | `200`, `8865 B`, `cell_count=4`, `full_at=95`, `points` has **122** entries; first `{"cell_volts":3.0,"pack_volts":12.0,"percent":0,"raw_percent":-5}`, last `{"cell_volts":4.153,"pack_volts":16.612,"percent":100,"raw_percent":100}` |
| GET | `/api/v1/pressure` | **armed** | `200` `{"ok":true,"position":8000,"position_pct":80,"active_slot":4,"touch_raw":2886,"touch_span_pct":100,"resting_level":1533,"press_delta":400,"pressure":2886,"raw_pct":70,"peak":2899,"pressed":true,"press_events":1,"frames":521,"streaming":true,"age_s":0.05,"sample_hz":19.6,"interval_ms":50.95}` |
| GET | `/api/v1/pressure/series` | **armed** | `200`, `243 B` with `count=0` while not armed, `count=191` once armed |
| GET | `/api/v1/pressure/series?seconds=N` | armed | `seconds` accepts **only** 10, 30, 60 |
| GET | `/api/v1/pressure/series?channel=N` | armed | accepts only 0 (primary) or 1 (secondary); both are always returned in `channel0` / `channel1` |

MEASURED on the out-of-range values, because the refusal is a plain string
`detail` here and not the Pydantic list shape the body routes use:

```
GET /api/v1/pressure/series?seconds=15   -> 422 {"detail":"seconds must be one of 10, 30, 60"}
GET /api/v1/pressure/series?channel=2    -> 422 {"detail":"channel must be 0 or 1"}
```

`?channel=0` and `?channel=1` each return that channel's own `values`
array; the response also carries the other channel's series as
`channel0` / `channel1`, so a caller that wants both takes them from the
same answer rather than making a second request.

**The answer carries a `unit` field that says what the numbers are:**
`"unit": "raw 12-bit ADC (touch position, not force)"`. The series is the
raw ADC, not the pressure in newtons and not a percentage.

### The device's own buttons

The device has three physical keys -- mode, `+` and `-` -- and the vendor
app dispatches them from the notification stream, so a key press arrives
over BLE like any other command. `GET /api/v1/buttons` is the public
family's view of that: what each key is bound to, the action menu, and
the keys that were pressed recently.

| Method | Path | Precondition | Measured |
|---|---|---|---|
| GET | `/api/v1/gait` | none | `200` `{"ok":true,"running":false,"step":0,"laps":0,"steps":[],"repeat":true,"elapsed":0.0,"error":""}` |
| POST | `/api/v1/gait/start` | connected | `200` — `{"steps": "30:3,70,20:1.5"}` walks a SEQUENCE of positions, each held for its time |
| POST | `/api/v1/gait/stop` | none | `200` — stops the gait and lets the device go back in |
| GET | `/api/v1/buttons` | none | `200` `{"ok":true,"bindings":{},"version":0,"buttons":["mode","plus","minus"],"actions":[...],"events":{"count":0,"events":[],"last":null},"bound_count":0}` |

`version` rises on every change to the bindings, so a caller that polls
can tell "the operator changed something" from "my copy is stale".

**The action catalogue is the server's.** Each entry in `actions` carries
`action` (the string the server takes back, e.g. `"travel:60"`), `kind`,
`arg` and a `label` for display. A caller does not compose an action
string of its own -- it takes one from the list, because the list and
the validator are the same table and a hand-built string can be one the
server refuses.

**`events` is an OBJECT, not a list:** `{"count": N, "events": [...],
"last": {...} or null}`. `count` is the number of presses since the last
clear, `events` is the recent history, and `last` is the most recent
one.

Binding a key is a POST to the UI family's `/api/buttons/bind` -- the
public family reads the bindings but does not write them, so a caller
cannot repurpose the operator's keys from outside.


## Watching the state: `GET /api/events` is a STREAM, not a poll

`GET /api/events` is Server-Sent Events. **A caller that treats it as a
JSON route will hang forever** — the connection stays open and each tick
arrives as a `data:` line. MEASURED headers and first frame:

```
HTTP/1.1 200 OK
content-type: text/event-stream; charset=utf-8
Transfer-Encoding: chunked

data: {"frames_sent": 641, "write_errors": 0, "target_percent": 0.0, ...}
```

Each `data:` payload is the SAME snapshot shape `GET /api/state` returns.
So a caller that wants live state has two honest options: read the stream
and parse each `data:` line, or poll `GET /api/state`.

**Never call `/api/events` with a plain JSON client and a short timeout
hoping it returns once.** Measured: a request with a 40 s read timeout
never returns and the client waits for the full timeout. Use a streaming
client, and remember to close it.

## The arm step: telemetry needs it, and it is not in /api/v1

Before arming, `GET /api/v1/pressure` returns nothing useful:

```
{"ok":true,"position":0,"position_pct":0,"touch_raw":null,"pressure":null,
 "frames":0,"streaming":false,"press_delta":400,"notify":{"total":0,...}}
```

`POST /api/arm` then returns `{"armed":true,"frames":52,"streaming":true, ...}`.

Two things a caller is most likely to get wrong here:

**First, `/api/v1/pressure` has no `armed` field.** `armed` belongs to the UI
route `/api/pressure`. If you poll `/api/v1/pressure` looking for `armed`, you
will conclude the sensors are down when they are working fine. Use `streaming`
and `frames` instead.

**Second, `arm_accepted` reflects whether frames are arriving, not whether a
write returned.** In `--simulate` mode the connect handshake already sent the
telemetry command, so `arm` sends nothing; its `source` field says exactly that:

```json
{"armed":true,"arm_accepted":true,"frames":52,"frames_at_arm":52,
 "source":"connect handshake (no extra command sent)"}
```

**Arm on a freshly connected link.** Arming on a link that has been streaming
for a while returns `arm_accepted:false` with `frames:0` — the previous session's
monitor is still being consulted. That is a stale-session artefact, not a device
fault. This skill's flow always disconnects before reconnecting, so each run
starts clean.

**Do not send the telemetry command twice.** The device treats a second
identical mode command as a mode switch and interrupts the stream the first one
started. `POST /api/arm` is therefore the right call, not a raw write.

## Reading errors

### 409 — not connected

Anything that needs a link returns this before you connect:

```
$ curl -s $B/stroke
{"detail":"not connected; POST /api/v1/connection/connect first"}       # 409
```

`GET /api/v1/connection`, `GET /api/v1/battery`, `GET /api/v1/battery/curve`,
`GET /api/v1/auto` and all four `auto` writes work while disconnected; they
report state rather than commanding it. Only `stroke` and `auto/start` require a
link.

**MEASURED, route by route, while disconnected — and the messages are NOT all
the same.** A caller that matches on the exact string must know that
`/api/v1/gait/start` says something shorter than the rest:

| Route | Body | Status | `detail` |
|---|---|---|---|
| `GET /api/v1/stroke` | — | 409 | `not connected; POST /api/v1/connection/connect first` |
| `POST /api/v1/stroke` | `{"percent":50}` | 409 | `not connected; POST /api/v1/connection/connect first` |
| `POST /api/v1/stroke/release` | — | 409 | `not connected; POST /api/v1/connection/connect first` |
| `POST /api/v1/auto/start` | valid body | 409 | `not connected; POST /api/v1/connection/connect first` |
| `POST /api/v1/intensity` | `{"level":1}` | 409 | `not connected; POST /api/v1/connection/connect first` |
| **`POST /api/v1/gait/start`** | `{"steps":"30:2"}` | **409** | **`not connected`** — no "; POST …" tail |
| `POST /api/v1/auto/stop` | — | **200** | no link needed |

**THE BODY IS VALIDATED BEFORE THE LINK IS CHECKED, and that decides whether
you see a 422 or a 409.** MEASURED while disconnected:

```
POST /api/v1/stroke  (no body)       -> 422 {"type":"missing","loc":["body"],"msg":"Field required"}
POST /api/v1/stroke  {"percent":150} -> 422 "Input should be less than or equal to 100"
POST /api/v1/stroke  {"vibration":10} -> 422 "Field required" (percent missing)
POST /api/v1/stroke  {"percent":50}  -> 409 not connected; POST ... first
```

**So a malformed body is a 422 whether or not a device is connected, and only
a VALID body reaches the 409.** A caller that reads a 422 as "the device is
not ready" is wrong: the request never got as far as the link check. This
ordering is FastAPI's — the body model is validated before the handler
runs, and the handler is where `_need_session()` lives.

`GET /api/v1/stroke` is a **read** and still returns 409 while disconnected —
the stroke family is the one family where even reading needs the link. The
other eight `/api/v1` reads (`connection`, `gait`, `auto`, `battery`,
`battery/curve`, `pressure`, `pressure/series`, `buttons`) all answer `200`
while disconnected.

**So a caller should branch on the STATUS, not on the message text.** Match
on `409` and treat the string as human-facing.

### 422 — four distinct shapes

| Trigger | Body | Meaning |
|---|---|---|
| `{"percent":150}` | `{"detail":[{"type":"less_than_equal","loc":["body","percent"],"msg":"Input should be less than or equal to 100","input":150,"ctx":{"le":100.0}}]}` | `percent` range `0..100` |
| `{"percent":50,"vibration":999}` | `{"detail":[{"type":"less_than_equal","loc":["body","vibration"],"msg":"Input should be less than or equal to 255","input":999,"ctx":{"le":255}}]}` | `vibration` range `0..255` |
| `{"vibration":10}` (no `percent`) | `{"detail":[{"type":"missing","loc":["body","percent"],"msg":"Field required","input":{"vibration":10}}]}` | `percent` is required |
| `{"min_percent":90,"max_percent":10}` to `/auto/params` | `{"detail":"max_percent (10) must be >= min_percent (90)"}` | inverted range |
| `{"max_percent":20}` when `min` is 35 | `{"detail":"max_percent (20) must be >= min_percent (35)"}` | inverted range after the partial merge |

### The inverted-range rule is deliberate — read this before you work around it

`POST /api/v1/auto/params` is a **partial update**: only the fields present are
applied. The server merges your fields onto the current parameters and then
validates. The underlying `AutoParams.clamp()` would happily swap an inverted
range so the motion stays well-defined — that is right for a UI slider, and
wrong for an API caller, because silently turning `{min: 80, max: 20}` into
`{min: 20, max: 80}` hides the mistake inside a `200`.

So the API **rejects** it with `422` instead:

```bash
# current min is 35; asking for max 20 is inverted after the merge
curl -s -X POST -H 'Content-Type: application/json' -d '{"max_percent":20}' $B/auto/params
{"detail":"max_percent (20) must be >= min_percent (35)"}
```

The `422` names **both** numbers, so a caller never has to re-read state to find
out what the server compared. Check the current range with `GET /api/v1/auto`
before sending an update, and send both bounds together.

**It validates before it clamps.** It does not silently swap. It does not return
`200` with swapped values. None of the three failure modes a caller usually
guards against are possible here.

### 503 — service not wired

Only reachable if the router was mounted without its dependencies, which does
not happen in the shipped app. `{"detail":"service not wired"}`.

## Field semantics that surprise callers

**`pressure` is not force.** The device quantises travel into five steps and
reports the deepest engaged one. `touch_raw` is a 12-bit ADC reading whose
deviation from the resting level says *where* the surface is touched. Neither
number says *how hard*. `position_pct` is the useful figure.

**`window_seconds` and `requested_seconds` are not comparable, and never will
be.** Measured on this session, both at `?seconds=10`:

| State | `count` | `window_seconds` | `requested_seconds` |
|---|---|---|---|
| before arm | `0` | `0.0` | `10.0` |
| streaming for a few seconds | `198` | `11.78` | `10.0` |
| a second later | `199` | `13.83` | `10.0` |

The buffer is older than the window you asked about, so `window_seconds` keeps
climbing past `10` while you watch, and it was already past it before any of
this was written. `monitor.py:403` states the rule in the source: *window only
bounds how many samples are retained, never how many seconds the caller asked
about* — the two are reported separately precisely so you do not have to
derive one from the other. **Do not compare them, and do not read
`window_seconds > requested_seconds` as a bug.** If you need exactly ten
seconds, slice it yourself using `t` (or `unix`); `t` is relative to the first
sample in the returned set, `unix` is absolute.

An earlier draft of this file listed this as a doc contradiction, on the
strength of one `26.8`-against-`10.0` pair. Both numbers were real and the
inference was wrong: they measure different quantities.

**`percent_shown` is filtered, `percent` is not.** `percent` is the voltage
converted right now. `percent_shown` is what the UI displays after a 10-second
median filter that confirms decreases twice and suppresses rises. It stays
`null` until the filter has enough samples — measured `null` with
`filter_reason:"waiting for the first reading"`. A caller wanting the
instantaneous figure should read `percent`.

**`frames_sent` counts writes, not frames received.** Measured `frames_sent:1`
immediately after connect while telemetry reported `frames:521`. They count
different things.

**Connecting twice is safe.** The second call returns
`{"ok":true,"connected":true,"already":true}`.

**Disconnecting stops the sweep.** `POST /api/v1/connection/disconnect` stops a
running auto motion before closing the link, so the device is not left holding a
position.

**The simulator identity is fixed.** `serial:"sim-device"`,
`firmware:"1.0.11"`, `power:15.0`, `rssi:-40`, `mtu:255`. These are simulator
values. Do not treat them as device measurements.

## CLI

`run_cli.py` has no `--simulate` flag — measured, the real usage line is
(two lines, and the subcommand list is longer than it used to be):

```
usage: vero [-h] [--target TARGET]
            {move,sweep,info,frame,scan,probe,status,watch,caps,frames,mode,gait,ramp,stop,tui} ...
```

`run_cli.py` always drives real hardware: `vero/cli.py:19` constructs
`DeviceSession(simulate=False, ...)`, and there is no flag that changes it.

`info` against the real device on this host succeeds — measured 2026-10-20.
**This block requires the device to be on the air**; when it is not, the same
command takes one of the failure branches below instead.

```
$ .venv/bin/python3 run_cli.py info
connecting to the device ...
connected. serial=UMVX<unit> fw=1.0.11 power=15.922368049621582 rssi=-26 mtu=255

  serial   : UMVX<unit>
  firmware : 1.0.11
  power    : 15.922368049621582 V
  RSSI     : -26 dBm
  MTU      : 255
  armed    : False
```

`power` tracks the battery pack voltage and drifts between runs (15.83 V and
15.93 V observed on the same day); `rssi` moves with the room. **The stable
fields are `serial`, `firmware` and `mtu`** — those reproduce on every run,
while `power` and `rssi` will not.

**This is the one block in this file that cannot be reproduced on demand.**
It needs the device powered on and not held by another central. The device
goes off the air for long stretches (it was off the air for the whole of one
measurement session on 2026-10-20, with `bluetoothctl info` reporting
`Device E7:97:7D:XX:XX:XX not available`), and while it is off the air this
command prints one of the failure first lines instead. That is the device's
state talking, not a defect in the command.

When the link cannot be made, the same command reports the boundary instead of
a traceback. The first line names the real cause; the bullets after it say what
to do next. **Read the first line, not the bullets** — the bullets are generic.

There are **three** different first lines, and they are not interchangeable.
All three below were produced by the repository's own code on this host, with
the repository **unmodified**. The two failure branches were reached naturally:
the device was off the air at the time, so `find_device()` found nothing and
the branch fired on its own.

**These two blocks are conditional on the device being off the air.** With the
device on the air, `run_cli.py info` succeeds, and `run_cli.py --target <addr>
info` *also* succeeds — a wrong address is silently corrected by the name
fall-through described below. The durable evidence for these two branches is
the source quote further down, not the capture.

**With `--target` (an address was given):**

```
$ .venv/bin/python3 run_cli.py --target 00:00:00:XX:XX:XX info
connecting to 00:00:00:XX:XX:XX ...
could not connect: 00:00:00:XX:XX:XX is not advertising.
  saw while scanning: ...
  - is the device on, and not connected to a phone?
  - Windows: Settings > Bluetooth should be on
  - run 'scan' to see what is actually advertising
```

The `saw while scanning:` line lists whatever was advertising at that moment,
so its contents differ on every run. One capture, in full:

```
could not connect: 00:00:00:XX:XX:XX is not advertising.
  saw while scanning: 6B:E9:90:XX:XX:XX  (no name advertised)  rssi=-44; 74:C6:39:XX:XX:XX  NGMQMLyrAx1s3jlyNDhIT9YC8  rssi=-45; D0:A1:1D:XX:XX:XX  (no name advertised)  rssi=-37; 63:CB:BE:XX:XX:XX  (no name advertised)  rssi=-34; 4F:B7:2D:XX:XX:XX  (no name advertised)  rssi=-17; E7:2E:00:XX:XX:XX  LYWSD02  rssi=-44
  - is the device on, and not connected to a phone?
  - Windows: Settings > Bluetooth should be on
  - run 'scan' to see what is actually advertising
```

**Without `--target` (scan by name):**

```
$ .venv/bin/python3 run_cli.py info
connecting to the device ...
could not connect: device not found by name (UMVX<unit>, Amorlinkvex) or service UUID.
  saw while scanning: ...
  If the device is powered on, the usual cause is that a phone is still connected to it:
  BLE permits exactly one central at a time, so close the vendor app completely and retry.
  - is the device on, and not connected to a phone?
  - Windows: Settings > Bluetooth should be on
  - run 'scan' to see what is actually advertising
```

One capture, in full:

```
could not connect: device not found by name (UMVX<unit>, Amorlinkvex) or service UUID.
  saw while scanning: E7:2E:00:XX:XX:XX  LYWSD02  rssi=-46; 88:12:AC:XX:XX:XX  (no name advertised)  rssi=-31; D0:A1:1D:XX:XX:XX  (no name advertised)  rssi=-53; 74:C6:39:XX:XX:XX  NGMQMLyrAx1s3jlyNDhIT9YC8  rssi=-46; 63:CB:BE:XX:XX:XX  (no name advertised)  rssi=-37; 4F:B7:2D:XX:XX:XX  (no name advertised)  rssi=-13
  If the device is powered on, the usual cause is that a phone is still connected to it:
  BLE permits exactly one central at a time, so close the vendor app completely and retry.
  - is the device on, and not connected to a phone?
  - Windows: Settings > Bluetooth should be on
  - run 'scan' to see what is actually advertising
```

Note that the device is **absent** from both `saw while scanning:` lists. That
is not a coincidence — it is the rule the two branches obey, stated below:
in the "not found by name" branch, no row in that list can carry a criterion
name, because a row that did would have matched and been returned.

The `saw while scanning:` contents are whatever was around at the time and will
differ on your run; the **first line** and the structure are what to read.

| First line | What it actually means | Trigger |
|---|---|---|
| `<addr> is not advertising.` | The device was **not found at all** — the address lookup missed *and* the name/UUID scan matched nothing. It does **not** mean "the device is off": the device may be broadcasting under a *different* address, or under a name outside the criterion set, and the search simply did not find it. It means "the search for this device found nothing". | An address was given (`--target`, `--address`, `POST /api/connect {"address": ...}`) **and** nothing matched by address, name or service UUID |
| `device not found by name (...) or service UUID.` | The scan **ran**, and **nothing matched** the name set or the vendor service UUIDs. Same reach as the row above — the device being absent from the scan, held by a phone, or advertising under an unrecognised name all land here. | **No address was given**, so the device is looked up by name or service UUID only, and no advertiser matched |
| `('No Bluetooth adapters found.', <BleakBluetoothNotAvailableReason.NO_BLUETOOTH: 1>)` | The **host** has no usable adapter — no adapter at all, none with the BLE `central` role, or none powered on. Raised by bleak before any scan happens. This host does not hit that one. | The host itself cannot do BLE; nothing was ever scanned |

**A wrong address usually does NOT produce the first line — it produces a
successful connection to the right device.** `find_device()` tries the address
first and, on a miss, deliberately falls through to a name/UUID search:

```python
# vero/discovery.py:223-232
    if address:
        dev = await BleakScanner.find_device_by_address(address, timeout=timeout)
        if dev is not None:
            return dev
        # Fall through: an address miss can be a stale cache entry.

    raw = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for _addr, dev, adv in _iter_pairs(raw):
        if _matches(dev, adv, names, services):
            return dev
```

Measured on this host with a deliberately wrong address:

```
$ .venv/bin/python3 -c 'import asyncio; from bleak import BleakScanner; from vero.discovery import find_device, _iter_pairs, _names_of; exec("async def m():\n    raw = await BleakScanner.discover(timeout=10, return_adv=True)\n    named = None\n    for a, d, v in _iter_pairs(raw):\n        nm = _names_of(d, v)\n        if nm:\n            named = (a, sorted(nm)[0])\n            break\n    if not named:\n        print(\"no named advertiser on the air right now\")\n        return\n    addr, name = named\n    print(\"a named advertiser on the air :\", addr, name)\n    d = await BleakScanner.find_device_by_address(\"00:00:00:XX:XX:XX\", timeout=8)\n    print(\"find_device_by_address(bogus) ->\", d)\n    got = await find_device(address=\"00:00:00:XX:XX:XX\", names=(name,), timeout=15)\n    print(\"find_device(address=bogus) ->\", got)\n    print(\"   its real address:\", getattr(got, \"address\", \"?\"))\n    print(\"   its real name   :\", getattr(got, \"name\", \"?\"))\nasyncio.run(m())")'
a named advertiser on the air : A0:5A:59:XX:XX:XX GameSir-Tarantula Pro
find_device_by_address(bogus) -> None
find_device(address=bogus) -> A0:5A:59:XX:XX:XX: GameSir-Tarantula Pro
   its real address: A0:5A:59:XX:XX:XX
   its real name   : GameSir-Tarantula Pro
```

The probe picks whatever named advertiser happens to be on the air and uses its
name as the criterion, so it works whatever the radio conditions are; only the
**shape** of the output is the claim — the address lookup misses, and the name
fall-through finds the device anyway.

So a bad `--target` is **not** caught by the address check. The device is
found by name and connected anyway. The address only decides *which* failure
message you get when the search finds nothing at all.

**The address does not change what the failure means — it only changes which
of the two messages you get.** Both branches sit behind one `if`, and it tests
the address:

```python
# vero/session.py:514-527
        if dev is None:
            seen = last_scan()
            detail = ("\n  saw while scanning: %s" % "; ".join(seen[:6])
                      if seen else "\n  saw nothing advertising at all")
            if self.address:
                raise RuntimeError(
                    "%s is not advertising.%s" % (self.address, detail))
            raise RuntimeError(
                "device not found by name (%s) or service UUID.%s\n"
                "  If the device is powered on, the usual cause is that a "
                "phone is still connected to it:\n"
                "  BLE permits exactly one central at a time, so close the "
                "vendor app completely and retry."
                % (", ".join(DEVICE_NAMES), detail))
```

**The `saw while scanning:` list comes from the same scan as the match, and
that decides what the two branches can mean.** `find_device()` performs one
`BleakScanner.discover()`, matches against those rows, and — only if nothing
matched — renders *the same rows* into the list:

```python
# vero/discovery.py:229-236
    raw = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for _addr, dev, adv in _iter_pairs(raw):
        if _matches(dev, adv, names, services):
            return dev
    del _last_scan[:]
    for addr, dev, adv in _iter_pairs(raw):
        _last_scan.append(describe(addr, dev, adv))
    return None
```

`_matches()` and `describe()` read the same name set, so a row that the list
renders with one of the criterion names would have matched, and
`find_device()` would have returned it. **Therefore, in the "not found by
name" branch, no row in that list carries a criterion name.** If you see the
device's name there, you are not looking at the "not found by name" line.
Measured on this host while the device was advertising at `rssi=-13`:

```
$ .venv/bin/python3 run_cli.py scan --seconds 12
  E7:97:7D:XX:XX:XX   UMVX<unit>                      -13 dBm  <<< THIS DEVICE
9 device(s) seen, 1 match this project
```

The `<<< THIS DEVICE` marker and the `1 match this project` count are the two
lines that carry the claim; the other rows and the device count are whatever
was around at the time. With the device off the air the same command prints
`0 match this project`, which is the state the two failure captures above were
taken in. `run_cli.py scan` never fails for want of a device — it reports what
is on the air — so this block is the cheapest way to tell the two states apart
before reading anything else in this section.

The `saw while scanning:` line is still the useful part of a failure — it
tells you what **was** advertising, and `saw nothing advertising at all`
(replacing it entirely) means the radio saw nothing at all, which is an
adapter problem rather than a device problem.

## Measured facts that contradict the docs

Recorded here rather than corrected in place, because the prose files are
covered by their own audit trail. One row is a case where **the docs are
right** and the natural instinct to record a contradiction is what would have
been wrong — see the note under the table.

| Claim | Measured |
|---|---|
| `hciconfig` is not installed | It is, at `/usr/bin/hciconfig`, and it lists `hci0` — `UP RUNNING`, `Barrot Technology Limited (2279)` |
| `/api/v1/pressure` reports `armed` | It has no `armed` field; `armed` is on `/api/pressure` |
| `run_cli.py` accepts `--simulate` | It has no such flag |
| `/api/v1/openapi.json` does not exist | It does: `200`, `45886 B`, `50 paths` (52 routes — two paths carry two methods each) |
| `auto/params` works only while connected | It returns `200` while disconnected |
| `auto/params` silently clamps an inverted range | It returns `422` |

**The OpenAPI row is a doc that holds up, not a doc that lies.** An earlier
draft of this table put `/openapi.json` (no `/api/v1` prefix) here with a `404`
as the measurement — that URL is genuinely not a route, so the 404 is true and
irrelevant: it tested a path nobody claimed existed and filed the result as a
contradiction. The machine-readable spec is at the prefixed path, it returns
`50` paths (52 routes — `/api/v1/stroke` and `/api/tick` each carry two
methods) including every route in this skill plus the UI family, and it is
the cheapest way to enumerate the surface:

```bash
curl -s http://127.0.0.1:8807/api/v1/openapi.json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d["paths"]), "paths")'
```

**The spec is the live surface, and the two families can be counted apart
from it.** Measured on the running service: 18 paths under `/api/v1/`, 27
under `/api/` (UI family), and 5 that serve the page itself (`/`,
`/index.html`, `/js/{name}`, `/vendor/{name}`, `/icon/{name}`).

Keeping it in a table titled "contradicts the docs" reads oddly. It is kept
there because removing the row silently would leave the deleted draft looking
like it was never tested, and the reason that draft was wrong is the single
most transferable mistake in this file: **a 404 is evidence about the URL you
asked, never about the claim you did not.**

## Related

- `docs/VERO-8800-API.md` — the Chinese quick-reference for callers
- `scripts/verify_api_doc.py` — re-checks every claim in that doc against the live service
- `00-START-HERE.md` — orientation for a person
- `README.md` — install and CLI usage
- `docs/` — protocol reverse-engineering conclusions with evidence-strength tags
- `vero/web/index.html` — the UI; `/api/...` exists to serve it
