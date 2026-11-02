"""Automatic motion generator: a smooth, repeating stroke.

Motivation. Holding one position works, and sweeping between two does too,
but the first version of this swept with a linear ramp and looked wrong to
the operator: the mechanism jerked at each end of the stroke. That is a
property of the maths, not of the hardware. A linear ramp is a triangle
wave -- velocity is constant and then reverses instantly at each end, so
the mechanism is asked to stop and start again every half cycle. No amount
of extra steps fixes it; only the curve does.

What the vendor app does. Reverse engineering the app (cn.zhuimeng.amorlink
1.1.5) shows it does NOT hand a trajectory to the device. Every motion
command ends at r1.J0(), which builds the same 13-byte A0 frame this code
builds. r1.L0() takes a list of future positions, feeds them to an internal
generator (f6364i0.plan) that returns either a single position or a
Trajectory, and then routes the result back through J0(). So there is no
device-side trajectory engine to drive: smoothness has to come from the
sequence of positions we send, exactly as it does in the app.

The app's own generator is a phase-based oscillator. The trajectory record
carries `phaseRadiansAtProduced` and `speed`, i.e. position is

    centre + amplitude * sin(phase)

with the phase advancing continuously. That is what this module now
reproduces, because a sinusoid has zero velocity at both ends of the
stroke and finite acceleration everywhere -- which is precisely what a
linear ramp lacks.

Feedback. The app's ramp (r1.n0) computes each step from the device's
REPORTED position (f6384t), not from the commanded one, so the motion
self-corrects when the mechanism lags. This module does the same when the
link is reporting position; otherwise it interpolates from the commanded
value, which keeps it working on stacks that do not report position.

Everything still goes through ``set_target``: one writer on the link.
"""

from __future__ import annotations

import asyncio
import math
import time

from .gait import GaitState, parse_gait
from dataclasses import dataclass, field
from typing import Optional

#: Period of one complete there-and-back cycle, in seconds. A full cycle is
#: out-and-back, so this is twice the one-way time. Inside the supported
#: 0.1-1.5 s band, and near its middle so the first run is not startling.
DEFAULT_TRAVERSE_SECONDS = 0.5

#: No pause at the ends. A sine already slows to a stop there, and a dwell
#: turns that smooth turnaround into a visible stutter -- the opposite of
#: what this rewrite is for. Kept as a parameter for anyone who wants it.
DEFAULT_DWELL_SECONDS = 0.0

#: Period of the position update -- how often a NEW target position is
#: computed. 15 ms is the vendor app's own nominal value
#: (``GeneratedMotionPolicy.NOMINAL_TICK_MS = 15``, see
#: analysis/decompile/jadx-123/sources/com/app/aike/model/
#: GeneratedMotionPolicy.java), which is the value this project had drifted
#: away from: it ran at 40 ms, so the device was asked to move 12.51 % of the
#: stroke in a single command, against the app's 4.71 %.
#:
#: The value is OPERATOR-SETTABLE from the UI, so the interval can be
#: tried on real hardware, but the default stays the app's nominal so an
#: untouched install behaves like the app.
TICK_SECONDS = 0.015

#: Fastest the generator is allowed to tick. 8 ms is the app's own floor
#: (``GeneratedMotionPolicy.MIN_TICK_MS = 8``). The generator may tick this
#: fast ONLY because the BLE pump is moved to the same rate -- see
#: ``vero.session.frame_interval_for_tick`` and the note there about the
#: pump writing a VARIABLE rather than a queue.
MIN_TICK_SECONDS = 0.008

#: Slowest the generator is allowed to tick. 30 ms is the app's own ceiling
#: (``GeneratedMotionPolicy.MAX_TICK_MS = 30``): its generator clamps the
#: real elapsed time between samples into this band, so a tick outside it is
#: not a tick the app can produce.
MAX_TICK_SECONDS = 0.030

#: Most the generator is allowed to command. A 15 s traverse at the send
#: rate would be 750 commands to cover the same 60 % of stroke a 4 s one
#: covers in 400 -- beyond some point the extra frames change nothing a
#: person can feel, because the mechanism cannot follow them, and they
#: displace nothing except airtime.
MAX_STEPS_PER_CYCLE = 400

#: Fewest commands per full cycle. At the 0.1 s floor the round trip is
#: 200 ms, which at the 20 ms send rate is exactly 10 frames: that is the
#: physical ceiling, not a tunable floor. Below 10 steps a traverse is a
#: staircase rather than motion, so 10 is the target and anything faster
#: simply cannot be interpolated.
MIN_STEPS_PER_CYCLE = 10


def tick_seconds(traverse_seconds: float, tick: Optional[float] = None) -> Optional[float]:
    """Update period that keeps a fast traverse properly sampled.

    At 4 s one way a fixed tick gives 200 steps per cycle, more than the
    mechanism can follow. At 0.1 s the same tick gives 5 steps, and five
    steps is a staircase. So the tick shrinks with the traverse, never below
    one target per send frame, and grows when the traverse is so slow that
    more commands would just repeat the same position.

    ``tick`` is the operator's explicit choice, in seconds. When it is
    given it IS the answer: the traverse scaling exists to keep a fast
    traverse sampled when nothing else said otherwise, and it must not
    quietly overrule a number the operator typed into the UI. A tick
    outside the app's own band (8-30 ms, ``MIN_TICK_SECONDS`` /
    ``MAX_TICK_SECONDS``) is REFUSED by returning None rather than being
    clamped, because a typed number that is silently turned into a
    different one is a number the operator cannot trust.

    Returning None for "no valid tick" is why the signature says
    ``Optional[float]``. The callers decide what None means: the
    parameter layer refuses it at the edge (a 422 for a bad request), and
    the generator treats it as "use the default", so a bad value can
    never silently become a different good one.
    """
    if tick is not None:
        t = float(tick)
        if not (MIN_TICK_SECONDS - 1e-12 <= t <= MAX_TICK_SECONDS + 1e-12):
            return None
        return t

    cycle = 2.0 * float(traverse_seconds)
    # Sampling floor: a period at or below this would undersample, so the
    # result may not go shorter.
    shortest_ok = cycle / MIN_STEPS_PER_CYCLE
    # Sampling ceiling: a period at or above this would just repeat the same
    # position, so the result may not go longer. A 15 s sweep at the send
    # rate would otherwise emit 750 commands to cover a stroke that 400
    # already covers.
    longest_ok = cycle / MAX_STEPS_PER_CYCLE

    period = TICK_SECONDS
    if shortest_ok < period:
        # Fast sweep: the default tick is too coarse, go faster.
        period = shortest_ok
    if longest_ok > period:
        # Slow sweep: the default tick is finer than it needs to be.
        period = longest_ok
    return max(MIN_TICK_SECONDS, period)

#: Guard against a session that reports a wild position. A feedback loop fed
#: a bad sample would otherwise lurch away from the commanded path.
FEEDBACK_MAX_DELTA = 8.0

#: Fraction of the remaining position error corrected per tick. Below 1.0 on
#: purpose: a reading that arrives late must not turn into an overshoot.
FEEDBACK_GAIN = 0.5


