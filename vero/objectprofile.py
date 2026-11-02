"""What is INSIDE the sleeve: an object's pressure profile and its length.

THE QUESTION
------------
The question: can the live telemetry say anything about the object inside
the inner tube -- how hard it is being squeezed, how long it is, and how
both are changing over time?

The telemetry already carries the answer, in the shape the device's own
quantiser uses. Each frame is FIVE position slots (位置1..位置5), each an
independent 12-bit reading of how hard that part of the tube is being
pressed. The device decides "a slot is engaged" at its own threshold
(800 for Vero -- ``v1.smali d(ArrayList)``), and reports the DEEPEST
engaged slot as the position.

So the five slots are not five samples of one thing: they are five
positions along the tube, each with its own pressure. An object lying in
the tube therefore produces a PATTERN across the slots -- a run of
engaged slots, with a pressure at each. That pattern is the measurement.

WHAT IS MEASURED
-----------------
``pressure``
    The mean pressure over the engaged slots. Not the peak: the peak
    describes a single point of contact, and the question is about the
    object as a whole. An object with one hard spot and a soft body has
    a high peak and a low mean, and the difference between them IS the
    shape information (see ``spread``).

``length``
    How many slots are engaged, expressed in the tube's own units. The
    device's quantiser says a slot is 20% of the travel, and the travel
    is the full stroke, so an engaged run of two slots is "40% of the
    tube's length". This is an ABSTRACT length -- it is not
    millimetres, and it is not meant to be. The device has no ruler.

``spread``
    How unevenly the pressure is distributed across the engaged slots:
    the standard deviation over the mean. Zero means every engaged slot
    is pressed equally (a uniform, even object); a large spread means
    the pressure is concentrated (a hard core, a tip, an uneven
    surface). This is the "shape" number the mean alone cannot give.

``extent`` / ``pulse``
    ``extent`` is the ratio of the mean to the peak -- how much of the
    peak's pressure is shared across the object rather than concentrated
    at one point. It is 1.0 for a perfectly even object.

    ``pulse`` is the spread of the mean pressure over the recent window:
    how much the object is pulsing. A still object has a near-zero pulse;
    a throb shows up here.

WHY THESE NUMBERS AND NOT OTHERS
---------------------------------
Every one of them is a ratio or a count over slots the device itself
engaged. None of them depends on a calibration constant, on a millimetre
scale, or on a hard-coded pressure limit -- so they stay meaningful
across devices, across sessions, and across a device's own drift. The
one threshold that IS used is the device's own (800), because "engaged"
has to mean what it means to the device: the same word the position
reading uses. Using a different threshold here would make this module
disagree with the position reading on the same frame, which is worse
than having no measurement at all.

The comparison numbers
----------------------
``vs_baseline`` compares the CURRENT reading against a baseline captured
over the recent window: how much bigger the pressure is now than its
recent average, and how much the length has changed. ``vs_peak`` compares
the current mean against the largest mean seen in the window. Both are
ratios, so they are scale-free.

The window
----------
Everything is computed over a rolling window of frames, and the window is
expressed in SECONDS rather than frames, because the frame rate is the
device's business (33 Hz or 60 Hz, it varies) and a window measured in
frames would mean different durations on different devices.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Sequence

# THE CONSTANTS ARE DECLARED HERE, NOT IMPORTED.
#
# The monitor already depends on this package, so importing the monitor's
# constants back would be a circular import -- and a circular import is
# not a style problem, it is an ImportError at module load. The numbers
# below are the device's OWN, transcribed from v1.smali:
#
#     SLOT_COUNT           five position slots per frame
#     POSITION_STEP        one slot is 20% of the travel (0x7d0)
#     POSITION_MAX         the travel is 10000 (0x2710)
#     VERO_SLOT_THRESHOLD  a slot is "engaged" above 800 (0x320)
#
# They are the same numbers the monitor uses, and they are duplicated on
# purpose rather than shared through a third module, because a shared
# module for two constants is more code than the duplication it removes.
# `scripts/object_profile_regression.py` asserts that the two copies
# still agree, so a change to one that misses the other is caught.
SLOT_COUNT = 5
POSITION_STEP = 2000            # 0x7d0
POSITION_MAX = 10000            # 0x2710
VERO_SLOT_THRESHOLD = 800       # 0x320

#: How many seconds of history the baseline is measured over.
#:
#: Long enough to span several breaths of a pulse, short enough that a
#: deliberate change in what is inside the tube is not averaged away.
BASELINE_SECONDS = 10.0

#: How many frames the window holds at the assumed rate. The window is
#: bounded so a device that streams faster than assumed cannot grow the
#: buffer without limit.
ASSUMED_HZ = 60.0

#: The number of slots a run must have before it counts as an object.
#:
#: ONE engaged slot is the device touching something at a single point --
#: that is a touch, not an object lying in the tube. Two or more is a
#: thing with a length.
MIN_SLOTS_FOR_OBJECT = 2

#: How long the AVERAGE window is.
#:
#: The operator asked for the reading and its average side by side, both
#: refreshed once a second, with the average taken over the readings in
#: between. So the average is over ONE SECOND of frames -- and the device
#: streams at about 33 Hz, so that second holds about 33 readings. That
#: is enough to smooth the sampling noise without hiding a real change:
#: a press that starts is visible in the average within a second.
#:
#: It is a SECOND and not a frame count, because the frame rate is the
#: device's business (33 Hz or 60 Hz) and a window measured in frames
#: would mean different durations on different devices.
AVERAGE_SECONDS = 1.0


def _mean(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _stdev(values: Sequence[float]) -> float:
    """Population standard deviation. Zero for an empty list."""
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / len(values))


class ObjectProfile:
    """The object-inside-the-sleeve measurement, over a rolling window.

    Feed it every telemetry frame; ask it for the measurement.

    The state is a window of per-frame readings, each of which is the
    set of ENGAGED slots and their pressures. The measurement over that
    window is what the page shows.
    """

    def __init__(self, window_seconds: float = BASELINE_SECONDS,
                 assumed_hz: float = ASSUMED_HZ,
                 threshold: int = VERO_SLOT_THRESHOLD):
        self.threshold = threshold
        self.window_seconds = window_seconds
        # THE WINDOW IS A COUNT, AND THE COUNT IS SIZED FROM A RATE.
        #
        # This is the shape the monitor's own buffer has, and it has the
        # same consequence: a window of N frames is N/hz SECONDS, and
        # the device's rate is the device's business. The device here
        # streams at ~33 Hz, so a window sized from an ASSUMED 60 Hz
        # holds 1.8x as many SECONDS as it claims -- the 10-second
        # baseline was really a 27-second baseline.
        #
        # The baseline is a COMPARISON, so what it is measured over
        # matters: a 27-second baseline is a slower baseline, and the
        # "跳动" number -- the spread of the mean over the window --
        # is a spread over 27 seconds instead of 10.
        #
        # So the window is cut to the frames the window actually covers,
        # at the rate the frames are ARRIVING. The rate is measured,
        # not assumed: it is the median gap between the frames, which
        # is the same way the monitor measures its own.
        need = max(8, int(window_seconds * assumed_hz * 1.5) + 1)
        self.window = need
        # Each entry: (timestamp, engaged_pressures list)
        self._frames: deque = deque(maxlen=self.window)
        # THE READING OVER TIME.
        #
        # The window above holds the ENGAGED SLOTS, which is what the
        # baseline is measured from. The operator also wants to SEE the
        # reading move, so the PROFILE -- the numbers the card shows --
        # is kept as its own series, on the same clock.
        #
        # It is a separate deque rather than extra columns on the frame
        # entries because the two are not the same length: a frame with
        # no engaged slot has no profile at all (there is no object),
        # while a frame with one has a profile. So the series is
        # shorter, and a caller that lines it up with the frames has to
        # do it on the TIME, not on the index -- the same rule the
        # monitor's own series follows.
        self._series: deque = deque(maxlen=self.window)
        # The best (highest mean) profile seen in the window, for vs_peak.
        self._peak_mean = 0.0
        self._peak_slots = 0
        # The best (longest) object seen in the window, for the length
        # change. Kept separately from the pressure peak because the
        # longest object and the hardest object are usually not the same
        # moment.
        self._peak_slots_seen = 0
        self.frames_seen = 0
        # THE ONE-SECOND AVERAGE'S READINGS.
        #
        # The operator asked for the reading AND its average side by side,
        # both refreshed once a second, with the average taken over the
        # readings in between. So the average needs the READINGS of the
        # last second, kept on their own clock.
        #
        # It is a separate buffer from the two above, and for the same
        # reason they are separate from each other: the baseline window
        # is a COUNT of frames sized from a rate, the series is a list of
        # PROFILES, and this is a list of RAW MEANS over a span of
        # seconds. Folding them together would change what each of them
        # means.
        #
        # Each entry is (timestamp, mean pressure, length percent). The
        # frames with no object are NOT in it: an empty tube is not a
        # reading of zero, and averaging it in would drag the average
        # down toward the surface's rest value.
        self._avg: deque = deque()

    # ------------------------------------------------------------- input
    def add(self, now: float, slots: list[int]) -> None:
        """Feed one frame's five slot readings.

        ``slots`` is the raw list the decoder produced -- five 12-bit
        readings, position 1 first. Slots the device has NOT engaged are
        dropped, because a reading of an untouched slot is the surface's
        own rest value and averaging it in would dilute the object.
        """
        self.frames_seen += 1
        engaged = [int(v) for v in slots[:SLOT_COUNT] if int(v) > self.threshold]
        self._frames.append((now, engaged))
        # THE AVERAGE'S WINDOW IS TRIMMED ON EVERY FRAME.
        #
        # It is trimmed here and not only when there is an object,
        # because a frame with no object is still a moment in time: if
        # the window were only trimmed on object frames, the readings
        # from before a gap would sit in it, and the first object after
        # the gap would be averaged against a second that had already
        # passed.
        while self._avg and now - self._avg[0][0] > AVERAGE_SECONDS:
            self._avg.popleft()
        if len(engaged) >= MIN_SLOTS_FOR_OBJECT:
            # The profile for THIS frame, recorded on the frame's own
            # clock. A frame with fewer than two engaged slots has no
            # profile -- one point is a touch, not an object -- so the
            # series has a GAP there rather than a zero, and the gap is
            # real: there was no object.
            mean = _mean(engaged)
            length = len(engaged) * POSITION_STEP / POSITION_MAX * 100
            # THE AVERAGE'S READINGS. The mean pressure and the length,
            # both on the frame's own clock, so the one-second window
            # they are averaged over is a real span of time.
            self._avg.append((now, mean, length))
            # THE AVERAGE, at THIS frame.
            #
            # It is computed here, where the frame's own clock is known,
            # and stored WITH the profile -- so the chart's average curve
            # is the average as of each frame, and not an average
            # recomputed later over a window that has moved on.
            avg_p, avg_l, _ = self._average()
            self._series.append((now, self._profile(engaged), avg_p, avg_l))
            if mean > self._peak_mean:
                self._peak_mean = mean
                self._peak_slots = len(engaged)
            if len(engaged) > self._peak_slots_seen:
                self._peak_slots_seen = len(engaged)

    # ------------------------------------------------------------ output
    def _current(self) -> dict | None:
        """The most recent frame's reading, as a profile.

        The CURRENT values are the last frame, not a window average: the
        operator wants to see what is happening NOW. The COMPARISON
        values are what make that number meaningful, and they are
        measured over the window.
        """
        if not self._frames:
            return None
        _, engaged = self._frames[-1]
        if not engaged:
            return None
        return self._profile(engaged)

    def _profile(self, engaged: list[int]) -> dict:
        """Turn a set of engaged pressures into the profile's numbers."""
        mean = _mean(engaged)
        peak = max(engaged)
        spread = _stdev(engaged)
        # The length is a COUNT of engaged slots, and it is put on the
        # device's own scale: a slot is 20% of the travel.
        length_pct = round(len(engaged) * POSITION_STEP / POSITION_MAX * 100)
        return {
            'slots': len(engaged),
            'pressure': round(mean),
            'peak': peak,
            'spread': round(spread),
            'extent': round(mean / peak, 3) if peak else 0.0,
            'length_pct': length_pct,
        }

    def series(self, seconds: float = 60.0) -> dict:
        """The reading over time, in the shape a chart can draw.

        The arrays are on ONE time base -- the frame's own clock -- and
        they are PARALLEL: every array has one entry per recorded
        profile. There is no gap in the middle, because a frame with no
        object is not recorded at all; the chart draws a continuous line
        through the readings that DID exist, and the "present" flag on
        the live reading says whether there is an object right now.

        That is a deliberate choice, and the alternative is worse: a
        chart with a hole in it for every frame the tube was empty would
        be a chart of the tube's emptiness, and the operator is looking
        at the object.

        ``t`` is seconds since the FIRST recorded profile, so it is on
        the same scale as the monitor's own series.
        """
        rows = list(self._series)
        if not rows:
            return {
                't': [], 'unix': [], 'pressure': [], 'length_pct': [],
                'extent': [], 'peak': [], 'count': 0, 'sample_hz': 0.0,
                'avg_pressure': [], 'avg_length_pct': [],
            }
        base = rows[0][0]
        out = {
            't': [round(t - base, 3) for t, _, _, _ in rows],
            # The SAME readings on the ABSOLUTE clock. The offset `t` is
            # for a chart that shares the other plots' time base; the
            # unix time is for a chart that has to line up with the
            # FRAME's own time, because the frame's time is what the
            # live push carries and the two must not become two
            # points of the same frame.
            'unix': [round(t, 3) for t, _, _, _ in rows],
            'pressure': [p['pressure'] for _, p, _, _ in rows],
            'length_pct': [p['length_pct'] for _, p, _, _ in rows],
            'extent': [p['extent'] for _, p, _, _ in rows],
            'peak': [p['peak'] for _, p, _, _ in rows],
            # THE AVERAGE, at each frame. It was computed when the frame
            # arrived, over the second of readings that ended at that
            # frame -- so the average curve is the average AS OF each
            # point, and not an average recomputed at draw time over a
            # window that has moved on.
            'avg_pressure': [a for _, _, a, _ in rows],
            'avg_length_pct': [l for _, _, _, l in rows],
            'count': len(rows),
        }
        # The rate, so the panel can say what the data's own cadence is
        # rather than the page's redraw rate.
        if len(rows) > 1:
            span = rows[-1][0] - rows[0][0]
            out['sample_hz'] = round((len(rows) - 1) / span, 1) if span > 0 else 0.0
        else:
            out['sample_hz'] = 0.0
        return out

    def _average(self) -> tuple[float | None, float | None, int]:
        """The average over the last second of readings.

        Returns ``(pressure, length_pct, samples)``. The two numbers are
        None when there is nothing to average -- which is different from
        averaging to zero, because an empty tube is not a reading of
        zero.

        The window is a span of SECONDS, cut on the readings' own
        timestamps. So it holds however many frames the device sent in
        that second -- about 33 at the device's own rate -- and the
        average is over all of them.

        The LENGTH is averaged as a PERCENT, not as a slot count. The
        two are on different scales, and averaging a count and then
        comparing it with a percentage would be subtracting two
        different things.
        """
        if not self._avg:
            return None, None, 0
        pressures = [p for _, p, _ in self._avg]
        lengths = [l for _, _, l in self._avg]
        return (round(_mean(pressures)),
                round(_mean(lengths)),
                len(self._avg))

    def _frames_in_window(self) -> list:
        """The frames the window actually covers, in seconds.

        The buffer is a COUNT of frames and the window is a claim about
        SECONDS, so the two have to be reconciled -- and the count is
        the wrong side of the reconciliation, because the device's
        frame rate is the device's business.

        The rate is measured, not assumed: it is the MEDIAN gap between
        the frames. The median rather than the mean, because one slow
        frame would drag a mean gap up and shorten the window for
        every frame after it -- a single hitch would silently halve
        the baseline.

        If the rate cannot be measured -- one frame, or every frame on
        the same timestamp -- the whole buffer is returned, because
        cutting on an unmeasurable rate would be worse than not
        cutting.
        """
        frames = list(self._frames)
        if len(frames) < 3:
            return frames
        gaps = []
        for i in range(1, len(frames)):
            gap = frames[i][0] - frames[i - 1][0]
            if gap > 0:
                gaps.append(gap)
        if not gaps:
            return frames
        gaps.sort()
        # The MEDIAN gap, not the mean. The mean is dragged up by one
        # slow frame, and a longer gap means a SHORTER window -- so a
        # single hitch would silently halve the baseline for every
        # frame after it.
        median_gap = gaps[len(gaps) // 2]
        # The cut: the frames within the window's seconds of the LAST
        # frame, measured on their own clock.
        #
        # The window is a count of frames, so the seconds it names have
        # to be turned into a COUNT at the rate the frames are arriving
        # at -- otherwise the count is right but the seconds are not,
        # and the two disagree by exactly the factor the device's rate
        # differs from the assumed one.
        #
        # The rate is measured, not assumed, and the median gap is the
        # robust way to measure it. A device at 33 Hz gives a median
        # gap of 30.3 ms, so a 10-second window is 330 frames -- and
        # the buffer is cut to that, on the frames' own timestamps.
        hz = 1.0 / median_gap
        want = int(self.window_seconds * hz) + 1
        if want >= len(frames):
            return frames
        return frames[-want:]

    def state(self) -> dict:
        """The whole measurement, ready for the page.

        Every number is either None (there is nothing to say) or a
        number. A dash is rendered in the UI for None, and a zero is
        rendered as a zero -- they are different claims and the page has
        to be able to tell them apart.
        """
        current = self._current()
        out = {
            'frames': self.frames_seen,
            'threshold': self.threshold,
            'window_seconds': self.window_seconds,
            'present': current is not None,
            # How many profiles the chart has. Zero means the chart is
            # empty -- which is a different thing from a chart whose
            # readings are all zero.
            'points': len(self._series),
        }
        # THE AVERAGE, over the last second of readings.
        #
        # It is computed from the buffer, not from the current frame, so
        # it is genuinely an AVERAGE and not a copy of the live value.
        # With one reading in the buffer the two are the same number --
        # which is true, and not a bug: one reading IS its own average.
        avg_pressure, avg_length, avg_samples = self._average()
        if current is None:
            out.update({
                'pressure': None, 'peak': None, 'spread': None,
                'extent': None, 'length_pct': None, 'slots': None,
                'baseline_pressure': None, 'baseline_length_pct': None,
                'vs_baseline': None, 'vs_peak': None, 'pulse': None,
                'length_change': None, 'peak_pressure': None,
                'peak_length_pct': None,
                'avg_pressure': avg_pressure, 'avg_length_pct': avg_length,
                'avg_samples': avg_samples,
                'avg_seconds': AVERAGE_SECONDS,
            })
            return out

        # THE FRAMES THE WINDOW ACTUALLY COVERS.
        #
        # The buffer is a COUNT of frames, and the baseline is a claim
        # about SECONDS, so the frames are cut to the seconds the
        # window names -- measured against the frames' own timestamps
        # and the rate they are arriving at.
        #
        # The rate is the median gap between the frames, which is how
        # the monitor measures its own. Using the median rather than
        # the mean matters: one slow frame would drag a mean gap up and
        # shorten the window for every frame after it.
        rows = self._frames_in_window()
        means = [_mean(e) for _, e in rows if len(e) >= MIN_SLOTS_FOR_OBJECT]
        lengths = [len(e) * POSITION_STEP / POSITION_MAX * 100
                   for _, e in rows if len(e) >= MIN_SLOTS_FOR_OBJECT]
        baseline_pressure = round(_mean(means)) if means else None
        baseline_length = round(_mean(lengths)) if lengths else None

        # Only frames that had an object count -- averaging in the empty
        # frames would drag the baseline toward the surface's rest value
        # and make every reading look like a spike. And the baseline
        # length is on the SAME SCALE as the current length: both are a
        # PERCENT of the tube. Averaging the raw slot COUNT and then
        # subtracting it from a percentage would make a steady object
        # report a large "length change" -- 3 slots against 60% is 57,
        # which is not a change at all, it is two different scales being
        # subtracted.
        #
        # The pulse: how much the per-frame mean varies around its own
        # window average. A still object scores near zero; a throb does
        # not.
        pulse = round(_stdev(means)) if len(means) >= 2 else None

        vs_baseline = None
        if baseline_pressure:
            vs_baseline = round(current['pressure'] / baseline_pressure, 3)
        vs_peak = None
        if self._peak_mean:
            vs_peak = round(current['pressure'] / self._peak_mean, 3)

        length_change = None
        if baseline_length is not None:
            length_change = current['length_pct'] - baseline_length

        out.update(current)
        out.update({
            # THE LIVE VALUE AND THE AVERAGE, side by side.
            #
            # `pressure` / `length_pct` are the LIVE values -- this
            # frame's reading. `avg_pressure` / `avg_length_pct` are the
            # average over the last second of readings. The page shows
            # both, and refreshes them once a second.
            'avg_pressure': avg_pressure, 'avg_length_pct': avg_length,
            'avg_samples': avg_samples,
            'avg_seconds': AVERAGE_SECONDS,
            'baseline_pressure': baseline_pressure,
            'baseline_length_pct': baseline_length,
            'vs_baseline': vs_baseline,
            'vs_peak': vs_peak,
            'pulse': pulse,
            'length_change': length_change,
            'peak_pressure': round(self._peak_mean),
            'peak_length_pct': (round(self._peak_slots_seen
                                      * POSITION_STEP / POSITION_MAX * 100)
                                if self._peak_slots_seen else None),
        })
        return out


def object_from_positions(values: list[int],
                          threshold: int = VERO_SLOT_THRESHOLD) -> dict | None:
    """The object reading for ONE frame, from the raw slot values.

    This is the pure function the rolling version is built on, and it is
    what a caller with a single frame uses.

    Returns None when no slot is engaged -- there is no object there,
    and "no object" is not a profile with zero pressure, it is the
    absence of one.
    """
    engaged = [int(v) for v in values[:SLOT_COUNT] if int(v) > threshold]
    if not engaged:
        return None
    mean = _mean(engaged)
    peak = max(engaged)
    return {
        'slots': len(engaged),
        'pressure': round(mean),
        'peak': peak,
        'spread': round(_stdev(engaged)),
        'extent': round(mean / peak, 3) if peak else 0.0,
        'length_pct': round(len(engaged) * POSITION_STEP / POSITION_MAX * 100),
    }
