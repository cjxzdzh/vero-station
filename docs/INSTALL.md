# INSTALL.md — vera-station-http-api v1.0.0

A **skill** for an agent runtime: a folder with a `SKILL.md` that the runtime
loads, plus the scripts that prove its claims. It is not an application and
it has no daemon of its own.

## What this bundle is

The HTTP API of a Vero device, documented by measurement. The device is
controlled over HTTP by a small web service; this skill tells an agent (or a
person writing a script) exactly what that service answers, which route needs
what first, and which two error codes are easy to confuse.

**The skill is documentation plus a verifier.** It does not install a service,
does not talk to a device on its own, and does not need to be running for the
documentation to be useful.

**This bundle documents a service you install separately.** The service is
the Vero Station project — <https://github.com/cjxzdzh/vero-station> — and
this bundle is only its HTTP API reference. If you have not installed the
service yet, do that first: the routes below are *documented* here, not
*served*.

## Step 0 — Check what you are pointing at (MANDATORY)

You are about to write commands that MOVE A PHYSICAL DEVICE. Do this before
any of them.

```bash
# 1. Is the service there?
curl -s -o /dev/null -w '%{http_code}\n' http://<lan-ip>:8800/

# 2. Is it a real device or a simulator?  THIS IS THE ONE THAT MATTERS.
curl -s http://<lan-ip>:8800/api/v1/connection \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("simulated:", d.get("simulated"), "| connected:", d.get("connected"), "| device:", d.get("device",{}).get("serial"))'
```

You must see one of these before continuing:

| You see | What it means | What you may do |
|---|---|---|
| `simulated: False` | **A REAL DEVICE.** A write moves it. | Read-only calls only, until you have a simulator (Step 3). |
| `simulated: True` | A simulator. | Anything. |

**If `simulated: False`, stop and start Step 3 first.** A `POST /api/v1/stroke`
against a real device moves a real device.

Also check the LAN gate, because it decides who else can reach your device:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://<lan-ip>:8800/api/state
```

`200` means **no access token is required** — every route, including the
device's identity and the recorded sessions, is reachable by anyone on the
LAN.
That is the shipped default, not a hole; but if this service is on a network
you do not fully control, set `VERO_ACCESS_TOKEN` and restart it (the
SKILL.md section "The LAN gate" has the exact commands).

## Step 1 — Extract + verify integrity

```bash
tar -xzf vera-station-http-api-v1.0.0.tar.gz
cd vera-station-http-api-v1.0.0/
sha256sum -c bundle.manifest          # every line must say OK
```

If a line says `FAILED`, the download is damaged — re-fetch it. The tarball's
own checksum is in `vera-station-http-api-v1.0.0.tar.gz.sha256`; check it
BEFORE extracting, so you can tell a bad download from a bad bundle.

## Step 2 — Install the skill where your runtime looks for skills

The skill is one directory. Copy it into your runtime's skills directory:

```bash
# wherever YOUR runtime reads skills from -- examples:
cp -r vera-station-http-api ~/.hermes/skills/       # Hermes
cp -r vera-station-http-api ~/.claude/skills/       # Claude Code
cp -r vera-station-http-api ~/.codex/skills/        # Codex
```

**One copy, one name.** Two copies under the same skill name make the name
ambiguous and the skill UNLOADABLE — a runtime that finds two candidates
refuses to guess which one to load. If you install it in more than one place,
rename the directory in one of them.

**Do not install it into a directory that contains other `SKILL.md` files** if
your runtime walks that tree recursively: it will register those too.

## Step 3 — Start a simulator for anything that writes

The service is normally started by whoever deployed it, against real
hardware. To test writes safely, start your own simulator:

```bash
cd <your vera-station checkout>
.venv/bin/python3 start_web.py --simulate --host 127.0.0.1 --port 8807
```

On Windows the interpreter is in the other place:

```bat
cd <your vera-station checkout>
.venv\Scripts\python.exe start_web.py --simulate --host 127.0.0.1 --port 8807
```

Then point your commands at `http://127.0.0.1:8807` instead of
`http://<lan-ip>:8800`.