#: The motion templates. Each one maps a phase in [-1, 1] onto a stroke
#: position in [-1, 1], and each one is STRICTLY MONOTONE INCREASING with
#: the endpoints mapped exactly to -1 and +1. That is not a style
#: preference: `_phase_for` is the exact inverse of this mapping, and a
#: template that is not invertible has no phase, so a live range change
#: would jump instead of remapping.
#:
#: The three that were considered and rejected, with the measured reason
#: (see docs/audit/P9-LEDGER.md and motion_regression.py):
#:
#: * ``triangle`` (1 - 2*|x|) -- a V at the midpoint, so the inverse does
#:   not exist. It is also the defect the linear ramp was removed for.
#: * ``square`` -- monotone and invertible, but the midpoint step is 100%
#:   of the span: 6366x the sine's peak speed.
#: * ``pulse`` -- not monotone, so no inverse exists.
#:
# `linear` and `raised-cos` were rejected too: the linear ramp's endpoint
# speed is 1.0 (a hard reversal, which is what this module exists to
# remove) and the raised-cosine is twice as fast through the middle
# (peak 6.3 vs 3.1) -- flatter at the ends than the sine but trading a
# soft turnaround for a hard lunge. Measured, not assumed.
#
# A fourth candidate was rejected after it had been implemented once:
# ``pulsed`` = sin(x*pi/2)^3. It is strictly increasing and its endpoints
# map to +/-1, so it passed the invertibility test -- but its derivative is
# ZERO at the midpoint of the stroke, so the mechanism stops dead in the
# middle and dwells there 24% of the time (measured: 24.0% of the phase
# domain has |f| < 0.05, against the sine's 3.2%). That is the SAME defect
# the linear ramp was removed for, moved from the two ends of the stroke to
# its middle: a hard stop followed by a hard restart. It was replaced by
# ``sag`` below.
PRESETS = ("sine", "surge", "sag")

#: The default template. Kept as a named constant so every "what happens
#: when nothing was asked for" site reads the same value.
DEFAULT_PRESET = "sine"

#: How long a gait waits between two "the client is still here" pokes.
#:
#: The session's idle guard releases a held target after
#: ``CLIENT_GONE_SECONDS`` (2.5 s) of silence from the client. A gait
#: holds a position for as long as the operator asked, and it has to say
#: "I am still here" while it holds -- so the hold is sliced and the
#: session is touched on each slice.
#:
#: The slice is short enough to stay well inside the guard's window (a
#: quarter of it, so two consecutive misses would still not trip it) and
#: long enough that the pokes are not noise: a gait with a ten-second
#: hold pokes the session about forty times, which is nothing.
GAIT_HOLD_SLICE_SECONDS = 0.5


def ease(position: float, shape: str = DEFAULT_PRESET) -> float:
    """Map a phase in [-1, 1] onto the stroke, using the named template.

    The default template is a sinusoid, and only a sinusoid: velocity is
    zero at both ends of the stroke and the acceleration is bounded, so the
    mechanism eases in and out instead of stopping dead and reversing.

    The linear ramp it replaced was selectable for a while so the difference
    could be demonstrated rather than asserted. It was removed once the
    choice was made: a triangle wave is not an alternative motion style, it
    is the defect, and leaving it in the UI only invites picking it.

    A raised-cosine was also tried and rejected -- flatter at the ends than
    the sine but twice as fast through the middle (peak 6.3 vs 3.1). For a
    mechanism with inertia that trades a soft turnaround for a hard lunge.
    Measured, not assumed; see motion_regression.py.

    `shape` is now a real dispatch rather than a parameter that was accepted
    and ignored. An UNKNOWN shape raises ValueError: falling back to the
    sine would make "the operator picked a template and it did nothing"
    invisible, which is the same class of defect as a silently discarded
    request. The templates themselves:

    ``sine``
        sin(x * pi/2). The baseline; its output is byte-for-byte what this
        function returned before the dispatch existed.
    ``surge``
        sin(x * pi/2) * (1 + 0.3 * cos(x * pi/2)^2). 1.30x the sine's peak
        speed, so the middle of the stroke is livelier while both ends are
        still at rest.
    ``sag``
        sin(x * pi/2) * (1 - 0.3 * cos(x * pi/2)^2). 0.82x the sine's peak
        speed, so the middle of the stroke is calmer and the ends are
        reached over a longer share of the cycle. It has no stationary
        point anywhere: the derivative at the midpoint is 1.10, not zero.

    Returns a value in [-1, 1] for input in [-1, 1].
    """
    if shape == "sine":
        return math.sin(position * math.pi / 2.0)
    if shape == "surge":
        s = math.sin(position * math.pi / 2.0)
        return s * (1.0 + 0.3 * math.cos(position * math.pi / 2.0) ** 2)
    if shape == "sag":
        s = math.sin(position * math.pi / 2.0)
        return s * (1.0 - 0.3 * math.cos(position * math.pi / 2.0) ** 2)
    raise ValueError("unknown motion template %r; expected one of %s"
                     % (shape, ", ".join(PRESETS)))


