"""Cross-platform session: one link, one writer, automatic recovery.

Built from the Linux version but with the platform-specific pieces isolated.
On Linux/BlueZ the link dies every ~40.4 s with HCI "Connection Timeout",
because BlueZ creates the link with a 420 ms supervision timeout -- a kernel
default, not a btusb hardcode -- while refusing the peripheral's request for
7.5 ms / 4000 ms (the command's Reason field is 0x02). An nRF52840 sniffer
watching the air shows the peripheral transmitting normally right through and
past the disconnect, and resuming steady advertising immediately after -- and
the same device holds an indefinite connection to a phone. So the defect is on
the host, and the question this portable build answers is whether it is
Linux-specific. The reading that the controller firmware ignored that
register is withdrawn: the host was silent for only 0.315 s before the death.
Whether the host's [LE] parameters remove the drop is not yet verified.

That is exactly why the recovery path here is kept identical across platforms:
if Windows survives and Linux does not, the comparison is clean.

Platform differences, all funnelled through this module:

  * ``bleak`` picks its backend automatically (BlueZ on Linux, WinRT on
    Windows, CoreBluetooth on macOS), so the BLE calls themselves are portable.
  * The GATT-cache refresh below shells out to ``bluetoothctl``, which exists
    only on Linux. On other platforms it is a no-op, and ``_refresh_gatt_cache``
    reports that it did nothing rather than pretending it worked.
  * There is no privileged MGMT access outside Linux either, so the recovery
    strategy is the same everywhere: reconnect fast and re-assert the position.
"""

from __future__ import annotations

import asyncio
import random
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional

from . import protocol as P
from .battery import battery as _battery, BatteryEstimator
from .monitor import CMD_TELEMETRY_STREAM
from .discovery import (DEVICE_NAME_PREFIXES, DEVICE_SERVICE_UUIDS, find_device,
                        find_device_by_address, last_scan, scan)

# --------------------------------------------------------------- constants

#: Retry cadence for a lost link. A growing backoff steps past the peripheral's
#: narrow advertising window; a short fixed cadence means several attempts land
#: inside each window.
RECONNECT_INTERVAL = 0.25
RECONNECT_JITTER = 0.15

#: Give up on one recovery attempt after this long.
#:
#: It used to be 6.0 s, which was smaller than a single ATTEMPT could take,
#: so it never fired where it mattered: the deadline is only checked between
#: attempts, and one attempt is a 25 s scan plus a 25 s connect plus a 2.5 s
#: identity wait. The deadline was therefore already expired by the time the
#: first attempt finished, which made it decorative -- the real bound was
#: RECONNECT_MAX_ATTEMPTS, and eight attempts of ~52 s each is seven minutes
#: of a wedged pump with no progress reported to the operator.
#:
#: The deadline is now what it says it is: the wall-clock budget for ONE
#: attempt, and it is larger than the longest single attempt can be, so the
#: attempt-count limit is what bounds the pass. It is also passed INTO the
#: attempt, so a device that is advertising but will not accept a
#: connection is cut loose after this long instead of after 52 s.
RECONNECT_DEADLINE = 60.0

#: The budget for ONE attempt, handed to the scan and to the connect.
#:
#: It is derived from RECONNECT_DEADLINE rather than fixed, so the two can
#: never drift apart again: the scan gets half the budget and the connect
#: gets the other half, and the identity wait is inside what is left.
#:
#: The split is deliberate. The scan (find_device) and the connect
#: (BleakClient.connect) are the two things that can each hang on a device
#: that is present but not answering, and giving each half the budget
#: means neither can consume the whole attempt on its own.
RECONNECT_SCAN_BUDGET = RECONNECT_DEADLINE / 2.0
RECONNECT_CONNECT_BUDGET = RECONNECT_DEADLINE / 2.0

#: The identity query's wait. It is a wait for a frame the device pushes,
#: so it is short: the device answers within a few hundred milliseconds
#: when it is going to answer at all.
RECONNECT_IDENTITY_WAIT = 2.5

#: Give up on recovery altogether once this many attempts have failed back to
#: back, whatever the wall clock says. The per-attempt deadline alone let a dead
#: device be retried forever: every window expired, no attempt ever succeeded,
#: and nothing checked the failure count. Reset by any successful link (or an
#: explicit connect()), so this only bites a device that is actually gone.
RECONNECT_MAX_ATTEMPTS = 8

#: A single control frame yields a millimetre of travel; a burst yields the
#: full stroke (confirmed visually). So recovery has to look like a short
#: stream, not one write.
REASSERT_FRAMES = 60
REASSERT_INTERVAL = 0.020

#: Default interval between BLE writes on the send pump.
#:
#: 15 ms, matching the default tick. It was 20 ms for the whole life of the
#: project, and 20 ms was the right default for a 40 ms tick -- the pump
#: fired twice per generator tick and repeated the last target, which is
#: harmless. But the default tick is now the vendor app's 15 ms nominal, and
#: a 20 ms pump is SLOWER than that: it would eat one position in three
#: before the device ever saw it. So the default follows the default tick.
#:
#: It is not a fixed rate: ``set_frame_interval`` moves it, and the motion
#: generator moves it to match the tick it is running. See
#: ``frame_interval_for_tick`` for why that coupling is not optional.
FRAME_INTERVAL = 0.015

#: Lowest interval the pump may be moved to. 8 ms is the app's own fastest
#: tick (``GeneratedMotionPolicy.MIN_TICK_MS = 8``), and a pump faster
#: than that would be writing frames no app writes.
MIN_FRAME_INTERVAL = 0.008

#: Highest interval the pump may be moved to. 30 ms is the app's own
#: slowest tick (``GeneratedMotionPolicy.MAX_TICK_MS = 30``); a slower pump
#: than that would be outside the range the app can produce.
MAX_FRAME_INTERVAL = 0.030


