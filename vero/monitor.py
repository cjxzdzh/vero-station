"""Live telemetry monitor for the Amorlink Vero.

The device emits 0x29 frames only after the host sends the telemetry command
(0x36, which reaches the wire as 0x54). Each frame carries fifteen values:
FIVE position slots sampled three times, de-interleaved with stride 5
(n1.java:198).

Values are 12-bit little-endian words (v1.e(): four hex characters per step,
``& 0x0FFF``), so the range is 0..4095 -- not 0..255. An earlier version of
this module read single bytes, which put the constants 0x0A and 0x0D into the
high byte and produced values around 2560 and 3328.

Position comes from the device's own quantiser (v1.smali d(ArrayList)): scan
the slots from the deepest index down, stop at the first above 800, and report
that index x 20%. Touching several slots reports the deepest one, so the
reading is a five-step position, not a magnitude. The value inside a slot says
how firmly that step is engaged -- it is not a force reading.

The device calibrates itself once after connecting: seven frames are averaged
per slot and 200 is added as a dead zone, which is why an idle surface reads
around 200 rather than 0.

Frames arrive at about 33 per second. That is the firmware sampling at 100 Hz
and packing three samples per notification, so the host cannot raise it.

A rolling window keeps the curve drawable without holding every frame.
"""
from __future__ import annotations

import time
from collections import deque

from .changepoints import ChangePointDetector
from .buttons import decode_button_event
from .objectprofile import ObjectProfile
from .pressure import decode_frame
from .reassembly import Reassembler
from .vibration import VibrationCalibrator
from .reassembly import Reassembler

#: Display names per slot. These are positions along the travel, not forces;
#: the numbers are what the slot read when that step was engaged.
SLOT_NAMES = ('位置1', '位置2', '位置3', '位置4', '位置5')
PRIMARY = 0
SECONDARY = 1

#: Frames between keep-alive requests. The App buffers 7 and then re-issues
#: command 52 (n1.java:160-162), so this cadence is not a guess.
BATCH_FRAMES = 7

#: How many button events the monitor keeps.
#:
#: The page shows "which key did I just press", so the history only has to
#: be deep enough for a person to look at it -- and bounded, because a
#: device that spams a key (a stuck button, a bouncing contact) must not
#: grow the buffer without limit.
BUTTON_EVENT_HISTORY = 50


#: The telemetry-start command, as it appears on the wire.
#:
#: ``r0.smali:322`` passes the integer 0x36 to ``T0()``. ``K0()`` then formats
#: it with ``String.valueOf(int)`` -- DECIMAL, per ``r1.smali:26101`` -- giving
#: the string "54", so the byte on the wire is **0x54**.
#:
#: Two traps produced opposite wrong answers here, both worth recording:
#:   * reading the smali constant 0x36 as the wire byte (it is not; it is the
#:     pre-format integer)
#:   * treating "52" (the hardcoded string at r1.smali:43451, used by the
#:     motion-mode path) as the telemetry command
#:
#: Frame: A5 5A 08 B0 3D 54 21 ED
CMD_TELEMETRY_STREAM = 0x36

# ------------------------------------------------------- position decoding
#
# The 15 decoded values are FIVE position slots sampled three times, not
# fifteen different quantities: n1.java consumes them with stride 5.
#
# v1.smali ``d(ArrayList)`` turns them into a position:
#
#     threshold = isVero ? 800 : 200            # 0x320 / 0xc8
#     for i = size-1 .. 0:                       # high slot index first
#         if values[i] > threshold: found = i; break
#     return clamp(found * 2000, 0, 10000)       # 0x7d0 / 0x2710
#
# So the reading is a 5-step position quantiser -- 0/20/40/60/80/100% -- and
# the raw magnitude inside a slot says how firmly that step is engaged, not
# how deep the travel is. Taking only slot 0, as this module used to, threw
# the position away entirely.

SLOT_COUNT = 5
POSITION_STEP = 2000            # 0x7d0
POSITION_MAX = 10000            # 0x2710
VERO_SLOT_THRESHOLD = 800       # 0x320
LEGACY_SLOT_THRESHOLD = 200     # 0xc8


def locate(values, threshold: int = VERO_SLOT_THRESHOLD) -> tuple[int, int | None]:
    """Five slots -> (position 0..10000, slot index or None).

    Transcribed from v1.smali: the scan runs from the highest slot index down
    and stops at the first slot above the threshold, so a reading that lights
    several slots reports the deepest one.
    """
    for i in range(min(len(values), SLOT_COUNT) - 1, -1, -1):
        if values[i] > threshold:
            return min(POSITION_MAX, i * POSITION_STEP), i
    return 0, None