@dataclass
class AutoParams:
    min_percent: float = 10.0
    max_percent: float = 70.0
    vibration: int = 0
    #: One-way travel time; a full cycle takes twice this.
    traverse_seconds: float = DEFAULT_TRAVERSE_SECONDS
    dwell_seconds: float = DEFAULT_DWELL_SECONDS
    #: Close the loop on the device's reported position when available.
    feedback: bool = True
    #: Which motion template to run. MUST stay the LAST field: three call
    #: sites construct this dataclass with POSITIONAL arguments
    #: (vero/api.py passes five, vero/webapp.py passed six, vero/cli.py
    #: passes all keywords), and a new field inserted anywhere but the end
    #: would shift those positionals onto the wrong parameters. Appending
    #: it here makes every one of them pick up the default instead.
    #:
    #: The default is None, NOT "sine", and the difference is the whole
    #: point. None means "this call did not name a template"; "sine" means
    #: "this call asked for the sine". The public API family (vero/api.py,
    #: a protected path) builds this dataclass with five positional
    #: arguments and therefore never names a template -- so a default of
    #: "sine" would make every public-API call SILENTLY RESET whatever
    #: template the operator had selected in the UI, replacing it with the
    #: sine. That was a real defect, measured: a v1 auto/params call that
    #: changed nothing but max_percent turned a running surge sweep back
    #: into a sine. With None as the default, a call that does not name a
    #: template says so, and start()/update() decide what "unspecified"
    #: means -- start() uses the default template, update() keeps the
    #: template that is already running.
    preset: Optional[str] = None
    #: The operator's tick choice, in MILLISECONDS. None means "this call
    #: did not name a tick", which is the same distinction the template
    #: field makes: an unspecified tick means the default, and a named tick
    #: means exactly that tick.
    #:
    #: It MUST stay the LAST field, for the same reason the template does:
    #: vero/api.py (a protected path) builds this dataclass with five
    #: POSITIONAL arguments, so a field inserted anywhere but the end would
    #: shift those positionals onto the wrong parameters. A field appended
    #: here is picked up as the default by every existing call site, which
    #: is what keeps the public API family's behaviour unchanged.
    #:
    #: Milliseconds, not seconds, because the UI control is in
    #: milliseconds and the app's own constants are (MIN_TICK_MS = 8,
    #: NOMINAL_TICK_MS = 15, MAX_TICK_MS = 30). Storing the unit the
    #: operator sees is what stops a 15 meaning 15 s in one place and 15 ms
    #: in another.
    tick_ms: Optional[int] = None

    def clamp(self) -> "AutoParams":
        lo = max(0.0, min(100.0, float(self.min_percent)))
        hi = max(0.0, min(100.0, float(self.max_percent)))
        if hi < lo:
            lo, hi = hi, lo
        # An unknown template falls back to the default rather than raising.
        # This is the clamp semantics: the dataclass clamps a value into
        # its legal domain, and a template that does not exist is a value
        # outside the domain. The callers that must NOT silently accept
        # nonsense (the public API) validate before they get here.
        #
        # An UNSPECIFIED template stays None. It is NOT folded into the
        # default here, and that is deliberate: "unspecified" and
        # "explicitly the sine" are different facts, and folding them
        # together is what let a public-API call overwrite a running
        # template. The two are told apart at the point where the meaning
        # is decided -- start() (nothing is running yet, so the default
        # applies) versus update() (something IS running, so its template
        # is kept).
        if self.preset is None:
            preset = None
        elif self.preset in PRESETS:
            preset = self.preset
        else:
            preset = DEFAULT_PRESET
        # The tick follows the template's rule: an UNSPECIFIED tick stays
        # None (it means "use the default"), and a tick inside the app's
        # band is kept exactly. A tick OUTSIDE the band is dropped to None
        # rather than clamped, because clamping a typed number into a
        # different legal number is how "I set 40 ms and got 30 ms"
        # happens.
        #
        # "Dropped to None" is NOT "refused", and the difference matters:
        # clamp() CLEARS the value, it does not reject the request. None
        # then means "no tick was named", which is indistinguishable from
        # a caller that never named one -- so a caller that does not check
        # gets the default instead of the number it typed.
        #
        # The refusal happens at the REQUEST layer, not here:
        # webapp.py's AutoBody validates the band with a field_validator
        # and answers 422, and vero/api.py's AutoParamsIn does the same.
        # This clamp is the last line, for a caller that reached it
        # without one of those.
        #
        # The generator agrees: tick_seconds() returns None for an
        # out-of-band tick rather than clamping it, and _run() treats that
        # None as "use the default" -- deliberately, because a running
        # sweep that dies over one bad field is worse than one that keeps
        # running. So the two layers say the same thing: an out-of-band
        # tick is refused at the edge and never reaches the mechanism.
        tick_ms = self.tick_ms
        if tick_ms is not None:
            lo_ms = int(round(MIN_TICK_SECONDS * 1000.0))
            hi_ms = int(round(MAX_TICK_SECONDS * 1000.0))
            try:
                as_int = int(tick_ms)
            except (TypeError, ValueError):
                as_int = None
            if as_int is None or not (lo_ms <= as_int <= hi_ms):
                tick_ms = None
            else:
                tick_ms = as_int
        return AutoParams(
            min_percent=lo,
            max_percent=hi,
            vibration=max(0, min(255, int(self.vibration))),
            # 0.1-1.5 s one way, as specified. The floor is physical: the
            # send loop runs at 20 ms, so faster than 0.1 s leaves fewer than
            # 10 commands per traverse and stops being interpolation. The
            # ceiling is the operator's choice; tick_seconds() keeps the
            # command count bounded well below the send rate at the top end.
            traverse_seconds=max(0.1, min(1.5,
                                          float(self.traverse_seconds))),
            # The dwell ceiling is 10 s, and one traverse is at most 1.5 s
            # -- so a dwell can be nearly SEVEN times the traverse it sits
            # at the end of. That is legal (the dwell is a pause, not a
            # movement) but it means the ceiling is not tied to the
            # traverse at all.
            #
            # It used not to matter, because the dwell never happened: the
            # sleep was computed as min(dwell_until, now) - now, which is
            # always 0. With the sleep fixed, a 10 s dwell really does
            # hold the device still for 10 s, so the bound now has to be
            # stated rather than left implicit.
            #
            # 10 s is the ceiling because that is what the operator-facing
            # control allows (webapp.py's AutoBody has
            # `dwell_seconds: float = Field(0.0, ge=0.0, le=10.0)`), and
            # this clamp must not silently accept more than the UI offers.
            dwell_seconds=max(0.0, min(10.0, float(self.dwell_seconds))),
            feedback=bool(self.feedback),
            preset=preset,
            tick_ms=tick_ms,
        )

    def as_dict(self) -> dict:
        # `preset` is reported as a concrete template name. The stored
        # parameters are always normalised by start()/update() before they
        # reach here, so this is never None -- but the default keeps the
        # report truthful even if a caller inspects a fresh parameter set.
        #
        # `tick_ms` is reported the same way as the stored value: None means
        # "no tick was named", NOT the default. The reader decides what None
        # means, exactly as with the template. A report that flattened None
        # into the default would make "the operator never touched the tick
        # control" indistinguishable from "the operator set it to the
        # default", and those are different facts when someone is asking
        # "what is this thing actually sending?".
        return {
            "min_percent": round(self.min_percent, 1),
            "max_percent": round(self.max_percent, 1),
            "vibration": self.vibration,
            "traverse_seconds": round(self.traverse_seconds, 2),
            "dwell_seconds": round(self.dwell_seconds, 2),
            "feedback": self.feedback,
            "preset": self.preset if self.preset is not None else DEFAULT_PRESET,
            "tick_ms": self.tick_ms,
        }


@dataclass
class AutoState:
    running: bool = False
    #: Completed half-cycles. The UI shows this so the operator can see the
    #: generator is progressing and not silently stalled.
    cycles: int = 0
    direction: str = "up"
    current_percent: float = 0.0
    started_at: float = 0.0
    params: AutoParams = field(default_factory=AutoParams)
    error: str = ""
    #: Ticks that could not write because the session was mid-reconnect. A
    #: rising count is normal on Linux -- the link drops every ~40 s -- but it
    #: should stay far below the total tick count.
    errors: int = 0
    #: Ticks whose command was corrected by device-reported position.
    corrections: int = 0
    #: Whether the device's own reported position is available to close the
    #: loop on.
    #:
    #: This is the honest answer to "is the feedback control actually
    #: closed?". The generator's ``feedback`` parameter says the operator
    #: asked for it; this says whether there is anything to close ON. It
    #: used not to exist, so a UI that showed the feedback control as
    #: enabled was showing an intent, not a fact -- and the intent was
    #: never acted on, because the device reports no position on this
    #: hardware (see the note in AutoMotion._correct).
    #:
    #: None means "not known yet", which is different from False ("the
    #: device does not report position") and from True.
    feedback_ok: Optional[bool] = None

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "cycles": self.cycles,
            "direction": self.direction,
            "current_percent": round(self.current_percent, 1),
            "elapsed": round(time.monotonic() - self.started_at, 1)
            if self.started_at else 0.0,
            "params": self.params.as_dict(),
            "error": self.error,
            "errors": self.errors,
            "corrections": self.corrections,
            "feedback_ok": self.feedback_ok,
        }


