"""Public HTTP API, mounted at /api/v1.

The UI's own endpoints grew organically (``/api/target``, ``/api/auto``,
``/api/state``) and return the whole internal snapshot. That is fine for a
page that knows the internals and wrong for anything else: it ties a caller to
field names that exist for the UI's benefit, and it offers no stable contract.

Everything here is shaped for a program driving the device from outside:

    GET  /api/v1/connection          link state, identity, counters
    POST /api/v1/connection/connect  open the link
    POST /api/v1/connection/disconnect
    GET  /api/v1/stroke              current stroke position
    POST /api/v1/stroke              {"percent": 0-100, "vibration": 0-255}
    POST /api/v1/stroke/release      let go (drops to zero)
    GET  /api/v1/auto                sweep state and its three parameters
    POST /api/v1/auto/start          begin sweeping
    POST /api/v1/auto/params         change min / max / traverse live
    POST /api/v1/auto/stop           end the sweep

Conventions, so a caller does not have to guess:

* Every response is JSON with an ``ok`` field.
* A rejected request returns 4xx with ``detail`` naming the reason; it never
  returns 200 with an error buried in the body.
* Parameter names match the UI's: ``min_percent``, ``max_percent``,
  ``traverse_seconds``.
* Writes to the stroke take effect immediately; a missing ``vibration`` means
  "leave it alone" rather than "zero", because a caller adjusting position
  should not silently stop the vibration it asked for earlier.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .battery import _CURVE, CELL_COUNT, FULL_AT, battery

router = APIRouter(prefix="/api/v1", tags=["public"])


# ----------------------------------------------------------------- models
#
# EVERY float field declares allow_inf_nan=False.
#
# Python's json.loads accepts the bare literals NaN, Infinity and
# -Infinity, so a request can carry a non-finite number into the
# server. Pydantic's range checks (ge/le) compare False against NaN, so
# the value is rejected -- and then the REJECTION raised: FastAPI's
# default handler puts the offending input into the error body, and the
# body is rendered with json.dumps(allow_nan=False), which raises
# ValueError. The observable result was a 500 with no information
# instead of a 422 that names the field.
#
# allow_inf_nan=False makes the refusal happen where it belongs -- in
# the model, before any handler runs -- so the answer is a 422.


class StrokeIn(BaseModel):
    percent: float = Field(..., ge=0, le=100, allow_inf_nan=False,
                           description="target position, 0-100")
    vibration: int | None = Field(None, ge=0, le=255,
                                  description="omit to keep the current level")
    hold: bool = Field(True,
                       description="keep the position until another action "
                                   "ends it (default). false = release it "
                                   "automatically when the caller goes "
                                   "quiet, as the page's safety net does")


class GaitIn(BaseModel):
    """A gait: a list of steps, each a position and a hold.

    The steps may be given as text -- "30:3,70,20:1.5" -- or as a list
    of objects. The text form is what a caller with a string has, and
    the object form is what a caller with structured data has.
    """

    steps: str | list = Field(...,
                               description="a list of steps, or 'pos:hold,...'")
    repeat: bool = Field(True, description="repeat the sequence forever")
    vibration: int = Field(0, ge=0, le=255)


class IntensityIn(BaseModel):
    """The device's three native intensity steps.

    ``level`` is the app's ORDINAL -- 0 = low, 1 = medium, 2 = high --
    not a percentage. The frames are hardcoded, one per step, each with
    the CRC the app hardcodes too, so there is no fourth step to send
    and no way to scale one.
    """

    level: int = Field(..., ge=0, le=2,
                       description="0=low, 1=medium, 2=high (the app's ordinal)")


class AutoStartIn(BaseModel):
    min_percent: float = Field(10, ge=0, le=100, allow_inf_nan=False)
    max_percent: float = Field(70, ge=0, le=100, allow_inf_nan=False)
    vibration: int = Field(0, ge=0, le=255)
    traverse_seconds: float = Field(0.5, ge=0.1, le=1.5,
                                    allow_inf_nan=False,
                                    description="one-way time; a cycle is 2x")
    dwell_seconds: float = Field(0.0, ge=0.0, le=10.0, allow_inf_nan=False)


class AutoParamsIn(BaseModel):
    """Partial update: only the fields present are applied."""

    min_percent: float | None = Field(None, ge=0, le=100,
                                      allow_inf_nan=False)
    max_percent: float | None = Field(None, ge=0, le=100,
                                      allow_inf_nan=False)
    vibration: int | None = Field(None, ge=0, le=255)
    traverse_seconds: float | None = Field(None, ge=0.1, le=1.5,
                                           allow_inf_nan=False)
    dwell_seconds: float | None = Field(None, ge=0.0, le=10.0,
                                        allow_inf_nan=False)


# ------------------------------------------------------- dependency wiring
#
# Imported lazily by the app module and injected, so this file stays testable
# and free of any dependency on how the session happens to be created.

_session_getter = None          # () -> DeviceSession
_auto_getter = None             # () -> AutoMotion
_auto_start = None              # async (AutoParams) -> dict
_auto_stop = None               # async () -> dict
_auto_update = None             # async (AutoParams) -> dict
_monitor_getter = None          # () -> PressureMonitor
#: The ONE lock that guards "a link is being opened or closed".
#:
#: It is INJECTED rather than imported, because it lives in webapp.py
#: and webapp.py imports THIS module -- so importing it back would be a
#: circular import. The lock is the app's, and this router is wired to
#: it.
#:
#: WHY IT MATTERS: the UI family and the public family drive the SAME
#: DeviceSession. The UI family's connect() takes the lock; this one
#: used to take nothing, so two callers could each start their own BLE
#: scan against one peripheral. BlueZ then answered one of them with
#: "org.bluez.Error.InProgress", which is a BlueZ internal error, not
#: the "a connection is already being made" answer the caller needed.
#: One lock, both families: the second caller waits and then finds the
#: link already up.
_connect_lock_getter = None     # () -> asyncio.Lock
#: Called BEFORE a link is opened, so the previous link's measurements
#: are cleared. Injected for the same reason as the lock: it owns this
#: module's state, and this module imports the router.
_link_opening = None            # () -> None
#: Called after a link is established, so the UI family's per-session
#: background work also runs for a link opened through THIS prefix.
#:
#: It is INJECTED, because the tasks it creates are module-level state
#: in webapp.py and webapp.py imports this module. Without it, a link
#: opened over /api/v1 had no 60 Hz broadcast, no recording and no
#: re-arm loop: the link was up and the panels were dead.
_link_established = None        # async (session) -> None
#: Called before a link is torn down, so the same background work is
#: stopped. Without it, a link opened over /api/v1 left its recorder
#: running and its file without an end line.
_link_closing = None            # async (session) -> None
#: () -> dict. The sweep's state, WITHOUT building one to answer it.
#:
#: The motion object is created on first use, so asking the public
#: endpoint "is a sweep running?" through the object getter would
#: build the whole machine -- and hold a reference to the session --
#: for a question whose answer is "no". The webapp already has a
#: reader that does not do that, and it is injected here rather than
#: re-implemented.
_auto_state_getter = None        # () -> dict


def wire(session_getter, auto_getter, auto_start, auto_stop, auto_update,
         monitor_getter=None, connect_lock_getter=None,
         link_opening=None, link_established=None, link_closing=None,
         auto_state_getter=None):
    """Install the callables this router needs. Called once at app setup."""
    global _session_getter, _auto_getter, _auto_start, _auto_stop
    global _auto_update, _monitor_getter, _connect_lock_getter
    global _link_opening, _link_established, _link_closing
    global _auto_state_getter
    _session_getter = session_getter
    _auto_getter = auto_getter
    _auto_start = auto_start
    _auto_stop = auto_stop
    _auto_update = auto_update
    _monitor_getter = monitor_getter
    _connect_lock_getter = connect_lock_getter
    _link_opening = link_opening
    _link_established = link_established
    _link_closing = link_closing
    _auto_state_getter = auto_state_getter
    # The commanded travel is a HOST-side reading: the device reports where
    # the surface is touched, never how far the rod was driven out. So the
    # monitor is given a reader for it, and samples it once per telemetry
    # frame on the frame's own clock.
    #
    # It is wired HERE rather than in webapp.py so the telemetry side of the
    # system keeps no knowledge of the send pump: the monitor stays
    # testable on its own, and the only place that knows both is the one
    # place that owns both.
    if monitor_getter is not None:
        monitor_getter().set_travel_provider(_make_travel_reader(session_getter))


def _connect_lock():
    """The shared connection lock, or a private one if none was wired.

    The fallback exists so this module stays usable on its own (the
    tests wire it directly). It is NOT the app's lock, so wiring it is
    what makes the two API families share one critical section.
    """
    if _connect_lock_getter is None:
        return _fallback_lock()
    return _connect_lock_getter()


_fallback_lock_obj = None


def _fallback_lock():
    global _fallback_lock_obj
    if _fallback_lock_obj is None:
        import asyncio
        _fallback_lock_obj = asyncio.Lock()
    return _fallback_lock_obj


def _make_travel_reader(session_getter):
    """Build the callable that reports the commanded travel, in percent.

    It reads the session's own ``active_percent`` -- the same number the
    "当前行程" tile and the 3D view show -- so the history, the live panel
    and the 3D view cannot disagree about where the rod is.

    None is returned when there is no session yet, and the monitor treats
    None as "unknown" rather than as zero: a missing reading and a rod at
    zero are different facts.
    """
    def read_travel():
        s = session_getter()
        if s is None:
            return None
        return getattr(s.stats, "active_percent", None)
    return read_travel


def _need_monitor():
    if _monitor_getter is None:
        raise HTTPException(status_code=503, detail="service not wired")
    return _monitor_getter()


def _need_session():
    if _session_getter is None:
        raise HTTPException(status_code=503, detail="service not wired")
    s = _session_getter()
    if not s.connected:
        raise HTTPException(status_code=409,
                            detail="not connected; POST /api/v1/connection/connect first")
    return s


def _bind_telemetry(s):
    """Hand the session the monitor and issue the telemetry command.

    Two separate things are missing without this, and fixing only the first
    produces a link that looks alive while reporting nothing:

    * ``raw_notify`` routes incoming frames into the monitor. Assigning it
      also captures the telemetry object (the setter does both), which is what
      ``arm_sensors`` reads ``armed`` / ``frames_seen`` from. Without the
      binding ``_telemetry`` stays None, so arming reports False no matter
      how many frames the device actually sent.
    * the command itself. Issuing it is what starts the stream, so a caller
      that connects over this prefix has to get it issued here or the
      endpoint reports a flat zero forever.

    Both used to happen only in the UI family's connect route, which left
    every /api/v1 caller with a connected session that could never produce
    telemetry. No regression script exercised that path.
    """
    m = _monitor_getter()
    s.raw_notify = m.feed
    return m


def _need_auto():
    if _auto_getter is None:
        raise HTTPException(status_code=503, detail="service not wired")
    return _auto_getter()


def _median(values):
    """The median of a list of numbers.

    Written out rather than imported so the behaviour is visible: for
    an EVEN count the median is the mean of the two middle values, and
    a reader of this file should not have to look it up.
    """
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return None
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _outage_seconds(st) -> Optional[float]:
    """How long the link has been down, in seconds, or None if it is up.

    The session records WHEN the link went down (a monotonic clock
    stamp), and the page turns that into a number by subtracting "now".
    The public endpoint publishes the same number, computed the same
    way -- it is read out of the session's own field rather than kept
    as a second counter, so the two cannot drift.
    """
    import time as _time
    if not st.link_down_at:
        return None
    return round(_time.monotonic() - st.link_down_at, 2)


def _client_age_seconds(st) -> Optional[float]:
    """How long ago the client was last seen, in seconds, or None.

    None means "the client has never been seen", which is a different
    statement from "the client was here 0.0 s ago".
    """
    import time as _time
    if not st.last_client_at:
        return None
    return round(_time.monotonic() - st.last_client_at, 2)


def _capability(session, name: str):
    """One of the model's three-state capability flags, or None.

    The flags are three-state -- True / False / None -- and None means
    "the model was not identified", which is a statement about what we
    KNOW, not about the hardware. The value is passed through as it is,
    so a caller can tell the three apart.
    """
    stats = getattr(session, "stats", None)
    if stats is None:
        return None
    return getattr(stats, name, None)


def _monitor_slot():
    """Which touch segment is engaged right now, or None.

    Read out of the PRESSURE MONITOR, which is the only thing that
    knows which of the device's five segments is lit. None means "no
    segment is lit" -- and also "the monitor has not seen a frame yet",
    because the monitor's own reading is None until the device streams.
    """
    if _monitor_getter is None:
        return None
    try:
        return _monitor_getter().state().get("active_slot")
    except Exception:                                       # noqa: BLE001
        # The monitor is not an answer this endpoint can depend on. If
        # it cannot be read, the slot is simply not reported -- the
        # rest of the connection state must still be answered.
        return None


def _auto_state_for_api() -> dict:
    """The sweep's state, as the public endpoint publishes it.

    THE SWEEP IS NOT BUILT JUST TO BE ASKED ABOUT IT. The motion
    object is created on first use, and creating one to answer "is a
    sweep running" would build the whole machine -- and hold a
    reference to the session -- for a question whose answer is "no".

    So the state is read through the INJECTED reader, which answers
    with the default state when no sweep has ever been started. When
    no reader was wired (this router used on its own), the motion
    object is consulted directly -- and the default state is answered
    if there is not one.
    """
    if _auto_state_getter is not None:
        try:
            return _auto_state_getter()
        except Exception:                                   # noqa: BLE001
            pass
    if _auto_getter is None:
        from .automotion import AutoState
        return AutoState().as_dict()
    try:
        auto = _auto_getter()
    except Exception:                                       # noqa: BLE001
        from .automotion import AutoState
        return AutoState().as_dict()
    state = getattr(auto, "state", None)
    if state is None:
        from .automotion import AutoState
        return AutoState().as_dict()
    return state.as_dict()


# ------------------------------------------------------------- connection

@router.get("/connection")
async def get_connection() -> JSONResponse:
    """Link state and device identity. Safe to call when disconnected.

    THE PUBLIC CONTRACT IS THE SESSION'S OWN SNAPSHOT, PLUS THE FEW
    EXTRA SHAPES A CALLER NEEDS.

    It used to publish EIGHT fields out of the thirty-six the session
    knows, so a program driving the device from outside could not read
    the numbers that make a disconnects diagnosable -- the reconnect
    attempts, the link outage, the corrections the feedback loop made,
    the sensors' armed state. The page reads them (they are in
    /api/state), but a caller of the PUBLIC contract could not.

    So the fields are published from ``stats.snapshot()``, which is the
    ONE place the session's state is described. A key added there
    reaches every surface that reads the snapshot, and the public
    endpoint cannot fall behind the dashboard again.

    WHAT IS NOT PUBLISHED. The snapshot carries the device's serial
    number, and the public API does publish it -- under ``device`` --
    because a caller of /api/v1 is explicitly asking which device it is
    talking to, and that is the API's job (see webapp.py's _snapshot(),
    which is a different surface and redacts it). What is NOT published
    here is the BLE address, the link's private identifiers and the
    operator's own identity: nothing in this response names a person
    or a card. Every value below is a MEASUREMENT or a COUNTER.
    """
    s = _session_getter()
    st = s.stats
    # THE SNAPSHOT IS THE SOURCE. It is read once, here, and the fields
    # below are taken out of it -- so the public endpoint and the
    # dashboard cannot disagree about a number.
    snap = st.snapshot()
    # THE SWEEP'S STATE IS READ ONCE, HERE. It was read four times in
    # an earlier draft of this endpoint, and the four readings were
    # taken at four different moments -- so the "corrections" a caller
    # read could disagree with the "elapsed" next to it.
    _auto = _auto_state_for_api()
    _auto_params = _auto.get("params") or {}
    return JSONResponse({
        "ok": True,
        "connected": s.connected,
        "link_ok": st.link_ok,
        # The rest is what makes a disconnects diagnosable rather than just
        # "it stopped": on a host that drops the link periodically, these are
        # the numbers that show it happening.
        "reconnects": st.reconnects,
        # THE MEDIAN, NOT THE LONGEST.
        #
        # This used to be `sorted(st.link_lifetimes)[-1]`, which is the
        # LAST element of the sorted list -- the MAXIMUM. The name says
        # median, the README's reading table says median, and the
        # reading table makes a DECISION with it: a link that survived
        # once for 100 seconds reports 100 here, while the median says
        # 40, and the two point at opposite conclusions about whether
        # the fault is in this host's Bluetooth stack.
        #
        # So the number is what its name says. The median of an even
        # count is the mean of the two middle values, which is what
        # statistics.median does.
        "median_link_lifetime": (round(_median(st.link_lifetimes), 1)
                                 if st.link_lifetimes else None),
        "connected_seconds": round(st.connected_seconds, 1),
        "frames_sent": st.frames_sent,
        "write_errors": st.write_errors,

        # ---------------------------------------------------- the link
        #
        # The recovery counters. A link that drops every ~40 s on Linux
        # is a different fault from one that drops once and stays down,
        # and the difference is these three numbers.
        "reconnect_attempts": snap["reconnect_attempts"],
        "reconnect_giveups": snap["reconnect_giveups"],
        "link_outage_s": _outage_seconds(st),
        "link_gave_up": snap["link_gave_up"],
        "operator_disconnected": snap["operator_disconnected"],
        "recovery_seconds": snap["recovery_seconds"],
        "gatt_refreshes": snap["gatt_refreshes"],
        "reassert_frames": snap["reassert_frames"],
        # Whether the CLIENT is still there, and for how long it has been
        # gone. The page's idle guard uses these; a caller watching the
        # device from outside needs to know whether the operator left.
        "client_present": s.client_present(),
        "client_age_s": _client_age_seconds(st),
        # The identity query's own answer: empty means it worked, and a
        # message names the reason it did not.
        "identity_error": snap["identity_error"],

        # ---------------------------------------------------- the motion
        #
        # What was commanded, what is being sent and what the DEVICE
        # reported. Three different numbers, and only one of them is
        # feedback.
        "target_percent": snap["target_percent"],
        "active_percent": snap["active_percent"],
        "reported_percent": snap["reported_percent"],
        "vibration": snap["vibration"],
        "streaming": snap["streaming"],

        # ---------------------------------------------------- the sweep
        #
        # The sweep's own counters: whether it is running, whether it is
        # closed-loop on a position the device actually reports, and how
        # many times it had to correct itself.
        #
        # THEY ARE PUBLISHED AS SCALARS, NOT AS THE SWEEP'S WHOLE STATE
        # DICT. The state's "params" carries the motion TEMPLATE, and
        # the template is deliberately NOT part of the public contract
        # (GET /api/v1/auto builds an explicit whitelist and leaves it
        # out). Publishing the whole dict here would be a second door
        # into the very thing that door was built to keep shut.
        #
        # The state is read ONCE, here, so the "corrections" a caller
        # reads cannot disagree with the "elapsed" next to it.
        "auto_running": bool(_auto.get("running")),
        "corrections": _auto.get("corrections"),
        # Whether the device's reported position is AVAILABLE to close
        # the loop on. This is the FACT; "feedback" below is the
        # operator's REQUEST. They are different, and both are read.
        "feedback_ok": _auto.get("feedback_ok"),
        "elapsed": _auto.get("elapsed"),
        "dwell_seconds": _auto_params.get("dwell_seconds"),
        # Whether the sweep is CLOSED-LOOP -- the operator's REQUEST
        # ("use the device's reported position").
        "feedback": _auto_params.get("feedback"),

        # ---------------------------------------------------- the sensors
        #
        # Whether the sensors were armed at all, and whether the device
        # was pushing telemetry when they were. Both are the difference
        # between "the panel is flat because nothing was touched" and
        # "the panel is flat because the device never streamed".
        "pump_alive": snap["pump_alive"],
        "sensors_armed": snap["sensors_armed"],
        "telemetry_frames_at_arm": snap["telemetry_frames_at_arm"],
        "stream_keepalives": snap["stream_keepalives"],
        "stream_commands": snap["stream_commands"],
        "position_sensors": _capability(s, "position_sensors"),

        # The live touch reading: which segment is engaged and how deep.
        "slot": _monitor_slot(),
        "travel_reported": snap["reported_percent"],

        "device": {
            "serial": st.serial,
            "firmware": st.firmware,
            # THE TWO MCU REVISIONS, AS REPORTED.
            #
            # The session keeps them APART, because the joined string
            # ("mcu_firmware", "200/200") cannot say WHICH of the two
            # answered: a device that reports only mcu1 gives "200/",
            # and one that reports only mcu2 gives "/200". Two
            # different facts, one shape.
            #
            # None means "this MCU's revision was not reported", which
            # is a different statement from 0.
            "mcu1_fw": snap["mcu1_fw"],
            "mcu2_fw": snap["mcu2_fw"],
            "power": st.power,
            # The voltage is what the device sends; the percent is derived from
            # it with the manufacturer's own curve. Both are reported so a
            # caller can see what the reading was based on.
            "battery_percent": (battery(st.power) or {}).get("percent"),
            "battery_cell_volts": (battery(st.power) or {}).get("cell_volts"),
            "rssi": st.rssi,
            "mtu": st.mtu,
        },
        "platform": snap.get("platform"),
        "simulated": bool(s.simulate),
        "error": st.last_error or st.identity_error or "",
    })


@router.post("/connection/connect")
async def post_connect() -> JSONResponse:
    """Open the link. Takes seconds: a BLE scan runs first.

    THE WHOLE OPENING TAKES THE APP'S CONNECTION LOCK. The UI family's
    connect() takes it, and the two families drive the SAME session, so
    without it two callers could each start their own BLE scan against
    one peripheral. BlueZ answered the loser with
    "org.bluez.Error.InProgress" -- a BlueZ internal error, not the
    "a connection is already being made" answer a caller needs. With
    the lock, the second caller waits and then finds the link up.

    The lock is HELD ACROSS THE SCAN, because the scan is the thing
    that has to be exclusive: it is the BlueZ scanner, and two scans
    against one peripheral is the failure. That is the same trade the
    UI family makes.
    """
    s = _session_getter()
    async with _connect_lock():
        if s.connected:
            return JSONResponse({"ok": True, "connected": True, "already": True})
        # The PREVIOUS LINK'S MEASUREMENTS ARE CLEARED BEFORE THE NEW
        # LINK IS MADE. The monitor's counters (frames_seen, peak,
        # press_events) describe one link; left alone they accumulated
        # across links, so /api/state's telemetry_frames was a lifetime
        # total and the touch_span_pct of a new link was divided by the
        # OLD link's peak. The order matters: clear, then connect, then
        # arm -- clearing after arming would leave the panel saying
        # 未激活 on a link that was streaming.
        if _link_opening is not None:
            _link_opening()
        try:
            await s.connect()
        except Exception as exc:                                  # noqa: BLE001
            # Surface the reason, not just the class: "device not found" and
            # "already connected to a phone" need different responses from a
            # caller deciding whether to retry.
            raise HTTPException(status_code=409,
                                detail="%s: %s" % (type(exc).__name__, exc)) from exc

        # Bind before arming, and arm here rather than leaving it to the caller:
        # the UI family's connect route did both, so a program driving the device
        # over this prefix used to get a link that stayed connected forever while
        # /pressure reported a flat zero. The command goes out once per link --
        # re-sending it interrupts the stream the first one started.
        m = _bind_telemetry(s)
        armed = await s.arm_sensors()
        m.arm(armed)

        # THE UI FAMILY'S PER-SESSION WORK RUNS FOR THIS LINK TOO.
        #
        # The arm loop, the 60 Hz broadcast and the 10 Hz recording pump
        # belong to the LINK, not to the endpoint that opened it. They
        # used to be created only by the UI family's connect route, so
        # a link opened over /api/v1 had a live BLE connection and
        # nothing else: no broadcast, no recording, no re-arm. The
        # panels looked dead and the recording never started, and the
        # only way to tell was to look at /api/records and see
        # recording: false.
        if _link_established is not None:
            await _link_established(s)

    return JSONResponse({"ok": True, "connected": s.connected,
                         "serial": s.stats.serial})


@router.post("/connection/disconnect")
async def post_disconnect() -> JSONResponse:
    """Close the link, and END THE RECORDING.

    THE LINK'S BACKGROUND WORK IS STOPPED HERE, not only in the UI
    family's disconnect. The tasks belong to the link; ending the link
    over one endpoint has to end them, whichever endpoint it was. The
    old version stopped only the sweep and the session, so a link
    closed over /api/v1 left the recorder running and left its file
    without an end line -- and the three background tasks were never
    cleared, so the next connect saw a live task and skipped creating
    its own.
    """
    a = _auto_getter()
    if a is not None and (a.running or a.gait_running):
        await a.stop()
    s = _session_getter()
    async with _connect_lock():
        await s.disconnect()
        # The recording is ended AFTER the link is down, so the
        # end-of-recording line carries the link's final state.
        if _link_closing is not None:
            await _link_closing(s)
    return JSONResponse({"ok": True, "connected": s.connected})


# ----------------------------------------------------------------- stroke

@router.get("/stroke")
async def get_stroke() -> JSONResponse:
    """Current position. ``percent`` is what was commanded; ``active`` is what
    the send loop is emitting right now.

    ``held`` says whether the position is being HELD -- that is, whether
    it will stay where it is until something else ends it. It is false
    when the position was commanded but is not held (a ``hold: false``
    stroke, a position nothing is keeping), and it is what a caller
    reads to know whether its command is still in force.
    """
    s = _need_session()
    st = s.stats
    return JSONResponse({
        "ok": True,
        "percent": round(st.target_percent, 1),
        "active_percent": round(st.active_percent, 1),
        "reported_percent": (None if st.reported_percent is None
                             else round(st.reported_percent, 1)),
        "vibration": st.vibration,
        "streaming": st.streaming,
        "auto_running": bool(_need_auto().running),
        # Is the position held? Read from the SESSION's own flag, not
        # recomputed here, so this cannot disagree with the guard that
        # actually enforces it.
        "held": bool(getattr(s, "_held_by_client", False)
                     or getattr(s, "_held_by_button", False)),
    })


@router.post("/stroke")
async def post_stroke(body: StrokeIn) -> JSONResponse:
    """Command a position. Applies immediately and HOLDS until changed.

    The hold is the point of this route, and it used to be a promise
    the route did not keep.

    An HTTP client has no page, so it cannot send the heartbeat that
    the page's hold depends on. The idle guard therefore treated it as
    an absent client and retracted the position CLIENT_GONE_SECONDS
    (2.5 s) after the command -- so a caller that asked for 100% got a
    200, watched the device go out, and then watched it come straight
    back in. Measured on the simulator against the real session:

        POST /api/v1/stroke {"percent": 100}   -> 200
        t=0.5s   device=10000
        t=3.5s   device=0      <- retracted, with nobody asking

    So the route now claims a hold of its own, and the position stays
    where it was put until something explicitly ends it:

      * another ``POST /stroke`` (a new position -- also re-claims the
        hold, so a caller can drive it to 100% and then to 30%);
      * ``POST /stroke/release``;
      * a sweep (``POST /auto/start``) or a gait (``POST /gait/start``)
        taking over;
      * ``POST /auto/stop`` / ``POST /gait/stop`` / the stop button;
      * a disconnect.

    THE PAGE'S SAFETY NET IS UNCHANGED. A browser tab that goes away
    still retracts in 2.5 s, because the page never claims this hold --
    only this route does. A caller that wants the old behaviour -- the
    position released automatically -- asks for it explicitly.
    """
    s = _need_session()
    # An omitted vibration means "keep the current level", so the gate has to
    # read the value that will actually be in effect -- `body.vibration` would
    # be None here and the guard would pass a request that then sets a
    # non-zero level on a model that has none. Merged first, gated second.
    vib = body.vibration if body.vibration is not None else s.stats.vibration
    try:
        s.require_vibration_allowed(vib)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    s.set_target(body.percent, vib)
    # THE HOLD IS CLAIMED AFTER THE COMMAND, and only if the command
    # was accepted. Claiming it first would leave a hold behind for a
    # command that was refused -- and a refused command that held the
    # previous position would be worse than no hold at all.
    #
    # A position of ZERO is not a hold: it is a let-go, and the device
    # should go home and STAY home. The release route is the explicit
    # way to say that, but a caller who sends 0 means it, and the guard
    # has nothing to retract.
    #
    # AND ``hold: false`` IS THE OPT-OUT. A caller that wants the OLD
    # behaviour -- the position released automatically once the caller
    # goes quiet, exactly as the page's safety net does -- says so.
    # Without the opt-out the change would be one-way: the hold would
    # be something a caller could not refuse.
    #
    # AND IT CLEARS THE HOLD IT REFUSES. Failing to SET the hold is not
    # enough: a hold claimed by an EARLIER command is still set, and it
    # would keep the new position held anyway -- so ``hold: false``
    # would have been a lie. Measured: after a held 100%, a stroke with
    # ``hold: false`` answered ``held: false`` and the position was
    # STILL held 4.5 s later, because the earlier hold was still in
    # force.
    #
    # It is cleared by set_target() above -- the one writer, which
    # supersedes any earlier hold. This branch used to clear it
    # explicitly, and the mutation harness reported that mutant as
    # SURVIVED: the two versions are equivalent, because set_target()
    # had already cleared it. Dead code that looks load-bearing is
    # worse than no code -- the next reader believes this branch is
    # what does it, and moves it somewhere else.
    if body.percent > 0 and body.hold:
        hold_for_client = getattr(s, "hold_for_client", None)
        if hold_for_client is not None:
            hold_for_client()
    return JSONResponse({"ok": True,
                         "percent": round(s.stats.target_percent, 1),
                         "vibration": s.stats.vibration,
                         "held": bool(body.percent > 0 and body.hold)})


@router.post("/stroke/release")
async def post_release() -> JSONResponse:
    """Let go: drop to zero and stop holding."""
    s = _need_session()
    await s.release()
    return JSONResponse({"ok": True, "percent": 0.0})


@router.post("/intensity")
async def post_intensity(body: IntensityIn) -> JSONResponse:
    """Set one of the device's three NATIVE intensity steps.

    The vendor app sends exactly three, each a whole frame with a
    hardcoded CRC, and they are the only intensity control that does
    not depend on the device's reported limits. Every other path scales
    the vibration by the device's own strokeMax / vibMax -- and a device
    that does not report those, or that ignores the scaled control
    frame, gets nothing.

    So this is the FALLBACK for a device that ignores the scaled
    frame, not a replacement for it.

    A model with no motor has no intensity, and the request is refused
    with a 422 for the same reason the scaled path refuses one: the
    caller must not be left believing a vibration happened.
    """
    s = _need_session()
    try:
        s.require_vibration_allowed(1)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    ok = await s.set_intensity(body.level)
    if not ok:
        raise HTTPException(status_code=502,
                            detail="the intensity frame was not accepted")
    return JSONResponse({"ok": True, "level": body.level})


# ------------------------------------------------------------------- auto

@router.get("/gait")
async def get_gait() -> JSONResponse:
    """What the gait is doing.

    A gait is a SEQUENCE of positions, each held for a while -- the
    sweep's cousin. The sweep moves between two positions forever in a
    sine; a gait walks a list.
    """
    # The gait's state comes from the motion object, so the public
    # family does not need a second reader for it.
    try:
        _g = _auto_getter().gait.as_dict()
    except Exception:                              # noqa: BLE001
        _g = {}
    return JSONResponse({"ok": True, **_g})


@router.post("/gait/start")
async def post_gait_start(body: GaitIn) -> JSONResponse:
    """Walk a sequence of positions, holding each for a while.

    The steps are parsed and range-checked here, so a gait that would
    be refused is refused while the caller is looking at it.

    A LINK IS REQUIRED, exactly like the public family's own sweep
    start. The gait did not require one, so a gait started with no
    device "ran", counted its laps, and reported success while nothing
    moved.
    """
    from .gait import GaitError, parse_gait, parse_gait_text
    # THE STEPS ARE PARSED FIRST, and the link is checked second, for
    # the same reason the UI family does it: a specific complaint about
    # the steps is more useful than a generic "not connected".
    try:
        if isinstance(body.steps, str):
            steps = parse_gait_text(body.steps)
        else:
            steps = parse_gait(body.steps, repeat=body.repeat)
    except GaitError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    s = _session_getter() if _session_getter is not None else None
    if s is None or not getattr(s, "connected", False):
        raise HTTPException(status_code=409, detail="not connected")
    if body.vibration:
        for st in steps:
            if not st.vibration:
                st.vibration = int(body.vibration)
    try:
        out = await _auto_getter().start_gait(steps, repeat=body.repeat)
    except GaitError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # start_gait() returns a DICT -- it calls self.gait.as_dict() itself.
    # Calling .as_dict() on that dict was an AttributeError, and the
    # route answered 500 on every call. The UI family returns the dict
    # as-is, and the two families must not disagree about this.
    return JSONResponse({"ok": True, **out})


@router.post("/gait/stop")
async def post_gait_stop() -> JSONResponse:
    """Stop the gait and let the device go back in."""
    out = await _auto_getter().stop_gait()
    return JSONResponse({"ok": True, **out})


@router.get("/auto")
async def get_auto() -> JSONResponse:
    """Sweep state plus the three settable parameters."""
    st = _need_auto().state.as_dict()
    return JSONResponse({
        "ok": True,
        "running": st["running"],
        "cycles": st["cycles"],
        "current_percent": st["current_percent"],
        "min_percent": st["params"]["min_percent"],
        "max_percent": st["params"]["max_percent"],
        "traverse_seconds": st["params"]["traverse_seconds"],
        "vibration": st["params"]["vibration"],
        "dwell_seconds": st["params"]["dwell_seconds"],
        "error": st["error"],
    })


def _merged(body: AutoParamsIn):
    """Apply a partial update onto the current parameters.

    The range is validated BEFORE clamping. AutoParams.clamp() swaps an
    inverted range so the motion stays well-defined, which is the right
    behaviour for the UI slider -- but for an API caller, silently turning
    {min: 80, max: 20} into {min: 20, max: 80} hides the mistake in a 200
    response. Reject it instead.
    """
    from .automotion import AutoParams

    cur = _need_auto().state.params
    lo = cur.min_percent if body.min_percent is None else body.min_percent
    hi = cur.max_percent if body.max_percent is None else body.max_percent
    if hi < lo:
        # "%g" and NOT "%g"-style formatting: %g prints 0.0000001 as
        # "1e-07", which is a number nobody asked for and cannot read at
        # a glance. "%s" prints the value the caller sent, which is the
        # whole point of putting it in the message.
        raise HTTPException(status_code=422,
                            detail="max_percent (%s) must be >= min_percent (%s)"
                                   % (hi, lo))
    return AutoParams(
        min_percent=lo,
        max_percent=hi,
        vibration=cur.vibration if body.vibration is None else body.vibration,
        traverse_seconds=cur.traverse_seconds
        if body.traverse_seconds is None else body.traverse_seconds,
        dwell_seconds=cur.dwell_seconds if body.dwell_seconds is None
        else body.dwell_seconds,
    ).clamp()


@router.post("/auto/start")
async def post_auto_start(body: AutoStartIn) -> JSONResponse:
    s = _need_session()
    if body.max_percent < body.min_percent:
        raise HTTPException(status_code=422,
                            detail="max_percent must be >= min_percent")
    try:
        s.require_vibration_allowed(body.vibration)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from .automotion import AutoParams
    # THE RUNNING TEMPLATE IS INHERITED, NOT RESET.
    #
    # The five positional arguments left ``preset`` and ``tick_ms`` at
    # their defaults (None), and AutoMotion.start() folds an
    # unspecified template into the DEFAULT one -- the sine. So a
    # /api/v1/auto/start call silently replaced the operator's chosen
    # template: the UI had set "surge", and one public-API call put it
    # back to sine without saying so.
    #
    # The template and the tick are read from the sweep that is
    # already running, and named explicitly. A caller of the public
    # API cannot name a template (the request model has no such
    # field), so "this call did not name one" has to mean "keep the
    # one that is in force" -- which is exactly what update() already
    # does for the same reason.
    _cur = _need_auto().state.params
    out = await _auto_start(AutoParams(
        min_percent=body.min_percent,
        max_percent=body.max_percent,
        vibration=body.vibration,
        traverse_seconds=body.traverse_seconds,
        dwell_seconds=body.dwell_seconds,
        preset=_cur.preset,
        tick_ms=_cur.tick_ms))
    return JSONResponse({"ok": True, "running": out["running"],
                         "min_percent": out["params"]["min_percent"],
                         "max_percent": out["params"]["max_percent"],
                         "traverse_seconds": out["params"]["traverse_seconds"]})


@router.post("/auto/params")
async def post_auto_params(body: AutoParamsIn) -> JSONResponse:
    """Change min / max / traverse (and the rest) while running.

    Takes effect on the next tick; a range change remaps the phase so the
    motion stays continuous instead of jumping to the new range's equivalent.
    """
    # Gated on the MERGED parameters, not on body.vibration: this endpoint
    # accepts a partial update, so an omitted vibration means "keep the current
    # level" and _merged() is what computes it. Gating the raw body would let a
    # caller leave a non-zero level in place on a model that has none, and
    # would equally refuse a harmless `{"max_percent": 60}` against a Vero.
    p = _merged(body)
    if p.max_percent < p.min_percent:
        raise HTTPException(status_code=422,
                            detail="max_percent must be >= min_percent")
    try:
        _need_session().require_vibration_allowed(p.vibration)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    out = await _auto_update(params=p)
    return JSONResponse({"ok": True, "running": out["running"],
                         "min_percent": out["params"]["min_percent"],
                         "max_percent": out["params"]["max_percent"],
                         "traverse_seconds": out["params"]["traverse_seconds"]})


@router.post("/auto/stop")
async def post_auto_stop() -> JSONResponse:
    out = await _auto_stop()
    return JSONResponse({"ok": True, "running": out["running"]})


# ------------------------------------------------------------------ pressure

#: Windows the API accepts. Named spans rather than an open-ended parameter so
#: a caller gets a bounded, predictable payload instead of whatever happens to
#: be in the buffer.
PRESSURE_WINDOWS = (10.0, 30.0, 60.0)
PRESSURE_MAX_WINDOW = 60.0


@router.get("/battery")
async def get_battery() -> JSONResponse:
    """Battery state, converted with the manufacturer's own curve.

    The device only reports a pack voltage, so a percent has to come from
    somewhere. This uses the same path the app does: pack -> per-cell
    (``/ 4.0``) -> nearest row of the 122-point discharge curve shipped in
    ``assets/battery_data.json`` -> above 95 becomes 100.

    ``percent`` is null while disconnected or before the identity frame has
    arrived. ``in_range`` says whether the voltage actually falls inside the
    measured curve, so a clamped figure is never mistaken for a measurement.
    """
    s = _session_getter()
    b = battery(s.stats.power)
    est = s.battery.state()
    if b is None:
        # THE SAME KEYS IN BOTH BRANCHES.
        #
        # This branch used to carry eight keys and the other thirteen,
        # so a caller reading both shapes saw two different contracts
        # for one endpoint -- and a caller that read a field without
        # checking whether it was there had to handle its absence in
        # one case and not the other. The keys are now the same set in
        # both branches; what differs is their VALUE.
        #
        # The values are null (or zero for a count), which is the
        # honest answer: there is no reading, so there is no number.
        # The extra "note" says WHY there is no number.
        return JSONResponse({
            "ok": True,
            "percent": None,
            "percent_shown": s.stats.battery_percent_shown,
            "samples": est["samples"],
            "median_cell_volts": est["median_cell_volts"],
            "filter_reason": est["reason"],
            "pack_volts": None,
            "cell_volts": None,
            "table_cell_volts": None,
            "table_percent": None,
            "in_range": False,
            "cell_count": 0,
            # THE LINK STATE IS IN BOTH BRANCHES, and it is the field
            # that tells the two apart: a caller reading "percent: null"
            # can tell "no device" from "a device that has not reported
            # yet".
            "connected": s.connected,
            "note": "no voltage reported yet; connect and read device info",
        })
    return JSONResponse({
        "ok": True,
        # percent: this voltage converted right now.
        # percent_shown: what the app displays, after the 1.2.3 filter
        #   (median of a 10 s window, decreases confirmed twice, rises
        #   suppressed). null until the filter has enough samples.
        "percent": b["percent"],
        "percent_shown": s.stats.battery_percent_shown,
        "samples": est["samples"],
        "median_cell_volts": est["median_cell_volts"],
        "filter_reason": est["reason"],
        "pack_volts": b["pack_volts"],
        "cell_volts": b["cell_volts"],
        "table_cell_volts": b["table_cell_volts"],
        "table_percent": max(0, min(100, b["table_percent"])),
        "in_range": b["in_range"],
        "cell_count": b["cell_count"],
        "connected": s.connected,
        # THE SAME KEY AS THE NO-VOLTAGE BRANCH, holding null. The two
        # branches now carry the SAME key set, exactly: a caller can
        # read any field without asking whether it is there. The note
        # explains a missing reading; when there IS a reading there is
        # nothing to explain, and null says that.
        "note": None,
    })


@router.get("/battery/curve")
async def get_battery_curve() -> JSONResponse:
    """The full discharge curve, for plotting or for checking a reading."""
    return JSONResponse({
        "ok": True,
        "cell_count": int(CELL_COUNT),
        "full_at": FULL_AT,
        "points": [{"cell_volts": v, "pack_volts": round(v * CELL_COUNT, 4),
                    # The table runs slightly below zero at its floor; the app
                    # clamps on the way out, so report the clamped value here
                    # rather than a figure the app would never display.
                    "percent": max(0, min(100, round(p * 100))),
                    "raw_percent": round(p * 100)}
                   for v, p in _CURVE],
    })


@router.get("/pressure")
async def get_pressure() -> JSONResponse:
    """The live touch reading, its position, and the telemetry counters.

    ``position_pct`` is the useful figure: the device quantises travel into
    five steps of 20% each and reports the deepest one currently engaged. The
    raw value says how firmly that step is engaged, not how hard, so it is not
    a force reading. Null before the first frame arrives.
    """
    m = _need_monitor()
    st = m.state()
    return JSONResponse({
        "ok": True,
        # This channel is a touch/position sensor. `pressure` is kept for
        # compatibility, but `touch_raw` is the honest name: the value is a
        # 12-bit ADC reading whose deviation from the resting level says where
        # the surface is touched, not how hard.
        # The reading that matters: five slots quantised to a position by the
        # device's own algorithm (v1.smali d(ArrayList)). position is 0..10000,
        # position_pct is that as a percentage of travel.
        "position": st.get("position"),
        "position_pct": st.get("position_pct"),
        "active_slot": st.get("active_slot"),
        "touch_raw": st["pressure"],
        "touch_span_pct": (round(st["pressure"] / st["peak"] * 100)
                           if st["pressure"] is not None and st["peak"] else None),
        "resting_level": st.get("idle_floor"),
        "press_delta": st.get("press_delta"),
        "pressure": st["pressure"],
        "raw_pct": st.get("raw_pct"),
        "peak": st["peak"],
        "pressed": st["pressed"],
        "press_events": st["press_events"],
        "frames": st["frames"],
        "streaming": st["streaming"],
        "age_s": st["age_s"],
        # Measured from arrival times on the server, not from how often this
        # endpoint happens to be polled.
        "sample_hz": st.get("hz"),
        "interval_ms": st.get("interval_ms"),
        "hz_from_median": st.get("hz_from_median"),
        "interval_samples": st.get("interval_samples"),
        # Distinguishes "the device is not sending" from "the device is
        # sending something this decoder does not recognise". Both look like a
        # flat zero curve, and they need different fixes.
        "notify": m.notify_summary(),
    })


@router.get("/pressure/series")
async def get_pressure_series(
        seconds: float = 10.0,
        channel: int = 0) -> JSONResponse:
    """Continuous readings over the last 10, 30 or 60 seconds.

    Defaults to 10 s; pass ``?seconds=30`` or ``?seconds=60`` for the longer
    spans. Only the three named windows are accepted so the response size stays
    bounded -- 60 s at the measured 60 Hz is roughly 3600 samples.

    The payload carries three time bases on purpose:

    * ``t``      seconds since the first sample in this response
    * ``unix``   absolute epoch seconds, so samples can be correlated with an
                external log
    * ``window_seconds``  what the buffer actually holds right now, which is
                smaller than ``requested_seconds`` until the device has been
                streaming long enough

    ``channel`` selects 0 (primary) or 1 (secondary); both are returned in the
    series, and the named key is a convenience mirror.

    Three different readings come back, and they are NOT parallel arrays:

    * ``pressure`` / ``channel1`` -- the raw 12-bit touch reading.
    * ``slot`` -- the triggered segment per frame (0..4, or null when no
      segment is lit), and ``position_pct`` -- that segment's depth in
      percent. Both are on the ``t`` time base.
    * ``travel_pct`` -- how far the rod was COMMANDED out, which the device
      never reports. It is on the ``travel_t`` time base, and it is empty
      until a travel provider is wired.

    ``covered_seconds`` says how many of the requested seconds each buffer
    actually holds, so a short answer is visible instead of silently
    truncated.
    """
    if seconds not in PRESSURE_WINDOWS:
        raise HTTPException(
            status_code=422,
            detail="seconds must be one of %s"
                   % ", ".join(str(int(w)) for w in PRESSURE_WINDOWS))
    if channel not in (0, 1):
        raise HTTPException(status_code=422, detail="channel must be 0 or 1")

    m = _need_monitor()
    data = m.series(seconds)
    key = "pressure" if channel == 0 else "channel1"
    return JSONResponse({
        "ok": True,
        "channel": channel,
        "values": data[key],
        "unit": "raw 12-bit ADC (touch position, not force)",
        "position": m.position,
        "position_pct": round(m.position / 10000 * 100),
        "count": data["count"],
        "requested_seconds": data["requested_seconds"],
        "window_seconds": data["window_seconds"],
        "sample_hz": data["sample_hz"],
        "peak": data["peak"],
        "t": data["t"],
        "unix": data["unix"],
        "channel0": data["pressure"],
        "channel1": data["channel1"],
        # The triggered segment and its depth, per frame. The same
        # quantiser the live panel reads, so the history and the live
        # readout cannot disagree about which segment is lit.
        "slot": data["slot"],
        "position_pct_series": data["position_pct"],
        # The commanded travel, on its own time base.
        "travel_t": data["travel_t"],
        "travel_pct": data["travel_pct"],
        "travel_samples": data["travel_samples"],
        # The change points, on the same time base as `t`, so the marks can
        # be drawn straight onto the curve. `change_t` is the offset of the
        # marked frame and `change_v` is that frame's value.
        "change_t": data["change_t"],
        "change_v": data["change_v"],
        # How much of the requested window each buffer actually covers.
        "covered_seconds": data["covered_seconds"],
    })


@router.get("/buttons")
async def get_buttons() -> JSONResponse:
    """The button bindings, the recent key presses, and the action menu.

    The device has three keys of its own (mode, plus, minus) and the
    vendor app dispatches them from the notification stream, so a key
    press is a command that arrives over BLE. This endpoint is the
    public family's view of that: what each key is bound to, and which
    keys were pressed recently.

    ``version`` rises on every change to the bindings, so a caller that
    polls can tell "the operator changed something" from "my copy is
    stale".
    """
    from .buttons import store as button_store
    st = button_store().as_dict()
    m = _need_monitor()
    st["events"] = m.button_state()
    st["bound_count"] = len(st.get("bindings") or {})
    return JSONResponse({
        "ok": True,
        "bindings": st["bindings"],
        "version": st["version"],
        "buttons": st["buttons"],
        "actions": st["actions"],
        "events": st["events"],
        "bound_count": st["bound_count"],
    })