class PressureMonitor:
    """Rolling-window consumer of 0x29 telemetry frames."""

    def __init__(self, window: int = 400, max_seconds: float = 65.0,
                 assumed_hz: float = 60.0):
        # The public API has to answer "the last 60 seconds", and a fixed
        # sample count cannot promise that: at the measured 60 Hz, 60 s needs
        # 3600 samples, so the old window of 400 held under 7 seconds and a
        # 60 s request silently returned a partial series. Size the buffer from
        # the requested span instead, with headroom for a device that streams
        # faster than assumed.
        need = int(max_seconds * assumed_hz * 1.5) + 1
        self.window = max(window, need)
        self.samples: deque = deque(maxlen=self.window)
        self.frames_seen = 0
        self.last_frame_at = 0.0
        self.peak = 0
        self.press_events = 0
        self._was_pressed = False
        # Lowest value seen while the surface is untouched. A press is judged
        # against this rather than a hard-coded ceiling, because the decoded
        # range depends on the device's own payload constants.
        self.idle_floor = None
        # Reported for reference only. Whether a step is engaged is decided by
        # the device's own 800 threshold in locate(), not by an amplitude
        # heuristic, so nothing in this module depends on this constant.
        self.press_delta = 400
        # Position 0..10000, and which slot supplied it (None when untouched).
        self.position = 0
        self.active_slot = None
        # Arrival-time histogram over a short window. The frame rate is the
        # single most useful number for judging whether the link is the limit
        # or the device is: 33 Hz lines up with a 30 ms BLE connection
        # interval, which means the host cannot raise it by polling harder.
        self._intervals = deque(maxlen=200)
        # Rolling window for the resting-value estimate.
        self._channel0 = deque(maxlen=1200)
        self._floor_samples = 200
        # Whether command 54 has been sent. Set optimistically by the caller
        # and corrected by the first frame actually arriving, because a write
        # that returns success is not proof the device started streaming.
        self.armed = False
        # The COMMANDED TRAVEL over time. The device's telemetry frames say
        # where the surface is TOUCHED; they never say how far the rod was
        # commanded out. Those are two different readings, and only the host
        # knows the second one -- so the app installs a provider (see
        # set_travel_provider) and the monitor samples it once per frame,
        # on the same clock as the frame itself.
        #
        # It is a separate deque rather than extra columns on `samples`
        # because the two are not guaranteed to be the same length: a
        # provider that is not installed, or that returns nothing, leaves
        # this one shorter. Sharing the maxlen keeps the memory bound the
        # same either way.
        self._travel_provider = None
        self._travel: deque = deque(maxlen=self.window)
        # The OBJECT PROFILE: what is inside the tube, measured from the
        # same frames. It has its own window, because the window it needs
        # (a baseline over several seconds) is a different length from
        # the one the trace needs.
        self._object = ObjectProfile()
        # Frames since the last keep-alive request. The App re-issues command
        # 52 every 7 frames (n1.java:161) and that is what sustains the
        # stream, so the same cadence is reproduced here.
        self.since_keepalive = 0
        self.keepalive_every = BATCH_FRAMES
        # True once the burst has been answered and no further command 52 is
        # owed. The App sets the equivalent flag (f6353a0) after its single
        # follow-up and never asks again.
        self.stream_confirmed = False
        # Set by DeviceSession so the monitor can request the next burst.
        self.keepalive_hook = None
        # Every notification the device sent, counted and fingerprinted. The
        # pressure decoder only accepts 41-byte A55A/A0 frames, so a zero here
        # with a non-zero notify_others means the device is speaking but not in
        # the format this build expects -- and a zero in both means it is not
        # speaking at all. Those need completely different fixes.
        self.notify_total = 0
        self.notify_frames = 0
        self.notify_other = 0
        self.other_signatures = {}
        # Frame-integrity counters. Measured, never enforced: see
        # decode_frame()'s docstring for why a failed CRC cannot yet be used to
        # drop a sample. A device that reports crc_fail without any link trouble
        # is producing corrupt frames, and a device that reports fragments is
        # telling us the 20-byte ATT payload is being reassembled somewhere.
        self.crc_fail_count = 0
        self.fragment_count = 0
        # THE REASSEMBLER.
        #
        # A telemetry frame is longer than one ATT payload can carry
        # (the link's MTU is 23 bytes), so it arrives in two
        # notifications -- and the project used to decode each half as
        # if it were a whole frame. Both halves then failed the frame's
        # own CRC, because the CRC is over the WHOLE frame, and both
        # were counted as "the frame did not verify".
        #
        # The app does the same thing (n1.java processes every
        # notification independently and nothing reassembles fragments
        # on either side), but the app has no recorder. This project
        # does, and the recorder was writing half-frames to disk.
        self._reassembler = Reassembler()
        # THE VIBRATION CALIBRATOR, and the last byte it read.
        #
        # The device's telemetry frames carry the vibration byte the
        # device is REPORTING -- the same byte the A0 control frame
        # carries on the way out. The project decoded it and threw it
        # away, so a vibration level was a COMMAND and never a
        # MEASUREMENT: "you asked for 80%" and "the device delivered
        # 62% of its range" were indistinguishable.
        #
        # The calibrator measures the device's own resting reading and
        # scales the rest against it, so the two can be told apart. A
        # device's resting reading is not zero, and it differs between
        # units and firmware versions, so it is measured rather than
        # assumed.
        self._vibrator = VibrationCalibrator()
        self._last_vibration = None
        # The change-point detector. It is fed the primary channel once
        # per frame, in arrival order, and it owns no device state of its
        # own -- the change-point detection is a judgement about the
        # pressure curve, and the curve is the monitor's.
        #
        # The sensitivity is not settable here. The detector runs at its
        # default threshold -- the CHART's change-point marks are still
        # drawn from it -- but nothing can set it.
        self.change_detector = ChangePointDetector()
        # THE DEVICE'S OWN BUTTONS.
        #
        # The vendor app dispatches button events inside the same A55A/A0
        # branch the telemetry frames use, and pressure.decode_frame()
        # DROPS them on purpose (its EVENT_DATA_IDS check) because a
        # decoder that only tests byte 3 would read a key press as a
        # pressure sample. The drop was right, but it threw the event
        # away, so the device's own keys could never drive anything.
        #
        # So the monitor recognises what pressure.py discards, records it
        # here, and lets the caller act on it. A button frame is NOT a
        # telemetry frame: it does not count towards frames_seen, it does
        # not move the curve, and it does not arm the sensors.
        self.button_events: deque = deque(maxlen=BUTTON_EVENT_HISTORY)
        self.button_count = 0
        # How many of the "other" frames were button frames. Kept apart
        # from button_count so the notify split stays exact: total ==
        # telemetry + other, and "other" includes the buttons.
        self.notify_buttons = 0
        # Called with the decoded event dict when a button frame arrives.
        # The monitor does not know how an action is carried out -- that
        # is the web layer's job -- so the hook is installed from outside.
        self.button_hook = None

    def set_button_hook(self, hook) -> None:
        """Install the callable that runs a button's bound action.

        The hook is called with the decoded event dict. It may be a
        coroutine function; the monitor does not await it (it is called
        from a synchronous BLE notify callback), so the web layer's hook
        schedules its own task.
        """
        self.button_hook = hook

    def _record_button(self, event: dict) -> None:
        """Record one button event and hand it to the hook.

        The hook is called DEFENSIVELY: a hook that raises must not take
        the notification with it, because the notification is also the
        thing the telemetry path needs. A key press that fails to run its
        action is a missed action, not a lost frame.
        """
        now = time.time()
        self.button_count += 1
        self.button_events.append({
            "at": round(now, 3),
            "unix": now,
            "button": event.get("button"),
            "data_id": event.get("data_id"),
            "raw": event.get("raw"),
        })
        hook = self.button_hook
        if hook is None:
            return
        try:
            hook(event)
        except Exception:                             # noqa: BLE001
            # A failing hook is a bug in the binding path, not in the
            # telemetry path. The event is already recorded, so the
            # operator can still see WHICH key failed.
            pass

    def button_state(self) -> dict:
        """The recent button events, newest last, plus the counters.

        Two counts, and they are DIFFERENT numbers:

        * ``count`` -- every button frame since the link came up. It is
          a counter, so it does not fall when the history is cleared.
        * ``pending`` -- how many events are in ``events`` RIGHT NOW.

        Reporting one number for both would be a lie in one direction or
        the other: a caller that wanted "how many key presses has this
        link seen" and a caller that wanted "how many are still in the
        list" would get the same answer, and one of them would be wrong.
        """
        return {
            "count": self.button_count,
            "pending": len(self.button_events),
            "events": [dict(e) for e in self.button_events],
            "last": dict(self.button_events[-1]) if self.button_events else None,
        }

    def clear_buttons(self) -> int:
        """Forget the recent button events. Returns how many were dropped."""
        n = len(self.button_events)
        self.button_events.clear()
        return n

    def batch_due(self) -> bool:
        """True exactly once: when the first burst of frames is complete.

        The App sends command 52 at the end of the connect handshake and then
        once more when the buffer reaches 7 frames (n1.java:160-162), after
        which ``f6353a0`` is set and the branch is never taken again. An
        earlier version of this code re-sent on every batch, which at 20 Hz
        meant a reset command every 350 ms -- the stream never got going and
        the panel showed nothing at all.
        """
        if not self.armed:
            return False
        if self.stream_confirmed:
            return False
        return self.since_keepalive >= self.keepalive_every

    def confirm_stream(self) -> None:
        """Stop requesting more. Called once the burst has been answered."""
        self.stream_confirmed = True
        self.since_keepalive = 0

    def _idle_floor(self):
        """Resting value of channel 0: the 10th percentile of the window.

        A low percentile rather than the minimum, so a single stray sample
        cannot move the baseline and a genuine press cannot redefine it.
        """
        if len(self._channel0) < self._floor_samples:
            return None
        ordered = sorted(self._channel0)
        return ordered[max(0, int(len(ordered) * 0.10))]

    def reset(self) -> None:
        self.samples.clear()
        self._travel.clear()
        self._channel0.clear()
        self.frames_seen = 0
        self.last_frame_at = 0.0
        self.peak = 0
        self.press_events = 0
        self._was_pressed = False
        # The change-point detector is reset with everything else: a new
        # link is a new curve, and the old curve's noise level does not
        # describe the new one.
        self.change_detector.reset()
        # Lowest value seen while the surface is untouched. A press is judged
        # against this rather than a hard-coded ceiling, because the decoded
        # range depends on the device's own payload constants.
        self.idle_floor = None
        # Reported for reference only. Whether a step is engaged is decided by
        # the device's own 800 threshold in locate(), not by an amplitude
        # heuristic, so nothing in this module depends on this constant.
        self.press_delta = 400
        # Position 0..10000, and which slot supplied it (None when untouched).
        self.position = 0
        self.active_slot = None
        # Arrival-time histogram over a short window. The frame rate is the
        # single most useful number for judging whether the link is the limit
        # or the device is: 33 Hz lines up with a 30 ms BLE connection
        # interval, which means the host cannot raise it by polling harder.
        self._intervals = deque(maxlen=200)
        # Rolling window for the resting-value estimate.
        self._channel0 = deque(maxlen=1200)
        self._floor_samples = 200
        self.armed = False
        self.since_keepalive = 0
        self.stream_confirmed = False
        self.notify_total = 0
        self.notify_frames = 0
        self.notify_other = 0
        self.notify_buttons = 0
        self.other_signatures = {}
        # Frame-integrity counters. Measured, never enforced: see
        # decode_frame()'s docstring for why a failed CRC cannot yet be used to
        # drop a sample. A device that reports crc_fail without any link trouble
        # is producing corrupt frames, and a device that reports fragments is
        # telling us the 20-byte ATT payload is being reassembled somewhere.
        self.crc_fail_count = 0
        self.fragment_count = 0
        # THE REASSEMBLER, CREATED FRESH.
        #
        # A telemetry frame is longer than one ATT payload can carry
        # (the link's MTU is 23 bytes), so it arrives in two
        # notifications -- and the project used to decode each half as
        # if it were a whole frame. Both halves then failed the frame's
        # own CRC, because the CRC is over the WHOLE frame, and both
        # were counted as "the frame did not verify".
        #
        # The app does the same thing (n1.java processes every
        # notification independently and nothing reassembles fragments
        # on either side), but the app has no recorder. This project
        # does, and the recorder was writing half-frames to disk.
        #
        # It is created HERE rather than in __init__ because this
        # method is where every other per-link counter is created, and
        # a new link is a new frame stream: half a frame from the old
        # link is not half of a frame in the new one.
        self._reassembler = Reassembler()
        # THE VIBRATION CALIBRATOR.
        #
        # The device's telemetry frames carry the vibration byte the
        # device is reporting -- the same byte the A0 control frame
        # carries on the way out. The project decoded it and threw it
        # away, so a vibration level was a COMMAND and never a
        # MEASUREMENT: "you asked for 80%" and "the device delivered
        # 62% of its range" were indistinguishable.
        #
        # The calibrator measures the device's own resting reading and
        # scales the rest against it, so the two can be told apart. A
        # device's resting reading is not zero, and it differs between
        # units and firmware versions, so it is measured rather than
        # assumed.
        self._vibrator = VibrationCalibrator()
        # The last vibration byte the device reported, kept so the
        # calibrated reading can be published without re-decoding.
        self._last_vibration = None
        # The button history belongs to the LINK, like every other
        # measurement here: a new link is a new measurement, and the keys
        # pressed under the previous link are not this link's keys.
        self.button_events.clear()
        self.button_count = 0

    def _rate_median(self):
        if not self._intervals:
            return None
        ordered = sorted(self._intervals)
        return ordered[len(ordered) // 2]

    def _idle_interval_ms(self):
        m = self._rate_median()
        return round(m * 1000, 2) if m else None

    def _rate_hz(self):
        m = self._rate_median()
        return round(1.0 / m, 1) if m else None

    def notify_summary(self) -> dict:
        top = sorted(self.other_signatures.items(),
                     key=lambda kv: -kv[1])[:6]
        return {
            "total": self.notify_total,
            "telemetry": self.notify_frames,
            "other": self.notify_other,
            # The button frames are a SUBSET of "other": they are frames
            # the telemetry decoder does not accept, and they are counted
            # here as well so an operator can tell "the device is sending
            # key presses" from "the device is sending something we do
            # not understand at all". The relation total = telemetry +
            # other still holds.
            "buttons": self.notify_buttons,
            "other_signatures": [
                {"sig": k, "count": v} for k, v in top],
            # Measured integrity, not enforced integrity: a frame whose CRC
            # did not match was still decoded and still counted above. These
            # two are how the operator finds out that happened.
            "crc_fail": self.crc_fail_count,
            "fragments": self.fragment_count,
            # The reassembly's own counts, here too: the notify summary
            # is what a panel reads to explain WHY the telemetry count is
            # lower than the total, and the answer is usually that the
            # frames arrived in pieces.
            **self._reassembler.state(),
        }

    def arm(self, ok: bool) -> None:
        """Record that command 54 was accepted.

        The write succeeding is not proof the device is streaming, so this is
        an optimistic flag that a real telemetry frame later confirms. Leaving
        it unset instead would keep the panel at 未激活 even though the
        command went out, which is exactly what the operator saw.
        """
        self.armed = bool(ok)
        # Re-arming restarts the sequence: the device owes one more batch, and
        # the single follow-up request is owed again.
        self.stream_confirmed = False
        self.since_keepalive = 0

    def set_travel_provider(self, provider) -> None:
        """Install the callable that reports the commanded travel.

        The provider is called with no arguments and returns the travel as a
        percentage (0..100), or None when it is unknown. It is called once
        per telemetry frame, so it must be cheap and must not raise: a
        provider that raises would take the frame down with it, and the
        frame is the reading everything else depends on.

        Why a provider rather than a direct read of the session: the monitor
        is the telemetry side of the system and has no business knowing
        about the send pump. The host wires the two together once, at
        startup, and the monitor stays testable on its own.
        """
        self._travel_provider = provider

    def _sample_travel(self, now: float):
        """Read the travel provider once, defensively. Returns (t, value)."""
        if self._travel_provider is None:
            return None
        try:
            value = self._travel_provider()
        except Exception:                             # noqa: BLE001
            # A provider that raises is a bug in the wiring, not in the
            # telemetry path. Losing one travel sample is recoverable;
            # losing the frame is not.
            return None
        if value is None:
            return None
        try:
            return (now, float(value))
        except (TypeError, ValueError):
            return None

    def feed(self, raw: bytes) -> bool:
        """Consume one notification. Returns True if it was a telemetry frame.

        A BUTTON FRAME IS NOT A TELEMETRY FRAME, so it does not make this
        method return True: it is recognised, recorded and handed to the
        hook, and the curve is not touched. That distinction is what keeps
        a key press from looking like a pressure sample -- the reason
        ``pressure.decode_frame()`` drops these frames in the first
        place.
        """
        self.notify_total += 1
        button = decode_button_event(raw)
        if button is not None:
            self._record_button(button)
            self.notify_buttons += 1
            # A button frame is counted as "other": it is a frame the
            # telemetry decoder does not accept, and the notify split is
            # what tells a silent device from a device speaking a
            # different dialect.
            self.notify_other += 1
            sig = "%dB:%s" % (len(raw), raw[:4].hex().upper())
            self.other_signatures[sig] = self.other_signatures.get(sig, 0) + 1
            return False
        # THE REASSEMBLY, BEFORE THE DECODE.
        #
        # A telemetry frame is longer than one ATT payload (the link's
        # MTU is 23 bytes), so it arrives in two notifications. The
        # buffer collects them, and the WHOLE frame is what gets
        # decoded -- so the frame's own CRC is checked against the
        # whole frame and can pass.
        #
        # The notification that does not complete a frame is NOT a
        # telemetry frame: it is counted as one (the device IS
        # streaming) but it does not become a sample, it does not move
        # the curve, and it does not arm the sensors. Counting a half
        # frame as a frame is what made the old numbers disagree: the
        # frame count was twice the sample count, and the curve showed
        # half as many readings as the counter said.
        whole = self._reassembler.feed(raw)
        if whole is None:
            # A fragment, or something that is not a frame at all. The
            # reassembler counts the ones it dropped; this counts the
            # notification itself, so the notify split stays exact.
            self.notify_other += 1
            sig = "%dB:%s" % (len(raw), raw[:4].hex().upper())
            self.other_signatures[sig] = self.other_signatures.get(sig, 0) + 1
            return False
        d = decode_frame(whole)
        if d is None:
            # Keep a short fingerprint of what did arrive. Without this there
            # is no way to tell "the device is silent" apart from "the device is
            # sending something this decoder does not recognise", and those
            # look identical on the dashboard: a zero peak either way.
            self.notify_other += 1
            sig = "%dB:%s" % (len(whole), whole[:4].hex().upper())
            self.other_signatures[sig] = self.other_signatures.get(sig, 0) + 1
            return False
        self.notify_frames += 1
        # The vibration byte goes to the CALIBRATOR, so the device's own
        # reported level becomes a measurement. The byte is the one the
        # decoder read -- the same offset the control frame carries it
        # in -- so the reading and the command are on one scale.
        self._last_vibration = d.get("vibration")
        self._vibrator.feed(self._last_vibration)
        # Integrity counters. These never change whether a frame is used -- a
        # fragment still contributes its samples, exactly as the app treats it,
        # because dropping it would hide data rather than fix it. They exist so
        # the dashboard can say *why* a curve looks wrong.
        if not d.get('crc_ok', True):
            self.crc_fail_count += 1
        if d.get('is_fragment'):
            self.fragment_count += 1
        # A REASSEMBLED FRAME IS NOT A FRAGMENT. The flag the decoder
        # sets says "this notification was shorter than one ATT
        # payload", and a frame that has been put back together is
        # longer than one -- so the flag is now false, and the counter
        # that used to count fragments counts the frames that arrived
        # whole.
        #
        # The counter keeps its name and its meaning: it says how many
        # notifications were shorter than one payload. A frame that was
        # reassembled from two of them is counted in the reassembler's
        # own counters instead, which is where the truth about the link
        # lives.
        # A real telemetry frame is the only trustworthy proof that command 52
        # took effect, so it sets the flag regardless of what the write
        # returned.
        self.armed = True
        groups = d['groups']
        if not groups:
            return False
        # the three groups repeat; take the first, which is the live sample
        slots = groups[0]
        now = time.time()
        self.frames_seen += 1
        self.since_keepalive += 1
        if self.last_frame_at:
            gap = now - self.last_frame_at
            if 0 < gap < 5.0:
                self._intervals.append(gap)
        self.last_frame_at = now
        self.samples.append((now, slots))
        # The OBJECT PROFILE is fed from the same frame, on the same
        # clock, so its window and the sample window describe the same
        # stretch of time. It is fed HERE rather than in a second place,
        # because a second call site would be a second chance to feed it
        # a different set of slots.
        self._object.add(now, slots)
        # The commanded travel is sampled on the SAME clock as the frame, so
        # the two series in `series()` share one time base and can be
        # correlated point for point.
        travel = self._sample_travel(now)
        if travel is not None:
            self._travel.append(travel)
        s0 = slots[PRIMARY] if len(slots) > PRIMARY else 0
        if s0 > self.peak:
            self.peak = s0
        # A sensor reading cannot be judged against a fixed number: the
        # decoded range depends on the device's own payload constants, and the
        # resting value drifts. Compare against a rolling low percentile
        # instead -- a plain running minimum collapses to the quietest sample
        # ever seen, which drags the threshold down until noise registers as a
        # press.
        self._channel0.append(s0)
        floor = self._idle_floor()
        # The change-point detector sees the SAME reading the chart draws,
        # on the SAME clock, so a mark on the chart and a change point in
        # the counter cannot disagree about which frame they belong to.
        self.change_detector.feed(now, s0)
        # Position is the primary reading: five slots scanned high-to-low,
        # each worth 20% of travel. Pressed means a slot is lit, which is what
        # the device's own threshold decides, not an amplitude guess.
        pos, slot = locate(slots)
        self.position = pos
        self.active_slot = slot
        pressed = slot is not None
        if pressed and not self._was_pressed:
            self.press_events += 1
        self._was_pressed = pressed
        return True

    @property
    def pressed(self) -> bool:
        """Whether a slot is currently lit.

        Decided by the device's own threshold rather than an amplitude
        heuristic: the previous version compared slot 0 against a floor that
        no longer exists, so it raised on every call and the UI silently showed
        "not pressed" while reporting a position.
        """
        return self.active_slot is not None

    def latest(self) -> list[int] | None:
        return list(self.samples[-1][1]) if self.samples else None

    def state(self) -> dict:
        latest = self.latest()
        age = (time.time() - self.last_frame_at) if self.last_frame_at else None
        return {
            # The UI polls this endpoint and renders 未激活 / 已激活 straight
            # from `armed`. It used to be returned only by POST /api/arm, so
            # every poll saw undefined and the panel was pinned to 未激活 for
            # the whole session regardless of what the device was doing.
            'armed': self.armed,
            'frames': self.frames_seen,
            'latest': latest,
            # WHAT IS INSIDE THE TUBE. The five slots are five positions
            # along the travel, each with its own pressure, so an object
            # lying in the tube is a PATTERN across them -- and that
            # pattern is the measurement. See vero/objectprofile.py.
            'object': self._object.state(),
            'named': ({SLOT_NAMES[i]: v for i, v in enumerate(latest)}
                      if latest else {}),
            'pressure': latest[PRIMARY] if latest else None,
            # Percentage of the 12-bit ADC full scale (4095), not of 255:
            # the old /255 divisor produced values above 1000 once the app's
            # 16-bit decoding replaced single-byte reads.
            'raw_pct': (round(latest[PRIMARY] / 4095 * 100)
                        if latest else None),
            'peak': self.peak,
            'pressed': self.pressed,
            'position': self.position,
            'position_pct': round(self.position / POSITION_MAX * 100),
            'active_slot': self.active_slot,
            'hz': self._rate_hz(),
            'hz_from_median': self._rate_hz(),
            'interval_ms': (round(self._rate_median() * 1000, 2)
                            if self._rate_median() else None),
            'interval_samples': len(self._intervals),
            'idle_floor': self._idle_floor(),
            'press_delta': self.press_delta,
            'press_events': self.press_events,
            # The change-point detector's MARKS -- the frames it found, on
            # the series' own clock.
            #
            # The detector's SENSITIVITY and the pressure-rise signal are
            # NOT published here: there is no control for either, and a
            # field nobody can set or act on is machinery that is still
            # running for nobody.
            #
            # The THRESHOLD and the NOISE LEVEL are a different matter,
            # and they are published for a reason that has nothing to do
            # with a control: the operator reports "there are no change
            # points" and there are two very different reasons for that.
            # Either nothing irregular happened, or the signal's own
            # spread raised the threshold above the irregularity. The
            # two look IDENTICAL on the screen -- no marks either way --
            # and only the numbers tell them apart.
            #
            # These are READ-ONLY. The sensitivity SLIDER was removed on
            # purpose ("移除控制下面变化灵敏度和压力变化切换参数等内容"),
            # and this does not bring it back: nothing here can be set.
            # It answers "why is nothing marked", not "mark it harder".
            'change_threshold': round(self.change_detector.threshold(), 2),
            'change_noise': round(self.change_detector.noise_level(), 2),
            'change_count': self.change_detector.count,
            #
            # The frame's OWN time, in unix seconds.
            #
            # The browser's live chart appends one point per real frame,
            # and it has to put the change points on the SAME clock --
            # the change points are frames of this series, so a mark has
            # to land under its own reading. Two clocks cannot be
            # aligned by hand without a second convention, so the frame
            # carries its own timestamp and the chart uses it.
            #
            # It is the time of the LAST frame, not of the snapshot: the
            # snapshot is built 60 times a second and the frames arrive
            # at ~33 Hz, so the last frame's time is the only thing here
            # that identifies WHICH reading this snapshot is about.
            'at': self.last_frame_at,
            # The change points, on the SAME clock as `at` -- they are
            # frames of this very series. The browser's live chart draws
            # them under their own reading, so they have to travel with
            # the frame's time and not on a second one.
            #
            # They are the FULL list, not just the new ones: the browser
            # keeps the ones it has not seen yet, keyed on their time.
            # Sending the whole list is what makes the chart correct
            # after a reconnect -- a browser that missed a mark still
            # gets it on the next push.
            'change_t': [t for t, _v, _r in self.change_detector.marks()],
            'change_v': [v for _t, v, _r in self.change_detector.marks()],
            # THE DEVICE'S OWN VIBRATION READING, calibrated against
            # its resting level. This is what the device reported, not
            # what was asked for -- and the two are the difference
            # between a control surface that works and one that
            # silently does not.
            'vibration_measured': self._vibrator.calibrated(
                self._last_vibration),
            'vibration_baseline': (None if self._vibrator.baseline is None
                                    else round(self._vibrator.baseline, 2)),
            'vibration_ready': self._vibrator.ready,
            # THE REASSEMBLY'S COUNTERS. A telemetry frame is longer than
            # one ATT payload, so the link splits it, and the buffer
            # puts it back together before the decode.
            #
            # "reassembled" counts the frames that were put back
            # together, and "reassembly_dropped" the ones that never
            # arrived whole. The second number is the one that says
            # something: a device whose frames keep getting lost is a
            # device whose readings are incomplete, and nothing else on
            # the panel would say so.
            **self._reassembler.state(),
            'streaming': bool(age is not None and age < 2.0),
            'age_s': round(age, 2) if age is not None else None,
            # THE DEVICE'S OWN BUTTONS. The count is here so a panel can
            # say "the device sent N key presses"; the history itself
            # lives in button_state(), because this payload is pushed
            # 60 times a second and the history does not belong in every
            # one of those.
            'button_count': self.button_count,
            'button_last': (dict(self.button_events[-1])
                            if self.button_events else None),
        }

    def series(self, seconds: float = 10.0) -> dict:
        """Pressure history over the last `seconds`, with enough metadata for
        a caller to interpret it without guessing.

        `window` only bounds how many samples are retained, never how many
        seconds the caller asked about, so the two are reported separately:
        `window_seconds` is what the buffer actually holds and can be smaller
        than `seconds` right after startup.

        Three things come back, and they are three different readings:

        * ``pressure`` / ``channel1`` -- the raw 12-bit touch reading of the
          two channels, sampled per frame.
        * ``slot`` -- WHICH of the five segments the device's own quantiser
          reported as triggered, and ``position_pct`` -- that segment's
          depth as a percentage of travel. The slot is derived from the
          frame's five raw values by the same ``locate()`` the live panel
          uses, so the history and the live readout cannot disagree about
          which segment is lit.
        * ``travel_pct`` -- how far the rod was COMMANDED out, which the
          device never reports. It is sampled from the host on the same
          clock as the frame, so it shares this series' time base.

        The three arrays are NOT the same length and are not parallel: the
        travel is only recorded when a provider is installed, and the
        slot/position are computed per frame while the travel is sampled
        per frame too, so those two do line up -- but the travel series is
        the shorter of the two when the provider is absent. Every array is
        therefore accompanied by its own time base, and a caller that needs
        to line them up has to do it on the time base rather than on the
        index.
        """
        now = time.time()
        cutoff = now - seconds
        rows = [(t, s) for t, s in self.samples if t >= cutoff]
        span = 0.0
        if len(rows) >= 2:
            span = rows[-1][0] - rows[0][0]
        hz = ((len(rows) - 1) / span) if span > 0 else 0.0
        base = rows[0][0] if rows else now
        # The slot is recomputed per frame rather than stored, so the history
        # and the live panel read the SAME function and cannot drift apart
        # when the decoding rules change.
        slots = []
        positions = []
        for _t, s in rows:
            pos, slot = locate(s)
            slots.append(slot)
            positions.append(round(pos / POSITION_MAX * 100))
        travel_rows = [(t, v) for t, v in self._travel if t >= cutoff]
        travel_base = travel_rows[0][0] if travel_rows else base
        return {
            't': [round(t - base, 3) for t, _ in rows],
            'unix': [round(t, 3) for t, _ in rows],
            'pressure': [s[PRIMARY] if len(s) > PRIMARY else 0 for _, s in rows],
            'channel1': [s[SECONDARY] if len(s) > SECONDARY else 0
                         for _, s in rows],
            # The triggered segment per frame: None where no segment is lit,
            # which is a real reading and not a missing one.
            'slot': slots,
            'position_pct': positions,
            # The commanded travel, on its own time base.
            'travel_t': [round(t - travel_base, 3) for t, _ in travel_rows],
            'travel_pct': [round(v, 2) for _, v in travel_rows],
            'count': len(rows),
            'requested_seconds': seconds,
            'window_seconds': round((now - self.samples[0][0]), 2)
                                 if self.samples else 0.0,
            'sample_hz': round(hz, 1),
            'peak': max((s[PRIMARY] if len(s) > PRIMARY else 0
                         for _, s in rows), default=0),
            # How much of the requested window the buffers actually cover,
            # per buffer. The caller asked for N seconds; this says how many
            # of them exist, so a short answer is visible rather than
            # silently truncated.
            'covered_seconds': {
                'pressure': round((rows[-1][0] - rows[0][0]), 2)
                             if len(rows) >= 2 else 0.0,
                'travel': round((travel_rows[-1][0] - travel_rows[0][0]), 2)
                          if len(travel_rows) >= 2 else 0.0,
            },
            'travel_samples': len(travel_rows),
            # The change points, on the SAME time base as `t` (they are
            # frames of this series, so they can be drawn straight onto
            # the curve without a second alignment). `change_t` is the
            # offset of the marked frame and `change_v` is that frame's
            # value, so a caller can draw a mark at the right height as
            # well as the right time.
            # THE OBJECT PROFILE'S OWN SERIES.
            #
            # The pressure series above is the raw per-frame readings.
            # This one is the OBJECT: the mean pressure over the slots
            # the device engaged, the length, and the evenness -- the
            # numbers the object card shows, over time.
            #
            # It is a NESTED key, not another top-level array, because
            # it is a different thing with a different shape: its own
            # time base, its own count, and its own cadence. Flattening
            # it into the top level would make it look like one more
            # channel of the same series, and it is not -- a caller that
            # lined it up with the pressure array BY INDEX would be
            # wrong, because the two have different lengths.
            'object': self._object.series(seconds),
            'change_t': [round(t - base, 3) for t, _, _ in
                         self.change_detector.marks(cutoff)],
            'change_v': [v for _t, v, _r in
                         self.change_detector.marks(cutoff)],
            # The button events INSIDE the requested window, on the same
            # time base as `t`, so a caller can mark a key press on the
            # curve at the moment it happened.
            #
            # They are their own array and NOT parallel with the pressure
            # one: a key press is an event, not a sample, so there is no
            # reading to put at every frame index. `button_t` is the
            # offset of the press and `button_v` is the key's name.
            'button_t': [round(e["at"] - base, 3)
                         for e in self.button_events if e["at"] >= cutoff],
            'button_v': [e["button"] for e in self.button_events
                         if e["at"] >= cutoff],
            'button_count': self.button_count,
        }

    def curve(self, seconds: float = 20.0) -> dict:
        """Trim the rolling window to `seconds` and return plottable series."""
        if not self.samples:
            return {'t': [], 's0': [], 's1': []}
        cutoff = time.time() - seconds
        rows = [(t, s) for t, s in self.samples if t >= cutoff]
        if not rows:
            rows = list(self.samples)[-1:]
        base = rows[0][0]
        return {
            't': [round(t - base, 2) for t, _ in rows],
            's0': [s[PRIMARY] if len(s) > PRIMARY else 0 for _, s in rows],
            's1': [s[SECONDARY] if len(s) > SECONDARY else 0 for _, s in rows],
        }