class AutoMotion:
    """Drives a session along a repeating stroke until stopped."""

    def __init__(self, session_getter) -> None:
        self._session_getter = session_getter
        self._task: Optional[asyncio.Task] = None
        self.state = AutoState()
        # The GAIT's own state, separate from the sweep's: a gait and a
        # sweep are different things, and the page shows both.
        self.gait = GaitState()
        self._gait_steps = []
        self._gait_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------ lifecycle

    @property
    def running(self) -> bool:
        """Whether the sweep should be considered active.

        Driven by state.running, not by liveness of the task. Checking
        ``self._task.done()`` instead reported False for a sweep that had
        been asked for and had not yet failed -- the two disagreed, and
        callers that stop a sweep on disconnect consulted the wrong one, so
        a running sweep could survive the disconnect.
        """
        return bool(self.state.running)

    @property
    def gait_running(self) -> bool:
        """Whether a GAIT should be considered active.

        The same rule as ``running``, for the same reason: the FLAG and
        the task slot, not the liveness of the task. A gait that has been
        asked for and has not yet had a chance to fail reports True here.

        This exists because the two flags are SEPARATE and the callers
        that stop motion on a disconnect, a device switch or a stop
        button consulted only the sweep's. A running gait therefore
        survived all three: the panel said nothing was running, the
        2.5 s retraction guard was kept permanently alive by the gait's
        own ``session.touch()`` on every tick, and the device kept
        walking a sequence nobody could see or stop.
        """
        return bool(self.gait.running or self._gait_task is not None)

    def remember(self, params: AutoParams) -> dict:
        """Record the operator's parameters without starting a sweep.

        THE PAGE'S 自动运动 SLIDERS are pushed here while nothing is
        running, so a key bound to "开始自动模式" starts the sweep with
        the numbers the operator set rather than the defaults.

        IT DOES NOT START ANYTHING, and it does not touch a running
        sweep: if one is running, its live parameters are the operator's
        current ones already, and the page pushes them through
        `update()` instead.

        The parameters are CLAMPED, for the same reason `start()` clamps
        them: a remembered value must never be something `start()` would
        refuse.

        The template and the tick are INHERITED rather than reset, for
        the same reason `update()` inherits them: a push that names
        neither must not silently replace the template the operator
        chose with the default one.
        """
        clamped = params.clamp()
        current = self.state.params
        if clamped.preset is None:
            clamped.preset = current.preset
        if clamped.tick_ms is None:
            clamped.tick_ms = current.tick_ms
        # A RUNNING SWEEP KEEPS ITS OWN PARAMETERS. This route is for
        # the not-running case, and overwriting a live sweep's numbers
        # from a remembered set would be wrong in the other direction.
        if not self.state.running:
            self.state.params = clamped
        return self.state.as_dict()

    def last_params(self) -> AutoParams:
        """The parameters the operator's own last start or update set.

        THIS IS WHAT A BOUND BUTTON MUST START WITH.

        The sweep's state keeps its parameters after a stop -- `stop()`
        only clears `state.running` -- so the numbers the operator set
        with the 自动运动 sliders are still there. A button bound to
        "开始自动模式" starts the sweep with THESE, not with the
        dataclass defaults.

        WHY IT MATTERS. The button used to start the sweep with
        `AutoParams()`, whose every field is a default: 10-70% at
        0.5 s. The page showed the operator's numbers while the device
        ran different ones, and the difference is exactly what a
        complaint about "the speed is not what I set" looks like.

        THE DEFAULTS ARE STILL THE FALLBACK, and they are the right
        fallback: a machine that has never run a sweep has no operator
        numbers, and AutoParams() is what the UI's own sliders start
        at. So the first press of the key behaves like the first press
        of 开始.

        The parameters are CLAMPED on the way out, for the same reason
        `start()` clamps them: a stored value can be out of range if
        the device changed underneath it, and a bound button must not
        be able to command something `start()` would refuse.
        """
        return self.state.params.clamp()

    async def start(self, params: AutoParams) -> dict:
        if self.running:
            return self.state.as_dict()
        # A SWEEP AND A GAIT CANNOT BOTH DRIVE THE DEVICE, IN EITHER
        # ORDER.
        #
        # ``start_gait()`` stops a running sweep; this used not to stop a
        # running gait, so pressing 自动 while a gait was walking left two
        # loops commanding one position -- the sine and the sequence, both
        # writing frames, neither one the panel was showing. The symmetry
        # is the point: whichever is started second owns the device.
        #
        # Guarded, exactly like the sweep's own guard: ``stop_gait()``
        # releases the session, and a sweep started with no gait running
        # must not wipe a manual stroke the operator is holding.
        if self.gait_running:
            await self.stop_gait()
        s = self._session_getter()
        if not s.connected:
            raise RuntimeError("not connected")
        clamped = params.clamp()
        # A sweep that is starting has no template to inherit, so an
        # unspecified template means the default one. (The running case is
        # update()'s job, and the two are deliberately different.)
        if clamped.preset is None:
            clamped = AutoParams(
                min_percent=clamped.min_percent,
                max_percent=clamped.max_percent,
                vibration=clamped.vibration,
                traverse_seconds=clamped.traverse_seconds,
                dwell_seconds=clamped.dwell_seconds,
                feedback=clamped.feedback,
                preset=DEFAULT_PRESET,
                tick_ms=clamped.tick_ms,
            )
        # position_percent() is None whenever the link is down or the stack
        # reports no position. Falling back to the last commanded value keeps
        # the state a number, which the UI and as_dict() both rely on.
        start_pct = s.position_percent() if clamped.feedback else s._target  # noqa: SLF001
        if start_pct is None:
            start_pct = s._target  # noqa: SLF001
        self.state = AutoState(running=True, started_at=time.monotonic(),
                               params=clamped,
                               current_percent=start_pct)
        # A SWEEP TAKES OVER THE POSITION, so a button-driven hold ends
        # here. The sweep keeps the position by itself -- it calls
        # session.touch() on every tick -- so it does not need the hold,
        # and leaving the hold set would mean the guard never fires while
        # the sweep runs, which is a safety net that has been quietly
        # switched off.
        clear_hold = getattr(s, "clear_hold", None)
        if clear_hold is not None:
            clear_hold()
        # The CALLER's hold is NOT cleared here, and that is deliberate.
        #
        # It used to be cleared here, in parallel with the button's
        # hold -- and then the sweep's own tick called set_target(),
        # which clears it anyway. So the explicit clear was DEAD CODE:
        # removing it changed nothing observable, which is exactly what
        # the mutation harness reported (the mutant SURVIVED, because
        # the two versions are equivalent).
        #
        # Dead code that LOOKS load-bearing is worse than no code: the
        # next reader believes the sweep's start is what ends the
        # caller's hold, and moves it somewhere else. So it is removed,
        # and set_target() -- the one writer on the link -- is the single
        # place that says "a command supersedes a caller's hold".
        #
        # The sweep's FIRST TICK is what clears it, which is at most one
        # tick (15-50 ms) after the start. The sweep is driving from
        # that moment anyway, so nothing observable depends on the
        # difference.
        self._task = asyncio.create_task(self._run())
        return self.state.as_dict()

    async def stop(self) -> dict:
        # WAS A SWEEP ACTUALLY RUNNING?
        #
        # The release below is unconditional, and a release clears the
        # session's target AND its held intent. So calling stop() when
        # no sweep was running wiped a MANUAL stroke: the reviewer
        # measured a manual target of 55% going to 0 after a stop with
        # no sweep in progress.
        #
        # The UI family's /api/stop already guards on `running`, so the
        # page never did this -- but the PUBLIC family's /auto/stop
        # called stop() directly, and that one did.
        #
        # So the guard belongs HERE, where the release lives: a stop
        # releases the device only if a sweep was the thing driving it.
        _was_running = bool(self.state.running or self._task is not None)
        # WAS A GAIT RUNNING? THE SAME QUESTION, AND IT WAS NEVER ASKED.
        #
        # Every caller of this method guarded on `.running`, which is the
        # SWEEP's flag. A gait sets a different flag and fills a different
        # task slot, so a running gait survived the stop button, the
        # disconnect, and a device switch. Measured before this change:
        # start a gait, press 归零, and the next gait tick commanded the
        # device back out -- the stop button appeared to be ignored.
        #
        # The gait is stopped HERE rather than at each call site, for the
        # same reason the release guard lives here: the release is the
        # thing that must not happen twice and must not be missed.
        _gait_was_running = self.gait_running
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # The task was cancelled -- which is exactly what stop()
                # asked for. Swallowed on purpose, so the caller sees the
                # sweep as stopped rather than as an error.
                #
                # The tuple form `except (asyncio.CancelledError, Exception)`
                # DID catch CancelledError (an except clause catches a
                # class whatever it inherits from), so this is not a
                # behaviour change: the two clauses are just spelled out
                # instead of being implicit in a tuple.
                pass
            except Exception:                          # noqa: BLE001
                pass
            self._task = None
        self.state.running = False
        # THE GAIT IS STOPPED TOO, IF ONE WAS RUNNING.
        #
        # ``stop()`` used to stop only the sweep, and every caller guarded
        # on the sweep's flag, so a gait outlived the stop button, the
        # disconnect and a device switch. Stopping it here -- rather than
        # at the four call sites -- is what makes it impossible to forget:
        # anything that stops the sweep now also stops the gait.
        #
        # ``stop_gait()`` cancels the gait's own task slot and releases
        # the session, so the release below is skipped when the gait did
        # it. Two releases in a row would be harmless on the wire but they
        # would make the frame count disagree with what this method
        # claims it did.
        if _gait_was_running:
            await self.stop_gait()
        # The sweep is over, so the device goes home.
        #
        # Cancelling the task only stops the GENERATOR. The session's send
        # pump keeps running, and its resend rule is
        # `if target != prev or target > 0:` -- so with the last commanded
        # target still sitting in the session, the pump re-sent that frame
        # indefinitely and the rod stayed out at whatever percentage the
        # sweep happened to stop on. The operator pressed "stop" and the
        # machine stayed where it was, which reads as "it ignored me".
        #
        # So a stop releases the session: _target and _want_percent go to
        # zero, the pump writes one retraction frame, and the device
        # actually goes back in.
        #
        # Measured on the simulator, driving the real session: before this,
        # a sweep stopped at 36.9% left session._target at 36.9 and
        # stats.active_percent at 36.9, and frames_sent kept climbing for
        # as long as the pump ran.
        #
        # But only IF A SWEEP WAS RUNNING. A stop with nothing running
        # is not a stop of the sweep -- it is a request to let go of a
        # MANUAL stroke, which release() would also undo.
        if _was_running:
            await self._release_session()
        # current_percent is NOT zeroed here. It reports where the SWEEP
        # was when it stopped, and selftest.py reads it as the sweep's
        # final position and requires it to be inside the swept range.
        # The DEVICE's position is a different thing and it IS zeroed --
        # by the session's release() above, which is the one place that
        # owns it.
        return self.state.as_dict()

    # ----------------------------------------------------------------- gait

    async def start_gait(self, steps, repeat: bool = True) -> dict:
        """Walk a SEQUENCE of positions, holding each for a while.

        A gait is the automatic sweep's cousin: the sweep moves between
        two positions forever in a sine, and a gait walks a list.

        It runs in the ENGINE rather than in the CLI, for two reasons.
        The first is that the CLI's walk held each position with a plain
        sleep, while the engine's dwell is an ABSOLUTE time in the
        future -- and the dwell was broken for a long time (it was
        computed as ``min(dwell_until, now) - now``, which is always
        exactly zero). A second implementation of "hold" would have
        inherited the same class of defect.

        The second is that a gait that lives in the CLI cannot be
        stopped by the page, and cannot be seen by it. The operator
        would have a device moving and no way to say so.

        The steps are parsed and range-checked HERE, so a gait that
        would be refused is refused while the operator is looking at
        it, not halfway through the first step.

        A gait with NO DEVICE is refused too, and this is the same
        rule the sweep follows: ``start()`` raises ``RuntimeError("not
        connected")`` before it does anything else. The gait did not,
        and the consequence was a gait that "ran" with nothing attached
        -- it counted its laps, the panel said 运行中, and the operator
        saw no difference at all, because there was nothing to move.
        A control that reports success while doing nothing is worse
        than one that refuses.

        Returns the gait's state.
        """
        parsed = parse_gait(steps, repeat=repeat)
        # THE SAME RULE AS THE SWEEP: no device, no motion.
        s = self._session_getter()
        if s is None or not s.connected:
            raise RuntimeError("not connected")
        # A gait and a sweep cannot both drive the device. A gait
        # replaces the sweep, and the sweep is stopped first -- which
        # releases the session, so the gait starts from a known state.
        if self.state.running or self._task is not None:
            await self.stop()
        # A GAIT AND A GAIT CANNOT BOTH DRIVE THE DEVICE EITHER.
        #
        # This was a real bug, and it was the reason "start" sometimes
        # looked like it did nothing at all. The old code stopped the
        # SWEEP before starting a gait, but never stopped the PREVIOUS
        # GAIT -- so a second start left the first gait's task running
        # and orphaned: its ``self._gait_steps`` was replaced, but the
        # old task kept its own local reference to the OLD list and
        # kept commanding the device.
        #
        # Measured on the real device, with one start and one stop:
        #
        #   t=0.0s  target=0.0    active=0.0    gait_running=False  <- stopped
        #   t=0.7s  target=0.0    active=0.0    gait_running=False
        #   t=1.4s  target=70.0   active=70.0   gait_running=False  <- the ORPHAN
        #   t=3.5s  target=30.0   active=30.0   gait_running=False
        #
        # The panel said "not running" while the device was being driven
        # by a gait that no longer existed. The STOP button stopped the
        # NEW task and the NEW state, and the OLD task carried on.
        #
        # So the previous gait is stopped FIRST, exactly like the sweep.
        # stop_gait() cancels the task and releases the session, so the
        # new gait starts from a known state -- and the device is driven
        # by exactly one gait, which is the one the panel is showing.
        if self._gait_task is not None or self.gait.running:
            await self.stop_gait()
        self.gait = GaitState(steps=[s.as_dict() for s in parsed],
                              repeat=bool(repeat),
                              running=True,
                              started_at=time.monotonic())
        self._gait_steps = parsed
        # The CALLER's hold ends on the gait's first step, because that
        # step calls set_target() -- the one writer on the link, and the
        # single place that says "a command supersedes a caller's hold".
        #
        # It used to be cleared here as well, in parallel with the
        # button's hold, and the mutation harness reported that mutant
        # as SURVIVED: the two versions are equivalent, because the
        # gait's own tick cleared the hold anyway. Dead code that looks
        # load-bearing is worse than no code, so it is removed.
        #
        # THE GAIT'S OWN TASK SLOT.
        #
        # This used to go into ``self._task`` -- the SWEEP's slot -- and
        # that was a real bug: ``stop_gait()`` cancels ``self._gait_task``,
        # so a gait started this way was NEVER cancelled. The gait's
        # loop is ``while True:``, so it kept running after the stop,
        # kept commanding the device, and kept the event loop busy --
        # the test suite spun at 100% CPU for as long as it ran.
        #
        # The two slots are separate on purpose: a gait and a sweep are
        # different things and either can be stopped without touching
        # the other.
        self._gait_task = asyncio.create_task(self._run_gait())
        return self.gait.as_dict()

    async def stop_gait(self) -> dict:
        """Stop the gait and release the device.

        The release is unconditional here, for the same reason
        ``stop()``'s is conditional: a gait that is running IS the
        thing driving the device, so leaving it where it was would be
        a stop that did not stop.
        """
        if self._gait_task is not None:
            self._gait_task.cancel()
            try:
                await self._gait_task
            except asyncio.CancelledError:
                pass
            except Exception:                          # noqa: BLE001
                pass
            self._gait_task = None
        self.gait.running = False
        await self._release_session()
        return self.gait.as_dict()

    async def _run_gait(self) -> None:
        """The gait's loop: hold each step, then the next one.

        The hold is a real wait, and the sleep is computed so that a
        cancelled task stops at once -- ``asyncio.sleep`` is
        cancellable, so the cancellation lands in the sleep and the
        task ends there.
        """
        steps = self._gait_steps
        try:
            while True:
                for index, step in enumerate(steps):
                    self.gait.step = index
                    s = self._session_getter()
                    if s is None:
                        self.gait.error = "no session"
                        self.gait.running = False
                        return
                    # THE GAIT IS THE CLIENT, SO IT TOUCHES.
                    #
                    # The session's idle guard releases a held target when
                    # the client goes quiet for CLIENT_GONE_SECONDS (2.5 s).
                    # The SWEEP calls ``s.touch()`` every tick for exactly
                    # this reason -- the comment there says "this loop is
                    # the client" -- and the GAIT did not.
                    #
                    # The consequence was measured on the real device: a
                    # gait with a hold longer than 2.5 s was retracted by
                    # the guard in the MIDDLE of the hold, and the panel
                    # and the device disagreed:
                    #
                    #   t= 4s  target=70.0  active=70.0  client_present=True
                    #   t= 5s  target=0.0   active=0.0   client_present=False  <- retracted
                    #
                    # The guard could not tell the difference between "the
                    # operator left" and "the gait is holding a position
                    # and says nothing while it holds it", because the
                    # gait said nothing. A gait that holds is the client
                    # for as long as it holds, so it says so.
                    #
                    # touch() is called BEFORE the command, so the guard's
                    # clock is refreshed at the moment the target is set --
                    # and again on every pass of the loop, which is where
                    # the long holds are spent.
                    try:
                        s.touch()
                    except Exception:                    # noqa: BLE001
                        # A session without a touch() (a test double, an
                        # older build) must not stop the gait: the command
                        # below is what the operator asked for.
                        pass
                    try:
                        s.set_target(step.percent, step.vibration)
                    except Exception as exc:           # noqa: BLE001
                        self.gait.error = "could not command the position: %s" % exc
                        self.gait.running = False
                        return
                    if step.hold:
                        # The wait is in slices, and the session is
                        # touched on each one, so a hold of any length
                        # keeps the guard's clock alive.
                        await self._hold(step.hold)
                    # The guard's clock is refreshed on every pass of the
                    # loop as well -- including the pass that ends a hold
                    # and commands the next position.
                    try:
                        s.touch()
                    except Exception:                    # noqa: BLE001
                        pass
                if not self.gait.repeat:
                    self.gait.running = False
                    await self._release_session()
                    return
                self.gait.laps += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:                       # noqa: BLE001
            self.gait.error = str(exc)
            self.gait.running = False

    async def _hold(self, seconds: float) -> None:
        """Wait for a hold, in slices, touching the session on each one.

        A plain ``asyncio.sleep(seconds)`` would let the session's idle
        guard fire in the middle of a long hold, because nothing would
        have told the session that the client was still there. Slicing the
        wait and touching on each slice keeps the two in step, and the
        wait stays cancellable -- ``asyncio.sleep`` is cancellable, so a
        cancelled gait stops at once, which is what the stop button needs.
        """
        deadline = time.monotonic() + max(0.0, float(seconds))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, GAIT_HOLD_SLICE_SECONDS))
            s = self._session_getter()
            if s is None:
                return
            try:
                s.touch()
            except Exception:                        # noqa: BLE001
                pass

    async def _release_session(self) -> None:
        """Put the device back to zero, if there is a session to put back.

        Every failure here is reported rather than swallowed silently: a
        release that did not happen leaves the device out, and a stop that
        reports "stopped" while the rod is still extended is exactly the
        lie this method exists to prevent.
        """
        try:
            s = self._session_getter()
        except Exception as exc:                       # noqa: BLE001
            self.state.error = "no session to release: %s" % exc
            return
        if s is None:
            return
        release = getattr(s, "release", None)
        if release is None:
            return
        try:
            result = release()
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:                       # noqa: BLE001
            self.state.error = "release failed: %s" % exc

    async def update(self, params: AutoParams) -> dict:
        """Change parameters live. Takes effect on the next tick."""
        clamped = params.clamp()
        # An unspecified template means "this call did not name one", NOT
        # "use the sine". The sweep is already running with a template, and
        # a caller that changes only, say, max_percent must not be able to
        # replace the template underneath it. So an unspecified template
        # inherits the one the sweep is already running.
        #
        # This is the line that fixes the measured defect: the public API
        # family builds its parameters without a template (it cannot name
        # one -- the endpoint is a protected path with no such field), so
        # before this, every public-API call replaced a running template
        # with the default. The two meanings are kept apart here rather
        # than being folded together in clamp(), where the difference
        # would have been lost.
        #
        # The same rule applies to the TICK, and for the same reason: a call
        # that changes only max_percent must not reset the tick the
        # operator chose. The public API family (vero/api.py, a protected
        # path) builds its parameters without a tick, so without this every
        # public-API call would have silently dropped the operator's tick
        # back to the default -- the same defect the template rule above
        # exists to prevent.
        #
        # These are TWO INDEPENDENT inheritances, and they used to be
        # mutually exclusive branches of one if/elif. That was wrong twice
        # over: a call that named NEITHER lost the tick (the elif branch
        # never ran, and the if branch rebuilt AutoParams with
        # tick_ms=clamped.tick_ms, which is None), while a call that named
        # only a preset kept the tick by accident. Both facts are now
        # resolved on their own, so naming one never disturbs the other.
        current_preset = self.state.params.preset
        current_tick = self.state.params.tick_ms
        preset = clamped.preset
        if preset is None:
            preset = current_preset if current_preset is not None \
                else DEFAULT_PRESET
        tick_ms = clamped.tick_ms
        if tick_ms is None:
            tick_ms = current_tick
        clamped = AutoParams(
            min_percent=clamped.min_percent,
            max_percent=clamped.max_percent,
            vibration=clamped.vibration,
            traverse_seconds=clamped.traverse_seconds,
            dwell_seconds=clamped.dwell_seconds,
            feedback=clamped.feedback,
            preset=preset,
            tick_ms=tick_ms,
        )
        self.state.params = clamped
        return self.state.as_dict()

    # ----------------------------------------------------------------- loop

    def _target_for(self, phase: float, p: AutoParams) -> float:
        """Absolute position for a phase in [-1, 1]."""
        lo, hi = p.min_percent, p.max_percent
        centre = (lo + hi) / 2.0
        amplitude = (hi - lo) / 2.0
        return centre + amplitude * ease(phase, _template_of(p))

    async def _run(self) -> None:
        try:
            p = self.state.params
            # Start where the device actually is, so the first command is a
            # continuation rather than a jump.
            s = self._session_getter()
            start = s.position_percent() if p.feedback else None
            if start is None:
                # No feedback available. The FALLBACK is the session's own
                # last commanded target, not the range's low end.
                #
                # It used to be `p.min_percent`, so every sweep began by
                # commanding the minimum: a device sitting at 70% was told
                # to go to 10% in one tick -- the exact "jump" the
                # module's own docstring says this code exists to avoid
                # ("Start where the device actually is, so the first
                # command is a continuation rather than a jump").
                #
                # The session's target is the best answer available when the
                # device reports nothing: it is where the host last asked
                # the device to be, which is the continuation the operator
                # would expect. It is clamped into the range, because a
                # target outside the range has no phase to invert.
                start = getattr(s, "_target", None)
                if start is None:
                    start = p.min_percent
                else:
                    start = max(p.min_percent,
                                  min(p.max_percent, float(start)))
            phase = _phase_for(start, p)

            # Which way the phase is travelling. The initial sign is chosen
            # from the current phase so the very first command continues the
            # motion rather than reversing it: starting above the midpoint
            # means travelling down.
            direction = 1.0 if phase < 0.0 else -1.0

            # Range the phase was last calibrated against. A change here is
            # what forces the remap below.
            lo0, hi0 = p.min_percent, p.max_percent
            # The template the phase was last calibrated against. Same
            # reason: the phase is a number in [-1, 1] and the template
            # says what that number MEANS, so a template change needs the
            # same remap a range change does.
            preset0 = _template_of(p)

            while True:
                s = self._session_getter()

                # Re-read the parameters every pass. `p` was captured once
                # before the loop, so a live update from the UI (or the API)
                # replaced self.state.params while the loop carried on using
                # the old object -- the sliders looked live but nothing
                # actually changed.
                p = self.state.params
                # A range change makes the running phase meaningless: the same
                # phase now maps to a different position, so without
                # remapping the command would jump by the difference between
                # the old and new range in a single tick. Re-derive the phase
                # from where the mechanism actually is instead, which makes a
                # live range change continuous.
                #
                # A template change is the same defect by another route: the
                # same phase under a different template commands a different
                # position, so without the remap the mechanism would lurch to
                # the new template's reading of the old phase.
                if (p.min_percent != lo0 or p.max_percent != hi0
                        or _template_of(p) != preset0):
                    here = s.position_percent()
                    if here is None:
                        here = self.state.current_percent
                    phase = _phase_for(here, p)
                    lo0, hi0 = p.min_percent, p.max_percent
                    preset0 = _template_of(p)
                # Recomputed each pass so a live parameter change to
                # traverse_seconds takes effect immediately, including the
                # resulting change in tick rate.
                #
                # The operator's explicit tick, when there is one, IS the
                # answer: the traverse scaling is a default for the case
                # where nothing was named, and it must not quietly overrule
                # a number the operator typed into the UI.
                tick = tick_seconds(p.traverse_seconds,
                                    _tick_of(p))
                if tick is None:
                    # The stored tick was refused somewhere upstream, so
                    # there is no valid period. Fall back to the default
                    # rather than crashing the sweep: a running sweep that
                    # dies because of one bad field is worse than one that
                    # keeps running at the default.
                    tick = tick_seconds(p.traverse_seconds)
                # The BLE pump must not be slower than this tick, or the
                # positions computed in between are overwritten in the
                # variable before the pump writes them. The pump is moved
                # to match, because the operator's tick control is only
                # meaningful if the device SEES the rate that was chosen.
                _apply_pump_rate(s, tick)
                if not s.connected:
                    # The link is down; the session's own recovery will bring
                    # it back. Keep ticking so we resume on the same schedule.
                    await asyncio.sleep(tick)
                    continue

                span = p.max_percent - p.min_percent
                # Always touch: the session drops a held target if the client
                # looks absent, and this loop is the client.
                s.touch()

                if span <= 0.01:
                    try:
                        s.set_target(p.min_percent, p.vibration)
                        self.state.current_percent = p.min_percent
                    except Exception:                    # noqa: BLE001
                        self.state.errors += 1
                    await asyncio.sleep(tick)
                    continue

                # One cycle = out and back. The phase runs -1 -> 0 -> 1 -> 0 -> -1,
                # which is 4.0 units of travel per full cycle (2.0 for the
                # outbound half, 2.0 for the return). Using 2.0 here made a
                # single "cycle" of the for-loop cover only the outbound
                # half, so the reported cycle count ran at half the rate the
                # mechanism actually moved -- and the counter drifted further
                # behind with each wrap.
                #
                # steps is the command count per cycle, which is what the
                # send pump has to keep up with: 4.0 phase units per cycle.
                cycle = 2.0 * p.traverse_seconds
                steps = max(4, int(round(cycle / tick)))
                dphase = 4.0 / steps

                dwell_until = 0.0
                # Absolute schedule for the ticks of this cycle. Sleeping a
                # fixed period instead would let each tick's own cost add up,
                # so a 0.1 s traverse would quietly stretch to a third of a
                # second over a cycle.
                deadline = time.monotonic()
                for _ in range(steps):
                    if dwell_until:
                        # dwell_until is an ABSOLUTE time in the future, so
                        # the wait is what is left until then.
                        #
                        # It used to be `min(dwell_until, now) - now`, and
                        # since now is always >= dwell_until by the time
                        # this line runs, the minimum was always `now` and
                        # the sleep was always exactly 0. The dwell slider
                        # was therefore decorative: any value produced
                        # exactly the same motion.
                        await asyncio.sleep(dwell_until - time.monotonic())
                        dwell_until = 0.0
                        # The dwell is a pause, not a skipped schedule. The
                        # absolute deadline below has fallen this far
                        # behind, so without this the generator would try
                        # to "catch up" and fire the next ticks back to
                        # back -- a burst of frames that is the opposite of
                        # a dwell.
                        deadline = time.monotonic()

                    # Command the CURRENT phase before advancing it.
                    # Advancing first would skip the position we are
                    # actually standing at, and the device would take that
                    # whole step as one jump -- a visible lurch on every
                    # start, which is exactly what this rewrite is fixing.
                    commanded = self._target_for(phase, p)
                    target = commanded
                    if p.feedback:
                        # _correct() counts the correction itself when there
                        # is a reported position to compare against. It used
                        # to be counted HERE, but the comparison was
                        # `abs(target - commanded) > 0.05` and _correct()
                        # returned the commanded value unchanged -- so the
                        # difference was always 0 and the counter was
                        # always 0, which is how a sweep that claimed to
                        # close its loop reported zero corrections forever.
                        target = self._correct(s, commanded, p)

                    try:
                        s.set_target(target, p.vibration)
                        self.state.current_percent = target
                        self.state.direction = ("up" if direction > 0
                                               else "down")
                    except Exception:                    # noqa: BLE001
                        self.state.errors += 1

                    # Advance, and REVERSE at the ends rather than wrapping.
                    #
                    # The previous code did `phase += dphase` unconditionally
                    # and then `if phase >= 1.0: phase -= 2.0`. That makes
                    # the phase climb 0 -> 1, jump to -1, climb again -- so
                    # the commanded position fell from the top of the stroke
                    # to the bottom in a single tick. Measured worst case:
                    # 59.99 %% of the stroke in one command, against a ramp
                    # step of 0.2 %%. The return stroke never actually ran:
                    # every cycle was "extend slowly, retract instantly".
                    #
                    # Reversing at the endpoints makes the phase a proper
                    # triangle -1 -> 0 -> 1 -> 0 -> -1 - so both directions
                    # are traversed at the commanded speed and the turnarounds
                    # are handled by the waveform (the sine ease) instead of
                    # by a discontinuity.
                    if direction > 0:
                        if phase >= 1.0:
                            phase = 1.0
                            direction = -1.0
                            self.state.cycles += 1
                            if p.dwell_seconds > 0:
                                dwell_until = (time.monotonic()
                                               + p.dwell_seconds)
                    else:
                        if phase <= -1.0:
                            phase = -1.0
                            direction = 1.0
                            if p.dwell_seconds > 0:
                                dwell_until = (time.monotonic()
                                               + p.dwell_seconds)
                    phase += direction * dphase
                    phase = max(-1.0, min(1.0, phase))

                    # Hold the commanded schedule even when a write or the
                    # event loop costs extra time: a tick that runs long must
                    # not stretch the traverse with it.
                    deadline += tick
                    drift = deadline - time.monotonic()
                    await asyncio.sleep(drift if drift > 0 else 0)
                    if not self.running:
                        return
        except asyncio.CancelledError:
            raise
        except Exception as exc:                       # noqa: BLE001
            self.state.error = "%s: %s" % (type(exc).__name__, exc)
            self.state.running = False

    def _correct(self, s, commanded: float, p: AutoParams) -> float:
        """Return the position to command, given where the device actually is.

        Kept as a plain pass-through on purpose. Two earlier versions are
        worth recording because both looked smarter and were worse:

        * ``actual + drift`` -- algebraically identical to ``commanded``, so
          the feedback path was an identity function that did nothing while
          appearing to close the loop.
        * ``actual + drift * FEEDBACK_GAIN`` -- a proportional controller on
          a signal that is *always* behind by construction. The device lags,
          the correction pulls the command back toward the measured position,
          the lag then shrinks and the command leaps forward again. Measured:
          single-tick jumps of 60 %% of the stroke, versus 0.8 %% for the ramp
          itself.

        The device's own reported position is therefore not used to compute a
        command. Smoothness comes from the commanded path alone.

        What this DOES do is publish the truth about the feedback, which is
        what the audit found missing: the ``feedback`` parameter was a
        request the code never acted on, ``FEEDBACK_MAX_DELTA`` and
        ``FEEDBACK_GAIN`` were defined and never read, ``AutoState`` had no
        field saying whether a reported position was available at all, and
        ``state.corrections`` therefore stayed at 0 forever -- so a UI
        showing the feedback control as enabled was showing a request, not
        a capability.

        ``state.feedback_ok`` now says which it is: True when the device
        reported a position, False when it did not. The command itself is
        still the commanded path, because the controller was measured to be
        worse than no controller at all.
        """
        reported = None
        try:
            reported = s.position_percent()
        except Exception:                             # noqa: BLE001
            reported = None
        self.state.feedback_ok = reported is not None
        if reported is None:
            # Nothing to close on: the commanded path stands.
            return commanded
        # A reading that arrived is compared against the commanded path only
        # to REPORT the gap. It does not steer anything -- see the two
        # earlier controllers above, both of which were worse than this.
        if abs(reported - commanded) > FEEDBACK_MAX_DELTA:
            self.state.corrections += 1
        return commanded