def _fw_revision(value) -> Optional[int]:
    """One MCU firmware revision, as an int, or None if it is not one.

    The device reports the revision as a NUMBER (a real Vero answers
    200). A value that is not a number -- a string, a float, a missing
    key -- is None rather than a guess: None means "this MCU's revision
    was not reported", and a caller can act on that, while a string
    "200" would have to be parsed again by every caller.

    A float that happens to be integral (200.0) is accepted and turned
    into the int 200, because the JSON decoder makes a number a float
    when it is written with a decimal point, and the value is the same
    number either way.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def frame_interval_for_tick(tick: float) -> float:
    """The pump interval that belongs with a given generator tick.

    The pump writes ``self._target``, a VARIABLE, not a queue. So if the
    pump is slower than the generator, the positions computed in between
    are overwritten before anything writes them, and the device sees fewer
    and LARGER jumps than the generator asked for. Measured (see
    docs/audit/P5.2-SMOOTHNESS-WORKORDER.md):

        generator 15 ms, pump 20 ms -> device sees  9.36 % of stroke
        generator 15 ms, pump 15 ms -> device sees  5.36 % of stroke
        the vendor app on the same sweep -> 4.71 % of stroke

    So the pump is moved to the tick itself: same period, so every
    position the generator computes is the one the device is handed. A
    pump that is FASTER than the tick is fine (it repeats the last
    target, which is what the hold behaviour needs), so the result is
    clamped into the pump's own band rather than left unbounded.
    """
    try:
        t = float(tick)
    except (TypeError, ValueError):
        return FRAME_INTERVAL
    return max(MIN_FRAME_INTERVAL, min(MAX_FRAME_INTERVAL, t))

#: With no client contact for this long, treat the operator as gone and release.
CLIENT_GONE_SECONDS = 2.5

#: How long a HELD TARGET survives the BLE link being down.
#:
#: This is a SEPARATE clock from CLIENT_GONE_SECONDS, and the separation is
#: the fix for "the manual stroke cannot be achieved, the automatic sweep is
#: fine".
#:
#: What was measured, on the simulator, driving the real session:
#:
#:   * The operator drags to 50%. The link drops.
#:   * While the link is down, POST /api/keepalive answers 409, so the page's
#:     heartbeat never reaches session.touch(). The page is open the whole
#:     time -- it is the LINK that is down, not the operator.
#:   * The pump's idle guard therefore sees "no client contact" after
#:     CLIENT_GONE_SECONDS and calls release().
#:   * release() zeroes `_want_percent` -- the held intent -- so the operator's
#:     position is gone for good, even though the link comes back seconds
#:     later and the operator never left the page.
#:
#: The automatic sweep never hit this because automotion.py calls
#: session.touch() DIRECTLY, in process, every tick: its presence never has to
#: travel through an HTTP endpoint that a dead link can refuse. That is the
#: whole asymmetry that the HTTP-driven path showed.
#:
#: So the two clocks are separated:
#:
#:   * CLIENT_GONE_SECONDS -- the OPERATOR left (page closed, tab discarded).
#:     Still 2.5 s, still enforced, still the safety net.
#:   * LINK_OUTAGE_HOLD_SECONDS -- the LINK was down. A held target survives
#:     this long, because the operator did not go anywhere.
#:
#: The LINK_OUTAGE clock only runs while the link is down, so a page that is
#: closed for 2.5 s still retracts immediately. What it buys is that a BLE
#: reconnect -- which takes a scan, so seconds -- no longer destroys the
#: operator's position.
LINK_OUTAGE_HOLD_SECONDS = 30.0

#: Stop after this many consecutive failed links so a dead device does not
#: produce an unbounded reconnect storm.
LINK_FAILURE_LIMIT = 8

IS_LINUX = sys.platform.startswith('linux')


@dataclass
class SessionStats:
    frames_sent: int = 0
    write_errors: int = 0
    connected_at: float = 0.0
    target_percent: float = 0.0
    active_percent: float = 0.0
    vibration: int = 0
    streaming: bool = False
    last_target_at: float = 0.0
    #: When the CLIENT was last seen, as opposed to last_target_at, which is
    #: when the session last had any contact at all.
    #:
    #: These were ONE field, and the conflation is what broke the manual
    #: control. `touch()` (client heartbeat) and `set_target()` (an operator
    #: command) both wrote `last_target_at`, so the pump's idle guard could
    #: not tell "the operator went away" from "the operator is still there
    #: but the link is down". While the link is down, the page's heartbeat
    #: cannot get through at all -- POST /api/keepalive answers 409 -- so the
    #: guard fired on a live page and released the operator's position.
    #:
    #: last_client_at is written ONLY by touch(). Nothing else writes it, so
    #: it measures exactly one thing: the client is present.
    last_client_at: float = 0.0
    #: When the BLE link last went down. The held target is protected while
    #: this is younger than LINK_OUTAGE_HOLD_SECONDS, because a link outage
    #: is not the operator leaving.
    link_down_at: float = 0.0
    power: Optional[float] = None
    firmware: str = ""
    serial: str = ""
    rssi: Optional[int] = None
    mtu: Optional[int] = None
    last_error: str = ""
    link_ok: bool = False
    consecutive_failures: int = 0
    #: True once consecutive_failures has reached LINK_FAILURE_LIMIT. The
    #: device is considered gone until a write succeeds or connect() runs, so
    #: recovery can stop churning against a peripheral that is not answering.
    link_gave_up: bool = False
    #: True when the OPERATOR ended the session -- the disconnect button,
    #: or the CLI's own disconnect. Nothing is being retried then, and
    #: nothing will be.
    #:
    #: This is a DIFFERENT fact from link_gave_up, and the two were being
    #: confused. link_gave_up means "the recovery gave up"; it says
    #: nothing about whether something is still TRYING. After an explicit
    #: disconnect the link is down, the failures are zero and the give-up
    #: flag is false -- so a page that asked "is a link up, and is nothing
    #: being retried?" answered "a link is not up, and the server has not
    #: given up", and read that as "a reconnect is in progress".
    #:
    #: An explicit disconnect used to read as "not connected, and something
    #: is trying to fix it", so the page showed 「重连中……」 after the
    #: operator had already closed the link. The disconnect was deliberate,
    #: so the page must not claim something is still being attempted.
    #:
    #: So the operator's own disconnect is recorded, and the page reads it
    #: as the fourth state: not connected, and deliberately so.
    operator_disconnected: bool = False
    reconnects: int = 0
    #: Attempts spent inside the current (or most recent) recovery pass, and
    #: how many recovery passes have exhausted RECONNECT_MAX_ATTEMPTS. A
    #: climbing giveup count means the device is gone, not that the radio is
    #: flaky — the operator has to reconnect by hand.
    reconnect_attempts: int = 0
    reconnect_giveups: int = 0
    pump_alive: bool = False
    sensors_armed: bool = False

    #: Times command 52 was re-sent after the first burst. The App does this
    #: exactly once, when its buffer reaches 7 frames.
    stream_keepalives: int = 0

    #: Telemetry frames the device had already pushed when /api/arm was last
    #: called. If this stays 0 across a session, the device is not streaming at
    #: all and no amount of host-side cleverness will produce a curve.
    telemetry_frames_at_arm: int = 0

    #: Times the A-generation telemetry command was sent. One per link is
    #: correct; a climbing count means the device is being reset repeatedly.
    stream_commands: int = 0
    #: Frames pushed by the post-link-up re-assert burst.
    reassert_frames: int = 0
    #: Seconds spent waiting for the link to come back, summed. This is what the
    #: operator feels as a hitch.
    recovery_seconds: float = 0.0
    #: GATT cache rebuilds forced during recovery.
    gatt_refreshes: int = 0
    #: How long each individual link has stayed up. On Linux this clusters
    #: around 40 s; the whole point of the Windows run is to see what it does
    #: there.
    link_lifetimes: list = field(default_factory=list)
    #: Cumulative connected time since the session started.
    connected_seconds: float = 0.0
    #: Why the last identity query produced nothing. Empty means it worked.
    #: Without this, a failed query looks identical to a device that simply
    #: has nothing to report.
    identity_error: str = ""
    #: The serial of the device THIS link is talking to, or "" while the
    #: identity query has not answered yet.
    #:
    #: This is deliberately separate from :attr:`serial`. ``serial`` is the
    #: last serial the session ever saw, and it is what the UI shows; it
    #: must survive a reconnect so the operator can still read which device
    #: they were connected to while the new link is being established.
    #:
    #: But refresh_identity() used ``if self.stats.serial: return`` to decide
    #: "the identity has already arrived", and since ``serial`` was never
    #: cleared, the SECOND connect() returned on its first check -- before
    #: the device had answered anything. The new device was therefore never
    #: identified: the model, the firmware, the battery, the RSSI and the
    #: MTU all stayed at the PREVIOUS device's values, and no error was
    #: raised either, because nothing had timed out.
    #:
    #: Measured: a connect -> disconnect -> connect cycle spent 0.202 s in
    #: the second connect (against the 2.5 s the query needs), and the
    #: identity_error stayed empty -- "identity failed" and "identity
    #: already known" looked identical from outside.
    link_serial: str = ""
    #: Position the DEVICE reported, as a percentage. Distinct from
    #: target_percent (what we asked for) and active_percent (what the send
    #: loop is currently emitting): only this one is feedback. None until
    #: the device actually reports.
    reported_percent: Optional[float] = None

    #: What the app would display for the battery: the 1.2.3 filtered reading,
    #: distinct from the immediate conversion of the newest frame. Written by
    #: DeviceSession whenever the estimator advances.
    battery_percent_shown: Optional[int] = None

    # ------------------------------------------------------ which device?
    #
    # Four fields that all answer "which model is this", and all three-state:
    # "" / False / True for `product` and the booleans' meaning is None =
    # not identified. The device never states its own capabilities -- the
    # caps query it answers carries sn / power / ble_1_fw / mcu1_fw / mcu2_fw /
    # RSSI / "MTU:" and nothing else -- so `product` is derived from the serial
    # prefix (protocol.PRODUCTS) and the two booleans come from the vendor
    # app's per-model table (protocol.DEVICE_PROFILES). See DEVICE_PROFILES
    # for the evidence class and for why None must survive as its own value.
    #
    #: Model name from the serial prefix. Empty "" until a session has been
    #: told a serial, after which it holds detect_product()'s answer, which is
    #: "Unknown" for a serial that matches no prefix. "" and "Unknown" both
    #: mean "not identified" but they are not the same string: "" is the
    #: never-asked state, "Unknown" is the asked-and-recognised-nothing
    #: answer. The two capability booleans carry the distinction properly, as
    #: None either way; this field carries it only as a display detail.
    product: str = ""
    #: True / False for a recognised model; None when we do not know the model.
    supports_vibration: Optional[bool] = None
    #: Same three-state shape. NOTE: reading True here does NOT mean the
    #: position decoder is correct for that model -- see DEVICE_PROFILES.
    position_sensors: Optional[bool] = None
    #: The two MCU firmware revisions as "mcu1/mcu2" (a real Vero reports
    #: 200/200). Empty until a caps frame carries them.
    mcu_firmware: str = ""
    #: The two MCU firmware revisions, AS THE DEVICE REPORTED THEM.
    #:
    #: ``mcu_firmware`` above is the JOINED string ("200/200"), which is
    #: what the CLI prints and what a human reads. These two are the raw
    #: values, one per MCU, and None means "this MCU's revision has not
    #: been reported" -- which is a different statement from "0".
    #:
    #: They are kept BESIDE the joined string rather than instead of it,
    #: because the joined string cannot say WHICH of the two answered: a
    #: device that reports only mcu1 produces "200/", which is
    #: indistinguishable from one that reports only mcu2 without looking
    #: at the slash.
    mcu1_fw: Optional[int] = None
    mcu2_fw: Optional[int] = None

    def snapshot(self) -> dict:
        lives = self.link_lifetimes[-12:]
        return {
            "frames_sent": self.frames_sent,
            "write_errors": self.write_errors,
            "target_percent": round(self.target_percent, 1),
            "reported_percent": (None if self.reported_percent is None
                                 else round(self.reported_percent, 1)),
            "active_percent": round(self.active_percent, 1),
            "vibration": self.vibration,
            "streaming": self.streaming,
            "serial": self.serial,
            "firmware": self.firmware,
            # THE TWO MCU REVISIONS, AS REPORTED. The joined string
            # ("mcu_firmware") is what a human reads; these two are what
            # a program reads, because only they can say WHICH MCU
            # answered. None means "not reported", which is a different
            # statement from 0.
            "mcu1_fw": self.mcu1_fw,
            "mcu2_fw": self.mcu2_fw,
            "power": self.power,
            # Derived, not measured: the device only sends a pack voltage, so
            # the percent comes from the manufacturer's discharge curve. Both
            # are exposed so the number can be checked against its input.
            "battery_percent": (_battery(self.power) or {}).get("percent"),
            "battery_cell_volts": (_battery(self.power) or {}).get("cell_volts"),
            # What the app would actually show: median over a 10 s window with
            # rises suppressed. Differs from battery_percent whenever the
            # newest frame is noise or the charge is climbing.
            "battery_percent_shown": self.battery_percent_shown,
            "rssi": self.rssi,
            "mtu": self.mtu,
            "last_error": self.last_error,
            "identity_error": self.identity_error,
            "link_ok": self.link_ok,
            "link_gave_up": self.link_gave_up,
            # The operator's own disconnect. The page reads it to tell
            # "not connected, and deliberately so" from "not connected,
            # and something is trying to fix it".
            "operator_disconnected": self.operator_disconnected,
            "reconnects": self.reconnects,
            "reconnect_attempts": self.reconnect_attempts,
            "reconnect_giveups": self.reconnect_giveups,
            "reassert_frames": self.reassert_frames,
            "recovery_seconds": round(self.recovery_seconds, 2),
            "gatt_refreshes": self.gatt_refreshes,
            "pump_alive": self.pump_alive,
            "sensors_armed": self.sensors_armed,
            "stream_keepalives": self.stream_keepalives,
            "telemetry_frames_at_arm": self.telemetry_frames_at_arm,
            "stream_commands": self.stream_commands,
            "platform": sys.platform,
            "link_count": len(self.link_lifetimes),
            "link_lifetimes": [round(x, 1) for x in lives],
            "median_link_lifetime": (round(sorted(lives)[len(lives) // 2], 1)
                                     if lives else None),
            "connected_seconds": round(self.connected_seconds, 1),
        }


class DeviceSession:
    """Owns the link, the frame stream, and recovery from link loss."""

    def __init__(self, simulate: bool = False,
                 interval: float = FRAME_INTERVAL,
                 address: Optional[str] = None) -> None:
        self.simulate = simulate
        self.interval = interval
        self.address = address
        self.stats = SessionStats()
        self._client = None
        self._write_char = None
        self._notify_char = None
        self._raw_notify = None
        # Set only by the real connect path; the simulator never goes through
        # start_notify, so initialise it here or the identity query raises.
        self._notify_ok = False
        # The 1.2.3 battery filter: median over a 10 s window, decreases need
        # two confirmations, rises never published.
        self.battery = BatteryEstimator()
        # The loop this session runs on, captured on connect: the notify
        # callback is synchronous and cannot await the keep-alive write.
        self._loop = None
        # The PressureMonitor fed by _raw_notify. Bound in _bind_notify so
        # the batch counter that sustains the stream is reachable; the bare
        # callback alone would only ever see bytes.
        self._telemetry = None
        # How long /api/arm waits for the first frame before reporting the
        # device as not armed.
        self.arm_grace_seconds = 2.7   # m.java: p(54)=2000 + 700
        self._peer_name = ""
        self._task: Optional[asyncio.Task] = None
        self._target = 0.0
        self._want_percent = 0.0
        self._vibration = 0
        self._want_vibration = 0
        # True while a DEVICE KEY is holding a position. See
        # hold_position(): the pump's idle guard consults it, and every
        # path that ends the position for a reason of its own clears it.
        self._held_by_button = False
        # True while an API CALLER is holding a position. See
        # hold_for_client(): the pump's idle guard consults it too.
        #
        # THIS IS A THIRD KIND OF HOLD, and it is not the same as either
        # of the other two:
        #
        #   * the PAGE's hold is a heartbeat -- the page says "I am still
        #     here" every second, and the hold lasts as long as it does;
        #   * the BUTTON's hold is a key press -- a fact about the DEVICE,
        #     made in person;
        #   * THIS one is a COMMAND from a program. The program sent
        #     "percent 100" and got a 200, and it has no page, so it can
        #     never send a heartbeat. Without a hold of its own the guard
        #     would retract the position CLIENT_GONE_SECONDS (2.5 s)
        #     after the command -- so the caller would get a success
        #     answer and a device that went straight back in.
        #
        # The bug this fixes, measured on the simulator against the real
        # session: `POST /api/v1/stroke {"percent": 100}` answered 200,
        # the device went to 100%, and 2.5 s later it was back at 0 --
        # with the caller having done nothing wrong and the server
        # having said yes.
        #
        # The docstring on the route already promised "Applies
        # immediately and holds until changed". It did not hold. This
        # flag is what makes the promise true.
        self._held_by_client = False
        # Set by the disconnected callback, cleared by the pump when it
        # acts on it. The callback runs on bleak's thread and the pump
        # runs on the event loop, so the fact travels through this flag
        # rather than through a call across the two.
        self._link_lost_at = 0.0
        # Set by set_target() and cleared by the pump. It releases the pump
        # the moment a new target exists, so the write carries the value
        # the generator JUST produced rather than whatever the pump's own
        # phase happened to land on. See set_target() for the measurement
        # that shows why the phase mattered.
        #
        # It is NOT created here. An asyncio.Event binds to the event loop
        # that first AWAITS it, and this session outlives its loop: the
        # session is a module-level object in webapp.py, while each
        # TestClient (and each uvicorn reload) runs its own loop. Measured:
        # an Event created in __init__ and awaited in a second loop raises
        # "RuntimeError: <asyncio.locks.Event object ...> is bound to a
        # different event loop" -- and the failure is SILENT, because it
        # kills the pump TASK while every assertion still passes.
        #
        # So the Event is created per loop, on demand. See
        # _target_event_for().
        self._target_event: Optional[asyncio.Event] = None
        self._target_event_loop = None
        self._running = False
        self._stopping = False
        self._reconnecting = False
        self._link_started_at = 0.0
        # Did the LAST scan see our device?
        #
        # It is the scan's own verdict, recorded where the scan
        # happened, and it is what tells the recovery the difference
        # between "the device refused the connection" and "the device
        # is not there at all". The recovery needs the difference
        # because the two need opposite answers: the first is worth
        # waiting for, the second is not.
        #
        # None means "no scan has run yet", which is neither answer.
        self._scan_saw_device: Optional[bool] = None

    # ------------------------------------------------------------- plumbing

    def _target_event_for(self) -> asyncio.Event:
        """The pump's wake-up event, bound to the CURRENT event loop.

        An asyncio.Event binds to the loop that first awaits it, and this
        session is reused across loops -- the session lives in a module
        global while each TestClient (and each server run) has its own
        loop. Reusing the Event across loops raises
        "RuntimeError: ... is bound to a different event loop", and it
        does so in the PUMP TASK, so the pump dies silently while every
        assertion still passes.

        Measured, with a real session and two loops: reusing the Event
        raised on the second loop; creating it inside each loop did not.
        An asyncio.Lock does NOT have this property (it re-binds), which
        is why webapp.py's module-level ``_connect_lock`` is fine and
        this Event was not.

        So the Event is rebuilt whenever the loop changes. The pending
        flag is carried across, so a target set on the old loop is not
        silently dropped on the new one.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop running (a synchronous caller). Return the existing
            # event, or a fresh one; it will bind when it is awaited.
            if self._target_event is None:
                self._target_event = asyncio.Event()
            return self._target_event
        if self._target_event is None or self._target_event_loop is not loop:
            was_set = self._target_event.is_set() if self._target_event else False
            self._target_event = asyncio.Event()
            if was_set:
                self._target_event.set()
            self._target_event_loop = loop
        return self._target_event

    def set_frame_interval(self, interval: float) -> float:
        """Move the BLE pump to a new write interval. Returns what it set.

        The pump reads ``self.interval`` on every pass, so this takes
        effect on the next frame without restarting the pump.

        The value is clamped rather than refused because the pump is an
        internal mechanism, not an operator-facing number: the operator
        sets a TICK, and the tick is what decides this interval. Clamping
        here means a bug upstream cannot produce a pump that spins at
        0.1 ms and starves the event loop.
        """
        try:
            t = float(interval)
        except (TypeError, ValueError):
            return self.interval
        t = max(MIN_FRAME_INTERVAL, min(MAX_FRAME_INTERVAL, t))
        self.interval = t
        return t

    def set_address(self, address: Optional[str]) -> Optional[str]:
        """Point this session at a different device. Returns the new address.

        ``address`` is the BLE hardware address of the peripheral to
        connect to. ``None`` means "find it by name or service UUID",
        which is the behaviour the session had before this existed.

        Changing the address while a link is up does NOT reconnect: the
        caller decides that, because reconnecting is a state change the
        operator asked for explicitly (by picking a device in the UI), and
        doing it here would tear down a working link as a side effect of
        a setter. The WebUI's connect path disconnects first, then sets
        the address, then connects.
        """
        self.address = (str(address).strip() or None) if address else None
        return self.address

    @property
    def connected(self) -> bool:
        if self.simulate:
            return self._client is not None
        if self._client is None:
            return False
        try:
            return bool(self._client.is_connected)
        except Exception:                             # noqa: BLE001
            return False

    def _should_run(self) -> bool:
        """Should the recovery keep going?

        "No" means a stop has been REQUESTED. It used to read
        ``self._running or not self._stopping``, which is "running, or
        nobody asked to stop" -- and with ``_running=True`` it answered
        True even after disconnect() had set ``_stopping=True``. So the
        reconnect loop's condition did not react to an operator's
        disconnect at all; it only stopped when the task was cancelled, and
        the window between those two is a race the operator cannot win.

        The rule now is the one the name says: a requested stop ends it.
        """
        if self._stopping:
            return False
        return True

    async def connect(self, recovering: bool = False,
                      budget: Optional[float] = None) -> None:
        """Open the link.

        ``recovering`` says the call comes from the RECOVERY path
        (:meth:`_reconnect`) rather than from the operator asking to
        connect. The difference matters for one thing: an operator who
        clicks "disconnect" while a recovery is in flight must stay
        disconnected.

        It used to be impossible to tell the two apart, because connect()
        unconditionally cleared both ``_stopping`` and
        ``operator_disconnected``. An in-flight recovery that succeeded
        therefore undid the operator's disconnect -- the link came back up
        and the page went to 「重连中……」 for a session the operator had
        deliberately closed.

        A recovery therefore touches NEITHER flag, and an operator-initiated
        connect clears both -- which is right, because the operator asking
        to connect again is exactly the intent those flags record.

        ``budget`` is the wall-clock budget for this attempt, in seconds.
        It is what keeps the recovery bounded: without it the scan and the
        connect each took their own hard-coded 25 s, so one attempt could
        outlast the deadline that was supposed to bound it.
        """
        if not recovering:
            self._stopping = False
        # A NEW LINK starts with NO identity of its own.
        #
        # This has to happen BEFORE the identity query below, not after it.
        # The model fields belong to the PREVIOUS device until this link
        # says otherwise, and they must not be inherited: a session that
        # connected a Vero and then a Mira must not keep answering "this
        # device has no vibration" from the Vero's row.
        #
        # None is the honest "we do not know" answer for both capability
        # booleans (the guard allows unknown models through rather than
        # refusing), and "" is the honest "never asked" answer for the
        # product name.
        #
        # link_serial is the per-LINK serial: stats.serial keeps the last
        # serial the session ever saw, because the operator still needs to
        # read which device they had while the new link is being
        # established. But refresh_identity() waits on link_serial, so
        # clearing it here is what makes the second connect() actually WAIT
        # for the new device instead of returning on the previous serial.
        self.stats.link_serial = ""
        self.stats.product = ""
        self.stats.supports_vibration = None
        self.stats.position_sensors = None
        self.stats.firmware = ""
        self.stats.mcu_firmware = ""
        self.stats.rssi = None
        self.stats.mtu = None
        self.stats.power = None
        if self.simulate:
            from .simulator import SimulatedTransport
            self._client = SimulatedTransport()
            await self._client.connect()
            # Same sequence as the real path, so the simulator exercises
            # identical code and any divergence shows up here rather than on
            # hardware: notify subscription, handshake, then the query.
            try:
                await self._client.start_notify(P.CHAR_NOTIFY_PRIMARY,
                                                self._on_notify)
                self._notify_ok = True
            except Exception:                             # noqa: BLE001
                self._notify_ok = False
            await self._post_connect_handshake()
            await self.refresh_identity()
            # Keep the unmistakably-fake serial: a run that reports a real
            # serial number can never be mistaken for a hardware result.
            #
            # It is the simulator's OWN serial, taken from the constant
            # both sides use. The two used to be "sim-device" here and
            # "SIM0000000000" on the simulator, so the session reported a
            # serial in a different FORMAT from the one the simulator's
            # caps frame carries -- and a reader comparing the two, or
            # slicing a prefix off the session's serial the way the page
            # does with a real one, saw two different shapes.
            from .simulator import SIM_SERIAL
            self.stats.serial = SIM_SERIAL
        else:
            await self._connect_ble(budget=budget)
            # One round trip on the way in, so the UI has an identity before
            # the operator touches anything. The handshake comes first: the
            # device ignores a caps query until it has been put into online
            # mode, so querying without it always returns nothing.
            await self._post_connect_handshake()
            # The wait is INSIDE the attempt's budget. _connect_ble
            # already took the identity wait out of the budget it split
            # between the scan and the connect, so this waits for what is
            # left of it -- otherwise the attempt would cost the budget
            # PLUS this wait, and eight attempts would outlast the pass
            # deadline (8 x 62.5 s = 500 s against a 480 s deadline).
            identity_wait = RECONNECT_IDENTITY_WAIT
            if budget is not None:
                identity_wait = min(RECONNECT_IDENTITY_WAIT,
                                    max(0.0, budget / 4.0))
            await self.refresh_identity(wait=identity_wait)

        self.stats.connected_at = time.monotonic()
        # Seed the client-present timestamp. Without this, a session that is
        # connected but has not yet been given a target computes
        # (monotonic_now - 0.0) as its idle age, which exceeds
        # CLIENT_GONE_SECONDS immediately and parks the send loop before it
        # ever writes a frame. Latent on the Linux build because something
        # there always called set_target first.
        self.stats.last_target_at = time.monotonic()
        # The link is up, so the outage window closes and the client clock
        # restarts. A reconnect is NOT the operator leaving -- the page is
        # still open and still sending heartbeats -- so the clock starts
        # again from now rather than staying at whatever the previous link
        # left behind.
        self._note_link_up()
        self.stats.last_error = ""
        self.stats.frames_sent = 0
        self.stats.write_errors = 0
        self.stats.consecutive_failures = 0
        self.stats.link_ok = True
        # --- per-link state. Everything below is a property of ONE link and
        # must start at zero when a new link comes up, because both arm gates
        # read these: _arm_soon's gate is `stream_commands == 0` (webapp.py)
        # and /api/arm's is `stream_commands > 0`. Neither was cleared here,
        # so after a reconnect the new link inherited the old link's command
        # count, both gates read it as "already commanded", and the link went
        # up with no telemetry command at all. Measured on the simulator
        # through the real _reconnect(): link 1 got 2 telemetry frames on the
        # wire, link 2 got 0 and its trace never grew, while /api/arm reported
        # "already commanded" -- a dead link behind a reassuring answer.
        #
        # `reconnects`, `link_lifetimes`, `recovery_seconds`, `connected_seconds`
        # and `gatt_refreshes` are deliberately NOT here: they are session
        # totals (their own docstrings say "cumulative"/"how long each link",
        # and index.html's card reads the totals across links).
        self.stats.stream_commands = 0
        self.stats.stream_keepalives = 0
        self.stats.sensors_armed = False
        self.stats.telemetry_frames_at_arm = 0
        self.stats.reassert_frames = 0
        # connect() is documented as clearing this one (its docstring: "until
        # a write succeeds or connect() runs"), and it did not.
        self.stats.link_gave_up = False
        # A NEW connection clears the operator's own disconnect -- but only
        # when the operator asked for the connection. A recovery that comes
        # up on its own must NOT resurrect a session the operator closed.
        if not recovering:
            # The operator is back, and the state they left behind is
            # history.
            self.stats.operator_disconnected = False
            # And so is a CALLER's hold. The operator asked for a link --
            # possibly to a DIFFERENT device -- so the caller's standing
            # instruction does not carry over to it.
            #
            # A RECOVERY does not clear it: the recovery is the same link
            # coming back, the caller's command is still in force, and
            # the position is restored from ``_want_percent`` below. So
            # the caller's 100% survives a BLE blip, and does NOT survive
            # the operator reconnecting.
            self._held_by_client = False
        # The one-shot follow-up latch lives on the monitor, not on the stats,
        # and it is per-link too: monitor.py's arm() clears it and
        # confirm_stream() sets it, so a link that already confirmed would
        # never owe its own follow-up. This firmware pushes a burst of 7 and
        # then waits (telemetry_regression.py), so an uncleared latch means
        # the new link goes quiet after its first burst. Measured: clearing
        # only the stats left link 2 with 1 wire frame and keepalives=0 where
        # link 1 had 2 and 1; clearing the latch as well makes link 2 match.
        if self._telemetry is not None:
            self._telemetry.stream_confirmed = False
            self._telemetry.since_keepalive = 0
            self._telemetry.armed = False
        self._link_started_at = time.monotonic()
        self.stats.link_lifetimes.append(0.0)      # placeholder, filled on drop
        self._start_loop()

    async def _connect_ble(self, budget: Optional[float] = None) -> None:
        from bleak import BleakClient

        # The budget for this attempt. It is split between the scan and the
        # connect, because those are the two things that can each hang on a
        # device that is advertising but will not accept a connection.
        #
        # Both used to be a hard-coded 25.0 s, which meant one attempt could
        # take 25 + 25 + 2.5 = 52.5 s -- far longer than the 6 s
        # RECONNECT_DEADLINE that was supposed to bound it. The deadline was
        # checked only BETWEEN attempts, so it expired inside the first one
        # and never had any effect; the pass was really bounded by
        # RECONNECT_MAX_ATTEMPTS, i.e. seven minutes of a wedged pump.
        if budget is None:
            budget = RECONNECT_DEADLINE
        # THE IDENTITY WAIT IS INSIDE THE BUDGET.
        #
        # The attempt costs three things: the scan, the connect, and the
        # identity wait. The identity wait was a CONSTANT outside the
        # budget, so an attempt could take budget + 2.5 s -- and with
        # eight attempts of that, the pass ran PAST its own deadline:
        #
        #   8 x (30 s scan + 30 s connect + 2.5 s identity) = 500 s
        #   the pass deadline = 60 x 8                    = 480 s
        #
        # So the give-up branch was unreachable for a device that is
        # simply OFF -- the case the recovery exists for -- and the pass
        # ended by running out of time instead of by running out of
        # attempts. The fix at the time lengthened the pass from 53 s to
        # 480 s, which is the opposite of what was wanted.
        #
        # So the budget is what is left AFTER the identity wait, and the
        # scan and the connect split that. The attempt now costs at most
        # the budget.
        identity_wait = min(RECONNECT_IDENTITY_WAIT, budget / 4.0)
        usable = max(1.0, budget - identity_wait)
        scan_budget = max(1.0, usable / 2.0)
        connect_budget = max(1.0, usable - scan_budget)

        dev = await find_device(
            address=self.address,
            names=DEVICE_NAME_PREFIXES,
            services=DEVICE_SERVICE_UUIDS,
            timeout=scan_budget)
        if dev is None and self.address:
            # THE SCAN FOUND NOTHING, BUT WE KNOW THE ADDRESS.
            #
            # This is the WINDOWS RECONNECT CASE, and it is why the two
            # surfaces used to disagree: the operator disconnected, then
            # connected again, and WINDOWS' OWN BLUETOOTH UI showed the
            # device as connected while this page showed 未连接.
            #
            # WHAT HAPPENS ON WINDOWS. The OS holds a GATT link of its
            # own, so the device is CONNECTED AT THE OS LEVEL and does
            # not advertise. An advertising scan cannot see a device
            # that is not advertising -- so the scan finds nothing, the
            # connect fails, and the page says "not connected" while the
            # machine's own Bluetooth panel says "connected". Both are
            # telling the truth about DIFFERENT things.
            #
            # THE FALLBACK. `find_device_by_address` asks the STACK for
            # a known address rather than for an advertisement. On
            # Windows that goes through WinRT's device cache, which
            # knows a device the OS holds a link to -- so it can answer
            # for a device that is not advertising. On Linux it is the
            # same advertising scan by address, so it costs one more
            # scan and finds nothing more; it is harmless there.
            #
            # IT IS ONLY TRIED WHEN WE KNOW THE ADDRESS. Without one,
            # there is nothing to ask for, and the "device not found by
            # name prefix" message -- which tells the operator the
            # useful thing (close the vendor app) -- is the right
            # answer.
            dev = await find_device_by_address(self.address,
                                               timeout=scan_budget)
            if dev is not None:
                # THE SCAN DID NOT SEE IT, BUT THE STACK DID. So the
                # device is there and quiet -- the OS holds a link, or
                # it is between advertisements. This is not a missing
                # device, and the recovery must treat it as present.
                self._scan_saw_device = True
        if dev is None:
            # THE DEVICE WAS NOT FOUND. That is the scan's own verdict,
            # and it is recorded HERE, where the scan happened.
            #
            # It is recorded here and not read later out of
            # `discovery.last_scan()` because that list is only written
            # on the not-found path -- so a connect that failed AFTER
            # the device was found would leave the list holding
            # whatever the PREVIOUS failed scan saw. Reading it then
            # would answer a question about the wrong scan.
            #
            # The verdict is a BOOLEAN and it means exactly one thing:
            # "this scan did not see our device". Whether that means
            # "the device is off" or "the device is out of range" is
            # the recovery's business, not the scan's.
            self._scan_saw_device = False
            seen = last_scan()
            detail = ("\n  saw while scanning: %s" % "; ".join(seen[:6])
                      if seen else "\n  saw nothing advertising at all")
            if self.address:
                raise RuntimeError(
                    "%s is not advertising.%s" % (self.address, detail))
            # The message names the PREFIXES, never a unit's own serial.
            # A prefix is a product-line constant (it is in the vendor app
            # too), so this says nothing about anybody's particular device.
            raise RuntimeError(
                "device not found by name prefix (%s) or service UUID.%s\n"
                "  If the device is powered on, the usual cause is that a "
                "phone is still connected to it:\n"
                "  BLE permits exactly one central at a time, so close the "
                "vendor app completely and retry."
                % (", ".join(DEVICE_NAME_PREFIXES), detail))

        # THE DISCONNECTED CALLBACK.
        #
        # Without it, the host only learns the link is gone when a WRITE
        # fails -- and a device that is powered off while the position is
        # at 0% never has a write sent, so it never fails, so the link
        # stays "up" in the UI for as long as the reconnect pass takes to
        # give up. This was seen in the field: the power button was
        # long-pressed and the page kept saying 已连接.
        #
        # The callback fires the moment the stack notices the
        # disconnect, which is what the operator is waiting for. It runs
        # on bleak's own thread, so it must not block and it must not
        # touch the event loop directly: it records the fact and the
        # pump's next pass acts on it.
        #
        # The callback belongs on the CLIENT, so it is registered here,
        # where the client is built -- not in the route and not in the
        # pump, because those are the places that react to a lost link
        # and none of them is where the link's loss is observed.
        c = BleakClient(dev, timeout=connect_budget,
                        disconnected_callback=self._on_disconnected)
        # THE SCAN DID SEE THE DEVICE. So a failure from here on is a
        # REFUSED connection, not a missing device -- and the recovery
        # has to keep trying, because the device is right there.
        self._scan_saw_device = True
        await c.connect()
        self._client = c
        self._peer_name = getattr(dev, "name", None) or dev.address

        self._write_char = P.CHAR_WRITE_PRIMARY
        self._notify_char = P.CHAR_NOTIFY_PRIMARY
        await self._subscribe_notify(c)

    async def _subscribe_notify(self, c) -> None:
        """Listen on BOTH of the device's notify characteristics.

        THE DEVICE HAS TWO OF THEM, AND THE APP LISTENS TO BOTH.

        The GATT layout, measured on real hardware
        (analysis/FIRMWARE-REPORT.md, section three)::

            service 6e400001-...   NUS compatible
              6e400002  [write]      the main TX
              6e400003  [notify]
            service 6f430001-...   the vendor's own
              6f430002  [write]
              6f430003  [notify]

        The app binds both, in the same file:

            BleManagerHandler$4.java:570  r1Var2.j      = <6e400003>  (f6086c)
            BleManagerHandler$4.java:601  r1Var5.f6368l = <6f430003> (f6089f)

        and the project's own capture rig does the same
        (analysis/scripts/hw_retest.py:55 subscribes to both).

        This project subscribed to ONE. A frame the device sent on the
        other characteristic never arrived at all -- it was never seen,
        never counted, never decoded. So the device's own button presses
        went unseen, and the panel said "0 buttons" for a reason that
        had nothing to do with the buttons: the frame was on a
        characteristic nobody was listening to.

        The two are INDEPENDENT. Notifications are best-effort: this
        device refuses the Enhanced ATT setup on some stacks and
        start_notify raises UNLIKELY_ERROR there. So one failing must
        not stop the other from being tried -- the primary carries the
        telemetry and the secondary may carry the button events, and
        losing one is not a reason to lose the other.

        The WRITE characteristic stays the primary one: the app sends
        its control frames to the primary TX.
        """
        # THE SECOND NOTIFY CHARACTERISTIC.
        self._notify_chars: List[str] = []
        self._notify_ok = False
        for _char in (P.CHAR_NOTIFY_PRIMARY, P.CHAR_NOTIFY_SECONDARY):
            try:
                await c.start_notify(_char, self._on_notify)
                self._notify_chars.append(_char)
                self._notify_ok = True
            except Exception as exc:                     # noqa: BLE001
                # The failure is recorded but the OTHER characteristic is
                # still tried: the two are independent, and a device that
                # refuses one of them still uses the other.
                self.stats.last_error = "notify: %s" % type(exc).__name__
        # The characteristic the project WRITES to is the primary one.
        # The secondary is notify-only for the device's own traffic.
        self._notify_char = P.CHAR_NOTIFY_PRIMARY

    def _on_notify(self, _sender, data) -> None:
        raw = bytes(data)
        self._ingest_caps(raw)
        if self._raw_notify is not None:
            try:
                self._raw_notify(raw)
            except Exception:                         # noqa: BLE001
                pass
        # The device delivers a burst and then waits. Ask for the next one once
        # a batch has accumulated, exactly as the App does, or the trace stops
        # after a handful of frames and never records anything again.
        self._maybe_keepalive_stream()

    def _maybe_keepalive_stream(self) -> None:
        """Send the single follow-up command 52 once the first burst arrives.

        Not a repeating keep-alive: the App sends 52 at the end of the connect
        handshake and once more when its buffer hits 7 frames, then sets
        f6353a0 and stops asking. Re-sending every batch resets the device's
        stream at 20 Hz and yields no data at all.
        """
        if self._telemetry is None or not self._telemetry.batch_due():
            return
        self._telemetry.confirm_stream()
        self.stats.stream_keepalives += 1
        if self._loop is None or self._loop.is_closed():
            return
        # The notify callback is synchronous; the write is not. Schedule it on
        # the loop the session already runs on.
        try:
            self._loop.create_task(self._send_stream_followup())
        except RuntimeError:
            pass

    async def _send_stream_followup(self) -> None:
        if not self.connected or self._client is None:
            return
        try:
            await self._write(P.app_set_mode(CMD_TELEMETRY_STREAM))
        except Exception:                             # noqa: BLE001
            # Every failure here is counted by _write() -- including the
            # "no link" case, which used to be raised before _write()'s
            # counting block and therefore escaped uncounted while this
            # comment claimed otherwise. The counter is single-sourced in
            # _write() for that reason.
            pass

    def _identify_model(self, serial: str) -> None:
        """Fill the three model fields from a serial, and nothing else.

        One place decides "which device is this", so the caps path and any
        future caller cannot derive it two different ways.

        An unrecognised prefix is NOT an error and NOT a Vero. It leaves the
        model as "Unknown" and both booleans as None, which the request guard
        treats as "we do not know" and therefore allows. Hardcoding a known
        model here would make the simulator claim to be hardware it is not, and
        would let a real device with an unlisted prefix inherit another
        model's capabilities.
        """
        self.stats.product = P.detect_product(serial)
        self.stats.supports_vibration = P.profile_capability(
            serial, "has_vibration")
        self.stats.position_sensors = P.profile_capability(
            serial, "has_position_sensors")

    def require_vibration_allowed(self, vibration: int) -> None:
        """Refuse a vibration command a model does not have.

        REFUSE, do not silently clamp to zero. The vendor app does not clamp
        either -- it hides the control entirely (Vero's screen calls
        setVibrationControlsVisible(false), DeviceControlVexActivity.java:669).
        Clamping would leave the caller believing a vibration happened, which
        is the failure this guard exists to prevent.

        ``None`` (model unrecognised) is allowed through, and so is any level
        of 0. Both are deliberate: 0 is "no vibration", which every model can
        accept, and None means the serial told us nothing -- refusing on that
        would break every device whose prefix is missing from the table.

        Raises ValueError with a message naming the model, which the HTTP
        layers turn into 422 -- the same status this codebase already uses for
        a validation failure (see /api/auto/params' max<min rejection). It is
        not a 409: nothing about the connection state conflicts.
        """
        if self.stats.supports_vibration is False and int(vibration) > 0:
            raise ValueError(
                "%s has no vibration capability; the vendor app hides the "
                "control for this model (DeviceResolver.java:24). Send "
                "vibration=0." % (self.stats.product or "this device"))

    def _ingest_caps(self, raw: bytes) -> None:
        """Pull identity fields out of a notification frame.

        This device does NOT answer a caps query on the write-response
        channel -- it pushes the resulting JSON on the notify channel a few
        hundred milliseconds later. Waiting synchronously for a write reply
        therefore always times out, which is why identity fields stayed
        empty until this path existed.

        Simulation is ignored here: the simulator answers through this same
        callback, and letting it through would overwrite the unmistakable
        sim-device marker set in connect().
        """
        if self.simulate:
            return
        if not raw or raw[0:2] != b"\xa5\x5a":
            return
        try:
            parsed = P.parse_framed_json(raw)
        except Exception:                             # noqa: BLE001
            return
        if not parsed:
            return
        # The envelope's CRC is checked BEFORE anything in it is believed.
        #
        # parse_framed_json returns the payload even when the CRC does not
        # match -- it reports the mismatch in the meta rather than
        # refusing the frame -- so a single flipped bit in the JSON body
        # produced a well-formed document with a corrupted serial. That
        # serial then went through _identify_model(), which looked up the
        # prefix table: a corrupted prefix reads as "Unknown", and Unknown
        # means the capability booleans are None, which means
        # require_vibration_allowed() ALLOWS a vibration command on a model
        # that has none. The guard flipped from "refuse" to "allow" on a
        # radio error.
        #
        # So the CRC is enforced here, at the one place the device's
        # identity enters the session. A frame whose CRC does not verify
        # carries no identity.
        meta = parsed[1] if isinstance(parsed, tuple) else {}
        if not meta.get("crc_ok", False):
            self.stats.identity_error = (
                "the device's identity frame failed its CRC check; the "
                "identity was not trusted")
            return
        doc = parsed[0] if isinstance(parsed, tuple) else parsed
        if not isinstance(doc, dict):
            return
        dev = doc.get("dev") or {}
        ble = doc.get("ble") or {}
        if not isinstance(dev, dict):
            dev = {}
        if not isinstance(ble, dict):
            ble = {}
        if dev.get("sn"):
            self.stats.serial = str(dev["sn"])
            # The serial of THIS link, which is what refresh_identity() waits
            # for. stats.serial keeps the last serial ever seen (the operator
            # still needs to read it), so it cannot be the wait condition --
            # see SessionStats.link_serial.
            self.stats.link_serial = str(dev["sn"])
            self.stats.identity_error = ""
            self._identify_model(str(dev["sn"]))
        if dev.get("ble_1_fw"):
            self.stats.firmware = str(dev["ble_1_fw"])
        mcu1, mcu2 = dev.get("mcu1_fw"), dev.get("mcu2_fw")
        if mcu1 is not None or mcu2 is not None:
            self.stats.mcu_firmware = "%s/%s" % (
                "" if mcu1 is None else mcu1, "" if mcu2 is None else mcu2)
        # THE TWO REVISIONS ARE ALSO KEPT APART, because the joined
        # string above cannot say WHICH of the two answered. A device
        # that reports only mcu1 gives "200/", and one that reports only
        # mcu2 gives "/200" -- two different facts that the joined
        # string shows as the same shape.
        #
        # They are read as INTEGERS, and a value that is not one is left
        # at None rather than stored as a string: the device reports a
        # revision NUMBER, and a caller that gets "200" as a string has
        # to parse it again.
        self.stats.mcu1_fw = _fw_revision(mcu1)
        self.stats.mcu2_fw = _fw_revision(mcu2)
        p = dev.get("power")
        if p is not None:
            try:
                self.stats.power = float(p)
                # Feed the estimator, not just the raw value: the app displays
                # a filtered reading (median over 10 s, decreases confirmed
                # twice, rises suppressed), and a gauge that flickers with
                # every frame is worse than useless for judging a charge.
                self.battery.feed(self.stats.power)
                self.battery.evaluate()
                self.stats.battery_percent_shown = self.battery.percent
            except (TypeError, ValueError):
                pass
        if ble.get("RSSI") is not None:
            try:
                self.stats.rssi = int(ble["RSSI"])
            except (TypeError, ValueError):
                pass
        # Exactly one key in this document ends in a colon: "MTU:". Reading
        # the bare "MTU" therefore never matched a real device, stats.mtu
        # stayed None and the WebUI MTU card rendered a permanent em dash,
        # while the simulator -- which had invented a colon-less key -- was
        # the only path that could fill it. parse_device_info() already read
        # "MTU:", so this aligns the session with it rather than inventing a
        # second convention.
        #
        # The firmware keys are "MTU:" but ble_1_fw / mcu1_fw / mcu2_fw --
        # no trailing colon, exactly as emitted. Verified against a real
        # capture, analysis/evidence/hw_retest.json log[0] reply[0]:
        #   dev -> ['ble_1_fw', 'mcu1_fw', 'mcu2_fw', 'power', 'sn']
        #   ble -> ['MTU:', 'RSSI']
        # PROTOCOL_V13.md used to claim all four carry a colon. It does not:
        # the firmware string table stores `"ble_1_fw"\t:\t`, where the colon
        # sits *outside* the quotes as the JSON key/value separator. Only
        # "MTU:" has a colon inside the quotes (stored as `MTU:"`). Do not
        # "fix" dev.get("ble_1_fw") to "ble_1_fw:" -- that would break a
        # field that is currently correct.
        if ble.get("MTU:") is not None:
            try:
                self.stats.mtu = int(ble["MTU:"])
            except (TypeError, ValueError):
                pass

    async def _post_connect_handshake(self) -> None:
        """Wake the device before asking it anything.

        The vendor app does not query straight after connecting. It first
        sends a zero-position control frame, then switches the device into
        online mode. A bare caps query sent before that is ignored, and the
        device answers nothing at all -- which looks exactly like a dead
        link. Sequence reconstructed from the app's POST_CONNECT path:

            C0() -> n0(0) zero the position -> H0(0,0) -> K0(52)
    """
        try:
            await self._write(P.app_a0_control(0), response=False)
            await asyncio.sleep(0.08)
            await self._write(P.app_set_mode(P.POST_CONNECT_MODE),
                              response=False)
            await asyncio.sleep(0.12)
        except Exception as exc:                             # noqa: BLE001
            # Non-fatal: the device may still answer a caps query anyway.
            self.stats.last_error = "handshake write failed: %s" % exc

    async def refresh_identity(self, wait: float = 2.5) -> None:
        """Send the caps query and wait for the device to answer by notify.

        The device answers on the notification channel, not on the
        write-response channel, so this deliberately does NOT wait for a
        write reply -- doing so always times out and reports a false failure.
        Instead it sends the query and gives the pushed frame time to land,
        which is handled by ``_ingest_caps``.

        Every failure mode is recorded in ``stats.identity_error`` rather
        than swallowed. A silent empty result is indistinguishable from a
        device that genuinely has nothing to report.
        """
        self.stats.identity_error = ""
        if self.simulate:
            # No BLE link here, so fill the same fields from the simulator
            # directly. The shape a caller sees must not depend on which
            # path produced it.
            from .simulator import SimulatedDevice
            d = SimulatedDevice()
            self.stats.firmware = d.firmware
            self.stats.power = d.power
            self.stats.rssi = d.rssi
            self.stats.mtu = d.mtu
            # Identify from the simulator's OWN serial (SIM0000000000), which
            # matches no product prefix, so the model reads "Unknown" and both
            # capability booleans stay None. That is the honest answer and it
            # is also the safe one: the guard then allows vibration through,
            # so nobody can claim the simulator refused a Mira-only command,
            # and nothing about a simulated run can be read as a statement
            # about a real device. Naming it Vero here would be worse than
            # useless -- it would make a simulated run refuse vibration and
            # look like the product rule was exercised.
            self._identify_model(d.serial)
            # The simulator answers its own identity query, so this link's
            # identity IS in hand. link_serial is what refresh_identity()
            # waits on, and it is per LINK, so it is set here too -- the
            # simulate path must satisfy the same contract as the real one,
            # or the wait below would always time out.
            self.stats.link_serial = d.serial
            return
        try:
            # No response=True: see the docstring.
            await self._write(P.app_caps_query(), response=False)
        except Exception as exc:                             # noqa: BLE001
            self.stats.identity_error = "caps query failed to send: %s: %s" % (
                type(exc).__name__, exc)
            return
        if not self._notify_ok:
            self.stats.identity_error = (
                "notifications are not enabled on this stack, so the device "
                "cannot report its identity")
            return
        # Wait for the pushed answer, refreshing as frames arrive.
        #
        # The wait is keyed on link_serial, NOT on stats.serial: stats.serial
        # is the LAST serial this session ever saw, and it is deliberately
        # kept across a reconnect so the operator can still read which
        # device they had. Keying the wait on it made the SECOND connect()
        # return on its very first check -- before the new device had
        # answered anything -- so the new device was never identified and
        # the model/firmware/battery/RSSI/MTU all stayed at the previous
        # device's values with no error to show for it.
        #
        # link_serial is per LINK: connect() clears it, and only the caps
        # frame of THIS link fills it in.
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self.stats.link_serial:
                return
            await asyncio.sleep(0.05)
        if not self.stats.link_serial:
            self.stats.identity_error = (
                "no identity arrived within %.1f s of the caps query" % wait)

    async def arm_sensors(self) -> bool:
        """Command 52 starts (and each re-issue sustains) the 0x29 stream.

        The App builds this frame as ``k("B0", 8, ["3D", "52"])`` -- the "52"
        is a hex string, so the byte on the wire is 0x52. It then re-sends the
        same command every 7 frames, because the device pushes a burst and
        waits.

        This used to send ``app_set_mode(0x36)``, which formats the decimal
        number 0x36 as "54" and puts 0x54 on the wire: a different mode. The
        device answered briefly and then went quiet, so the panel froze after
        a few frames and never recorded anything again.
        """
        self._loop = asyncio.get_running_loop()
        # The A-generation telemetry command is 0x36 on the wire (0x54 as a
        # byte), and the connect handshake does NOT send it: j.java:78 sends
        # 0x34, which is the motion mode. r0.java:71-79 is the only A-gen
        # telemetry start, and this endpoint is where it belongs.
        #
        # It must be sent exactly once per link. A second identical mode
        # command interrupts the stream the first one started.
        if self._notify_ok and self._client is not None:
            try:
                await self._write(P.app_set_mode(CMD_TELEMETRY_STREAM))
                self.stats.stream_commands += 1
            except Exception:                         # noqa: BLE001
                self.stats.last_error = "telemetry command failed"
                return False
        if self._telemetry is not None:
            armed = self._telemetry.armed
            frames = self._telemetry.frames_seen
        else:
            armed = False
            frames = 0
        # m.java:45 waits r1.F(0, p(54)) + 700 ms after T0(54) before the
        # next step, and p(54) is 2000 for this device -- so the app allows
        # 2.7 s. The 0.5 s this used to wait was short enough to report
        # "not armed" on a link that was working.
        if not armed:
            await asyncio.sleep(self.arm_grace_seconds)
            if self._telemetry is not None:
                armed = self._telemetry.armed
                frames = self._telemetry.frames_seen
        self.stats.sensors_armed = armed
        self.stats.telemetry_frames_at_arm = frames
        # Report what was observed, not that the check ran. An earlier version
        # returned True unconditionally, which made the UI claim 已激活 while
        # the device was sending nothing at all.
        return armed

    async def disconnect(self) -> None:
        self._stopping = True
        self._running = False
        # A DISCONNECT ENDS EVERYTHING, including a button-driven hold.
        # The hold outlives the link otherwise: the flag is on the
        # session, the session outlives the link, and a hold that
        # survived a disconnect would keep the guard quiet on the NEXT
        # link, which is a different device or a different operator.
        self._held_by_button = False
        # And the CALLER's hold ends here for the same reason: a
        # caller's position belongs to the link it was commanded on. On
        # the next link the device may be a different one, and a hold
        # that survived would keep the guard quiet on it.
        self._held_by_client = False
        # THE OPERATOR ENDED THIS. Nothing is being retried, and the page
        # must say "未连接" rather than "重连中……".
        #
        # The flag is set HERE, in the session, rather than in the route:
        # every way of ending a session -- the WebUI's disconnect button,
        # the CLI's disconnect, the public API's -- goes through this
        # method, so one place decides what "the operator stopped it"
        # means.
        self.stats.operator_disconnected = True
        self.stats.link_gave_up = False
        if self._task is not None:
            # A task belongs to the loop it was created on, and this
            # session outlives its loop: it is a module-level object in
            # webapp.py, while every TestClient and every server run has
            # its own loop. Cancelling a task that belongs to an ALREADY
            # CLOSED loop raises "RuntimeError: Event loop is closed" --
            # Task.cancel() schedules a callback on the task's own loop,
            # and that loop is gone.
            #
            # Measured: a second connect in a new loop followed by a
            # disconnect raised exactly that, and the pump task was left
            # pending ("Task was destroyed but it is pending!").
            #
            # So the task is only cancelled when its loop is still open.
            # A task on a closed loop is already dead -- the loop's
            # shutdown destroyed it -- so there is nothing to cancel, and
            # the reference is simply dropped.
            _task_loop = None
            try:
                _task_loop = self._task.get_loop()
            except Exception:                          # noqa: BLE001
                _task_loop = None
            try:
                _this_loop = asyncio.get_running_loop()
            except RuntimeError:
                _this_loop = None
            if (_task_loop is not None and _task_loop.is_closed()):
                self._task = None
            elif (_this_loop is not None and _task_loop is not None
                  and _task_loop is not _this_loop):
                # The task belongs to a DIFFERENT live loop. Cancelling it
                # from here would schedule the cancellation on that other
                # loop, which is not being driven by this call. Drop the
                # reference and let that loop's own shutdown deal with
                # it.
                self._task = None
            else:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    # The task was cancelled, which is exactly what this
                    # code asked for. It is SWALLOWED on purpose.
                    #
                    # The audit claimed the tuple form
                    # `except (asyncio.CancelledError, Exception)` was
                    # equivalent to `except Exception` because
                    # CancelledError is a BaseException in 3.8+. That is
                    # wrong about the tuple: an except clause listing a
                    # class catches it whatever it inherits from, so the
                    # tuple DID catch CancelledError -- it caught strictly
                    # MORE than `except Exception` would. Re-raising here
                    # instead aborts the whole disconnect(), which is why
                    # the two clauses are spelled out rather than left
                    # implicit in a tuple.
                    pass
                except Exception:                          # noqa: BLE001
                    pass
                self._task = None
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:                         # noqa: BLE001
                pass
        self._client = None
        self._write_char = None
        self.stats.link_ok = False
        self.stats.pump_alive = False
        # An explicit disconnect is the operator leaving (or the app closing),
        # not a link outage, so the outage clock must not be started here.
        self.stats.link_down_at = 0.0
        # The battery filter holds a 10 s window of voltages. Left across a
        # reconnect it would mix two sessions and publish a median belonging
        # to neither.
        self.battery.reset()
        self.stats.battery_percent_shown = None
        # THE ERROR IS CLEARED HERE, and it must be.
        #
        # Why would the page still show a message after the device is off
        # AND the operator has clicked disconnect?
        #
        # Measured, on the running service: the message was a LEFTOVER.
        # The sequence is:
        #   * the device is powered off, so the next write fails;
        #   * the send loop catches the failure and calls _reconnect(),
        #     which calls connect(), which raises "device not found ...";
        #   * that exception text lands in `last_error`;
        #   * the operator then clicks \u65ad\u5f00, which stops the loop --
        #     but nothing cleared `last_error`.
        # So the page kept showing a message about a link the operator had
        # deliberately closed, and it named a device the operator was no
        # longer connected to.
        #
        # An explicit disconnect is a CLEAN state: there is no error, the
        # operator asked for this. Leaving a stale failure on screen is
        # the page disagreeing with its own state -- the header says
        # \u672a\u8fde\u63a5 and the warning box says a device is missing.
        self.stats.last_error = ""
        self.stats.consecutive_failures = 0

    @property
    def raw_notify(self):
        """The sink for decoded notifications (a PressureMonitor.feed)."""
        return self._raw_notify

    @raw_notify.setter
    def raw_notify(self, fn):
        # Captured alongside the callback: sustaining the telemetry stream
        # needs the monitor's batch counter, and the bound method alone does
        # not expose it. Assigning one without the other silently disables
        # the keep-alive and the trace stops after the first burst.
        self._raw_notify = fn
        self._telemetry = getattr(fn, "__self__", None)
        if not hasattr(self._telemetry, "batch_due"):
            self._telemetry = None

    async def _write(self, frame: bytes, response: bool = False):
        """Write one frame. Returns the device's reply when response=True.

        The simulator implements the same write_gatt_char signature as a bleak
        client on purpose, so the real path and the simulated path cannot drift.
        """
        try:
            # "no link" is a WRITE FAILURE, so it is raised INSIDE the
            # counted block rather than before it.
            #
            # It used to be raised before, which meant the failure counter
            # never saw it: a caller that swallowed the exception (there
            # was one -- the stream keep-alive) also swallowed an uncounted
            # failure, so a link that was already gone could keep missing
            # its keep-alive without ever tripping the limit that is
            # supposed to end the recovery. The counters are
            # single-sourced in this method for exactly that reason.
            if self._client is None:
                raise RuntimeError("no link")
            if self.simulate:
                reply = await self._client.write_gatt_char(None, frame,
                                                           response=response)
            else:
                reply = await self._client.write_gatt_char(
                    self._write_char, frame, response=response)
        except Exception:
            # Count the failure here rather than at each of the ~10 call sites,
            # and surface LINK_FAILURE_LIMIT: a device that refuses this many
            # writes in a row is gone, and recovery should stop pretending
            # otherwise instead of retrying until the wall clock runs out.
            self.stats.write_errors += 1
            self.stats.consecutive_failures += 1
            # A failed write IS a link outage. Start the outage clock here,
            # because this is the first moment the host knows the link is
            # gone -- and the pump's idle guard needs that clock to tell a
            # BLE outage apart from an operator who walked away.
            self._note_link_down()
            if self.stats.consecutive_failures >= LINK_FAILURE_LIMIT:
                self.stats.link_ok = False
                self.stats.link_gave_up = True
                self.stats.last_error = (
                    "link lost after %d consecutive failed writes; "
                    "reconnect required"
                    % self.stats.consecutive_failures)
            raise
        self.stats.consecutive_failures = 0
        if not self.stats.link_ok:
            # A successful write proves the link is up, and it CLEARS the
            # give-up flag -- so the page stops saying 「重连中……」 and
            # starts saying the link is up.
            #
            # The recovery was guarded by `and not self.simulate`, so under
            # the simulator the flag was never cleared: a simulated session
            # that had given up stayed "gave up" for ever, even though the
            # next write worked. That is a simulator that cannot reproduce
            # the real link's state transitions, so any behaviour verified
            # against it was verified against a state the hardware never
            # reaches.
            #
            # The guard was there because the simulator's writes always
            # succeed, so under simulation the flag could only be cleared by
            # a write that had actually failed first. That is still true:
            # the clearing is inside the "a write succeeded" branch, so it
            # can only happen after a failure. The simulate exclusion was
            # redundant, not protective.
            self.stats.link_ok = True
            self.stats.link_gave_up = False
            self.stats.last_error = ""
        return reply

    # -------------------------------------------------------------- commands

    def set_target(self, percent: float, vibration: int = 0) -> dict:
        pct = max(0.0, min(100.0, float(percent)))
        self._target = pct
        self._vibration = max(0, min(255, int(vibration)))
        self._want_percent = pct
        self._want_vibration = self._vibration
        self.stats.target_percent = pct
        self.stats.vibration = self._vibration
        self.stats.last_target_at = time.monotonic()
        # An operator command is also proof that the operator is there. It
        # does not go through touch(), because a command can arrive over the
        # public API (/api/v1/stroke) with no page open at all -- and that
        # caller is exactly as present as a browser.
        self.stats.last_client_at = time.monotonic()
        # A COMMAND SUPERSEDES A CALLER'S HOLD.
        #
        # The caller's hold means "do not auto-retract this position,
        # because I have no heartbeat to send". A command from any OTHER
        # source is a newer fact, and it comes with its own lifecycle:
        #
        #   * the PAGE's command is governed by the page's heartbeat, so
        #     the page's safety net applies -- a tab that goes away
        #     retracts in 2.5 s;
        #   * the SWEEP's and the GAIT's commands are governed by the
        #     sweep's own tick, and they clear the hold themselves;
        #   * the CLI's command is governed by the CLI's own life -- it
        #     releases when it is done.
        #
        # So the hold is cleared here, in the ONE writer, rather than at
        # each call site -- because a call site that forgets is exactly
        # how the caller's hold ended up keeping the PAGE's position out
        # after the tab closed. Measured before this: an agent commanded
        # 100%, the page moved the travel to 30%, the tab closed, and the
        # position stayed out -- the caller's hold was still in force and
        # the page's safety net never got to run.
        #
        # The API's own stroke route claims the hold AGAIN, right after
        # this call, so it is unaffected: the order is command, then
        # claim, and the claim is the newer fact.
        #
        # The BUTTON's hold is NOT cleared here: a key press is a fact
        # about the device, and the button's own path claims it after
        # this call too.
        self._held_by_client = False
        # Release the pump.
        #
        # The pump used to read this VARIABLE on its own schedule, and
        # that is where the second half of the stutter came from. The
        # generator and the pump each keep an absolute schedule, but they
        # start at different moments, so their phases differ by a fixed
        # offset. A pump that samples at that offset sometimes lands on
        # the value the generator produced TWO ticks ago, and the device
        # is then asked to move by the distance of two ticks in one
        # command.
        #
        # Measured, with the real generator: at a 15 ms tick the device's
        # worst jump was 9.36 % of the stroke against the generator's own
        # 4.69 % -- exactly double -- and the samples carried a stale
        # value in 0 cases, so it was not staleness but the PHASE of the
        # two schedules. Releasing the pump removes the phase by
        # construction: the write happens after the generator produced the
        # value, so the device is never asked to move by more than one
        # tick's worth.
        #
        # The pump still has its own ticker for the HOLD case (a target
        # that stops changing must keep being re-sent, or the device
        # drops it), so this is a wake-up, not a replacement for the
        # pump's schedule.
        self._target_event_for().set()
        return self.stats.snapshot()

    def touch(self) -> None:
        """Record that the CLIENT is present.

        This is the ONLY writer of ``last_client_at``. It means "a page is
        open and talking to us", and it is deliberately separate from
        ``last_target_at``, which means "the session had any activity".

        The separation is the fix for the manual stroke dying on every link
        drop. Both used to write the same field, so the pump's idle guard
        could not distinguish "the operator closed the tab" from "the
        operator is sitting there while the BLE link is down". The second
        case is the common one: a BLE reconnect needs a scan, so the link is
        down for seconds, and while it is down POST /api/keepalive answers
        409 -- so the heartbeat cannot reach this method, the guard fires on
        a live page, and release() drops the operator's position.

        touch() also ends any link-outage window, because a heartbeat that
        gets through proves the link is back AND the operator is still
        there.

        It is not the ONLY way the outage clock is cleared: _note_link_up()
        clears it too, and that runs from connect() and from a successful
        _reconnect(). This comment used to claim touch() was the only
        writer, which was not true -- and it mattered, because the two
        clear the clock with different meanings. touch() says "a client
        is talking to us"; _note_link_up() says "the link came back, and
        the client is presumed to still be there". The second is the more
        optimistic of the two, and it is bounded: no heartbeat follows if
        the tab really did close, so the idle guard fires 2.5 s later.
        """
        now = time.monotonic()
        self.stats.last_client_at = now
        self.stats.last_target_at = now
        self.stats.link_down_at = 0.0

    def hold_position(self) -> None:
        """Hold the current target until something explicitly ends it.

        This is what a DEVICE KEY press needs.

        A key bound to ``travel:60`` moves the device to 60% and the
        device holds it. But the pump's idle guard releases the position
        when no client has been seen for ``CLIENT_GONE_SECONDS``, and the
        page's heartbeat only runs while the page's OWN manual slider is
        the current intent -- the page calls ``markManualHold()`` from
        ``push()`` and from nowhere else. So a button-driven position was
        released 2.5 s after the key press, with nobody having gone
        anywhere: the page was open, the link was up, and the operator had
        just asked for 60%.

        A single ``touch()`` would NOT fix that: it would buy 2.5 s and
        then the guard would fire again, because nothing was keeping the
        client clock fresh. So the hold is a FLAG, and the flag is what
        the guard consults.

        The fix belongs HERE, in the server, and not in the page, because
        the server is the only party that knows a key was just pressed.
        The page's heartbeat is about the PAGE; a key press is about the
        DEVICE, and the device has to stay where it was told to stay
        whether or not anybody is watching.

        The position is still released the moment it should be: by
        ``stop()`` (the operator lets go), by a sweep taking over, or by
        the link going away. Every one of those clears the flag, so the
        hold cannot outlive the thing that asked for it.
        """
        self._held_by_button = True
        self.touch()

    def clear_hold(self) -> None:
        """End a button-driven hold, so the guard can do its job again.

        Called by every path that ends the position for a reason other
        than the operator closing the tab: the stop button, a sweep
        taking over, the device being released. Without it, a button-set
        position would survive the operator's own stop, which is the
        opposite of what the button was asked to do.
        """
        self._held_by_button = False

    def hold_for_client(self) -> None:
        """Hold the current target for a CALLER that has no page.

        This is what an HTTP client asks for when it commands a position
        and means it: "go to 100% and STAY there until I tell you
        otherwise".

        WHY IT IS NEEDED, MEASURED
        ---------------------------
        On the simulator, against the real session:

            POST /api/v1/stroke {"percent": 100}   -> 200
            t=0.5s   target=100.0   device=10000
            t=2.0s   target=100.0   device=10000
            t=3.5s   target=0.0     device=0      <- retracted

        The guard fired at CLIENT_GONE_SECONDS (2.5 s) because the caller
        has no page and therefore sends no heartbeat, and
        ``client_present()`` was therefore False. The caller had asked
        for 100% and been told 200 OK; the device went out and came
        straight back in.

        WHY IT IS NOT THE PAGE'S HOLD
        ------------------------------
        The page's hold is a heartbeat: the page keeps saying "I am
        here", and when it stops saying so the hold ends. That is
        correct for a page -- a closed tab SHOULD let go, and that is
        the safety net that keeps an unattended device from staying
        out.

        An API caller cannot send a heartbeat, because it has no page.
        But it IS present -- it is a program running a loop, holding a
        socket open, waiting for the device to stay where it put it.
        Treating it as absent means the one caller that said "hold this"
        is the one caller that never gets it.

        So this hold is claimed BY THE COMMAND and ends only when
        something explicitly ends it: a release, a stop, a sweep taking
        over, a gait taking over, a disconnect, or a new link.

        THE SAFETY NET IS NOT SWITCHED OFF
        ----------------------------------
        This does not weaken the page's safety net. The page's own
        retract-on-close path is unchanged: a page that stops sending
        heartbeats still retracts in 2.5 s, because the page never
        claims THIS hold -- only the API's stroke route does.

        And it is bounded in the same way the operator's stop is: an
        explicit action ends it. There is no way to make the device
        stay out forever by accident, because every path that ends a
        position for a reason of its own clears this flag too.
        """
        self._held_by_client = True
        # The client clock is refreshed as well, so the guard does not
        # fire on the very next pass between the command and the flag
        # being read.
        self.touch()

    def clear_client_hold(self) -> None:
        """End a caller-driven hold, so the guard can do its job again.

        Called by every path that ends the position for a reason other
        than the caller asking for a different one: the release, the
        stop, a sweep or a gait taking over, the disconnect, and the
        new link.

        Without it, a caller's 100% would survive the operator's own
        stop -- the opposite of what the stop button was asked to do.
        """
        self._held_by_client = False

    def client_present(self) -> bool:
        """Is a client still there?

        True if the client was seen within CLIENT_GONE_SECONDS, or if the
        link has been down for less than LINK_OUTAGE_HOLD_SECONDS -- a link
        outage is not the operator leaving, and the heartbeat cannot arrive
        during one.
        """
        now = time.monotonic()
        if (now - self.stats.last_client_at) <= CLIENT_GONE_SECONDS:
            return True
        if self.stats.link_down_at:
            outage = now - self.stats.link_down_at
            if outage <= LINK_OUTAGE_HOLD_SECONDS:
                return True
        # A DEVICE KEY is holding the position. The key press is the
        # client: the operator is at the device, not at the page, and the
        # position they asked for has to stay where they put it until
        # something else ends it.
        #
        # This is checked LAST, so it cannot mask a client that really
        # has gone: a tab that closes while a key-set position is held
        # still leaves the hold in place, which is correct -- the key
        # press was a separate, deliberate act, and it is ended by the
        # stop button, by a sweep taking over, or by the link going
        # away, not by a browser tab closing.
        if self._held_by_button:
            return True
        # A CALLER is holding the position. The caller is a program, not
        # a page, so it cannot send a heartbeat -- but it IS present, and
        # it asked for this position explicitly.
        #
        # This is checked LAST, for the same reason the button's hold is:
        # it cannot mask a client that really has gone. A page that
        # closes while a caller's position is held still leaves the hold
        # in place -- which is correct, because the caller's command was
        # a separate, deliberate act, and it is ended by the release, the
        # stop, a sweep taking over, or the link going away.
        if self._held_by_client:
            return True
        return False

    def _on_disconnected(self, client=None) -> None:
        """The stack noticed the link is gone.

        This is bleak's disconnected callback, and it is the ONLY way the
        host hears about a link loss promptly.

        Without it, the only signal was a failed WRITE -- and a device
        that is switched off while the position is at 0% has no write to
        fail. The link therefore stayed "up" in the UI until the
        reconnect pass gave up. This was seen in the field: the power
        button was long-pressed and the page went on saying 已连接.

        The callback runs on bleak's own thread. It must not block, and it
        must not touch the event loop: it records the fact, and the pump's
        next pass -- which is on the loop -- acts on it.

        It is deliberately NOT the same as a link outage. A link outage
        is "the link dropped and we are trying to get it back"; this is
        "the link dropped", and the recovery is the same code either
        way. What matters here is that the fact is recorded the moment it
        happens, instead of whenever a write happened to fail.
        """
        # A DISCONNECT ENDS EVERYTHING, including a button-driven hold.
        # The hold outlives the link otherwise.
        self._held_by_button = False
        # THE CALLER'S HOLD SURVIVES A LINK OUTAGE, and this is the
        # opposite of the button's hold on purpose.
        #
        # A button press is a fact about the DEVICE, made in person: if
        # the link drops, nobody is holding the button any more, and the
        # device may well have been picked up. The hold ends.
        #
        # A caller's command is a fact about the CALLER: "go to 100% and
        # stay there". The link dropping does not unmake it -- the
        # caller did not ask for anything new, and it is still waiting
        # for the device to be where it put it. And the position IS
        # restored when the link comes back, because ``_want_percent``
        # still holds it and the recovery restores from it.
        #
        # So the hold stays, and the position stays held across the
        # outage. This is the same property the manual stroke has had
        # since the two clocks were split apart -- and the asymmetry is
        # the point: the button is a physical act, the command is a
        # standing instruction.
        #
        # It is ended by the explicit actions instead: the release, the
        # stop, a sweep or a gait taking over, and the operator's
        # explicit disconnect.
        # The link is down from here on, whatever the recovery does. The
        # pump's idle guard uses this clock to tell a BLE outage apart
        # from an operator who walked away.
        self._note_link_down()
        self.stats.link_ok = False
        self.stats.consecutive_failures = max(1, self.stats.consecutive_failures)
        if not self.stats.last_error:
            self.stats.last_error = "link lost, reconnecting..."
        # The fact, for the pump to act on. The callback cannot call the
        # recovery itself: it runs on bleak's own thread, and the
        # recovery is a coroutine on the event loop.
        self._link_lost_at = time.monotonic()

    def _note_link_down(self) -> None:
        """Mark the start of a link outage, once per outage."""
        if not self.stats.link_down_at:
            self.stats.link_down_at = time.monotonic()

    def _note_link_up(self) -> None:
        """The link is back up.

        Two things happen here, and both are deliberate:

        * the outage window closes, because there is no outage any more; and
        * the client clock is refreshed, because the link came back FOR the
          client -- the client is presumed to still be there, and the
          heartbeat will confirm it within a second.

        The second half is what bounds the cost of a false assumption. If
        the tab was in fact closed during the outage, no heartbeat follows,
        so the idle guard fires CLIENT_GONE_SECONDS later and the position
        is released. The operator's position is therefore over-extended by
        at most 2.5 s -- against a link outage during which the host could
        not have retracted anything anyway, because there was no link to
        retract it over.
        """
        self.stats.link_down_at = 0.0
        self.stats.last_client_at = time.monotonic()

    def position_percent(self) -> Optional[float]:
        """Where the device says it is, as a percentage, or None.

        This is the feedback signal a closed-loop motion generator needs.
        It is None whenever the link is down or the device has not reported a
        position, and callers must handle that: guessing a value here would
        silently turn the loop into something that looks closed but is not.

        On this hardware the 0x29 telemetry frames are what carry position,
        and they only flow after the sensor arming command, so a session
        that has not been armed reports None rather than a guess.

        THE VALUE COMES FROM THE TELEMETRY, AND IT NOW ACTUALLY FLOWS.
        """
        if not self.connected:
            return None
        # The MONITOR holds the device's reported position: it is the
        # thing that decodes the telemetry frames, and locate() turns the
        # five slot readings into a position of 0..10000.
        #
        # The reading used to live only there. `stats.reported_percent`
        # was declared, documented as "the position the DEVICE reported",
        # published in every snapshot, and read by position_percent() --
        # and NOTHING EVER WROTE IT. So the field was always None, the
        # feedback loop could never close, and `feedback_ok` was always
        # False.
        #
        # The project KNEW: protocol.py's own comment said "nothing writes
        # SessionStats.reported_percent ... Fixing the threshold alone
        # would change nothing until a position actually flows", and
        # scripts/p3c_regression.py pinned "it is never assigned" as a
        # regression assertion. The assertion protected the defect.
        #
        # So the position is read from the monitor HERE, and the monitor
        # is the only thing that can know it. Nothing is written into the
        # stats from outside, so there is no second copy to fall out of
        # step -- one source, read where it is needed.
        pct = self._reported_position()
        if pct is None:
            return None
        return max(0.0, min(100.0, pct))

    def _reported_position(self) -> Optional[float]:
        """The device's reported position, as a percentage, or None.

        Read from the telemetry monitor, which is the only thing that
        decodes the frames the position comes from.

        None means "the device has not reported a position" -- a link
        that is down, a monitor that is not bound yet, or a device whose
        sensors are not armed. It is NOT 0: 0 is a position (fully
        retracted), and confusing the two would make a device that has
        said nothing look like a device that is fully in.
        """
        monitor = self._telemetry
        if monitor is None:
            return None
        position = getattr(monitor, "position", None)
        if position is None:
            return None
        try:
            value = float(position)
        except (TypeError, ValueError):
            return None
        # The monitor's position is 0..10000 (POSITION_MAX), and the
        # percentage is 0..100. The conversion lives in the monitor
        # itself too -- position_pct -- so the two cannot disagree.
        max_pos = getattr(monitor, "POSITION_MAX", None)
        if max_pos is None:
            from .monitor import POSITION_MAX as max_pos
        if not max_pos:
            return None
        return value * 100.0 / float(max_pos)

    async def release(self) -> None:
        self._target = 0.0
        self._want_percent = 0.0
        self._vibration = 0
        self.stats.target_percent = 0.0
        self.stats.active_percent = 0.0
        self.stats.streaming = False
        # A release ENDS the position, so it ends the hold that was
        # keeping it. Without this, a key-set position would survive the
        # operator's own stop -- the button would be holding something
        # that had just been let go of.
        self._held_by_button = False
        # And the CALLER's hold. A release IS the caller saying "let go",
        # so the hold that was keeping the position must end here too --
        # otherwise the release would drop the position but leave the
        # hold set, and the next command would be held by a hold the
        # caller already ended.
        self._held_by_client = False

    async def stop(self) -> dict:
        await self.release()
        return self.stats.snapshot()

    async def set_mode(self, mode: int) -> bool:
        """Put the device into one of the app's modes.

        The frame is the app's own: ``app_set_mode(mode)`` builds the
        ``A55A 08 B0 3D <mode>`` frame with the app's own formatter, and
        this writes it through the SAME ``_write()`` the telemetry command
        uses -- so the mode command is counted by the write-error counter
        and the reconnect logic sees it exactly like every other frame.

        It is a separate method rather than a button action spelled out in
        the web layer, because "change the device's mode" is a DEVICE
        operation, and the web layer's job is to decide WHEN to do it.

        Returns True when the frame was written. A mode the app cannot
        transmit (a single digit) is refused before anything is written:
        ``app_set_mode`` raises for it, and a refused mode must not
        reach the radio.
        """
        try:
            frame = P.app_set_mode(int(mode))
        except (TypeError, ValueError):
            return False
        try:
            await self._write(frame)
        except Exception:                             # noqa: BLE001
            return False
        return True

    async def set_intensity(self, level) -> bool:
        """Set one of the device's three native intensity steps.

        THE VENDOR APP SENDS EXACTLY THREE, and they are the only
        intensity control that does not depend on the device's reported
        limits. Every other path scales the vibration by the device's
        own strokeMax / vibMax -- and a device that does not report
        those, or that ignores the scaled control frame, gets nothing.
        The three fixed steps do not depend on any of that: each is a
        whole frame with a hardcoded CRC, transcribed from the app's own
        smali.

            ordinal 0 (low)     A55A0BD00A00001442EC23
            ordinal 1 (medium)  A55A0BD00A00001C4245AA
            ordinal 2 (high)    A55A0BD00A000028421463

        It is a separate method rather than a button action spelled out
        in the web layer, because "set the intensity" is a DEVICE
        operation and the web layer's job is to decide WHEN to do it.

        Returns True when the frame was written. A level the app cannot
        transmit is refused before anything is written: a refused level
        must not reach the radio.
        """
        try:
            frame = P.app_set_intensity(level)
        except (TypeError, ValueError):
            return False
        try:
            await self._write(frame)
        except Exception:                             # noqa: BLE001
            return False
        return True

    # ------------------------------------------------------------- recovery

    def _record_link_end(self) -> None:
        if not self._link_started_at:
            return
        life = time.monotonic() - self._link_started_at
        if self.stats.link_lifetimes:
            self.stats.link_lifetimes[-1] = life
        self.stats.connected_seconds += life
        self._link_started_at = 0.0

    async def _reconnect(self, from_pump: bool = False) -> bool:
        if self._reconnecting:
            return False
        self._reconnecting = True
        started = time.monotonic()
        self.stats.last_error = "link lost, reconnecting..."
        self._record_link_end()
        # The link is down from here on, whatever the outcome of the recovery
        # pass. The pump's idle guard uses this clock to hold the operator's
        # position across the outage instead of releasing it -- the operator
        # did not go anywhere, the BLE link did.
        self._note_link_down()
        try:
            if not from_pump:
                await self._stop_stream()
            if self._client is not None:
                try:
                    await self._client.disconnect()
                except Exception:                     # noqa: BLE001
                    pass
            self._client = None
            self._write_char = None
            self.stats.link_ok = False

            # The pass is bounded by the attempt count; the deadline bounds
            # ONE attempt, and the budget handed to each attempt is what is
            # LEFT of it. Both bounds are needed and they are different
            # things:
            #
            #   * the deadline used to be 6 s while a single attempt took
            #     up to 52.5 s (25 s scan + 25 s connect + 2.5 s identity),
            #     so the deadline expired INSIDE the first attempt and had
            #     no effect at all. The pass was really bounded by the
            #     attempt count: eight attempts of ~52 s is seven minutes
            #     with a wedged pump.
            #   * the attempt count alone leaves each attempt unbounded,
            #     so a device that advertises but refuses connections
            #     still stalled the pump for the whole timeout.
            #
            # So each attempt now gets the smaller of the per-attempt
            # budget and whatever is left before the pass deadline.
            #
            # THE PASS DEADLINE IS THE ATTEMPT BUDGET TIMES THE ATTEMPT
            # COUNT, PLUS A SLACK FOR THE TIME BETWEEN THE ATTEMPTS.
            #
            # Without the slack the two bounds are EXACTLY equal: eight
            # attempts of sixty seconds is 480 s, and the deadline was
            # 480 s. The loop's "while ... < deadline" is checked BEFORE
            # the attempt count, so whichever bound was reached first
            # decided the exit -- and with the attempts costing exactly
            # their budget, the deadline won by a hair. The give-up
            # branch was unreachable and the pass ended by running out
            # of time instead of by running out of attempts.
            #
            # The slack is the time the loop spends BETWEEN the attempts
            # (the interval and the jitter) plus one attempt's worth, so
            # the attempt count is reached first and the give-up branch
            # is the one that runs.
            pass_budget = (RECONNECT_DEADLINE * RECONNECT_MAX_ATTEMPTS
                           + RECONNECT_DEADLINE)
            deadline = time.monotonic() + pass_budget
            attempts = 0
            while self._should_run() and time.monotonic() < deadline:
                if attempts >= RECONNECT_MAX_ATTEMPTS:
                    # Back the whole way off instead of re-entering this loop
                    # forever. The pump calls _reconnect() on every iteration,
                    # so without this a device that is off the desk produced a
                    # retry every RECONNECT_INTERVAL indefinitely.
                    self.stats.reconnect_giveups += 1
                    self.stats.last_error = (
                        "reconnect gave up after %d failed attempts "
                        "(link failure limit %d)"
                        % (attempts, RECONNECT_MAX_ATTEMPTS))
                    self.stats.consecutive_failures = attempts
                    # The recovery is OVER: nothing else will try, so the
                    # session must say so.
                    #
                    # link_gave_up was only ever set in _write()'s failure
                    # counter, which this path never reaches -- so the
                    # server had given up while still telling the page
                    # "not connected, and something is trying to fix it".
                    # The page reads exactly that combination (linkEverUp &&
                    # !link_gave_up && !operator_disconnected) and shows
                    # 「重连中……」 forever, with no way for the operator to
                    # learn that the server has stopped trying.
                    #
                    # This is the OTHER half of the same problem: the server
                    # gave up on its own, and the page has to be able to
                    # say that too.
                    self.stats.link_gave_up = True
                    self.stats.link_ok = False
                    return False
                await asyncio.sleep(
                    RECONNECT_INTERVAL
                    + random.uniform(-RECONNECT_JITTER, RECONNECT_JITTER))
                if not self._should_run():
                    return False
                attempts += 1
                self.stats.reconnect_attempts = attempts
                try:
                    # The recovery comes up on its own, so it must not
                    # clear the operator's disconnect -- see connect()'s
                    # docstring.
                    #
                    # The flags are passed as POSITIONAL-free keyword
                    # arguments that a caller may override, and the call
                    # is made defensively: a test double that replaces
                    # connect() with a zero-argument function (there is one
                    # in manual_hold_regression.py, which models an outage
                    # by making connect() raise) must still be callable.
                    # A TypeError from such a double is not a link
                    # failure, so it is retried without the extra
                    # arguments rather than counted as one.
                    remaining = deadline - time.monotonic()
                    attempt_budget = min(RECONNECT_DEADLINE, remaining)
                    try:
                        await self.connect(recovering=True,
                                           budget=attempt_budget)
                    except TypeError as exc:
                        if "recovering" not in str(exc) and \
                                "budget" not in str(exc):
                            raise
                        await self.connect()
                except Exception as exc:              # noqa: BLE001
                    self.stats.last_error = f"reconnect failed: {exc}"
                    self.stats.consecutive_failures = attempts
                    if "UNLIKELY" in str(exc) or "0x0e" in str(exc).lower():
                        await self._refresh_gatt_cache()
                    # THE DEVICE IS GONE, AND THE RECOVERY STOPS.
                    #
                    # A device that has gone OFF -- powered down, or its
                    # light dimmed too
                    # far to answer. There is nothing to reconnect TO,
                    # and the recovery's own budget says so: eight
                    # attempts of sixty seconds is NINE MINUTES of the
                    # page saying 「重连中……」 for a device that is not
                    # coming back.
                    #
                    # So the recovery is cut short when the DEVICE
                    # says it is gone rather than when the CLOCK runs
                    # out. "gone" is read from the scan: a scan that
                    # found the device's own advertisement is a device
                    # that is ON and refusing us; a scan that found
                    # NOTHING at all is a device that is off.
                    #
                    # The distinction matters, because the two need
                    # opposite answers:
                    #
                    #   * the device is advertising but will not accept
                    #     the connection -> keep retrying. It may be in
                    #     the vendor app, and closing the app is a
                    #     thing the operator can do;
                    #   * the device is not advertising at all -> it is
                    #     off or dying. Retrying for nine minutes tells
                    #     the operator nothing they cannot already see,
                    #     and it holds the card in its "connected" shape
                    #     the whole time.
                    #
                    # The check is made AFTER the first failed attempt,
                    # so it never fires on a device that is merely slow
                    # to answer -- and it is not a guess: it asks the
                    # radio what is out there.
                    if self._device_is_gone():
                        self.stats.reconnect_giveups += 1
                        self.stats.link_gave_up = True
                        self.stats.link_ok = False
                        self.stats.last_error = (
                            "设备已离线（扫描不到设备广播）")
                        return False
                    continue
                self.stats.reconnects += 1
                self.stats.consecutive_failures = 0
                self.stats.reconnect_attempts = 0
                self.stats.link_ok = True
                self.stats.last_error = ""
                # The link is back: the outage clock stops, and the operator's
                # position is restored from the held intent.
                #
                # `_want_percent` is what makes this work. release() zeroes it,
                # so if the idle guard had fired during the outage there would
                # be nothing here to restore -- which is precisely why the
                # guard must not fire on a live page during a link outage.
                self._note_link_up()
                if self._want_percent > 0:
                    self._target = self._want_percent
                    self._vibration = self._want_vibration
                    self.stats.target_percent = self._want_percent
                    self.stats.last_target_at = time.monotonic()
                await self._reassert_position()
                self.stats.recovery_seconds += (time.monotonic() - started)
                if not from_pump:
                    self._start_loop()
                return True
            # THE PASS ENDED WITHOUT A LINK. This is the other way out of
            # the loop, and it has to say the same thing as the other one.
            #
            # The loop ends either because the ATTEMPTS ran out (the
            # branch above, which sets link_gave_up) or because the
            # DEADLINE ran out. The two are exactly on the boundary:
            # the deadline is RECONNECT_DEADLINE * RECONNECT_MAX_ATTEMPTS
            # and each attempt may burn a whole RECONNECT_DEADLINE, so
            # eight attempts of sixty seconds is exactly the deadline.
            # Whichever fires first is a race -- so the deadline exit
            # has to set the flag too.
            #
            # It did not, and that is exactly the defect the fix was
            # supposed to remove: the recovery gave up, nothing else
            # would try, and the page went on showing 「重连中……」 --
            # waiting for a recovery that was never coming.
            self.stats.link_gave_up = True
            self.stats.link_ok = False
            if not self.stats.last_error:
                # The limit is the PASS deadline, which includes the
                # slack -- so the number here has to be the same one.
                # It used to report the attempt budget times the count
                # without the slack, which is 60 s short of what the
                # operator actually waited for.
                self.stats.last_error = (
                    "reconnect gave up after %d attempts (time limit %ds)"
                    % (attempts, int(pass_budget)))
            return False
        finally:
            self._reconnecting = False

    def _device_is_gone(self) -> bool:
        """Is the device itself gone, as opposed to merely unreachable?

        THE ANSWER COMES FROM THE SCAN THAT JUST RAN, not from a fresh
        radio operation and not from a stale list.

        The attempt that just failed ran a scan, and the scan's own
        verdict was recorded at the time (see `_scan_saw_device` and
        `_connect_ble`). So this reads that verdict, and it does not
        touch the radio again -- a recovery that scanned on every
        attempt would spend its whole budget scanning instead of
        connecting.

        THE TWO FAILURES NEED OPPOSITE ANSWERS.

        The connect attempt can fail for two very different reasons:

          * the device WAS advertising and refused the connection. It is
            ON -- in the vendor app, or busy with another central. There
            is something to wait for, and the operator can act on it.

          * the device WAS NOT ADVERTISING AT ALL. It is off, or its
            light dimmed too far to answer. There is nothing to wait
            for.

        The second case is a device that powered itself down or dimmed too
        far to answer. Retrying it for the full nine-minute budget is nine
        minutes of the page saying 「重连中……」 about a device that is
        not coming back, and -- worse -- nine minutes of the device card
        stuck in its "connected" shape, with 断开 as the only thing
        offered, while the operator is trying to work out what to do.

        BUT "NOT SEEN" IS NOT "OFF".
        #
        # A scan that saw NOTHING AT ALL -- not even a neighbour's
        # headphones -- is not evidence about our device. An empty
        # room, a radio that missed the advertisement, a device in
        # another room: all of them look the same from here. So the
        # verdict is only conclusive when the radio demonstrably works
        # and our device is the one missing from it.
        #
        # That is why the scan's own list is still consulted: it says
        # whether the radio saw ANYTHING. An empty list is "unknown",
        # and "unknown" keeps the recovery going -- because ending
        # every recovery in an empty room would be worse than waiting.
        #
        # A SIMULATED SESSION NEVER HAS A GONE DEVICE. The simulator is
        # always there, so the question does not apply to it -- and the
        # simulator's scan would find nothing, which would make every
        # simulated outage look like a device that had been switched
        # off. So this is False under simulation.
        """
        if self.simulate:
            return False
        # NO SCAN HAS RUN, so there is no verdict to read. Unknown.
        if self._scan_saw_device is None:
            return False
        # THE SCAN SAW THE DEVICE. So it is on, and it is refusing us.
        if self._scan_saw_device:
            return False
        # THE SCAN DID NOT SEE IT. Now the radio's own health decides
        # whether that is evidence.
        from . import discovery as _discovery
        try:
            seen = _discovery.last_scan()
        except Exception:                             # noqa: BLE001
            return False
        if not seen:
            # The radio saw NOTHING. That is not evidence about our
            # device, so the recovery keeps going.
            return False
        # THE RADIO WORKS AND OUR DEVICE IS MISSING.
        #
        # The list holds DESCRIPTIONS, not addresses, and a
        # description carries the advertised name -- which carries the
        # serial PREFIX. So the test is a prefix test, and the prefixes
        # come from the protocol module's own table, so this cannot
        # drift from the table the rest of the project uses.
        #
        # The values are the prefix LISTS, not the keys: the keys are
        # model names ("Vero"), and the on-air name carries a prefix
        # ("UMVXEXAMPLE03"). Matching the key would never match
        # anything, and every device would read as gone.
        prefixes = []
        for _prefix_list in P.PRODUCTS.values():
            for _p in _prefix_list:
                prefixes.append(str(_p).upper())
        for line in seen:
            up = str(line).upper()
            for pref in prefixes:
                if pref in up:
                    # Something of ours IS advertising. So the device
                    # is on -- it just would not take our connection.
                    return False
        # Our product line is not out there, but OTHER devices are.
        # So the radio is working, and our device is the one missing.
        return True

    async def _reassert_position(self) -> None:
        """Burst the target back at the device after link-up."""
        target = self._target
        if target <= 0:
            return
        frame = P.app_a0_control(
            int(round(target / 100.0 * P.STROKE_MAX)), self._vibration)
        sent = 0
        # Every exit from this burst has to publish how far it got. The three
        # bare `return`s above this line used to skip the assignment, so the
        # only value ever written was REASSERT_FRAMES -- a partial burst was
        # not 30, it was the previous link's number, which then read as a
        # healthy full burst. Measured on the simulator: injecting a link
        # death at frames 0, 15, 30, 59 and 60 left reassert_frames at a
        # pre-seeded 999 every single time, and a real 31-of-60 burst left it
        # at 0.
        #
        # That also made any "was the burst truncated?" criterion structurally
        # unfalsifiable: the values 1..REASSERT_FRAMES-1 were unreachable, so
        # `reassert_frames and reassert_frames < REASSERT_FRAMES` could not
        # ever be true -- it passed a run where the re-assert never happened.
        try:
            for _ in range(REASSERT_FRAMES):
                if not self._should_run() or self._client is None:
                    return
                try:
                    await self._write(frame)
                    self.stats.frames_sent += 1
                    sent += 1
                except Exception:                     # noqa: BLE001
                    return
                # Publish as the burst progresses, not only when it ends. The
                # old single write after the loop meant every reader -- the
                # panel, accept_recovery.py's outage sampler -- saw either the
                # previous link's number or 0 for the whole 1.2 s burst, so a
                # half-finished burst was indistinguishable from one that never
                # started. Measured with the pump off: cuts at 10/30/58/59
                # frames now report 10/30/58/59 instead of all reporting the
                # stale 999.
                self.stats.reassert_frames = sent
                await asyncio.sleep(REASSERT_INTERVAL)
        finally:
            # `finally`, not a trailing statement: a bare `return` runs it, so a
            # burst cut short at 0 reports 0 rather than inheriting the last
            # link's count. This is what makes 1..REASSERT_FRAMES-1 reachable
            # and therefore what makes a truncated-burst criterion falsifiable.
            self.stats.reassert_frames = sent

    async def _refresh_gatt_cache(self) -> None:
        """Rebuild BlueZ's GATT database. Linux-only, and honestly reported.

        BlueZ returns UNLIKELY_ERROR when its cached attribute table no longer
        matches the peripheral; a disconnect plus a rescan fixes it. There is no
        equivalent outside Linux, so elsewhere this records nothing rather than
        implying a refresh that did not happen.
        """
        if not IS_LINUX:
            return
        import subprocess
        env = {**dict(__import__("os").environ),
               "DBUS_SYSTEM_BUS_ADDRESS":
                   "unix:path=/run/dbus/system_bus_socket"}
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:                         # noqa: BLE001
                pass
            self._client = None
            self._write_char = None
            self.stats.link_ok = False
        if not self.address:
            return
        for cmd, timeout in (
                (["bluetoothctl", "disconnect", self.address], 20),
                (["bluetoothctl", "--timeout", "20", "scan", "on"], 35),
                (["bluetoothctl", "scan", "off"], 15)):
            try:
                # In a thread, not inline. `bluetoothctl --timeout 20 scan
                # on` was MEASURED at 20.05 s, and a blocking call in the
                # event loop stops EVERYTHING for that long -- including
                # the SIGTERM handler, which is how a shutdown could be
                # left hanging with the loop wedged behind a subprocess.
                #
                # Same command, same timeout, same swallow-everything
                # behaviour, same 1.0 s sleep between steps. Only the
                # thread it runs in changed.
                await asyncio.to_thread(subprocess.run, cmd,
                                       capture_output=True, env=env,
                                       timeout=timeout)
            except Exception:                         # noqa: BLE001
                pass
            await asyncio.sleep(1.0)
        self.stats.gatt_refreshes += 1

    # ------------------------------------------------------------ send loop

    def _start_loop(self) -> None:
        # It used to set `self._stopping = False` here. That undid the
        # operator's own disconnect: disconnect() sets _stopping=True and
        # operator_disconnected=True, but an in-flight _reconnect() that
        # then succeeded called connect(), which reset the flag and
        # cleared the operator's intent -- so the page went back to
        # 「重连中……」 for a link the operator had deliberately closed.
        #
        # The page showed 「重连中……」 for a link the operator had
        # deliberately closed.
        #
        # The stop flag is now only cleared where a stop is meant to end:
        # in connect(), which the operator reaches by asking to connect.
        # A recovery that comes up on its own must not undo a stop.
        if self._task is None or self._task.done():
            self._running = True
            # The pump's loop is captured HERE, where the pump is
            # actually created, rather than only in arm_sensors().
            #
            # It used to be captured in arm_sensors() alone, which meant
            # self._loop could name a loop that was already CLOSED: the
            # session is a module-level object that outlives its loop,
            # so a second connect in a new loop left self._loop pointing
            # at the first one. _maybe_keepalive_stream() then scheduled
            # its follow-up onto the closed loop and raised
            # "RuntimeError: Event loop is closed" -- from inside a notify
            # callback, where nothing catches it.
            #
            # Capturing it where the task is created keeps the two in
            # step: the loop that runs the pump is the loop that the
            # session schedules its asynchronous work on.
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                # No loop running: leave self._loop alone. _start_loop
                # is only ever called from inside the loop.
                pass
            self._task = asyncio.create_task(self._run())

    async def _stop_stream(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # The task was cancelled, which is the POINT of cancelling
                # it. Swallowed on purpose: the caller asked for the pump
                # to stop and is waiting for it to stop, and re-raising
                # here would abort whatever called _stop_stream().
                #
                # The audit called this tuple "equivalent to except
                # Exception" because CancelledError is a BaseException in
                # 3.8+. That is wrong about the tuple: an except clause
                # listing a class catches it whatever it inherits from, so
                # the tuple DID catch CancelledError -- and it caught MORE
                # than `except Exception` would. The two clauses are
                # therefore spelled out here rather than left implicit in a
                # tuple, so the next reader can see which case is which.
                pass
            except Exception:                          # noqa: BLE001
                pass
            self._task = None
        self.stats.pump_alive = False

    async def _run(self) -> None:
        """Pump frames until stopped. Owns the watchdog and recovery.

        The pump keeps an ABSOLUTE schedule, not a relative sleep. That is
        not a style choice, and it is the second half of why a tick control
        needs this loop to change.

        A relative sleep (``await asyncio.sleep(self.interval)``) makes
        each pass take the interval PLUS whatever the pass itself cost,
        and the cost of a pass is not constant: it depends on what else
        the event loop is doing. So the pump's period wanders by a few
        percent, and -- the part that matters -- its phase against the
        motion GENERATOR wanders with it. The generator keeps an absolute
        deadline; the pump did not. Two clocks with the same nominal
        period and no shared phase means the pump samples the generator's
        output at an arbitrary point in the cycle, so the device is asked
        to move by the distance of one OR two ticks, depending on where
        the phase happened to land.

        Measured, with the real generator driving a real pump: the device
        saw a worst-case jump of 9.36 % of the stroke at a 15 ms tick,
        against the generator's own 4.69 % -- exactly double, and exactly
        what a two-tick jump looks like. With the pump on the same
        absolute schedule, the ratio measured 1.00.

        The absolute schedule also removes the drift the relative sleep
        caused in the pump's own rate: measured against the simulator,
        the relative pump's real period was 15.4 ms for a nominal 15 ms
        (+3.0 %), and it moved further off as the loop got busier.

        The pump WAITS for a new target as well as keeping its own
        ticker. The two clocks have the same nominal period but no shared
        phase, and that phase is what made the device jump two ticks at
        once. Waiting for the generator's write removes the phase by
        construction -- the write carries the value the generator JUST
        produced, never one from a tick ago.

        The pump's own ticker is still there for the HOLD case: a target
        that stops changing has to keep being re-sent, because the device
        drops a target that stops arriving. So the wait is bounded by the
        interval rather than open-ended.
        """
        self._running = True
        self.stats.pump_alive = True
        prev = None
        caps_due = 0.0
        try:
            while self._running:
                now = time.monotonic()
                # The IDLE GUARD. It releases the target when the operator is
                # gone -- and "gone" now has two separate meanings, which is
                # the whole fix.
                #
                # Measured, on the simulator, driving the real session: with
                # one conflated timestamp, a BLE link drop released the
                # operator's position even though the page stayed open. The
                # heartbeat could not get through during the outage (POST
                # /api/keepalive answers 409 while the link is down), so the
                # guard fired on a live page. The automatic sweep never hit
                # it because automotion.py calls session.touch() in process,
                # every tick -- which is exactly the asymmetry between the
                # two paths: the manual stroke cannot be achieved, while
                # the automatic sweep works.
                #
                # So the guard asks TWO questions now:
                #   * has the CLIENT gone?    -> last_client_at, CLIENT_GONE_SECONDS
                #   * has the LINK been down?  -> link_down_at, LINK_OUTAGE_HOLD_SECONDS
                #
                # A closed tab fails the first immediately, so the safety net
                # still retracts in 2.5 s. A live page during a BLE outage
                # fails only the second, and only after 30 s.
                #
                # THE GUARD MUST ACTUALLY RETRACT, AND THAT IS THE WHOLE
                # POINT OF THIS BLOCK.
                #
                # It used to do this:
                #
                #     if not self.client_present():
                #         if self.stats.active_percent > 0:
                #             await self.release()
                #         prev = None
                #         await asyncio.sleep(0.05)
                #         continue
                #
                # and release() only CLEARS THE HOST'S INTENT -- it sets
                # _target, _want_percent, _vibration and the stats to
                # zero. It writes no frame. So the guard cleared the
                # host's picture of the device and the DEVICE kept the
                # position it was told to hold.
                #
                # And the `continue` made it worse: it skipped the whole
                # write block below, which is the only place a frame
                # goes out. The `prev = None` on the line above was
                # meant to make the next pass re-send, but the next pass
                # takes the SAME branch -- the client is still gone -- so
                # it never reached the write block either. The two lines
                # together meant the retraction was never sent.
                #
                # Measured, on the simulator, driving the real session:
                # 158 control frames were written, the last one at
                # t=2.506 s with drive=6000, and NOT ONE frame with
                # drive=0 was ever written. After 3.0 s the device was
                # still at position 6000 -- 60% of the way out. Closing
                # the tab did not retract the device; it only made the
                # HOST forget that the device was out.
                #
                # So the retraction is WRITTEN here, explicitly, and the
                # pass does not `continue` past it. The frame is the
                # ordinary A0 control frame with drive=0, built by the
                # same builder every other frame uses, so there is one
                # way to tell the device to retract.
                if not self.client_present():
                    if self.stats.active_percent > 0:
                        await self.release()
                        # THE RETRACTION FRAME. Written here rather than
                        # left to the write block below, because that
                        # block is on the other side of a `continue` that
                        # this branch always takes.
                        #
                        # It is written ONCE -- on the pass where the
                        # guard first fires -- and not on every pass
                        # afterwards: once the device is at zero, writing
                        # zero again is noise on the wire and nothing
                        # else. The `active_percent > 0` test above is
                        # what makes it once: release() sets
                        # active_percent to 0, so the next pass finds it
                        # at 0 and does not write again.
                        try:
                            await self._write(P.app_a0_control(0, 0))
                            self.stats.frames_sent += 1
                        except Exception as exc:           # noqa: BLE001
                            # A write that fails here is counted by
                            # _write() like any other, and the recovery
                            # is the same one: the link is gone, so the
                            # device is either off or out of range, and
                            # there is nothing more this process can do
                            # about where the device is.
                            if not self.stats.last_error:
                                self.stats.last_error = str(exc)
                    prev = None
                    await asyncio.sleep(0.05)
                    continue

                # THE LINK WENT DOWN, AND THE STACK TOLD US.
                #
                # The disconnected callback records the fact the moment it
                # happens, and this is where the host acts on it. Without
                # this, a device switched off while the position was at
                # 0% produced no write to fail, so nothing noticed, and
                # the page went on saying 已连接 until the reconnect pass
                # gave up.
                #
                # The recovery is the SAME one a failed write uses. The
                # only thing that is new is that the trigger arrives
                # promptly instead of never.
                if self._link_lost_at:
                    self._link_lost_at = 0.0
                    ok = await self._reconnect(from_pump=True)
                    if not ok:
                        self.stats.active_percent = 0.0
                        self.stats.streaming = False
                        prev = None
                        await asyncio.sleep(0.2)
                    continue

                # Wait for a new target, but never longer than the pump's
                # own interval. The timeout is the HOLD case: a target
                # that stops changing must still be re-sent, or the device
                # drops it (CLIENT_GONE_SECONDS / touch()).
                #
                # The event is fetched per pass, because it binds to the
                # loop that awaits it and this session outlives its loop
                # (see _target_event_for).
                target_event = self._target_event_for()
                try:
                    await asyncio.wait_for(target_event.wait(),
                                           timeout=self.interval)
                    target_event.clear()
                except asyncio.TimeoutError:
                    pass
                now = time.monotonic()

                target = self._target
                if target != prev or target > 0:
                    frame = P.app_a0_control(
                        int(round(target / 100.0 * P.STROKE_MAX)),
                        self._vibration)
                    try:
                        await self._write(frame)
                        self.stats.frames_sent += 1
                        prev = target
                    except Exception as exc:           # noqa: BLE001
                        # _write() already counted this failure against
                        # LINK_FAILURE_LIMIT; do not double-count here or the
                        # limit fires at half its intended threshold.
                        if not self.stats.last_error:
                            self.stats.last_error = str(exc)
                        ok = await self._reconnect(from_pump=True)
                        if not ok:
                            self.stats.active_percent = 0.0
                            self.stats.streaming = False
                            prev = None
                            await asyncio.sleep(0.2)
                        continue

                self.stats.active_percent = target
                self.stats.streaming = target > 0

                if now >= caps_due:
                    try:
                        await self._write(P.app_caps_query(), response=True)
                    except Exception:                 # noqa: BLE001
                        pass
                    caps_due = now + 5.0

                # No sleep at the end of the pass. The wait at the TOP of
                # the pass is the pump's ticker: it returns as soon as a
                # new target exists, and otherwise times out after the
                # interval. Sleeping here as well would add a second
                # interval to every pass, so the pump would run at half
                # the rate it is supposed to.
        except asyncio.CancelledError:
            raise
        finally:
            self.stats.pump_alive = False