**The simulator is opt-in and only the web entry point can reach it.**
`start_web.py` defaults to real hardware; `--simulate` is what turns the
simulator on. The CLI (`run_cli.py`) has **no** `--simulate` flag at all and
always drives real hardware.

Check it:

```bash
curl -s http://127.0.0.1:8807/api/v1/connection \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("simulated:", d.get("simulated"))'
# simulated: True
```

## Step 4 — Run the verifier

The bundle ships the script that produced the measurements in SKILL.md and in
`references/VERO-8800-API.md` (the Chinese quick-reference). Run it against
**your** service and see the checks pass:

```bash
VERO_LIVE_HOST=<lan-ip> .venv/bin/python3 scripts/verify_api_doc.py
```

On Windows:

```bat
set VERO_LIVE_HOST=<lan-ip>
.venv\Scripts\python.exe scripts\verify_api_doc.py
```

The verifier is cross-platform: it finds the venv interpreter in either
layout, and it skips the systemd-unit checks when the machine has no
`systemctl` — which is every Windows machine, and is the same path a
Linux machine without a unit already took.

What it does:

* enumerates the live OpenAPI spec and checks the surface is 50 paths / 52
  routes;
* checks every path the documentation names actually exists;
* re-runs every **read-only** claim against the deployed service;
* checks the LAN-gate claim and the SSE claim;
* starts its **own** simulator and re-runs every **write** claim there — it
  never writes to your real device;
* checks the per-route 409 messages, and that a malformed body is a 422 even
  while disconnected.

Expected: `60 passed, 0 failed`, and exit code 0. (The count is 60 and
not 62 on a machine that has no `vera-preview.service` unit: the two
deployment checks describe the reference deployment, and they are skipped
where there is no unit.)

If the service you point it at is older than this documentation, some checks
will fail and the output will say which claim is out of date — **that is the
script working**, not a broken bundle. Read the failing check against the
live service before trusting either side.

**The verifier needs the project's own tree** for `start_web.py` and the
`vero` package. If you only took the skill folder, run only the read-only
part against a live service; the write checks need the tree.

## Step 5 — Use it

The skill is now loadable by name `vera-station-http-api`.

A first call, end to end, against the simulator:

```bash
U=http://127.0.0.1:8807
B=$U/api/v1

curl -s -X POST $B/connection/connect
curl -s -X POST $U/api/arm; sleep 3          # telemetry needs this
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"percent":45,"vibration":90}' $B/stroke
curl -s $B/pressure
curl -s -X POST $B/stroke/release
curl -s -X POST $B/connection/disconnect
```

**Two things that trip people up, both measured:**

* **Arming must come after connecting**, and there is no `/api/v1/arm` — the
  telemetry step is `POST /api/arm` in the UI family.
* **The body is validated before the link is checked.** A malformed body is a
  `422` whether or not a device is connected; only a *valid* body reaches the
  `409`. So a `422` does not mean "the device is not ready".

## What is in the bundle

```
vera-station-http-api-v1.0.0/
├── SKILL.md                        the skill: the full API reference
├── INSTALL.md                      this file
├── BUILD_INFO.json                 provenance
├── bundle.manifest                 per-file sha256 (sha256sum -c compatible)
├── references/
│   └── VERO-8800-API.md            the Chinese quick-reference
└── scripts/
    └── verify_api_doc.py           re-checks every documented claim
```

## Provenance and honesty

`BUILD_INFO.json` records where this came from, when, and what the
verification was. Read it rather than trusting the prose here.

The measurements in this bundle were taken against a real deployed service on
2026-10-31, and every quoted response is reproduced verbatim in
`bundle.manifest`-verified files. The numbers that are expected to be true
everywhere (route counts, error shapes, the 422-before-409 ordering) were
checked by a verifier that was itself **mutation-tested**: eight deliberate
corruptions were applied to the documentation, and the verifier caught all
eight.