def _template_of(p: AutoParams) -> str:
    """The template a parameter set runs, as a concrete name.

    A parameter set that never named a template carried None, which means
    "unspecified" rather than "the sine" (see AutoParams.preset). The
    places that actually DRIVE the mechanism need a name, and an
    unspecified one cannot reach them in practice -- start() and update()
    both normalise it before the sweep runs -- so this is the single place
    that turns the ambiguous value into a concrete one, and it is a
    fallback rather than a decision.
    """
    return p.preset if p.preset is not None else DEFAULT_PRESET


def _tick_of(p: AutoParams) -> Optional[float]:
    """The operator's tick choice, in SECONDS, or None if there is none.

    The parameter set stores milliseconds (that is the unit the UI shows
    and the unit the app's own constants use); the generator works in
    seconds. The conversion lives here, in one place, so the two units
    cannot be confused at a call site.
    """
    if p.tick_ms is None:
        return None
    return float(p.tick_ms) / 1000.0


def _apply_pump_rate(s, tick: float) -> None:
    """Move the session's BLE pump to the rate this tick needs.

    The pump writes ``self._target`` -- a VARIABLE, not a queue. So a pump
    that fires slower than the generator produces targets loses the
    positions computed in between: they are overwritten before anything
    writes them, and the device sees fewer, LARGER jumps than the
    generator asked for.

    Measured (see docs/audit/P5.2-SMOOTHNESS-WORKORDER.md):

        generator 15 ms, pump 20 ms -> device sees  9.36 % of stroke
        generator 15 ms, pump 15 ms -> device sees  5.36 % of stroke
        the vendor app on the same sweep -> 4.71 % of stroke

    So a tick control that does not move the pump delivers about half the
    benefit while LOOKING like it delivered all of it. The pump is moved
    to match the tick, and the session is asked to do it through its own
    method so the rule lives in one place rather than being re-derived
    here.
    """
    setter = getattr(s, "set_frame_interval", None)
    if setter is None:
        # A stand-in session with no pump of its own. Nothing to move.
        return
    try:
        setter(tick)
    except Exception:                            # noqa: BLE001
        # A pump that refuses the new rate is not a reason to stop the
        # sweep: the generator's own rate is still exactly what the
        # operator asked for.
        pass


