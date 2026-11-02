"""Cross-platform web control surface.

Same design as the Linux build, minus the Linux-only pieces, plus a link
diagnostics panel -- on Windows that panel is the point: the user needs to see
whether this host drops the link at ~40 s like the Linux one does.

Binding is explicit. The Linux version bound to 0.0.0.0, which is wrong for a
control surface on a home network. This defaults to 127.0.0.1 and requires an
explicit --host 0.0.0.0 to be reachable from another machine.

Routes:
    GET  /                 the page
    GET  /api/state        snapshot
    GET  /api/events       server-sent events
    POST /api/connect      open the link
    POST /api/disconnect   close it (stops any sweep first)
    POST /api/target       {percent, vibration}
    POST /api/stop         immediate zero (stops any sweep first)
    POST /api/auto/start   {min, max, vibration, ...} begin sweeping
    POST /api/auto/stop    end the sweep
    POST /api/auto/params  change the sweep while running
    GET  /api/auto         sweep state
    GET  /api/pressure     telemetry snapshot
    POST /api/arm          command 54, stream 0x29 frames
    POST /api/keepalive    client heartbeat
    GET  /api/frame/{n}    the exact bytes for a level (debug)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from typing import Optional

# Running `python -m vero.webapp` from inside the package directory cannot
# resolve the package itself, because Python puts the *current* directory on
# the path, not its parent. Put the parent there too, using os.path so Windows
# separators work. Harmless when the package is already importable.
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (FileResponse, JSONResponse, Response,
                               StreamingResponse)
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from . import protocol as P
from .automotion import (AutoMotion, AutoParams, MAX_TICK_SECONDS,
                         MIN_TICK_SECONDS, PRESETS, TICK_SECONDS)
from .buttons import (ActionError, BUTTON_NAMES, VIB_TOGGLE_ON,
                      parse_action, store as button_store)
from .monitor import PressureMonitor
from .recorder import (MAX_PAGE_SIZE, RECORDS_DIR, Recorder,
                       list_recording_names, list_recordings, read_curve,
                       read_header, record_name_pattern, write_imported)
from .session import DeviceSession

HERE = os.path.dirname(os.path.abspath(__file__))

#: Where the recordings live, and what a recording's file name may look
#: like.
#:
#: The name pattern is deliberately narrow -- a timestamp and a counter,
#: nothing else -- and it is checked BEFORE the name is joined to the
#: directory, so a name carrying a path separator or a ".." can never be
#: turned into a path. The realpath check afterwards is the second half:
#: a symlink inside the directory would pass the pattern.
_RECORDS_DIR = RECORDS_DIR

#: A recording's file name.
#:
#: The name pattern is deliberately narrow -- a timestamp and a counter,
#: nothing else -- and it is checked BEFORE the name is joined to the
#: directory, so a name carrying a path separator or a ".." can never be
#: turned into a path. The realpath check afterwards is the second half:
#: a symlink inside the directory would pass the pattern.
#:
#: There are TWO shapes, because there are two kinds of file in the
#: directory:
#:
#:   <timestamp>.jsonl          -- a recording this machine took
#:   20261024-155150-导入.jsonl      -- an archive imported from a file
#:
#: The "-导入" marker is what tells the list which is which, and it is
#: part of the name so it survives a restart and a move between
#: machines. Nothing else is allowed in: the marker is a fixed word, not
#: a free-text field, so a name can still not carry a path.
#:
#: THE PATTERN IS THE RECORDER'S OWN, NOT A SECOND COPY OF IT. The
#: recorder used to write "%s-%03d.jsonl" and this regex allowed
#: exactly three digits -- and "%03d" is a MINIMUM width, not a maximum,
#: so the 1000th recording was named "...-1000.jsonl": it appeared in
#: the history list and could not be opened, because this regex refused
#: it. The server wrote files it could not read back.
#:
#: Now the writer and the reader share ONE pattern
#: (recorder.record_name_pattern()), so a name the recorder produces is
#: a name this regex accepts -- by construction, not by remembering.
_RECORD_NAME = record_name_pattern()

#: The largest request body the import endpoint accepts, in bytes.
#:
#: The import endpoint takes a whole archive as one JSON body and
#: writes it out row by row, so its size is the size of the recording.
#: Measured on this tree: 200 000 sample points import in 0.69 s and
#: produce a file of that many lines. Without a limit, one POST could
#: write an arbitrarily large file AND hold the event loop for the
#: duration of the write.
#:
#: 64 MiB is far above any archive this project produces (a 60-minute
#: session at 10 samples a second is about 36 000 samples) and far
#: below anything that could exhaust memory. The refusal is a 413 with
#: a reason, not a 500.
MAX_IMPORT_BODY_BYTES = 64 * 1024 * 1024

#: The largest number of sample points one import may carry.
#:
#: A separate limit from the byte limit, because the two guard
#: different things: the byte limit bounds the MEMORY the request body
#: costs, and this one bounds the SIZE OF THE FILE the request writes.
#: A body of 20 000 000 tiny samples is small in bytes and enormous in
#: rows, and each row is a line on disk.
MAX_IMPORT_SAMPLES = 2_000_000

#: The tick band the operator may choose, in milliseconds.
#:
#: These are the VENDOR APP's own constants, not numbers invented here:
#: ``GeneratedMotionPolicy.MIN_TICK_MS = 8``,
#: ``NOMINAL_TICK_MS = 15``, ``MAX_TICK_MS = 30``
#: (analysis/decompile/jadx-123/sources/com/app/aike/model/
#: GeneratedMotionPolicy.java). The app clamps its own sampling period
#: into this band, so an interval outside it is not an interval the app
#: can produce -- which is exactly the point: the control lets the
#: operator try what the app does, and nothing beyond it.
#:
#: The clamp lives on the SERVER because a slider can be driven by a
#: script and a request can be forged; the frontend's own bounds are a
#: convenience, not the rule.
_TICK_MIN_MS = int(round(MIN_TICK_SECONDS * 1000.0))
_TICK_MAX_MS = int(round(MAX_TICK_SECONDS * 1000.0))
_TICK_NOMINAL_MS = int(round(TICK_SECONDS * 1000.0))

#: How long a scan may run before the endpoint gives up. The scan itself
#: is what takes the time -- bleak collects advertisements for the whole
#: window -- so this is the scan window, not a timeout on a fast call.
SCAN_SECONDS = 8.0

#: THE DEVICE SCAN IS A SHARED RESOURCE, SO IT IS DONE ONCE AT A TIME.
#:
#: BlueZ's scanner is one scanner. Two concurrent scans do not scan
#: twice as fast -- they fight, and the loser answers with an error
#: that means nothing to the operator. The old code let every request
#: start its own 8-second scan, so a page that polled the endpoint
#: twice would queue two 8-second scans behind each other.
#:
#: The lock is per-PROCESS, not per-request: the second caller WAITS
#: for the first scan and then gets its own fresh scan. That is not
#: "the same answer for everyone" -- it is "the scanner is used by one
#: caller at a time", which is what the radio can actually do.
_scan_lock = asyncio.Lock()

# The page ships inside the package so a copy of this folder is enough to run
# it -- copying to Windows must not require a sibling directory. Prefer the
# in-package location, and fall back to a sibling one only if that is somehow
# missing, so an existing checkout keeps working either way.
_PACKAGE_WEB = os.path.join(HERE, "web")
_SIBLING_WEB = os.path.join(os.path.dirname(HERE), "web")
if os.path.isdir(_PACKAGE_WEB):
    WEB_DIR = _PACKAGE_WEB
elif os.path.isdir(_SIBLING_WEB):
    WEB_DIR = _SIBLING_WEB
else:
    raise RuntimeError(
        "index.html not found; expected %s" % os.path.join(
            _PACKAGE_WEB, "index.html"))

# The UI's own endpoints live on /api/*. The public, stable contract is
# under /api/v1 and documented in vero/api.py. Interactive docs are enabled
# because the API is meant to be called from outside this process -- someone
# has to be able to discover it without reading the source.
app = FastAPI(title="Vero control", version="1.0",
              docs_url="/api/v1/docs", redoc_url=None,
              openapi_url="/api/v1/openapi.json")


#: THE OPTIONAL LAN GATE.
#:
#: The service is started with `--host 0.0.0.0`, so every device on the
#: LAN can reach it -- and there is no authentication on any route. For
#: a control panel that is a risk; for THIS project it is a private
#: control surface for a personal device, and the operator's own network
#: is the boundary.
#:
#: MEASURED on the live service before this: the whole API answered a
#: LAN request with no credential -- `GET /api/state` returned the
#: device's identity, and `GET /api/records` returned the recorded
#: sessions. No credential was readable -- the API key is never echoed --
#: but the control surface itself was open.
#:
#: THE GATE IS OPT-IN AND OFF BY DEFAULT, because turning it on
#: changes how the operator reaches the panel, and that is the
#: operator's decision and not the agent's.
#:
#: Set `VERO_ACCESS_TOKEN` to a secret and every request must carry
#: it, as `X-Vero-Token` or `?token=`. The page sends it: it is
#: written into the page by the server when the gate is on.
#:
#: With the variable unset, nothing changes -- the service behaves
#: exactly as it does now.
ACCESS_TOKEN = os.environ.get("VERO_ACCESS_TOKEN", "").strip()



@app.middleware("http")
async def _access_gate(request: Request, call_next):
    """The optional LAN gate. See ACCESS_TOKEN.

    With the variable unset this does nothing at all -- the service
    behaves exactly as it did before, which is what makes it safe to
    ship: the operator turns it on when they want it.

    With it set, a request that does not carry the token is refused
    with 401. The page's own assets are exempt, because the page has
    to load before it can send the token -- and the token is handed to
    the page in the HTML itself.
    """
    if not ACCESS_TOKEN:
        return await call_next(request)
    path = request.url.path
    # The assets the page needs before it can authenticate.
    if path in ("/", "/index.html") or path.startswith("/js/") \
            or path.startswith("/vendor/") or path.startswith("/icon/"):
        return await call_next(request)
    given = request.headers.get("x-vero-token", "").strip()
    if not given:
        given = request.query_params.get("token", "").strip()
    if given != ACCESS_TOKEN:
        return JSONResponse(status_code=401,
                            content={"detail": "需要访问令牌。"})
    return await call_next(request)


#: THE ONE PLACE A NON-FINITE NUMBER IS REFUSED.
#:
#: Python's json.loads accepts the bare literals ``NaN``, ``Infinity``
#: and ``-Infinity``, so a request can carry a non-finite number into
#: the server. Pydantic's own range checks (ge/le) compare False
#: against NaN, so the request is rejected -- and then the rejection
#: ITSELF raised: FastAPI's default handler puts the offending input
#: into the error body, and the body is rendered with
#: ``json.dumps(allow_nan=False)``, which raises ValueError. The
#: observable result was a 500 with no information on endpoints that
#: take a float: /api/target, /api/auto/params, /api/auto/start,
#: /api/v1/stroke.
#:
#: So the refusal is made EXPLICIT and GLOBAL: every float field in
#: every request model declares ``allow_inf_nan=False`` (see
#: _finite_float() below), and this handler scrubs whatever non-finite
#: number is still in the error report so that even a field that
#: escaped the model check cannot produce a 500.
#:
#: One boundary, not one per endpoint: a rule that has to be repeated
#: at every call site is a rule that will be forgotten at one of them,
#: and the endpoint that forgot it is the one that 500s.
def _scrub_non_finite(value):
    """Replace every non-finite number in a structure with its NAME.

    The name ("NaN", "Infinity") rather than null, because the point of
    the message is to say what was wrong with the request: "the value
    you sent was not a number a measurement can hold" is more useful
    than "the value you sent was null".
    """
    if isinstance(value, float):
        if value != value:
            return "NaN"
        if value == float("inf"):
            return "Infinity"
        if value == float("-inf"):
            return "-Infinity"
        return value
    if isinstance(value, dict):
        return {k: _scrub_non_finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_non_finite(v) for v in value]
    return value


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request,
                               exc: RequestValidationError) -> JSONResponse:
    """Answer a refused request with 422, and never with a 500.

    The default handler re-serialises the offending input, and a
    non-finite input cannot be serialised by strict JSON. So the input
    is scrubbed HERE, before the body is built.

    The errors are first run through ``jsonable_encoder`` for the same
    reason the default handler does it: an error's ``ctx`` holds the
    exception that was raised (a ValueError for a failed range check),
    and that is not JSON. Encoding it first, then scrubbing, means the
    one place that knows about non-finite numbers is the only place
    that has to.

    The raised exception's own message is put back into the body as
    ``msg``: ``jsonable_encoder`` turns a ValueError into ``{}``,
    which throws away the sentence the operator needs -- "tick_ms must
    be between 8 and 30 ms" is the whole answer, and an empty object
    is not.

    The message is read from the RAW error list, before the encoding:
    ``jsonable_encoder`` has already flattened the exception by then.
    """
    raw = exc.errors()
    errors = jsonable_encoder(raw)
    for index, item in enumerate(errors):
        if not isinstance(item, dict):
            continue
        item["input"] = _scrub_non_finite(item.get("input"))
        # The RAW error's ctx still holds the exception itself.
        if index < len(raw):
            raw_ctx = raw[index].get("ctx")
            if isinstance(raw_ctx, dict):
                raised = raw_ctx.get("error")
                if isinstance(raised, Exception) and str(raised):
                    item["msg"] = str(raised)
    return JSONResponse(status_code=422, content={"detail": errors})


_monitor = PressureMonitor()

_session: Optional[DeviceSession] = None
_opts = {"address": None, "simulate": False}
_subscribers: list[asyncio.Queue] = []
_auto_motion: Optional[AutoMotion] = None
_session_started: float = 0.0
#: Background tasks owned by the current session. Declared here so connect()
#: and disconnect() agree on them: one arm loop and one status pump per
#: session, never one per request.
_arm_task: Optional[asyncio.Task] = None
_pump_task: Optional[asyncio.Task] = None
#: The recorder's sampling pump. A third per-session task, owned by the
#: same connect()/disconnect() pair as the arm loop and the status pump,
#: so it starts and stops with the link and never outlives it.
_record_task: Optional[asyncio.Task] = None

#: The RECORDER. One per server process, holding at most one open
#: recording.
#:
#: The session is recorded "from the connection, ending when the
#: connection drops", with the operator pressing nothing. So the recorder
#: is owned here, started by connect() and stopped by
#: disconnect() -- the two places that already know when the link comes
#: up and goes down. There is no second definition of "the link is up"
#: anywhere in the program.
#:
#: It is a MODULE-LEVEL object, like the monitor and the session, and for
#: the same reason: it is per-server state, not per-request.
#:
#: THE RECORDER IS POINTED AT _RECORDS_DIR, NOT AT ITS OWN DEFAULT.
#: The recorder's default is the recorder module's RECORDS_DIR, and the
#: webapp's own constant is that same path -- but they are two
#: SEPARATE OBJECTS, and a test (or anything else) that redirects
#: webapp._RECORDS_DIR redirects only ONE of them. The recorder then
#: wrote into the real directory while the endpoints read the
#: temporary one, so the recording appeared in the response and was
#: not on disk where the response said it was.
#:
#: One directory, named once: the recorder is constructed with the
#: webapp's own directory, so the two cannot disagree.
#:
#: The recorder's directory is read DYNAMICALLY, through a small
#: indirection, because _RECORDS_DIR itself is a module-level constant
#: that a test may redirect at runtime. A recorder that captured the
#: path at import time would keep writing into the original directory
#: after the constant had been moved -- which is exactly the split
#: this is here to prevent.
#: The directory the recordings live in, CACHED.
#:
#: os.path.realpath() resolves every symlink in the path, and it was
#: called on the records directory once per request that opened a
#: recording. The answer cannot change while the server is running
#: (the directory is created once and never moved), so it is computed
#: once and reused.
_RECORDS_DIR_REAL = os.path.realpath(_RECORDS_DIR)


def _records_dir() -> str:
    """The directory the recordings live in, read at the moment of use."""
    return _RECORDS_DIR


def _records_dir_real() -> str:
    """The records directory, resolved, read at the moment of use."""
    return os.path.realpath(_RECORDS_DIR)


_recorder = Recorder(_records_dir)

#: How often the recorder takes a sample.
#:
#: The device streams 0x29 telemetry frames at the rate the device sends
#: them; the monitor's own series() reports that rate as `sample_hz`. The
#: recorder samples on a fixed wall-clock interval rather than on every
#: frame, because a frame rate that varies with the link would make the
#: recorded file's density a function of the radio rather than of the
#: measurement.
#:
#: 0.1 s gives ten samples a second, which is finer than any motion the
#: device reports and fine enough to see a stall.
RECORD_INTERVAL_S = 0.1

#: How often the status pump broadcasts the live state.
#:
#: The live panels update at 60 FPS. 1/60 s is
#: 16.7 ms, which is the display's own frame period: the panels are then
#: redrawn as fast as the screen can show them.
#:
#: This is the RENDER rate, not the DATA rate. The device's own telemetry
#: frames arrive at about 33 Hz -- the firmware samples at 100 Hz and packs
#: three samples per notification, and that ratio is fixed in the firmware,
#: so no host-side setting can raise it. Broadcasting at 60 Hz does not
#: invent frames: the same reading is sent more often, and the browser
#: redraws it more often. The curve on the chart still has one point per
#: real frame.
#:
#: The cost is measured: `_snapshot()` is 0.0026 ms and its JSON encoding
#: is 0.0070 ms on this tree, so this interval is about 0.016% of one
#: core.
PUMP_INTERVAL_S = 1.0 / 60.0

#: The settings of the pressure-change link.
#:
#: REMOVED. There is no change-point sensitivity control and no
#: "switch the parameters when the pressure rises" link.
#:
#: The link's SETTINGS, its endpoints (/api/rise-link) and the
#: snapshot key that carried them are all gone. The DETECTION that
#: fed them is still in the monitor, because the chart's change-point
#: marks come from the same detector and those marks are NOT what was
#: removed -- only the controls that exposed the detector's
#: sensitivity, and the link that switched the stroke parameters.

#: Guards the check-then-act in connect(): ``if s.connected: return`` followed
#: by ``await s.connect()`` has an await between the test and the use, so two
#: requests arriving together both saw ``connected == False`` and both called
#: connect(). bleak then builds two clients for one peripheral and both write
#: to the same characteristic. One link per request batch, checked and created
#: inside a single critical section.
_connect_lock = asyncio.Lock()


def session() -> DeviceSession:
    global _session
    if _session is None:
        _session = DeviceSession(simulate=bool(_opts["simulate"]),
                                address=_opts["address"])
    return _session


def _auto() -> AutoMotion:
    global _auto_motion
    if _auto_motion is None:
        _auto_motion = AutoMotion(session)
    return _auto_motion


def _auto_state() -> dict:
    """The sweep's state, WITHOUT building one to answer a question.

    ``_snapshot()`` is called 60 times a second by the broadcast pump,
    and it used to call ``_auto()`` to read the sweep's state. But
    ``_auto()`` BUILDS an AutoMotion when there is not one yet -- so
    the first snapshot after start-up constructed the whole motion
    machine, held a reference to the session, and did it all for a
    question that was answered with "not running, default parameters".

    The state is read directly when there is no motion object, so a
    server that has never run a sweep never builds one.
    """
    if _auto_motion is None:
        from .automotion import AutoState
        from .gait import GaitState
        out = AutoState().as_dict()
        # The gait's state travels WITH the sweep's, so a caller that
        # already reads the sweep does not need a second poll for the
        # gait. The two are separate things -- a gait is a SEQUENCE and
        # a sweep is a range -- but they are read together.
        out["gait"] = GaitState().as_dict()
        return out
    out = _auto_motion.state.as_dict()
    out["gait"] = _auto_motion.gait.as_dict()
    return out


def _motion_is_driving() -> bool:
    """Is anything of ours currently commanding the travel?

    Two things drive the device from this server: the sweep
    (``AutoMotion.running``) and the gait (``AutoMotion.gait_running``).
    A manual stroke is not one of them -- the session holds that, and the
    sites below are not allowed to let go of it.

    This predicate exists because the four teardown sites (stop,
    disconnect, switching to another device, the page's stop) each used
    to spell the check out as ``_auto_motion.running`` alone. A gait was
    therefore left walking through a disconnect, a device swap and a
    press of 停止 -- and the only reason it was visible at all was that
    nothing was there to stop it. Spelled once, the gait cannot be
    forgotten again.
    """
    return _auto_motion is not None and (
        _auto_motion.running or _auto_motion.gait_running)


# ------------------------------------------------------------ button actions
#
# THE BUTTON IS A CONTROL CHANNEL.
#
# The device has three keys of its own (mode, plus, minus), and the
# vendor app dispatches them from the notification stream. That means a
# key press is a command that arrives over BLE -- it does not need the
# page, it does not need a BLE write from the host, and it works even
# while the operator's browser is not on the screen.
#
# So a key press must REACH THE DEVICE'S CONTROL PATH, and the control
# path already exists: /api/target, /api/stop and /api/auto/* all go
# through the session's own commands. This executor therefore REUSES
# those paths rather than re-implementing them -- a second implementation
# of "set the travel" would be a second answer to the same question, and
# the two would drift.
#
# The one thing that is genuinely new here is the VIBRATION TOGGLE, and
# even that is built out of the existing one: the session knows the
# current level, and "toggle" means "set the level to the other one".

#: The action kinds this executor can carry out. Every one of them needs
#: a live link: an action that moved the device with no link is an action
#: that lied, so the executor refuses them all when nothing is
#: connected.
_ACTION_NEEDS_LINK = True


async def _execute_action(action: str) -> dict:
    """Carry out one action string. Returns a result dict.

    The result always says what was attempted (``action``), whether it
    ran (``ok``), and why not when it did not (``error``). A refused
    action is a normal outcome, not an exception: the device may be
    disconnected, or the model may have no vibration motor, and the
    caller needs to be able to say which.
    """
    kind, arg = parse_action(action)          # raises ActionError
    s = session()
    result = {"action": action, "kind": kind, "arg": arg, "ok": False,
              "error": ""}
    if not s.connected:
        result["error"] = "not connected"
        return result

    if kind == "none":
        # Doing nothing on purpose is a SUCCESS: the operator asked for
        # the key to do nothing, and it did nothing.
        result["ok"] = True
        return result

    if kind == "travel":
        # The SAME command /api/target issues, through the same session
        # call, with the same model guard.
        try:
            s.require_vibration_allowed(0)
        except ValueError as exc:
            result["error"] = str(exc)
            return result
        s.set_target(float(arg or 0), 0)
        # A KEY-SET POSITION IS HELD.
        #
        # set_target() refreshes the client clock once, which is enough
        # to prove the operator is there -- but not enough to HOLD: the
        # pump's idle guard releases the position CLIENT_GONE_SECONDS
        # (2.5 s) later, because the page's heartbeat only runs while
        # the page's own manual slider is the current intent. So a key
        # bound to "travel:60" moved the device to 60% and then, 2.5 s
        # later, the guard released it back to 0% -- with the page open
        # and the link up the whole time.
        #
        # The hold is claimed HERE, in the server, because the server is
        # the only party that knows a key was pressed. It is the same
        # claim touch() makes, so every other way of ending the hold
        # still works: the operator's stop button, a sweep taking over,
        # or the link going away.
        if float(arg or 0) > 0:
            s.hold_position()
        result["ok"] = True
        return result

    if kind == "vib":
        level = int(arg or 0)
        try:
            s.require_vibration_allowed(level)
        except ValueError as exc:
            result["error"] = str(exc)
            return result
        # A vibration command does not move the rod, so the travel it
        # holds is the travel it already has.
        s.set_target(s._target, level)        # noqa: SLF001
        result["ok"] = True
        return result

    if kind == "vib_toggle":
        current = int(s.stats.vibration or 0)
        want = 0 if current > 0 else VIB_TOGGLE_ON
        try:
            s.require_vibration_allowed(want)
        except ValueError as exc:
            result["error"] = str(exc)
            return result
        s.set_target(s._target, want)          # noqa: SLF001
        result["ok"] = True
        result["arg"] = want
        return result

    if kind == "intensity":
        # The device's three NATIVE steps. They are the fallback when
        # the scaled control frame is ignored -- and they do not depend
        # on the device reporting its limits, which is exactly when the
        # scaled frame is the one that does not work.
        #
        # A model with no motor cannot have an intensity, so the same
        # guard the scaled path uses applies here.
        try:
            s.require_vibration_allowed(1)
        except ValueError as exc:
            result["error"] = str(exc)
            return result
        ok = await s.set_intensity(int(arg))
        result["ok"] = bool(ok)
        if not ok:
            result["error"] = "the intensity frame was not accepted"
        return result

    if kind == "auto_start":
        # THE BUTTON STARTS THE SWEEP THE OPERATOR CONFIGURED.
        #
        # It used to start it with `AutoParams()` -- every field at its
        # default -- so a key bound to "开始自动模式" ran a sweep the
        # operator never asked for: 10-70% at 0.5 s, whatever the
        # 自动运动 sliders said.
        #
        # MEASURED: set the sliders to 20-80% with a 1.0 s traverse,
        # press the key, and the sweep came up at 10-70% with 0.5 s.
        # The page showed the operator's numbers while the device ran
        # different ones.
        #
        # The parameters come from the sweep's own LAST STATE, which is
        # where the operator's own numbers already live: the UI's
        # "开始" posts them to /api/auto/start, and /api/auto/params
        # updates them while it runs. So the button reuses the same
        # set the page and the API already agree on, rather than
        # inventing a second copy.
        try:
            await _auto().start(_auto().last_params())
        except RuntimeError as exc:
            result["error"] = str(exc)
            return result
        result["ok"] = True
        return result

    if kind == "auto_stop":
        await _auto().stop()
        result["ok"] = True
        return result

    if kind == "stop":
        # The SAME command /api/stop issues: stop the sweep first, then
        # retract the device.
        if _motion_is_driving():
            await _auto_motion.stop()
        await session().stop()
        result["ok"] = True
        return result

    if kind == "mode":
        ok = await s.set_mode(int(arg or 0))
        result["ok"] = bool(ok)
        if not ok:
            result["error"] = "the mode command could not be written"
        return result

    result["error"] = "unknown action %r" % kind
    return result


def _on_button_event(event: dict) -> None:
    """The monitor's button hook: run whatever the key is bound to.

    This runs in the BLE notify callback, which is SYNCHRONOUS and must
    not block -- the write path is a coroutine, so the action is
    scheduled on the session's own event loop rather than awaited here.

    A key with no binding does nothing. That is not an error: the
    operator may have bound only one of the three keys.
    """
    name = event.get("button")
    if name is None:
        return
    try:
        action = button_store().get(name)
    except Exception:                             # noqa: BLE001
        return
    if not action:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop here (a test driving the monitor synchronously): the
        # action is run to completion on this thread instead of being
        # dropped. It is still the same executor.
        try:
            asyncio.run(_execute_action(action))
        except Exception:                         # noqa: BLE001
            pass
        return
    loop.create_task(_execute_action(action))


def _install_button_hook() -> None:
    """Give the monitor the button hook.

    Called once, at import, so the device's own keys work from the FIRST
    notification -- whether the link was opened by the UI family or by
    the public family, and whether or not a page is open. The button is
    a control channel of its own, and a channel that only works while a
    page is open is not a channel.
    """
    _monitor.set_button_hook(_on_button_event)


# The hook is installed HERE, right after the function that installs it,
# so the monitor is never live without one.
_install_button_hook()


# ------------------------------------------------------------- public API

from . import api as _api   # noqa: E402  (needs the helpers above)

# Bound lazily: passing _auto() directly would build a DeviceSession (and with
# it, the whole transport) at import time, before --simulate or --address have
# been applied. Lambdas defer that to the first request.
#
# THE LOCK AND THE TWO LINK HOOKS ARE INJECTED HERE BECAUSE THE PUBLIC API
# FAMILY (/api/v1) DRIVES THE SAME SESSION. The public family's connect
# used to take no lock at all, so two callers -- one from each family --
# could each start their own BLE scan against one peripheral, and BlueZ
# answered the loser with an internal "operation already in progress"
# error instead of "a connection is already being made". And the public
# family's connect never created the arm loop, the 60 Hz broadcast or
# the recording pump, so a link opened over /api/v1 had a live BLE
# connection and nothing else: no recording, no broadcast, no re-arm.
#
# The lock cannot be imported here (this module imports api.py, so an
# import back would be circular), and the hooks cannot be either (they
# own this module's task state). So they are handed over through the
# wiring call -- one dependency, named for what it does.
_api.wire(
    session_getter=lambda: session(),
    auto_getter=lambda: _auto(),
    auto_start=lambda params: _auto().start(params),
    auto_stop=lambda: _auto().stop(),
    auto_update=lambda params: _auto().update(params),
    monitor_getter=lambda: _monitor,
    connect_lock_getter=lambda: _connect_lock,
    link_opening=lambda: _link_opening(),
    # The hook takes the session and says whether a link was just MADE.
    # The flag is passed through rather than dropped: api.py's own
    # connect calls it on the NEW-link path, and the flag has to reach
    # the function that owns the uptime clock.
    link_established=lambda s, new_link=True: _link_established(s, new_link),
    link_closing=lambda s: _link_closing(s),
    # The sweep's state, read WITHOUT building a sweep to answer "is
    # one running?". The public connection endpoint publishes the
    # sweep's counters, and it is polled, so asking it must not
    # construct the motion machine on the first call.
    auto_state_getter=lambda: _auto_state(),
)
app.include_router(_api.router)


async def _link_established(s: DeviceSession, new_link: bool = True) -> None:
    """Start the per-session background work for a link.

    Called by BOTH connect routes -- the UI family's and the public
    family's -- because the tasks belong to the LINK and not to the
    endpoint that opened it. The same "already running?" guard the UI
    family's own connect() uses is used here, so opening the link twice
    (once through each family) cannot leave two pumps competing over
    one session.

    `new_link` says whether a link was just MADE, or whether this call
    only found an ALREADY-UP link and is making sure the background work
    is running for it.

    It matters for exactly one thing: the session's uptime clock. The
    clock answers "how long has this link been up", so it is reset when
    a link is made -- and NOT when an existing link is merely confirmed.
    Resetting it on the confirmation made the second connect() answer
    "uptime 0.0" instead of "uptime 1.2", which is a lie: the link had
    been up the whole time.

    THE MONITOR IS RESET BY _link_opening(), BEFORE THE LINK IS MADE,
    NOT HERE. The monitor's counters (frames_seen, peak, press_events,
    the resting floor, the change points) describe ONE link, and a new
    link is a new measurement. They used to accumulate across links:
    frames_seen was a lifetime total, so /api/state's telemetry_frames
    did not describe the current link, and peak was the historical
    maximum, so the touch_span_pct of a new link was divided by the OLD
    link's peak and came out too small.

    The reset cannot happen HERE, because this function runs AFTER
    arm_sensors(): the reset clears ``armed``, and clearing it after
    the sensors were armed left the panel saying 未激活 on a link that
    was streaming. The order has to be: reset, connect, arm.
    """
    global _arm_task, _pump_task, _record_task, _session_started
    if new_link:
        _session_started = time.monotonic()
    s.raw_notify = _monitor.feed                       # noqa: SLF001
    if _arm_task is None or _arm_task.done():
        _arm_task = asyncio.create_task(_arm_soon(s))
    if _pump_task is None or _pump_task.done():
        _pump_task = asyncio.create_task(_status_pump())
    # The RECORDING starts here, with the link, and the operator does
    # nothing to make it happen. Started AFTER the connect succeeded,
    # so a failed connect does not leave an empty recording behind.
    if not _recorder.recording:
        _recorder.start(_record_meta(s))
    if _record_task is None or _record_task.done():
        _record_task = asyncio.create_task(_record_pump())


def _link_opening() -> None:
    """Prepare for a NEW link: clear the previous link's measurements.

    Called BEFORE the BLE connect, so the monitor's per-link state
    belongs to the link that is being opened rather than to the one
    that ended.

    The monitor's counters describe ONE link: frames_seen, peak,
    press_events, the resting floor, the change points. They used to
    accumulate across links, which meant two different things were
    wrong at once. /api/state's telemetry_frames was a LIFETIME total,
    so it did not describe the link the operator was looking at; and
    peak was the historical maximum, so the touch_span_pct of a new
    link was divided by the OLD link's peak and came out too small.

    The session's own per-link counters were already cleared inside
    connect(). The monitor's were not, and the two families of state
    disagreed about what "this link" means.
    """
    _monitor.reset()


async def _link_closing(s: DeviceSession) -> None:
    """Stop the per-session background work for a link.

    Called by BOTH disconnect routes. A dropped connection ends the
    recording, so the file gets its end line whichever endpoint closed
    the link -- and the three tasks are cleared, so the next connect
    creates its own.
    """
    global _arm_task, _pump_task, _record_task
    for task in (_arm_task, _pump_task, _record_task):
        if task is not None and not task.done():
            task.cancel()
    _arm_task = None
    _pump_task = None
    _record_task = None
    # The link is gone, so the device is no longer streaming. Leaving the
    # flag set would make the panel claim 已激活 on the next connection
    # until the first frame arrived -- the opposite of the dead panel this
    # fixes.
    _monitor.armed = False
    # "连接断开算结束记录" -- the disconnect ENDS the recording and
    # writes it to disk. It is here, after the link is down, so the
    # end-of-recording line can carry the final state of the link.
    _stop_recording(s)


async def broadcast() -> None:
    # _snapshot(), not stats.snapshot(): the UI distinguishes "not connected"
    # from "connected but the device has not reported yet" via s.connected, and
    # a bare stats snapshot has no such key. Building the payload here instead
    # of in one place is exactly how the two paths drifted apart in the first
    # place (P4.5).
    snap = _snapshot()
    # The ITERATION IS OVER A COPY on purpose: the body may remove the
    # queue it is holding, and removing from the list being iterated
    # would skip the next subscriber. The REMOVE inside the body is on
    # the real list -- that is what makes a dead subscriber actually go
    # away, instead of being written to forever.
    for q in list(_subscribers):
        try:
            q.put_nowait(snap)
        except asyncio.QueueFull:
            # A slow client LOSES THIS FRAME, and the next broadcast
            # carries a newer one. The queue holds one snapshot; a
            # client that cannot keep up is not served a growing backlog
            # of stale frames.
            pass
        except Exception:
            # A subscriber whose stream died between the generator's cleanup
            # and this broadcast must not abort the fan-out for every other
            # tab. Drop it here as well: the next yield of gen() would find a
            # closed loop and fail anyway.
            try:
                _subscribers.remove(q)
            except ValueError:
                pass


class TargetBody(BaseModel):
    percent: float = Field(..., ge=0, le=100, allow_inf_nan=False)
    vibration: int = Field(0, ge=0, le=255)


class AutoBody(BaseModel):
    min_percent: float = Field(10, ge=0, le=100, allow_inf_nan=False)
    max_percent: float = Field(70, ge=0, le=100, allow_inf_nan=False)
    vibration: int = Field(0, ge=0, le=255)
    #: 0.10-1.50 s one way. The floor is physical (20 ms send rate); the
    #: ceiling is the operating range this control is meant to cover.
    traverse_seconds: float = Field(0.5, ge=0.1, le=1.5,
                                    allow_inf_nan=False)
    dwell_seconds: float = Field(0.0, ge=0.0, le=10.0, allow_inf_nan=False)
    feedback: bool = Field(True)
    #: Which motion template to run. The allowed values are enumerated
    #: rather than free text, so a request naming a template that does not
    #: exist is refused with a 422 instead of being quietly replaced.
    preset: str = Field("sine")
    #: The operator's tick choice, in MILLISECONDS.
    #:
    #: Optional on purpose, and None means "this request did not name a
    #: tick" -- the same distinction the template field makes. A sweep
    #: that is already running inherits the tick it was started with, so
    #: a request that changes only, say, max_percent cannot silently
    #: reset the interval the operator chose.
    #:
    #: The band is checked by a validator rather than by ``ge``/``le``,
    #: because pydantic's own message for a bound says only the bound it
    #: tripped ("Input should be greater than or equal to 8") and never
    #: the other end. A message that names only one end leaves the
    #: reader guessing which direction they went wrong in.
    tick_ms: Optional[int] = Field(None)

    @field_validator("preset")
    @classmethod
    def _known_preset(cls, v: str) -> str:
        # The UI family validates the template here, at the edge, and
        # refuses what it does not know. That is the opposite of what
        # AutoParams.clamp() does -- clamp puts a value into its legal
        # domain, so an unknown template there falls back to the default.
        # Both are right, because they are different contracts: a browser
        # posting a typo should be told, not silently given a sine.
        if v not in PRESETS:
            raise ValueError("unknown motion template %r; expected one of %s"
                             % (v, ", ".join(PRESETS)))
        return v

    @field_validator("tick_ms")
    @classmethod
    def _tick_in_band(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return v
        return _check_tick_band(v)

    def to_params(self) -> AutoParams:
        # Keyword arguments, not positional. A positional call here is what
        # made adding a field dangerous: vero/api.py builds the same
        # dataclass with five positionals, so a field inserted in the
        # middle would shift them onto the wrong parameters. Keywords make
        # that class of mistake impossible at every call site.
        return AutoParams(min_percent=self.min_percent,
                          max_percent=self.max_percent,
                          vibration=self.vibration,
                          traverse_seconds=self.traverse_seconds,
                          dwell_seconds=self.dwell_seconds,
                          feedback=self.feedback,
                          preset=self.preset,
                          tick_ms=self.tick_ms)


def _check_tick_band(v: int) -> int:
    """Is this tick inside the vendor app's own band?

    The band is 8-30 ms, which is ``GeneratedMotionPolicy``'s
    MIN_TICK_MS / MAX_TICK_MS. The app clamps its own sampling period
    into that band, so an interval outside it is not an interval the app
    can produce -- the control lets the operator try what the app does,
    and nothing beyond it.

    The message names BOTH ends, because a message that names only one
    leaves the reader guessing which direction they went wrong in. It
    also says the number was REFUSED rather than clamped, because
    "clamped" and "refused" are different facts and the operator is
    going to read this and try to reproduce it.

    This is the ONLY place the band is checked. The endpoint used to
    repeat the check as a belt-and-braces branch, and that duplication
    was a defect in its own right: the mutation harness proved it -- a
    mutation that disabled the endpoint's copy left every assertion
    green, because the request model's validator had already refused
    the value. Two copies of one rule means one of them can be removed
    without anything noticing, and a rule that can be removed without
    anything noticing is not enforced twice, it is enforced once and
    documented twice.

    So the rule lives here, the request model calls here, and the
    endpoint trusts it.
    """
    ms = int(v)
    if not (_TICK_MIN_MS <= ms <= _TICK_MAX_MS):
        raise ValueError(
            "tick_ms must be between %d and %d ms (the vendor app's own "
            "range: GeneratedMotionPolicy.MIN_TICK_MS=%d, "
            "MAX_TICK_MS=%d). Got %d, which was REFUSED, not clamped."
            % (_TICK_MIN_MS, _TICK_MAX_MS, _TICK_MIN_MS, _TICK_MAX_MS, ms))
    return ms


class GaitBody(BaseModel):
    """A gait: a list of steps, each a position and a hold.

    The steps may be given as text -- "30:3,70,20:1.5" -- or as a list
    of objects. The text form is what the page's single input uses, and
    the object form is what a caller with structured data uses.
    """

    steps: str | list = Field(...,
                               description="a list of steps, or 'pos:hold,...'")
    repeat: bool = Field(True, description="repeat the sequence forever")
    vibration: int = Field(0, ge=0, le=255,
                           description="the vibration for a step that names none")


class TickBody(BaseModel):
    """The tick the operator chose, in milliseconds.

    ``extra="forbid"`` is the load-bearing part. A request carrying a
    field the server does not know is a request the operator got wrong,
    and silently ignoring it produces the worst possible outcome: the
    control appears to work while the value that was meant is not the
    value that is applied. Refusing it with a 422 says exactly which
    field is wrong.
    """
    tick_ms: int = Field(...)

    model_config = {"extra": "forbid"}

    @field_validator("tick_ms")
    @classmethod
    def _tick_in_band(cls, v: int) -> int:
        return _check_tick_band(v)


class ConnectBody(BaseModel):
    """Which device to connect to.

    Optional in every sense: the body itself may be absent (the old
    ``POST /api/connect`` with no body must keep working), and the
    address inside it may be absent too (connect to the current target).

    ``extra="forbid"`` for the same reason as TickBody: an unknown field
    is a mistake worth reporting rather than ignoring.
    """
    address: Optional[str] = None

    model_config = {"extra": "forbid"}


# ------------------------------------------------------------------ pages

@app.get("/")
async def index() -> Response:
    """Serve the page.

    no-store matters here: a stale cached copy kept rendering an older UI
    during development, and a user refreshing after an update would hit the
    same thing. The page is a few kilobytes.

    THE ACCESS TOKEN IS WRITTEN INTO THE PAGE, WHEN THE GATE IS ON.

    The page has to be able to authenticate before it can send
    anything, and the assets are exempt from the gate -- so the token
    has to arrive with the page itself.

    It is written as a JSON literal, so a token with a quote or a
    backslash in it cannot break out of the script. And with the gate
    OFF (the default), nothing is written and the page is served
    byte-for-byte as it was.
    """
    path = os.path.join(WEB_DIR, "index.html")
    if not ACCESS_TOKEN:
        return FileResponse(
            path, headers={"Cache-Control": "no-store, must-revalidate"})
    with open(path, "r", encoding="utf-8") as fh:
        html = fh.read()
    token_js = "<script>window.__VERO_TOKEN=%s;</script>" % json.dumps(
        ACCESS_TOKEN)
    # Put it right after <head>, so the page's own script can read it.
    html = html.replace("<head>", "<head>" + token_js, 1)
    return Response(content=html, media_type="text/html",
                    headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/index.html")
async def index_alias() -> FileResponse:
    # Some browsers and fetch calls request the file by name directly.
    return await index()


# ------------------------------------------------------------------ own JS
#
# The page's OWN modules -- the ones this project wrote, as opposed to the
# vendored third-party ones below -- are served from the same directory,
# by name, from a whitelist for the same reason the vendor list is a
# whitelist: there is no directory listing, and a name that was not
# enumerated cannot be asked for.
#
# recchart.js is the interactive record chart. It is a separate module
# because it carries its own state machine (the viewport, the hidden
# channels, the pointer), and that state has to be separate from the
# page's live-telemetry state.

#: The page's own modules. They live in a "js" DIRECTORY so that the
#: URL they are imported by and the path they sit at are the same
#: shape -- see own_js() below for why that matters.
OWN_JS_DIR = os.path.realpath(os.path.join(WEB_DIR, "js"))

_OWN_JS_FILES = frozenset({
    "recchart.js",
    "replay.js",
})

_OWN_JS_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


@app.get("/js/{name}")
async def own_js(name: str) -> FileResponse:
    """Serve one of this project's own modules by exact name, or 404.

    The modules live in vero/web/js/, so the URL the page imports and
    the FILE on disk have the same shape. That is not cosmetic: the
    test servers serve vero/web/ as their document root, so under them
    "/js/recchart.js" IS "vero/web/js/recchart.js" -- the same file,
    by the same path. A layout where the page imported "/js/x.js" and
    the file sat at "vero/web/x.js" would be served by this route and
    404 under every test server, which is exactly what happened.
    """
    if "\x00" in name:
        raise HTTPException(404, "not found")
    if not _OWN_JS_NAME.match(name):
        raise HTTPException(404, "not found")
    if name not in _OWN_JS_FILES:
        raise HTTPException(404, "not found")
    path = os.path.realpath(os.path.join(OWN_JS_DIR, name))
    if os.path.dirname(path) != os.path.realpath(OWN_JS_DIR):
        raise HTTPException(404, "not found")
    if not os.path.isfile(path):
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="text/javascript",
                        headers={"Cache-Control": "no-store, must-revalidate"})


# ------------------------------------------------------------------ the icon

# The page's own icons, served from the web directory by exact name.
#
# They live in vero/web/ beside index.html rather than in a directory of
# their own, because the URL the page asks for and the path the file
# sits at are then the same shape -- the same reason own_js() keeps its
# modules under /js/. The icons are a WHITELIST for the same reason the
# vendor list is a whitelist: there is no directory listing, and a name
# that was not enumerated cannot be asked for.
#
# The set is small on purpose. A favicon is asked for by the BROWSER,
# without the page asking, from a URL the page does not control -- so
# this is the one place where the set of served files is visible to
# something that was not written by this project. Three names, each
# named exactly, each of them a real file on disk.

ICON_FILES = frozenset({
    "favicon.svg",
    "favicon-32.png",
    "apple-touch-icon.png",
})

_ICON_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

#: The media type each icon is served as. A favicon served as
#: text/plain is not a favicon: the browser refuses it and falls back
#: to the default globe, so the page looks unbranded for a reason that
#: is invisible in the network log.
_ICON_MEDIA = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


@app.get("/icon/{name}")
async def icon(name: str) -> FileResponse:
    """Serve one of this project's own icons by exact name, or 404.

    The gates are the ones own_js() uses, for the same reason: the name
    pattern stops a separator or a percent-escape, the whitelist stops
    anything that was not deliberately published, and the realpath
    comparison stops a symlink inside the directory pointing out of it.
    """
    if "\x00" in name:
        raise HTTPException(404, "not found")
    if not _ICON_NAME.match(name):
        raise HTTPException(404, "not found")
    if name not in ICON_FILES:
        raise HTTPException(404, "not found")
    path = os.path.realpath(os.path.join(WEB_DIR, name))
    if os.path.dirname(path) != os.path.realpath(WEB_DIR):
        raise HTTPException(404, "not found")
    if not os.path.isfile(path):
        raise HTTPException(404, "not found")
    ext = os.path.splitext(name)[1].lower()
    return FileResponse(path, media_type=_ICON_MEDIA.get(ext, "application/octet-stream"),
                        headers={"Cache-Control": "no-store, must-revalidate"})


# ---------------------------------------------------------------- vendored JS

# third-party ES modules live in vero/web/vendor and are served one file at a
# time by name. three.js is MIT; LICENSE.three.js sits beside the code.

VENDOR_DIR = os.path.realpath(os.path.join(WEB_DIR, "vendor"))

# Only these names exist. A whitelist, not a pattern: there is no directory
# listing and no way to ask for anything that was not enumerated here at the
# moment the file was vendored.
VENDOR_FILES = frozenset({
    "three.module.min.js",
    "three.core.min.js",
    "OrbitControls.js",
    "Line2.js",
    "LineGeometry.js",
    "LineMaterial.js",
    "LineSegments2.js",
    "LineSegmentsGeometry.js",
    "LICENSE.three.js",
    # uPlot, the record chart's engine. MIT; the licence sits beside it.
    "uPlot.esm.js",
    "uPlot.min.css",
    "uPlot.LICENSE.txt",
})

# A single path segment of letters, digits, dot, underscore and hyphen. This
# admits "three.core.min.js" and rejects "", "a/b", "a\\b" and anything
# percent-encoded, because '%' is simply not in the class. Encoding is rejected
# on the raw segment, before any decoding, so there is no decode-then-check
# ordering to get wrong.
#
# MEASURED, do not "fix" this comment without re-measuring: the dot IS in the
# class, so "..", "." and "..." all match. This regex is NOT the gate that stops
# traversal -- an earlier version of this comment claimed it was, which was
# simply false. Traversal is stopped by the two gates after it: a dot-segment is
# never in VENDOR_FILES (the whitelist), and realpath(VENDOR_DIR/name) leaves
# the directory (the realpath comparison). webapp_regression.py asserts that
# division, so removing either of those gates turns the suite red.
_VENDOR_NAME = re.compile(r"^[A-Za-z0-9._-]+$")



@app.get("/vendor/{name}")
async def vendor(name: str) -> FileResponse:
    """Serve one vendored module by exact name, or 404.

    Four gates, because one is not enough. A NUL is refused outright, the name
    pattern stops traversal, the whitelist stops anything not deliberately
    vendored, and the realpath comparison stops a symlink inside the directory
    pointing out of it. Each covers a failure the others do not.
    """
    # A NUL is refused explicitly, and the redundancy with _VENDOR_NAME is the
    # point: '\x00' is not in that character class, so the pattern already
    # excludes it today. But that is an accident of which characters the class
    # happens to list, not a statement that NUL must be refused, and it is the
    # only thing standing between a NUL and os.path.realpath -- which raises
    # ValueError on one rather than returning a path, so the request would be
    # answered 500 instead of 404. Measured: with the gates in their current
    # order the three NUL names do return 404, and this gate changes no
    # behaviour. What it changes is that the requirement is now stated here
    # instead of inherited from a character class nobody reading this route
    # would think to check.
    #
    # It is still a gate, and it can still be moved below the realpath call.
    # What holds the order in place is not this comment: it is the behavioural
    # check in webapp_regression.py, which asks for a NUL name and requires 404,
    # plus the source assertion that realpath comes after both name gates. If
    # this gate is shuffled below the realpath call, those turn red.
    if "\x00" in name:
        raise HTTPException(404, "not found")
    if not _VENDOR_NAME.match(name):
        raise HTTPException(404, "not found")
    if name not in VENDOR_FILES:
        # The WHITELIST is what stops the dot-segments, and it is the
        # gate that is asserted to do so (webapp_regression.py checks
        # the division of labour: the PATTERN stops separators and
        # spaces, the WHITELIST stops "." and ".."). Moving the
        # dot-segment check above the whitelist would make that
        # assertion false without making the server any safer -- the
        # whitelist already refuses both.
        raise HTTPException(404, "not found")
    path = os.path.realpath(os.path.join(VENDOR_DIR, name))
    if path != os.path.join(VENDOR_DIR, name):
        # A symlink, or a traversal that survived the pattern. Serving it would
        # hand out whatever it points at.
        raise HTTPException(404, "not found")
    if not os.path.isfile(path):
        raise HTTPException(404, "not found")
    # The CONTENT TYPE depends on the file, and it has to.
    #
    # This route used to answer every file with text/javascript, and
    # that included the stylesheet -- so the browser was served a
    # stylesheet labelled as a script. It did not refuse it: it loaded
    # it, and applied NONE of it, because a stylesheet that is not
    # text/css is not a stylesheet.
    #
    # The symptom was uPlot's own layout never taking effect: its
    # plot area is absolutely positioned by its stylesheet, and without
    # it the plot area was pushed out of the box and the pointer
    # landed on the legend instead of on the chart. So the chart
    # rendered, the legend showed the values, and the pointer read
    # nothing -- and the wheel did nothing.
    #
    # Nothing in the page's own CSS showed it, because the page's CSS
    # was fine; the library's was missing.
    if name.endswith(".css"):
        media_type = "text/css; charset=utf-8"
    elif name.endswith(".txt"):
        media_type = "text/plain; charset=utf-8"
    else:
        media_type = "text/javascript; charset=utf-8"
    return FileResponse(
        path, media_type=media_type,
        # no-store is NOT changed to a cacheable header here, even
        # though the vendored files never change for a given build.
        # The reason is a CONTRACT, not a habit: webapp_regression.py
        # asserts that every importmap target is served no-store, and
        # that assertion is the thing that stops a STALE vendor file
        # being served after the package was updated in place -- which
        # is a real failure mode on a Windows install, where the
        # package is updated over the old one and the browser keeps
        # the old ES module.
        #
        # The cost is 200 KB re-downloaded per reload. That is a
        # performance nit, and M-8 records it as one; the correctness
        # the assertion protects is worth more than the bytes.
        headers={"Cache-Control": "no-store, must-revalidate"})


# ------------------------------------------------------------------ status

def _snapshot() -> dict:
    """The one payload every client surface reads from.

    /api/state, the SSE broadcast and the SSE timeout push all used to build
    their own dict, and the three drifted: broadcast() pushed stats.snapshot()
    alone (no "connected"), while the timeout path added "connected" and
    "auto" by hand. The UI keys the "connected but the device has not
    reported yet" wording off s.connected, so on the main broadcast path --
    the only one that fires while things are actually happening -- it always
    saw undefined and collapsed back to the em dash. One constructor, three
    call sites, no key list duplicated anywhere.

    Nothing may be dropped from here: /api/state is a public response and the
    UI reads every one of these keys.
    """
    s = session()
    snap = s.stats.snapshot()
    # 「上报」 IS THE DEVICE'S READING, NOT A FIELD SOMEONE HAS TO REMEMBER
    # TO SET.
    #
    # `SessionStats.reported_percent` was declared, documented as "the
    # position the DEVICE reported", published in every snapshot, and read
    # by the page's 上报 tile -- and nothing has ever written it. Measured
    # on a connected, armed, streaming session: the telemetry card showed
    # position_pct 60 / 40 / 80 while reported_percent was None in the
    # same payload. The operator saw a real position on one card and an
    # em dash on the other, from the same frames.
    #
    # The reading already exists and is already correct:
    # ``session.position_percent()`` reads the telemetry monitor -- the
    # one thing that decodes the 0x29 frames -- and ``locate()`` turns
    # the five slot readings into a position. So the payload asks that
    # method and publishes its answer.
    #
    # NOT a copy written into the stats. Two suites guard that
    # (reported_position_regression.py §5, p3c_regression.py): a second
    # copy is a second thing to fall out of step, and the whole point of
    # the fix is that there is one source. The field on the stats stays
    # unset; the payload carries the reading.
    #
    # ``None`` is preserved as ``None``. A device that has not reported
    # is not a device at zero -- the page's em dash is the right answer
    # for the first and the wrong one for the second.
    snap["reported_percent"] = s.position_percent()
    snap["connected"] = s.connected
    snap["simulate"] = s.simulate
    snap["auto"] = _auto_state()
    snap["uptime"] = round(time.monotonic() - _session_started, 1) \
        if _session_started else 0.0
    snap["platform"] = sys.platform
    # The device's own telemetry count, so the UI can say "the device is not
    # pushing" instead of leaving the operator to wonder about a zero peak.
    snap["telemetry_frames"] = _monitor.frames_seen
    # Enough to tell a silent device from a device speaking an unexpected
    # dialect, which look identical on the dashboard otherwise.
    snap["notify"] = _monitor.notify_summary()
    # Which model this is, added HERE and nowhere else. /api/state, the SSE
    # broadcast and the SSE timeout push all read this function, so a key added
    # to a call site instead would appear on one surface and be missing from the
    # other two -- the exact drift this constructor exists to prevent. The three
    # values are three-state: supports_vibration / position_sensors are None
    # when the model was not identified, which is a statement about what we
    # know, not about the hardware.
    snap["product"] = s.stats.product
    snap["supports_vibration"] = s.stats.supports_vibration
    snap["position_sensors"] = s.stats.position_sensors
    # The PRESENCE READINGS. Until now the two clocks that decide whether
    # the operator's position is still held -- `last_client_at` (the
    # operator is there) and `link_down_at` (the link is down) -- were
    # INTERNAL. There was no way to read them, so a hardware reproduction
    # could only be judged by behaviour, never by the numbers that
    # actually explain it.
    #
    # These are added HERE, in the one constructor, and nowhere else: the
    # five client surfaces all read this function, so the key set stays
    # identical across them by construction.
    #
    # `client_present()` is READ, never re-implemented here. The idle
    # guard's answer and this one must be the same answer, and the only
    # way to guarantee that is to ask the same question.
    snap["client_present"] = s.client_present()
    snap["client_age_s"] = (None if not s.stats.last_client_at
                             else round(time.monotonic()
                                        - s.stats.last_client_at, 2))
    snap["link_outage_s"] = (None if not s.stats.link_down_at
                              else round(time.monotonic()
                                         - s.stats.link_down_at, 2))
    # IS A MANUAL STROKE BEING HELD, AND IS IT ONLY A MANUAL STROKE?
    #
    # A held stroke is the one position the server keeps driving because
    # a PAGE asked it to, and the page's heartbeat is what keeps it
    # alive. A refreshed page knows neither half of that: its slider
    # starts at 0, its heartbeat does not run, and the guard then
    # releases a position the operator is still standing in front of.
    # Measured before this key existed: a held 60% went to 0.0 within
    # one second of a reload while the operator never left the page.
    #
    # So the snapshot says plainly what is held AND what is holding it:
    # a non-zero target, with no sweep and no gait driving it. Computed
    # HERE, from the same predicate the teardown sites use, so the page
    # cannot form its own idea of "am I driving this?" and disagree with
    # the server.
    #
    # The synced playback used to be the third thing that could hold the
    # device, and it was excluded here by name. There is no synced
    # playback any more, so the predicate is the two that remain.
    _held_percent = float(s.stats.target_percent or 0.0)
    snap["manual_hold"] = {
        "active": (_held_percent > 0.0
                   and not _motion_is_driving()),
        "percent": _held_percent,
        "vibration": int(s.stats.vibration or 0),
    }
    # The pressure-change link's settings and its live
    # state. It rides in the SHARED constructor, so the SSE push, the
    # timeout push and /api/state all carry it and the browser learns
    # about a pressure rise from the same 60 Hz push that carries the
    # travel -- no second endpoint to poll and no second clock.
    #
    # The LIVE TELEMETRY rides here too, so the push carries the pressure
    # as well as the travel. It used to be a separate poll of
    # /api/pressure every 700 ms, which is what made the telemetry
    # numbers jump. The push is already happening at 60 Hz and it
    # already has the reading, so the poll was both redundant and the
    # reason the numbers were not smooth. The keys are the ones
    # PressureMonitor.state() publishes, so the history and the live
    # readout still read the same function.
    #
    # The monitor's state is read ONCE and handed to the payload, so it
    # cannot disagree with itself. The cost is measured: state() is
    # 0.0011 ms on this tree.
    _tel = _monitor.state()
    snap["telemetry"] = _tel
    # THE SERIAL NUMBER IN THE PAYLOAD IS THE PRODUCT PREFIX.
    #
    # The web page's own API is reachable by anything on the LAN, and
    # the serial identifies one physical unit -- so publishing it here
    # publishes the identity of whoever tested with it, to anyone who
    # asks. The prefix says which model the device is, which is what
    # the page shows and what anyone reading the payload needs.
    #
    # The PUBLIC REST API (vero/api.py) is a different thing: there the
    # caller explicitly asks which device it is talking to, and that is
    # the API's job. This payload is the dashboard's, and the dashboard
    # shows a model.
    snap["serial"] = _product_prefix(snap.get("serial"))
    return snap


@app.get("/api/state")
async def state() -> JSONResponse:
    return JSONResponse(_snapshot())


@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    q: asyncio.Queue = asyncio.Queue(maxsize=8)

    # Registration happens on the first generator step, not here. A handler
    # that appends eagerly leaks whenever the response is abandoned before
    # the body iterator is ever pulled -- the generator's finally block then
    # never runs, because the generator body never started. Measured before
    # this change: 20 abandoned connections left 20 queues in _subscribers,
    # so every browser tab that opened and closed leaked one, and broadcast()
    # went on writing into them forever. Registering inside gen() ties the
    # queue's lifetime to the generator that drains it: no drain, no entry.
    async def gen():
        _subscribers.append(q)
        try:
            while True:
                try:
                    snap = await asyncio.wait_for(q.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    # Idle keepalive. Same constructor as the broadcast and as
                    # /api/state, so a client that reconnects sees the same key
                    # set either way -- it used to hand-patch connected and auto
                    # here, which made the timeout push disagree with the real
                    # broadcast on every other key (P4.5).
                    snap = _snapshot()
                if await request.is_disconnected():
                    break
                # The module-level json, not __import__("json"). The
                # per-frame import was a lookup on every one of the 60
                # frames a second, per subscriber, and it did the work
                # of a name that is already bound at the top of the
                # module. It also disagreed with JSONResponse about
                # non-finite numbers: this dumps() has allow_nan=True by
                # default, so a NaN in the snapshot went to the browser
                # as the bare literal NaN -- which JSON.parse refuses --
                # while the same value through JSONResponse was a 500.
                # One encoder, one rule: allow_nan=False, so a snapshot
                # that cannot be written is refused here rather than
                # pushed to a browser that cannot read it.
                yield "data: %s\n\n" % json.dumps(snap, allow_nan=False)
        finally:
            # remove, not "if in": the same queue must not be able to survive
            # one disconnect, and a double-finalize would otherwise raise.
            try:
                _subscribers.remove(q)
            except ValueError:
                pass

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.get("/api/devices")
async def devices() -> JSONResponse:
    """List the BLE devices that are advertising right now.

    The devices that are available are listed, and one of them can be
    clicked to connect.

    The list is EVERY advertiser, not only the ones this project knows.
    The vendor app's own name matching is a fast path only (the
    advertised name is not stable across hosts), so filtering to "our"
    devices here would hide exactly the thing the operator is asking to
    see: what is out there.

    Each row carries enough to tell two devices apart:

    ``address``
        the identity. Two devices of the same model differ only by this.
    ``name``
        the advertised name, or null when the device advertises none.
    ``rssi``
        the signal strength, in dBm. This comes from the
        ADVERTISEMENT, not from the device object -- bleak's BLEDevice
        has no rssi field at all, which is why discovery.scan() keeps
        the advertisement instead of discarding it.
    ``product``
        the model, from the serial/name prefix. "Unknown" for a device
        this project does not recognise.
    ``identified``
        whether the model was recognised. It is False for "Unknown",
        and that is a different statement from "the model is known and
        has no vibration".
    ``has_vibration`` / ``has_position_sensors``
        the vendor app's capability row for that model, or null when
        the model is unknown. null is NOT the same as false: it means
        "we do not know", not "the hardware lacks it".
    ``evidence``
        where the capability row comes from. Always "vendor-app-source"
        for a known model, because the rows come from decompiling the
        app -- the Vero in hand reports no capabilities over the wire at
        all, so nothing here was confirmed from the device.

    A host with no Bluetooth adapter gets a 503 with a readable reason.
    That is not hypothetical: the machine this was developed on has no
    adapter and bleak raises BleakBluetoothNotAvailableError. Letting
    that reach the client as a 500 would show "Internal Server Error"
    and say nothing about the cause.
    """
    from . import discovery as _discovery

    try:
        # The scan is taken under the process-wide scan lock, so two
        # callers cannot have two BLE scans running at once. The scan
        # itself has an await inside it, so it does not block the event
        # loop -- but the RADIO is a single shared thing, and two scans
        # against it fight.
        async with _scan_lock:
            found = await _discovery.scan(seconds=SCAN_SECONDS)
    except Exception as exc:                          # noqa: BLE001
        detail = "%s: %s" % (type(exc).__name__, exc)
        if _is_no_adapter(exc):
            detail = ("本机没有蓝牙适配器，无法扫描设备。"
                      "（原始错误：%s）" % detail)
        return JSONResponse(
            status_code=503,
            content={"detail": detail, "devices": [],
                     "error": type(exc).__name__})

    rows = []
    for dev, adv in found:
        rows.append(_device_row(dev, adv))

    # ONLY THE DEVICES THIS BUILD CAN DRIVE.
    #
    # Only the devices this build can actually drive are listed. A device
    # that cannot be controlled is not offered.
    #
    # The filter reads the SAME `controllable` flag the row carries, so a
    # device that is offered is a device the send path can drive, and a
    # device that is hidden is hidden for the reason the row states.
    #
    # The hidden ones are COUNTED and reported, not silently dropped: the
    # operator has to be able to tell "there is nothing out there" from
    # "there is something out there and it cannot be controlled". The
    # second case is the one that needs a name on it -- a device that
    # broadcasts and is not offered looks like a missing device until the
    # operator is told it was there.
    controllable = [r for r in rows if r["controllable"]]
    hidden = [r for r in rows if not r["controllable"]]
    #
    # THE ADDRESSES ARE REDACTED IN THE PAYLOAD TOO.
    #
    # The scan picks up whatever is in radio range, and that includes
    # other people's phones, headphones and watches. Their addresses are
    # their own business. The OUI is kept -- it says who made the
    # adapter, which is public and is what the evidence needs -- and the
    # device bytes, which identify the card, are masked.
    #
    # The CONNECTABLE device's address is a different case: the operator
    # has to connect to it, and the connect call needs the real one.
    # So the row carries the real address (the page does not print it)
    # and the address it PRINTS is the redacted one.
    from .discovery import _redact_address
    return JSONResponse({"devices": controllable,
                         "count": len(controllable),
                         "hidden_count": len(hidden),
                         "hidden": [{"product": r["product"],
                                     "address": _redact_address(r["address"]),
                                     "reason": P.non_controllable_reason(
                                         r["name"] or r["address"])}
                                    for r in hidden],
                         "scan_seconds": SCAN_SECONDS})


def _is_no_adapter(exc: Exception) -> bool:
    """Is this the "this host has no Bluetooth adapter" failure?

    Matched by CLASS NAME rather than by importing bleak's exception
    here, because importing bleak.exc at module scope would make the
    module unimportable on a machine without bleak at all -- and this
    file has to import for the simulator and for the tests.
    """
    return "BleakBluetoothNotAvailable" in type(exc).__name__


def _device_row(dev, adv) -> dict:
    """One row of the device list.

    Kept as a function so the shape is built in ONE place: the endpoint
    and the tests then read the same row, and a field cannot be added to
    one of them and forgotten in the other.

    ``controllable``
        whether THIS BUILD can drive the model. It is read from
        :func:`vero.protocol.is_controllable`, so the answer and the
        filter that uses it cannot disagree -- a device that is offered
        as connectable is a device the send path can actually drive.
    """
    from .discovery import _names_of, _redact_address, is_ours

    names = sorted(n for n in _names_of(dev, adv) if n)
    name = names[0] if names else None
    address = getattr(dev, "address", None) or ""
    product = P.detect_product(name or address)
    profile = P.device_profile(name or address)
    known = profile is not None
    # THE SAME PREDICATE THE CONNECT PATH USES.
    #
    # The list used to look only at the NAME, while the connect path
    # also accepted the vendor service UUID. So a device that
    # advertised the UUID but no usable name -- a BlueZ host with a
    # local alias, a CoreBluetooth host with an empty cached name, a
    # WinRT host with no local name -- was CONNECTABLE but HIDDEN from
    # the list. The operator's only way in was to know the address.
    #
    # The name is a fast path, never the only path: the UUIDs come
    # from the device itself, so they hold on any host.
    ours = is_ours(dev, adv)
    return {
        "address": address,
        "name": name,
        "names": names,
        "rssi": getattr(adv, "rssi", None) if adv is not None else None,
        "product": product,
        "identified": known,
        "has_vibration": profile.get("has_vibration") if known else None,
        "has_position_sensors": (profile.get("has_position_sensors")
                                 if known else None),
        "evidence": ("vendor-app-source" if known
                     else "unidentified"),
        # "controllable" means THIS BUILD CAN DRIVE IT -- not "it is
        # one of ours".
        #
        # The two are different questions, and conflating them was a
        # mistake: a Nexo is one of ours and the send path does not
        # speak its transport, so a Nexo must NOT be offered. But a
        # device that advertised the vendor service UUID and no usable
        # name is one of ours AND of an unknown model -- it might be a
        # Vero, which IS drivable -- so it is offered, with the note
        # saying the model could not be read.
        #
        # So the model decides when the model is known, and the UUID
        # rescues only the case where the model could not be read.
        "controllable": (P.is_controllable(name or address)
                         or (ours and not known)),
        "note": _device_note(product, known, ours),
        # The REDACTED address: the OUI, with the device bytes masked.
        #
        # The row's "address" is the real one, because the connect call
        # needs it -- the page hands it back and the server connects to
        # it. But "address" is also what any reader of the payload sees,
        # and the scan picks up other people's devices. So the row also
        # carries the redacted form, and that is what is ever printed.
        "address_masked": _redact_address(address),
    }


def _device_note(product: str, known: bool, ours: bool = False) -> str:
    """What the operator should know about this model, in one line.

    ONLY THE HARDWARE'S OWN CAPABILITIES. No statement about what this
    project has or has not been run against: that is the developer's
    own testing history, it says nothing about the device, and it
    identifies the machine and the unit the developer happened to own.
    The operator needs to know what the DEVICE can do -- whether it
    vibrates, whether it has position sensors -- and nothing else.

    Nexo is called out separately because it is not a matter of degree:
    the B-generation transport (V13 multi-point packets) is not wired
    into the send path at all, so connecting one very likely does
    nothing. That is a statement about the DEVICE, so it stays.
    """
    if not known:
        if ours:
            # It IS one of ours -- the vendor service UUID said so --
            # but the advertisement carried no name prefix, so the
            # MODEL is unknown. That is a different statement from
            # "this is somebody else's device", and the operator has
            # to be able to tell them apart.
            return ("本厂设备（厂商服务标识确认），但广播未带型号前缀："
                    "本项目无法判断它是哪个型号，接上后请确认。")
        return ("未识别型号：本项目的 BLE 协议只对本厂设备成立，"
                "连接前请确认这是不是目标设备。")
    if product == "Vero":
        return "Vero：无震动，有位置传感器。"
    if product == "Mira":
        return "Mira：可震动，有位置传感器。"
    if product == "Nexo":
        return ("Nexo：B 代设备，走 V13 多点打包帧，"
                "而该发送路径本项目尚未接入，接上去大概率不动。")
    if product == "Wyrh":
        return "Wyrh：可震动，无位置传感器。"
    return ""


# --------------------------------------------------------------- the tick

@app.get("/api/tick")
async def tick_state() -> JSONResponse:
    """Report the tick in force, and the band the operator may choose.

    The reported numbers are the ones the SERVER is actually using, not
    the ones a slider nominally shows. That distinction is the whole
    point of this endpoint: the operator is going to read these numbers
    and decide what to try on the hardware, so a report that showed a
    nominal value would be a lie in the one place where the truth
    matters.

    Three numbers, because there are two clocks and a band:

    ``tick_ms`` / ``tick_hz``
        the generator's period -- how often a NEW target position is
        computed.
    ``frame_interval_ms`` / ``frame_hz``
        the BLE pump's period -- how often a frame is written to the
        link. The pump writes a VARIABLE, not a queue, so a pump slower
        than the generator eats the positions in between before the
        device ever sees them. The pump therefore follows the tick, and
        the report says so rather than leaving it implicit.
    ``min_tick_ms`` / ``max_tick_ms``
        the band the operator may choose -- the vendor app's own 8-30 ms.
    """
    s = session()
    return JSONResponse(_tick_report(s))


def _tick_report(s: DeviceSession) -> dict:
    """The tick report, built in one place.

    The tick in force is the one the GENERATOR will actually use, which
    is ``tick_seconds(traverse, named)`` and not simply "the number the
    operator typed". That distinction is the whole point of this
    endpoint.

    The generator scales the tick with the traverse when no tick was
    named: the traverse scaling exists to keep a FAST traverse sampled
    (a 0.1 s round trip needs a short tick) and to keep a SLOW one from
    repeating the same position (a 15 s sweep needs a long one). So the
    unnamed tick is not always the nominal -- at a 4 s traverse it comes
    out at 20 ms and at an 8 s one at 40 ms.

    Reporting the nominal in those cases would be exactly the defect
    this endpoint exists to prevent: a number on screen that is not the
    number being sent. The operator is going to read this and try it on
    the hardware, so the value is computed from the same function the
    generator calls.

    The rates are computed from the intervals that are reported, not
    independently, so the two can never disagree. They are NOT rounded
    to one decimal: at 15 ms the exact rate is 66.666... Hz, and 66.7
    is 0.05 % away from it. The operator is going to compare these
    numbers against the vendor app's own numbers, so a rounding that
    moves the value is a rounding that loses information they came
    for.
    """
    from .automotion import tick_seconds

    params = _auto().state.params
    named = params.tick_ms
    # The generator's own call, with the same arguments it makes. When
    # the stored tick was refused somewhere upstream this returns None
    # and the generator falls back to the unnamed default; the report
    # follows that same fallback rather than inventing a third answer.
    effective = tick_seconds(params.traverse_seconds,
                             named / 1000.0 if named is not None else None)
    if effective is None:
        effective = tick_seconds(params.traverse_seconds)
    tick_ms = int(round(effective * 1000.0))
    # The pump's interval is the tick, clamped into the pump's own band.
    frame_ms = int(round(s.interval * 1000.0))
    # The two rates are computed from the intervals that are reported,
    # and the DIVISION IS GUARDED. frame_ms can be 0 if the session's
    # interval is 0 (a session that was never told what to use), and
    # 1000.0 / 0 raises ZeroDivisionError -- which is a 500 on an
    # endpoint whose whole job is to REPORT numbers. A rate that cannot
    # be computed is reported as null, which is what "unknown" means.
    return {
        "tick_ms": tick_ms,
        "tick_hz": round(1000.0 / float(tick_ms), 2) if tick_ms else None,
        "frame_interval_ms": frame_ms,
        "frame_hz": round(1000.0 / float(frame_ms), 2) if frame_ms else None,
        "min_tick_ms": _TICK_MIN_MS,
        "max_tick_ms": _TICK_MAX_MS,
        "nominal_tick_ms": _TICK_NOMINAL_MS,
        "running": bool(_auto().running),
        "source": "operator" if named is not None else "default",
    }


@app.post("/api/tick")
async def set_tick(body: TickBody) -> JSONResponse:
    """Set the interval at which positions are sent.

    Takes effect IMMEDIATELY, without restarting the service: the pump
    reads its interval on every pass, and a running sweep's parameter
    set is replaced in place, so the next tick already uses the new
    period.

    A running sweep is updated with the new tick. A sweep that is not
    running has nothing to update, so the choice is remembered on the
    parameter set and applies to the next one -- which is what makes
    "set the interval, then start the sweep" work.

    The band is enforced by the request model, so a value outside the
    vendor app's 8-30 ms is a 422 rather than a silent clamp. A typed
    number that is quietly turned into a different legal number is a
    number the operator cannot trust, and this is a control they are
    going to try on real hardware.
    """
    s = session()
    ms = int(body.tick_ms)
    # The band was already checked by the request model, which calls
    # _check_tick_band(). There is deliberately no second check here: a
    # mutation that removed this copy left every assertion green,
    # because the model's validator had already refused the value -- so
    # the second copy was not a second line of defence, it was a second
    # copy of a rule that could be deleted without anything noticing.

    # The pump follows the tick, so the device actually sees the rate
    # the operator asked for. frame_interval_for_tick() clamps into the
    # pump's band, which is the same 8-30 ms.
    from .session import frame_interval_for_tick
    s.set_frame_interval(frame_interval_for_tick(ms / 1000.0))

    # Put the choice on the parameter set, so it survives until the next
    # sweep and so a sweep that is already running picks it up.
    current = _auto().state.params
    _auto().state.params = AutoParams(
        min_percent=current.min_percent,
        max_percent=current.max_percent,
        vibration=current.vibration,
        traverse_seconds=current.traverse_seconds,
        dwell_seconds=current.dwell_seconds,
        feedback=current.feedback,
        preset=current.preset,
        tick_ms=ms,
    )
    if _auto().running:
        await _auto().update(_auto().state.params)

    return JSONResponse(_tick_report(s))


# ------------------------------------------------------------------- links

@app.post("/api/connect")
async def connect(body: Optional[ConnectBody] = None) -> JSONResponse:
    """Open the link, optionally to a device the operator picked.

    The body is optional in both senses. The old call --
    ``POST /api/connect`` with no body at all -- must keep working, and
    it does: the parameter defaults to None and the handler treats "no
    body" exactly like "no address", which is "connect to the current
    target".

    Switching devices is a disconnect, a re-point and a reconnect, and
    the three are done HERE rather than in the browser, because the
    per-link state has to be cleared exactly once. ``connect()`` on the
    session already resets the per-link counters (the P4.6 fix), and
    ``/api/disconnect`` already stops a running sweep -- so a switch
    that went through the UI as three separate calls could leave a
    sweep running against the old device for the duration of the
    round-trip.

    Reusing the session rather than building a new one is safe BECAUSE
    the per-link state is reset inside ``connect()``: a new link does
    not inherit the old link's command count. That was the P4.6 trap,
    and the reset is the reason the reuse is not a fresh instance of
    it.
    """
    global _session_started, _arm_task, _pump_task, _record_task
    s = session()
    wanted = body.address.strip() if (body and body.address) else None

    async with _connect_lock:
        if wanted and s.address != wanted:
            # A different device: the link to the old one has to go first,
            # or the session would be asked to talk to two peripherals.
            if _motion_is_driving():
                await _auto_motion.stop()
            if s.connected:
                await s.disconnect()
                # The old device's recording ends with its link, not with
                # the new device's arrival. The two are separate
                # recordings, and merging them would put two devices'
                # travel on one curve.
                _stop_recording(s)
            s.set_address(wanted)
        if s.connected:
            # The link is already up -- and the per-session background work
            # may NOT have been started, because the link could have been
            # opened through the PUBLIC API family. So the work is
            # (re)started here rather than skipped: the tasks belong to the
            # link, and this is the one place that knows the link is up.
            # Skipping it is what left a link with no broadcast, no
            # recording and no re-arm.
            #
            # new_link=False: the link is ALREADY up, so the uptime clock
            # must NOT be reset. The clock answers "how long has this
            # link been up", and this link has been up since the first
            # connect -- resetting it here would report the age of the
            # link as zero.
            await _link_established(s, new_link=False)
            return JSONResponse(_snapshot())
        # The PREVIOUS LINK'S MEASUREMENTS ARE CLEARED BEFORE THE NEW
        # LINK IS MADE. The monitor's counters describe one link; left
        # alone they accumulated across links, so /api/state's
        # telemetry_frames was a lifetime total and the touch_span_pct of
        # a new link was divided by the OLD link's peak. The order
        # matters: clear, then connect, then arm.
        _link_opening()
        try:
            await s.connect()
        except Exception as exc:                          # noqa: BLE001
            s.stats.last_error = "%s: %s" % (type(exc).__name__, exc)
            raise HTTPException(status_code=409,
                                detail=s.stats.last_error) from exc

        # These are per-session, not per-request. Reconnecting after a drop
        # called connect() again, which spawned a second pump; two pumps then
        # competed over one DeviceSession, and the duplicate arm loop kept
        # re-issuing command 54 for as long as any one of them saw it unarmed.
        #
        # The same helper the public family's connect calls, so both
        # families start exactly the same set of tasks.
        await _link_established(s)
    # _snapshot(), not stats.snapshot(): index.html feeds this response
    # straight into apply(), which keys the "connected but not reported yet"
    # wording off s.connected. A bare stats snapshot has no such key, so the
    # four cards flashed back to the em dash at the exact moment the operator
    # clicked connect, and stayed there until the next SSE broadcast (P4.6).
    return JSONResponse(_snapshot())


@app.post("/api/disconnect")
async def disconnect() -> JSONResponse:
    global _arm_task, _pump_task, _record_task
    if _motion_is_driving():
        await _auto_motion.stop()
    async with _connect_lock:
        s = session()
        await s.disconnect()
        # The same helper the public family's disconnect calls: the three
        # tasks are cancelled and cleared, the monitor is marked unarmed
        # and the recording is ended -- whichever endpoint closed the
        # link.
        #
        # The ORDER matters: the recording is ended AFTER the link is
        # down, so the end-of-recording line carries the FINAL state of
        # the link (its last error, its link_ok flag, how many frames it
        # sent). Ending it first would write a line describing a link
        # that was still up.
        await _link_closing(s)
    # Same constructor as every other client surface. Nothing in the UI reads
    # this body today (doDisconnect throws it away and feeds apply() a literal
    # {link_ok:false}), but a handler that returns a differently-shaped payload
    # is exactly how /api/connect drifted in the first place -- so the break in
    # payload uniformity is closed now rather than left as a trap for whoever
    # wires the UI up to it (P4.6).
    return JSONResponse(_snapshot())


# ---------------------------------------------------------------- commands

@app.post("/api/target")
async def target(body: TargetBody) -> JSONResponse:
    s = session()
    if not s.connected:
        raise HTTPException(status_code=409, detail="not connected")
    # Model gate, before any state is touched. Refuses (422) rather than
    # clamping; see DeviceSession.require_vibration_allowed.
    try:
        s.require_vibration_allowed(body.vibration)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    s.set_target(body.percent, body.vibration)
    # A PAGE COMMAND SUPERSEDES A CALLER'S HOLD, and set_target() does
    # it -- in the one writer, so no call site can forget. The caller's
    # hold means "do not auto-retract this position, because I have no
    # heartbeat to send"; the page HAS a heartbeat, so the page's
    # positions follow the page's own rules -- and one of those rules
    # is the safety net: a tab that goes away retracts in 2.5 s.
    #
    # Measured before this: an agent commanded 100% (claiming the
    # caller's hold), the page then moved the travel, the page's tab
    # closed, and the position stayed out -- because the caller's hold
    # was still in force and the page's safety net never got to run.
    #
    # It is a supersede, not a fight: the page's command does not change
    # the position -- it changes who owns it.
    #
    # _snapshot(), not the session's own stats.snapshot().
    #
    # The session's snapshot carries the transport's fields; _snapshot()
    # carries the session's PLUS the ones the page reads off the device:
    # product, supports_vibration, position_sensors, connected, telemetry.
    # The two differ by fifteen keys, and the page's apply() reads every
    # one of them.
    #
    # So this answer used to MISS supports_vibration, and the page's
    # apply() -- which hides the vibration card when the model is known
    # not to have one -- read `undefined` and SHOWED the card. The SSE
    # broadcast that followed carried the full snapshot, read `false`,
    # and HID the card. The operator saw the vibration bar appear and
    # disappear every time they moved the travel.
    #
    # The same constructor as every other client surface, for the same
    # reason /api/connect and /api/disconnect use it: a handler that
    # returns a differently-shaped payload is how the two drifted apart
    # in the first place.
    return JSONResponse(_snapshot())


@app.post("/api/stop")
async def stop() -> JSONResponse:
    if _motion_is_driving():
        await _auto_motion.stop()
    await session().stop()
    # The same constructor as every other client surface -- see
    # /api/target's note. This one used to answer with the session's own
    # snapshot, which carries no `connected` and no model keys, so the
    # page's apply() could not tell "the device is still there" from
    # "the answer was shaped differently".
    return JSONResponse(_snapshot())


@app.post("/api/keepalive")
async def keepalive() -> JSONResponse:
    """Heartbeat from the page.

    Holding a position is a one-shot command, so the server cannot tell "parked
    at 60%" from "tab closed" by the target alone.
    """
    s = session()
    if not s.connected:
        raise HTTPException(status_code=409, detail="not connected")
    s.touch()
    return JSONResponse({"ok": True})


@app.get("/api/frame/{percent}")
async def frame_for(percent: int, vibration: int = 0) -> JSONResponse:
    # The percent is CLAMPED, and the CLAMPED value is what the answer
    # reports. The old version clamped the drive value but echoed the
    # raw percent back, so the answer said "percent: -5" next to
    # "drive: 0" -- two fields about the same number that disagreed,
    # in a response whose whole purpose is to show what the wire bytes
    # will be.
    clamped = max(0, min(100, percent))
    d = int(round(clamped / 100.0 * P.STROKE_MAX))
    # THE VIBRATION GOES INTO THE FRAME, BECAUSE IT IS IN THE FRAME.
    #
    # The A0 control frame carries the vibration in byte 5, and the
    # frame this endpoint shows is the frame the page says it is
    # sending. It used to build it WITHOUT the vibration, so the byte
    # was always 0x00 and the CRC was computed over a frame that was
    # not the one on the wire.
    #
    # Measured: with the vibration at 90, the frame actually sent is
    # A55A0DA0B05AA0010F88132610, and the page showed
    # A55A0DA0B000A0010F8813B0E6. Three bytes differ -- the vibration
    # byte and the two CRC bytes -- and the page's caption says this
    # is the frame on the wire. It was not. It was the frame for a
    # device with no vibration.
    #
    # The vibration is clamped to the wire's range (one byte) and the
    # CLAMPED value is what goes into the frame and into the answer,
    # so the hex and the reported vibration cannot disagree.
    vib = max(0, min(P.VIBRATION_MAX, int(vibration)))
    f = P.app_a0_control(d, vib)
    return JSONResponse({"percent": clamped, "drive": d,
                         "vibration": vib,
                         "hex": f.hex(" ").upper()})


# ---------------------------------------------------------------- recordings

@app.get("/api/records")
async def records(page: int = 0, size: int = 0) -> JSONResponse:
    """Every recording on disk, newest first.

    The "数据记录" tab's list. The metadata comes from reading each
    recording's own header, so the list cannot disagree with the files.

    The directory is passed EXPLICITLY, for the same reason the read
    endpoint resolves its name against _RECORDS_DIR: the two endpoints
    must look at the SAME directory. list_recordings() with no argument
    falls back to the recorder module's own default, which happens to
    be the same path -- but "happens to" is not a reason, and a test
    that moves the directory would see the list reading one place and
    the read endpoint reading another.
    """
    # THE LISTING IS ALSO READ IN A WORKER THREAD.
    #
    # list_recordings opens every file in the records directory and
    # reads its first line. Measured on a directory of 2581 recordings
    # that is 39 ms -- and the endpoint is polled every two seconds, so
    # the event loop was blocked for 2.3 of the 60 Hz broadcast's frames
    # every time the list was asked for. The BLE status pump and the
    # recording sampler wait in the same queue, so the recording itself
    # got holes.
    #
    # read_curve() moved into a thread pool for exactly the same
    # reason, and the list is the bigger of the two: it touches every
    # file, while a read touches one.
    # THE PAGE IS OPTIONAL, AND A FULL LIST IS STILL A FULL LIST.
    #
    # A caller that asks for no page gets every recording, exactly as
    # before -- so nothing that worked stops working. A caller that asks
    # for a page gets that page AND the total, so a page control knows
    # how many pages there are.
    #
    # The saving is in the READS, not in the answer: the whole list
    # reads every file's header, and a page reads the headers of the
    # recordings ON it. On a directory of 4261 recordings that is the
    # difference between 4261 file reads every two seconds and 100.
    if size and size > 0:
        page = max(0, int(page))
        size = max(1, min(int(size), MAX_PAGE_SIZE))
        # The NAMES first, so the total is known without reading a
        # single header. The order is the same one the full list uses.
        names = await run_in_threadpool(list_recording_names, _RECORDS_DIR)
        total = len(names)
        start = page * size
        window = names[start:start + size]
        out = []
        for name in window:
            info = await run_in_threadpool(
                read_header, os.path.join(_RECORDS_DIR, name))
            if info is not None:
                out.append(info)
        return JSONResponse({"records": out,
                             "recording": _recorder.recording,
                             "current": _recorder.path,
                             "page": page, "size": size, "total": total,
                             "pages": (total + size - 1) // size})
    records = await run_in_threadpool(list_recordings, _RECORDS_DIR)
    return JSONResponse({"records": records,
                         "recording": _recorder.recording,
                         "current": _recorder.path,
                         "total": len(records)})


@app.get("/api/records/{name}")
async def record(name: str) -> JSONResponse:
    """One recording, as plottable series.

    `name` is a FILE NAME, not a path. The file is resolved inside the
    records directory and the result is checked to be inside it, so a
    name like "../../etc/passwd" cannot read anything else -- the same
    rule the vendor endpoint applies to its own file names.

    THE READ HAPPENS IN A WORKER THREAD. read_curve() parses every line
    of the file, and a 150 MB recording is 868 808 json.loads calls --
    measured at 1.58-1.92 s on this tree. Called directly from an
    async route, that ran on the event loop thread with no await point
    in the middle, so the WHOLE server stopped: the SSE broadcast pump
    (60 Hz), the BLE status pump and the 10 Hz recording sampler all
    waited for it. The visible cost was a recording with 1.6 s holes in
    it, because the sampler's own sleep expired while the loop was
    blocked and the sample was never taken.

    Starlette's run_in_threadpool moves the blocking work to a worker
    thread and yields the event loop meanwhile, so the other clients
    keep being served while this one waits.
    """
    if not _RECORD_NAME.match(name):
        raise HTTPException(404, "not found")
    path = os.path.join(_RECORDS_DIR, name)
    real = os.path.realpath(path)
    if not os.path.isfile(real):
        # A RECORDING THAT IS STILL BEING WRITTEN.
        #
        # A recording is written under a ".part" name and renamed into
        # place when it ends, so the history list only ever shows
        # COMPLETE recordings. But the page re-reads the recording that
        # is being written RIGHT NOW -- every two seconds, while it
        # grows -- and it asks for it by its FINAL name.
        #
        # Without this fallback that request was a 404, so the live
        # recording could not be opened at all while it was running:
        # the row was there, the page asked for it, and the server said
        # "not found". The rename-into-place is right (a half-written
        # file is not a recording), but the reader has to be able to
        # see the one that is being written.
        #
        # The temporary file is inside the same directory and is
        # resolved the same way, so the path check below still applies
        # to it.
        temp = os.path.realpath(path + ".part")
        if os.path.isfile(temp):
            real = temp
        else:
            raise HTTPException(404, "not found")
    if os.path.dirname(real) != _records_dir_real():
        raise HTTPException(404, "not found")
    curve = await run_in_threadpool(read_curve, real)
    if curve is None:
        raise HTTPException(404, "not found")
    return JSONResponse(curve)


@app.post("/api/records/import")
async def import_record(request: Request) -> JSONResponse:
    """Import an exported archive INTO the history list.

    The import targets the HISTORY LIST, not the chart.

    So the import is not "put this file on the chart" -- it is "put this
    file into the history". The difference is not cosmetic: a chart that
    holds a file in the browser loses it on the next reload, it is not
    in the list of 历史记录, it cannot be exported again from the list,
    and it disappears when another archive is picked. An import that
    writes a real file into the records directory has none of those
    problems, because from the moment it lands it is just another
    recording.

    The body is the export envelope: {"format": "vero-record",
    "recording": {...}}. The envelope's marker is checked, so a random
    JSON file is refused with a message rather than written as an empty
    recording.

    THE BODY IS READ AS BYTES AND ITS SIZE IS CHECKED FIRST. The
    parameter used to be a bare ``dict``, so FastAPI parsed the whole
    body into memory with no ceiling on it: one POST could write an
    arbitrarily large file and hold the event loop for the whole write.
    The body is now read as bytes (so the size is known before it is
    parsed), refused with a 413 if it is too large, and only then
    parsed.

    THE META IS CHECKED HERE, BEFORE ANY FILE IS TOUCHED. A "meta" that
    is a list or a string used to raise inside write_imported() AFTER
    the file had been created and truncated, so every refused import
    left a 0-byte file behind -- and this endpoint is unauthenticated.
    """
    body = await request.body()
    if len(body) > MAX_IMPORT_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail="导入失败：文件太大（%d 字节，上限 %d 字节）。"
                   % (len(body), MAX_IMPORT_BODY_BYTES))
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(400, "导入失败：请求体不是合法的 JSON。") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "导入失败：请求体不是一个对象。")
    if payload.get("format") != "vero-record":
        raise HTTPException(400, "导入失败：不是本项目导出的档案。")
    recording = payload.get("recording")
    if not isinstance(recording, dict):
        raise HTTPException(400, "导入失败：档案格式不对。")
    times = recording.get("t")
    if not isinstance(times, list) or not times:
        raise HTTPException(400, "导入失败：档案里没有采样点。")
    if len(times) > MAX_IMPORT_SAMPLES:
        raise HTTPException(
            status_code=413,
            detail="导入失败：采样点太多（%d 个，上限 %d 个）。"
                   % (len(times), MAX_IMPORT_SAMPLES))
    meta = recording.get("meta")
    if meta is not None and not isinstance(meta, dict):
        # The shape is checked HERE, at the edge, so the refusal happens
        # before anything is written -- and the reason is in the
        # operator's language.
        raise HTTPException(400, "导入失败：档案里的 meta 不是一个对象。")
    name = await run_in_threadpool(write_imported, recording, _RECORDS_DIR)
    if not name:
        raise HTTPException(500, "导入失败：无法写入记录目录。")
    curve = await run_in_threadpool(read_curve, os.path.join(_RECORDS_DIR, name))
    if curve is None:
        raise HTTPException(500, "导入失败：写入的档案读不回来。")
    return JSONResponse({"name": name, "recording": curve})


# --------------------------------------------------------------- telemetry

@app.get("/api/pressure")
async def pressure() -> JSONResponse:
    return JSONResponse(_monitor.state())


@app.post("/api/arm")
async def arm() -> JSONResponse:
    s = session()
    if not s.connected:
        raise HTTPException(status_code=409, detail="not connected")
    # This endpoint must be able to command the link on demand -- the UI calls
    # it straight after /api/connect, before _arm_soon()'s 0.6 s settle has
    # fired. What it must NOT do is send a second identical mode command once
    # one has already gone out: session.py:551 says the command must be sent
    # exactly once per link, because a second identical mode command
    # interrupts the stream the first one started. That rule used to be
    # violated here every time the operator pressed the button after the link
    # had already settled.
    #
    # So: command the link if nobody has yet, otherwise report. Measured on
    # the simulator -- stream_commands was 1 here when _arm_soon had run, and
    # became 2 because this handler armed again.
    already_commanded = s.stats.stream_commands > 0
    if already_commanded:
        ok = s.stats.sensors_armed or (
            s._telemetry.armed if s._telemetry is not None else False)
    else:
        ok = await s.arm_sensors()
    # `ok` means "frames are arriving", not "a write returned". When this
    # request commanded the link the device needs a moment to push its first
    # frame, so report the command's own acceptance rather than the monitor's
    # older False -- otherwise the panel says the arm failed when it merely
    # has not taken effect yet. The monitor corrects itself on the first
    # frame either way.
    _monitor.arm(ok)
    out = {**_monitor.state()}
    out["arm_accepted"] = ok
    out["frames_at_arm"] = s.stats.telemetry_frames_at_arm
    # Must describe what this request actually did, because a UI that reads
    # it to explain the button has no other way to know. It must not name a
    # cause it cannot see: the command for this link may have gone out from
    # _arm_soon, from an earlier press of this same button, or from the
    # previous link before connect() started clearing per-link counters. This
    # string used to say "already commanded by connect", and connect() sends
    # no telemetry command at all (session.py:547-549) -- which made the
    # sentence false for every button press and, after a reconnect, false in
    # the most dangerous way possible: it labelled a link that had received
    # nothing as one that had already been set up.
    out["source"] = ("this link was already commanded; none sent here"
                     if already_commanded
                     else "commanded by this request")
    out["notify"] = _monitor.notify_summary()
    return JSONResponse(out)


# ------------------------------------------------------------ auto motion

@app.post("/api/auto/start")
async def auto_start(body: AutoBody) -> JSONResponse:
    s = session()
    if not s.connected:
        raise HTTPException(status_code=409, detail="not connected")
    if body.max_percent < body.min_percent:
        raise HTTPException(status_code=422,
                            detail="max_percent must be >= min_percent")
    try:
        s.require_vibration_allowed(body.vibration)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # A SWEEP USED TO STOP THE VIDEO PUMP, THE WAY THE VIDEO STOPPED THE
    # SWEEP.
    #
    # The exclusion was one-way: /api/videosync/start stopped the sweep,
    # but starting a sweep left the video pump running. Both were live
    # asyncio tasks calling `session.set_target()` on the SAME session
    # object, and there is no lock on the device write -- so two
    # controllers were driving one mechanism.
    #
    # There is no video pump any more, so there is nothing to stop here
    # and no exclusion to make. The sweep is the only thing that commands
    # the position from the server, besides a manual stroke.
    try:
        return JSONResponse(await _auto().start(body.to_params()))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/auto/stop")
async def auto_stop() -> JSONResponse:
    return JSONResponse(await _auto().stop())


@app.post("/api/auto/remember")
async def auto_remember(body: AutoBody) -> JSONResponse:
    """Remember the operator's sweep parameters WITHOUT starting a sweep.

    WHY THIS ROUTE EXISTS. The page's 自动运动 sliders are pushed to the
    server only while a sweep is running, so a key bound to
    "开始自动模式" started the sweep with the DEFAULTS -- 10-70% at
    0.5 s -- no matter what the sliders said. The operator set the
    sliders, pressed the key, and the device ran numbers the page was
    not showing. That is the complaint this answers.

    So the page now pushes the parameters here when nothing is running,
    and the bound button starts the sweep with what it finds.

    THE RULES, and they are the ones /api/auto/params already has:

      * a LINK IS REQUIRED -- the sweep belongs to a device, and the
        model check needs one to read;
      * max_percent must be >= min_percent;
      * the vibration must be allowed for this model.

    What it does NOT do is start anything. It only records the
    parameters, so the next start -- from the button or from the page
    -- uses them.
    """
    s = session()
    if not s.connected:
        raise HTTPException(status_code=409, detail="not connected")
    if body.max_percent < body.min_percent:
        raise HTTPException(status_code=422,
                            detail="max_percent must be >= min_percent")
    try:
        s.require_vibration_allowed(body.vibration)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(_auto().remember(body.to_params()))


@app.post("/api/auto/params")
async def auto_params(body: AutoBody) -> JSONResponse:
    # A LINK IS REQUIRED, like /api/auto/start. The sweep's parameters
    # are the sweep's, and the sweep belongs to a link: without one,
    # this route used to change a GLOBAL parameter set, so a call made
    # while disconnected rewrote the range the NEXT connection would
    # start with -- and the model check read the PREVIOUS device's
    # identity, because there was no device to ask.
    s = session()
    if not s.connected:
        raise HTTPException(status_code=409, detail="not connected")
    if body.max_percent < body.min_percent:
        raise HTTPException(status_code=422,
                            detail="max_percent must be >= min_percent")
    # body.vibration is a plain int (default 0), not int|None, and to_params()
    # replaces the sweep's whole parameter set -- so this value IS the level
    # that will be in effect after the call. No effective-value merge is needed
    # here; only /api/v1/stroke carries an optional vibration.
    try:
        s.require_vibration_allowed(body.vibration)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(await _auto().update(body.to_params()))


@app.get("/api/gait")
async def gait_state() -> JSONResponse:
    """What the gait is doing.

    The gait's own state, separate from the sweep's: a gait and a sweep
    are different things, and the page shows both.
    """
    return JSONResponse(_auto().gait.as_dict())


@app.post("/api/gait/start")
async def gait_start(body: GaitBody) -> JSONResponse:
    """Walk a sequence of positions, holding each for a while.

    The steps are parsed and range-checked here, so a gait that would
    be refused is refused while the operator is looking at it.

    A LINK IS REQUIRED, exactly like /api/auto/start. The sweep refuses
    with a 409 when there is no device, and the gait did not -- so a
    gait started with no device "ran", counted its laps, and the panel
    said 运行中 while nothing moved. The operator saw no difference
    between starting a gait and not starting one, which is the whole
    complaint this answers.
    """
    from .gait import GaitError, parse_gait, parse_gait_text
    # THE STEPS ARE PARSED FIRST, and the link is checked second.
    #
    # The order matters: a request that names an impossible step is
    # wrong in a way the operator can see and fix ("that position is
    # not 0..100"), and a request with no device is wrong in a way
    # they cannot fix by editing the steps. The specific complaint
    # comes first, and the connection is checked only when the steps
    # themselves are fine.
    try:
        if isinstance(body.steps, str):
            steps = parse_gait_text(body.steps)
        else:
            steps = parse_gait(body.steps, repeat=body.repeat)
    except GaitError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    s = session()
    if not s.connected:
        raise HTTPException(status_code=409, detail="not connected")
    # A step that names no vibration takes the request's.
    if body.vibration:
        for st in steps:
            if not st.vibration:
                st.vibration = int(body.vibration)
    try:
        out = await _auto().start_gait(steps, repeat=body.repeat)
    except GaitError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        # The engine refuses a gait with no device, the same way the
        # sweep does. 409, not 500: the request was refused, it did not
        # break.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(out)


@app.post("/api/gait/stop")
async def gait_stop() -> JSONResponse:
    """Stop the gait and let the device go back in."""
    return JSONResponse(await _auto().stop_gait())


@app.get("/api/auto")
async def auto_state() -> JSONResponse:
    out = _auto().state.as_dict()
    # The gait's state travels WITH the sweep's, so a caller that
    # already reads the sweep does not need a second poll for the gait.
    # The two are separate things -- a gait is a SEQUENCE and a sweep is
    # a range -- but they are read together.
    out["gait"] = _auto().gait.as_dict()
    return JSONResponse(out)


# ---------------------------------------------------------------- buttons
#
# The device has three keys of its own, and the vendor app dispatches
# them from the notification stream (n1.java:308-336). These four routes
# are the whole "capture and bind" surface:
#
#   GET  /api/buttons          the bindings, the recent events, the menu
#   POST /api/buttons/bind     bind one key to one action
#   POST /api/buttons/execute  run one action by hand
#   POST /api/buttons/clear    forget the recent events
#
# The BINDING is stored on disk (vero/buttons.py), so it survives a
# restart; the EVENTS are in memory, because a key press is a moment and
# a moment does not survive.

class ButtonBindBody(BaseModel):
    """The body of a bind request.

    ``action`` is a plain string rather than an enum, because the legal
    actions carry a number ("travel:60") and an enum cannot say that.
    The string is validated by ``buttons.parse_action``, which is the
    ONE place that decides what a legal action is -- so the HTTP layer
    and the file layer cannot disagree about it.
    """
    button: str = Field(...)
    action: str = Field(...)


class ButtonExecuteBody(BaseModel):
    """The body of a manual execute request."""
    action: str = Field(...)


@app.get("/api/buttons")
async def buttons_state() -> JSONResponse:
    """The bindings, the recent button events, and the action menu.

    The ``version`` is the number the page watches: it rises on every
    change to the bindings, so a page that polls can tell "the operator
    changed something" from "the page is stale".
    """
    st = button_store().as_dict()
    st["events"] = _monitor.button_state()
    st["bound_count"] = len(st.get("bindings") or {})
    return JSONResponse(st)


@app.post("/api/buttons/bind")
async def buttons_bind(body: ButtonBindBody) -> JSONResponse:
    """Bind one key to one action, or unbind it with an empty action.

    An empty action means "unbind", because that is what an operator
    clearing a dropdown means. A malformed action is a 422, and a
    malformed one leaves the stored bindings exactly as they were.
    """
    if body.action.strip() == "":
        try:
            out = button_store().remove(body.button)
        except ActionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse(out)
    try:
        out = button_store().set(body.button, body.action)
    except ActionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(out)


@app.post("/api/buttons/execute")
async def buttons_execute(body: ButtonExecuteBody) -> JSONResponse:
    """Run one action by hand.

    The SAME executor the device's own keys go through, so a manual
    trigger and a key press cannot behave differently.
    """
    try:
        result = await _execute_action(body.action)
    except ActionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # An action that could not run is a 409, not a 200 with a buried
    # error: the caller asked for the device to move and it did not.
    if not result.get("ok"):
        status = 409 if result.get("error") == "not connected" else 422
        raise HTTPException(status_code=status,
                            detail=result.get("error") or "the action failed")
    return JSONResponse(result)


@app.post("/api/buttons/clear")
async def buttons_clear() -> JSONResponse:
    """Forget the recent button events. The bindings are NOT touched."""
    dropped = _monitor.clear_buttons()
    return JSONResponse({"ok": True, "cleared": dropped,
                          "events": _monitor.button_state()})


# ------------------------------------------------- pressure change link
#
# REMOVED. There is no change-point sensitivity control and no
# "switch the parameters when the pressure rises" link.
#
# The two routes that carried the link's settings (/api/rise-link GET
# and POST), the body model they validated, and the snapshot key that
# published the link's live state are all gone. The route count in
# SKILL.md's table moved with them -- the table is checked against the
# routes the code declares, so leaving it at 24 would have meant the
# skill and the code drifting apart.


# ------------------------------------------------------------- background

async def _arm_soon(s: DeviceSession) -> None:
    """Arm telemetry after connect, and re-arm after every reconnect.

    The gate is `stream_commands`, not `sensors_armed`. Those answer different
    questions: stream_commands answers "has a telemetry command gone out on
    this link yet", sensors_armed answers "have frames arrived". Before this
    gate was changed, the UI's real order -- connect, then arm immediately --
    put two arm_sensors() calls in flight at once: /api/arm read
    stream_commands==0, passed its gate, and then awaited arm_sensors(), whose
    2.7 s grace sleep yielded the loop to _arm_soon, whose 0.6 s settle had by
    then also fired on sensors_armed==False. Both passed their gates before
    either incremented the counter, so the link received command 54 twice and
    the device streams from the second one. Measured: the calls nest as
    ENTER /api/arm(c) cmd=0 -> ENTER _arm_soon cmd=1 -> EXIT _arm_soon cmd=2
    -> EXIT /api/arm cmd=2.

    Aligning the gate with stream_commands makes /api/arm's decision and
    _arm_soon's decision read the same quantity, so the second caller to
    arrive finds a non-zero counter and reports instead of commanding.
    """
    await asyncio.sleep(0.6)
    while s.connected:
        if s.stats.stream_commands == 0:
            await s.arm_sensors()
        await asyncio.sleep(5.0)


async def _status_pump() -> None:
    """Broadcast live status and revive a dead send loop.

    The interval is this: the panels that read the live state
    have to move at 60 FPS, not jump every 700 ms. The pump therefore
    wakes every PUMP_INTERVAL_S seconds (1/60) and broadcasts.

    The COST of that is measured, not assumed. On this tree
    `_snapshot()` costs 0.0026 ms and the JSON encoding of its result
    0.0070 ms, so a broadcast every 16.7 ms is about 0.016% of one core.
    The pump is not the bottleneck; the device's ~33 Hz frame rate is.
    That is why the broadcast is not throttled: the data is what it is,
    and sending it at the display's rate is what makes the panels feel
    attached to the device instead of attached to a timer.

    The wake-up is a sleep, not a busy loop, and the loop yields to the
    event loop between broadcasts, so a slow client (a browser on the far
    side of the LAN) cannot make the server spin.
    """
    try:
        while session().connected:
            await asyncio.sleep(PUMP_INTERVAL_S)
            s = session()
            if s.connected and not s.stats.pump_alive and s._task is not None:  # noqa: SLF001
                s._start_loop()                       # noqa: SLF001
            await broadcast()
    except asyncio.CancelledError:
        raise


async def _record_pump() -> None:
    """Sample the session into the recorder, for as long as the link is up.

    This is the pump that makes "记录从连接开始自动记录" true: it starts
    with the connection and ends with it, and the operator never touches
    it. The interval is RECORD_INTERVAL_S.

    A sample is taken from the SAME two sources the live panel reads --
    the session's own statistics for the travel, the pressure monitor for
    the pressure and the position -- so a recording and the live view can
    never disagree about what the device was doing. A second, parallel
    reading of the device would be a second answer to the same question,
    and the two would drift.
    """
    try:
        while session().connected:
            await asyncio.sleep(RECORD_INTERVAL_S)
            s = session()
            if not s.connected:
                break
            pressure = _monitor.state()
            # EVERY POSITION'S OWN PRESSURE IS RECORDED, not just the
            # primary one.
            #
            # Every segment's every pressure value is recorded.
            #
            # The monitor already publishes all five slots under `named`
            # (位置1..位置5) and the raw list under `latest`. Recording
            # only the primary left the other four readings unrecorded,
            # so a recording could not answer "what was the pressure
            # where the finger was" -- which is the question a recording
            # exists to answer.
            #
            # The columns are named `press_位置N`, one per slot, so the
            # file stays a flat JSON-lines table: a recording is read
            # back with read_curve(), which pulls one array per column,
            # and a nested object per row would need a second reader.
            _row = {
                "travel_target": _round_or_none(s.stats.target_percent),
                "travel_active": _round_or_none(s.stats.active_percent),
                # THE DEVICE'S OWN POSITION, FROM THE SAME READING THE
                # LIVE PANEL NOW SHOWS.
                #
                # This used to read `s.stats.reported_percent`, which
                # nothing writes -- so the column was null in every
                # recording, and a recording could not answer "where was
                # the device according to the device". The docstring
                # above already promised the sample comes from the same
                # two sources the live panel reads; the position is read
                # from the monitor, which is where it actually lives.
                "travel_reported": _round_or_none(s.position_percent()),
                "pressure": pressure.get("pressure"),
                "position_pct": pressure.get("position_pct"),
                "slot": pressure.get("active_slot"),
                "vibration": s.stats.vibration,
            }
            _named = pressure.get("named") or {}
            for _slot_name, _value in _named.items():
                _row["press_%s" % _slot_name] = _value
            _recorder.sample(**_row)
    except asyncio.CancelledError:
        raise


def _round_or_none(value):
    """Round a float, or pass None through untouched."""
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _record_meta(s) -> dict:
    """The metadata written into a recording's header.

    Everything here is read from the session, so the header describes the
    connection it belongs to rather than whatever the process happens to
    know later. The values are the ones that make a recording
    interpretable on its own: which device, what it said it was, and
    whether the run was a real link or the simulator.

    THE SERIAL NUMBER IS THE PRODUCT PREFIX, NOT THE WHOLE NUMBER.

    The serial identifies one physical unit. A recording is a file that
    gets copied around, exported and shared, and the unit number in it
    identifies the device it came from -- and, once enough of them are
    in one place, the person who tested with it.

    The PREFIX is the product line ("UMVx"), the same for every unit of
    that model, and it is what makes a recording interpretable: it says
    which model the run was on. The unit number says nothing a reader
    needs, so it is not written.

    The address is redacted for the same reason, and with the same rule
    the scan summary uses: the OUI says who made the adapter, and the
    device bytes identify the card.
    """
    from .discovery import _redact_address
    return {
        "address": _redact_address(getattr(s, "address", None) or "") or None,
        "product": s.stats.product,
        "firmware": s.stats.firmware,
        "serial": _product_prefix(s.stats.serial),
        "simulate": bool(getattr(s, "simulate", False)),
        "platform": sys.platform,
    }


def _product_prefix(serial: Optional[str]) -> Optional[str]:
    """The product prefix of a serial number.

    A serial of the shape "<prefix><unit number>" -- say "UMVX" followed
    by ten unit characters -- reduces to "UMVX". The prefix is the
    product line, the same for every unit of that model, and what
    follows it is the unit's own number.

    The example is deliberately NOT a real serial: a real one in a
    docstring is a real one in the repository, and the repository's own
    privacy check reads every blob for exactly that.

    THE PREFIX COMES FROM THE PROTOCOL MODULE'S OWN TABLE, not from a
    regular expression. The first attempt at this was "the letters before
    the first digit", which gives "UMVxA" -- five characters, one too
    many -- because the serial is "UMVx" + "A9D9A7065B": the unit
    number starts with a LETTER too. A prefix that is one character
    wrong is a prefix that does not match the table, so it would have
    been a prefix that identifies nothing.

    So the longest matching prefix from PRODUCTS is used, which is the
    same table the device matching uses. One table, so the prefix that
    is published and the prefix that identifies the model cannot
    disagree.
    """
    if not serial:
        return serial
    up = serial.upper()
    best = ""
    for _prefixes in P.PRODUCTS.values():
        for _p in _prefixes:
            if up.startswith(_p) and len(_p) > len(best):
                best = _p
    if best:
        return best
    # A serial that matches no known prefix (the simulator's, for one)
    # yields the whole string: there is nothing to cut it at, and cutting
    # a serial that has no prefix would leave nothing to identify the
    # model with.
    return serial


def _stop_recording(s) -> Optional[str]:
    """End the recording. Returns the file that was written, or None.

    Called from the two places the link can end: the explicit disconnect
    and the switch to a different device. The end-of-recording line
    carries the session's final state, so a recording says how it ended
    (normally, or with a link error) without anyone having to infer it.
    """
    if not _recorder.recording:
        return None
    return _recorder.stop({
        "ended": "disconnect",
        "last_error": s.stats.last_error,
        "link_ok": s.stats.link_ok,
        "frames_sent": s.stats.frames_sent,
        "telemetry_frames": _monitor.frames_seen,
    })


#: How long the server may take to shut down gracefully before uvicorn
#: cancels whatever is left.
#:
#: uvicorn's `timeout_graceful_shutdown` defaults to None, which means
#: "wait forever". It waits for the in-flight requests to finish, and
#: `/api/events` is a server-sent-events stream that never finishes -- a
#: browser tab with the page open holds one forever. So a plain SIGTERM
#: (which is what `systemctl stop` sends) waited forever, and systemd's
#: `TimeoutStopUSec=1min 30s` eventually escalated to SIGKILL.
#:
#: Measured, on the tree that shipped before this change: with an SSE
#: client connected, SIGTERM took 30.02 s to exit and still needed the
#: SIGKILL; without an SSE client it took 0.51 s. The browser's long-lived
#: stream was the whole difference.
#:
#: 10 s is longer than the shutdown hook needs (it only has to close the
#: BLE link) and short enough that a restart does not drag.
DEFAULT_GRACEFUL_TIMEOUT = 10


def _install_shutdown_lifespan() -> None:
    """Teach the app to close the BLE link when the process shuts down.

    WHY THIS IS INSTALLED HERE AND NOT AT MODULE LEVEL
    -------------------------------------------------
    The hook disconnects a MODULE-LEVEL session object. If it were
    installed at import time, every `with TestClient(webapp.app)` in the
    regression suites would disconnect the global session on exit, and
    those suites share one process and one session across many cases.
    Installing it in `main()` -- which no test calls -- keeps the hook
    exactly as wide as the thing it is for: a real server run.

    WHY THE TIMEOUT ALONE IS NOT ENOUGH
    ------------------------------------
    uvicorn's `Server.shutdown()` waits for the running requests with
    `timeout_graceful_shutdown` FIRST, and only THEN sends the lifespan
    shutdown event:

        await asyncio.wait_for(self._wait_tasks_to_complete(),
                               timeout=self.config.timeout_graceful_shutdown)
        ...
        await self.lifespan.shutdown()

    So with the timeout left at None (wait forever), the lifespan hook --
    and therefore the BLE disconnect -- is never reached: the process is
    still sitting in the wait when systemd's SIGKILL arrives. Measured:
    a shutdown hook with no timeout still took 20 s and still needed the
    SIGKILL. The timeout and the hook are two halves of one fix, and
    neither half works alone.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(_app):
        # The loop exception handler goes HERE, not before uvicorn.run():
        # uvicorn creates the loop it serves on, and a handler installed
        # before that would go on a loop that is never used.
        _install_peer_vanished_handler()
        try:
            yield
        finally:
            # A RUNNING GAIT IS STOPPED BEFORE THE LINK CLOSES.
            #
            # The gait's loop sends a frame every step. If the link goes
            # down first, the loop keeps running against a closed session
            # until the process exits, and the recorder sees writes after
            # the link's own end line. Stopping it first is the same
            # order every other teardown site uses.
            try:
                if _motion_is_driving():
                    # `_auto()` returns the object the guard just proved
                    # exists; it does not build one here.
                    await _auto().stop()
            except Exception as exc:                      # noqa: BLE001
                # Same rule as the disconnect below: a failure here must
                # not keep the process from exiting.
                print("motion stop failed: %s" % exc, flush=True)
            s = _session
            if s is not None:
                try:
                    await s.disconnect()
                    print("session disconnected", flush=True)
                except Exception as exc:                  # noqa: BLE001
                    # A failure to close the link must not stop the process
                    # from exiting -- it is already shutting down.
                    print("session disconnect failed: %s" % exc, flush=True)
                finally:
                    # A SIGTERM ends the connection, so it ends the
                    # recording. Without this the file would be left
                    # half-written: the lines are on disk (every sample is
                    # flushed), but the recording would have no END line,
                    # and a list built from the headers would show a
                    # session that never finished.
                    try:
                        _stop_recording(s)
                    except Exception as exc:              # noqa: BLE001
                        print("recording close failed: %s" % exc,
                              flush=True)

    app.router.lifespan_context = _lifespan


#: The exception types that mean "the peer went away", which is not a
#: fault in this service.
_PEER_VANISHED = (
    ConnectionResetError,
    BrokenPipeError,
    ConnectionAbortedError,
)


def _install_peer_vanished_handler():
    """Stop the console from printing a traceback for a vanished peer.

    Installed on the RUNNING loop, so it is the loop uvicorn actually
    uses. It is installed AFTER uvicorn's own handler, so it wraps that
    one rather than replacing it: anything that is not a peer vanishing
    still gets the full default treatment.
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop here. The lifespan calls this from inside the loop
        # uvicorn runs, so this branch means somebody called it from
        # somewhere else -- and there is nothing to install on.
        return False
    try:
        previous = loop.get_exception_handler()
    except Exception:                                       # noqa: BLE001
        previous = None

    def handler(_loop, context):
        exc = context.get("exception")
        if isinstance(exc, _PEER_VANISHED):
            # The peer went away. Not a fault here, and saying so would
            # only teach the operator to ignore the console.
            return
        if previous is not None:
            previous(_loop, context)
        else:
            _loop.default_exception_handler(context)

    loop.set_exception_handler(handler)
    return True


def main(argv=None) -> int:
    import uvicorn

    ap = argparse.ArgumentParser(
        prog="vero",
        description="Vero / Amorlink BLE control")
    ap.add_argument("--address", default=None,
                    help="MAC address; omit to scan by name")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (use 0.0.0.0 for LAN access)")
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--simulate", action="store_true",
                    help="run against the built-in simulator")
    ap.add_argument("--graceful-timeout", type=float,
                    default=DEFAULT_GRACEFUL_TIMEOUT,
                    help="seconds to wait for in-flight requests at "
                         "shutdown before cancelling them (default: %d)"
                         % DEFAULT_GRACEFUL_TIMEOUT)
    args = ap.parse_args(argv)

    _opts["address"] = args.address
    _opts["simulate"] = args.simulate

    # The abandoned ".part" files are swept at START-UP. A recording is
    # written under a ".part" name and renamed into place when it
    # ends, so a process that was killed mid-recording left its ".part"
    # file on disk forever. This is the only moment it is certain that
    # no recording of THIS process is in flight.
    try:
        _swept = _recorder.sweep_abandoned()
    except Exception as exc:                          # noqa: BLE001
        print("recording sweep failed: %s" % exc, flush=True)
        _swept = 0
    if _swept:
        print("removed %d abandoned partial recording(s)" % _swept,
              flush=True)

    _install_shutdown_lifespan()

    print("=" * 60)
    print("  Vero control  --  http://%s:%d" % (args.host, args.port))
    print("  platform: %s" % sys.platform)
    print("  target  : %s" % (args.address or "scan by name"))
    if args.simulate:
        print("  SIMULATOR MODE -- no real hardware is being driven")
    print("=" * 60)
    # THE PEER-VANISHED NOISE IS HANDLED, AND ONLY THAT.
    #
    # On Windows the operator saw this in the console, right after the
    # service came up:
    #
    #     Exception in callback
    #     _ProactorBasePipeTransport._call_connection_lost()
    #     ConnectionResetError: [WinError 10054]
    #     远程主机强迫关闭了一个现有的连接。
    #
    # It is asyncio's OWN transport callback failing, not an
    # application error: the browser's EventSource was open across a
    # restart of the service, so the socket the server still held was
    # reset by the peer, and asyncio's default handler printed the
    # whole traceback.
    #
    # The peer vanishing is EXPECTED. A browser tab closes, a laptop
    # sleeps, the network drops -- and none of those is a fault in this
    # service. Printing a traceback for them teaches the operator to
    # ignore the console, which is exactly how a REAL fault gets
    # missed.
    #
    # So the loop's exception handler recognises THIS and says nothing;
    # everything else keeps the default behaviour, traceback and all.
    #
    # The match is deliberately narrow: a ConnectionResetError, or a
    # BrokenPipeError, on a connection the peer ended. A timeout, a
    # protocol error or an application exception is NOT swallowed.
    #
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning",
                timeout_graceful_shutdown=args.graceful_timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())