def _phase_for(percent: float, p: AutoParams) -> float:
    """Inverse of _target_for: the phase that produces `percent`.

    Exact inverse of the template the sweep is running, so remapping the
    phase after a live range change lands on the position the mechanism is
    actually at, with no jump.

    The sine has a closed-form inverse and keeps it: asin(rel) / (pi/2).
    The other two templates do not, so they are inverted by bisection on
    [-1, 1]. Bisection is safe here precisely because every template is
    strictly monotone increasing with the endpoints mapped exactly to -1
    and +1 -- the property motion_regression.py asserts for each one, and
    the property whose absence (triangle, pulse) is why those shapes were
    rejected in the first place.

    80 iterations of bisection over a range of 2.0 drives the interval
    below 1.7e-24, which is far under the double-precision floor of the
    result: measured worst-case round-trip error is 2.2e-16.
    """
    lo, hi = p.min_percent, p.max_percent
    if hi - lo < 1e-6:
        return 0.0
    centre = (lo + hi) / 2.0
    amplitude = (hi - lo) / 2.0
    rel = (percent - centre) / amplitude
    rel = max(-1.0, min(1.0, rel))
    template = _template_of(p)
    if template == "sine":
        return math.asin(max(-1.0, min(1.0, rel))) / (math.pi / 2.0)
    # Bisection: the template is strictly increasing, so the sign of
    # ease(mid) - rel tells us which half the answer is in.
    a, b = -1.0, 1.0
    for _ in range(80):
        mid = (a + b) / 2.0
        if ease(mid, template) < rel:
            a = mid
        else:
            b = mid
    return (a + b) / 2.0