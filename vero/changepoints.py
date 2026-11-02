"""Pressure change-point detection.

The live telemetry chart carries two features:

  1. the points where the pressure changed on its OWN, marked on the curve,
     with a sensitivity slider;
  2. a link that fires when the pressure RISES: the one-way traverse time
     and the maximum travel are switched to the operator's configured values.

There is no SENSITIVITY control and no pressure-rise link. So (2) is
gone -- the RiseDetector that fed it, and every constant that described
it -- and (1) keeps only the MARKS: the detector still runs at its
default threshold and the chart still draws them, but nothing can set
the threshold any more.

The criterion for (1):

    正常情况下，如果是自动行程，压力点就是会规律变化的，非规律的部分就是
    按压的力道发生一些改变。

In English: under an automatic stroke the pressure varies REGULARLY, and the
IRREGULAR part is a change in how hard the surface is being pressed.

So the detector's job is not "find a big value". It is "find the part that
does not fit the pattern". That is a residual problem, and this module
solves it as one:

  * the REGULAR part is the local trend, estimated with a MEDIAN over a
    short centred window. Median, not mean: a press is a spike, and a mean
    would be dragged by the very spike we are looking for;
  * the residual is ``value - trend``;
  * a frame is a CHANGE POINT when its residual is large compared with the
    residual spread of the recent past.

Why a residual against a local trend rather than a first difference: a
first difference kills the trend but also kills a genuine step, because a
step IS a large first difference. A residual against a median baseline
keeps both -- a smooth ramp has a small residual, a step has a large one.

THE SCALE ESTIMATOR. The obvious choice, the median absolute deviation,
is wrong here, and measurably so. On a clean sinusoidal stroke the median
of an 11-point window equals the centre value more than half the time, so
more than half the residuals are exactly zero and the MAD is exactly zero
-- a threshold of zero marks every frame whose residual is not exactly
zero, which is 27% of a perfectly regular curve. Measured on the signal in
scripts/changepoint_check.py: MAD gave 0.00 and 163 marks out of 600
frames, of which only 3 were the real irregularities.

So the scale is a HIGH PERCENTILE of the absolute residuals instead (the
90th). A percentile measures the spread of the BULK of the residuals
while ignoring the sparse outliers we are looking for -- which is exactly
the property wanted. On the same signal it gives 58.01, so a threshold
of 3x that is 174: every regular residual (max 65.4) is below it and the
irregularity (806.1) is far above. The detector's sensitivity multiplies
that percentile.

THE PRESSURE-RISE SIGNAL IS GONE. It used to live here too, and it was
the second half of that feature: a judgement that the pressure had gone
UP, latched until it came back down, which the web app's
"switch the parameters when the pressure rises" link acted on. That
control is gone, so the detector, its constants and its state are gone
with it.

The history of that detector is kept in the git log rather than here: it
was a subtle criterion (a band measured over a window that ends some
frames ago, with an absolute margin above it, so the threshold does not
scale with the stroke's amplitude), and it is worth reading if the
pressure-rise behaviour ever comes back. What is left here is only what
the CHART needs: the frames the detector marked.

Nothing in this module touches the device. It reads a list of (time, value)
and returns numbers, so it can be exercised without hardware.
"""
from __future__ import annotations

from collections import deque

#: How many recent residuals the scale is measured over. At the device's
#: measured ~33 Hz this is about three seconds -- long enough to contain a
#: whole up-and-down phase of an automatic stroke (so the "regular" part
#: is measured, not guessed) and short enough to follow a change that
#: happened seconds ago.
NOISE_WINDOW = 100

#: Which percentile of the absolute residuals is the scale. High enough to
#: ignore the sparse outliers being looked for, low enough to describe the
#: bulk of the residual spread. See the module docstring for why the MAD
#: does not work here.
SCALE_PERCENTILE = 0.90

#: How many recent frames the local trend is estimated over. A centred
#: window of odd length has a true middle, so the trend at the centre frame
#: is not skewed by the frame itself.
TREND_WINDOW = 11

#: How many residuals the scale needs before the detector is allowed to
#: mark anything. Below this the spread estimate is not trustworthy, and
#: the first frames of a stream have a one-sided trend (there is no history
#: on the left), which produces a startup transient that is not a change
#: in the pressure at all.
NOISE_WARMUP = 30

#: The multiplier applied to the scale. A frame is marked when its
#: residual exceeds this many times the scale.
#:
#: It is not settable from the web app: there is no sensitivity control,
#: so the detector runs at this one value.
DEFAULT_SENSITIVITY = 3.0


def _median(values) -> float:
    """The median of a sequence. An empty sequence has a median of 0."""
    n = len(values)
    if n == 0:
        return 0.0
    ordered = sorted(values)
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2.0


def _percentile(values, p: float) -> float:
    """The p-th percentile of a sequence, by linear interpolation.

    An empty sequence has a percentile of 0.
    """
    n = len(values)
    if n == 0:
        return 0.0
    ordered = sorted(values)
    if n == 1:
        return float(ordered[0])
    pos = p * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(ordered[lo]) * (1.0 - frac) + float(ordered[hi]) * frac


def _mad(values, centre) -> float:
    """Median absolute deviation of `values` about `centre`."""
    if not values:
        return 0.0
    return _median([abs(v - centre) for v in values])


class ChangePointDetector:
    """Finds the irregular part of a pressure series.

    The detector keeps its own short history of the raw readings and the
    residuals it computed for them, so the scale is measured on the
    residuals of THIS session rather than assumed from a constant.

    The residual of a frame is computed against a CENTRED median, which
    means it can only be computed once the frames on both sides of it have
    arrived. So the detector evaluates a frame late, by half a window, and
    reports the mark with the frame's own timestamp. The mark therefore
    lands on the right place on the chart even though it is discovered a
    few frames later.

    `feed` is called once per frame, in arrival order. `points` returns the
    frames that were marked, each with the time and the value, so the
    caller can draw a mark at the right place on the chart.
    """

    def __init__(self, sensitivity: float = DEFAULT_SENSITIVITY,
                 noise_window: int = NOISE_WINDOW,
                 trend_window: int = TREND_WINDOW):
        self.sensitivity = sensitivity
        self.noise_window = max(8, int(noise_window))
        # The trend window must be odd so it has a true centre.
        tw = max(3, int(trend_window))
        self.trend_window = tw if tw % 2 else tw + 1
        # The raw readings and their times, oldest first. The buffer is
        # longer than the windows need so the chart can ask for the marks
        # of the whole visible span.
        self._values: deque = deque(maxlen=4096)
        self._times: deque = deque(maxlen=4096)
        # The residuals, aligned with _values.
        self._residuals: deque = deque(maxlen=4096)
        # The frames that were marked, newest last.
        self._marks: deque = deque(maxlen=4096)

    def reset(self) -> None:
        self._values.clear()
        self._times.clear()
        self._residuals.clear()
        self._marks.clear()

    @property
    def count(self) -> int:
        """How many frames have been marked so far."""
        return len(self._marks)

    def feed(self, when: float, value: float) -> bool:
        """Take one frame. Returns True when a change point was found.

        The frame is marked when its residual against the local trend
        exceeds `sensitivity` times the scale measured on the recent past.
        The scale is measured EXCLUDING this frame's own residual, so a
        single huge frame cannot inflate the threshold and hide itself.

        Because the trend is centred, the residual of a frame is only
        final once the frames on both sides of it have arrived. So the
        return value reports whether the frame that JUST arrived turned
        out to be a change point -- which is the frame that can now be
        evaluated, half a window back.
        """
        self._times.append(when)
        self._values.append(float(value))
        n = len(self._values)
        half = self.trend_window // 2
        # The frame that can now be evaluated: half a window back, so a
        # centred window is available around it.
        j = n - 1 - half
        if j < 0:
            self._residuals.append(0.0)
            return False
        vals = list(self._values)
        trend = _median(vals[j - half:j + half + 1])
        residual = vals[j] - trend
        # The scale comes from the residuals of the frames BEFORE this
        # one, so a spike cannot raise its own threshold.
        pool = list(self._residuals)[-self.noise_window:]
        if len(pool) < NOISE_WARMUP:
            self._residuals.append(residual)
            return False
        scale = self._scale(pool)
        self._residuals.append(residual)
        if scale <= 0.0:
            # No spread yet -- a perfectly flat history. A residual that is
            # large against the signal's own scale is still worth marking,
            # but only then.
            scale = max(1.0, abs(_median(pool)))
            if abs(residual) > scale * self.sensitivity:
                self._mark(self._times[j], vals[j], residual)
                return True
            return False
        if abs(residual) > self.sensitivity * scale:
            self._mark(self._times[j], vals[j], residual)
            return True
        return False

    @staticmethod
    def _scale(pool) -> float:
        """The spread of a residual pool: a high percentile of |residual|.

        NOT the median absolute deviation -- that collapses to zero on a
        regular curve, where more than half the residuals are exactly
        zero. See the module docstring for the measurement.
        """
        return _percentile([abs(r) for r in pool], SCALE_PERCENTILE)

    def _mark(self, when, value, residual) -> None:
        self._marks.append((when, value, residual))

    def marks(self, since: float | None = None) -> list:
        """The marked frames, optionally only those at or after `since`."""
        if since is None:
            return list(self._marks)
        return [m for m in self._marks if m[0] >= since]

    def points(self, since: float | None = None) -> list:
        """The marked frames as (time, value) pairs, for drawing a mark."""
        return [(m[0], m[1]) for m in self.marks(since)]

    def noise_level(self) -> float:
        """The current residual spread, in the signal's own units.

        Reported so the UI can say what the threshold currently is, rather
        than leaving the operator to guess why a point was or was not
        marked.
        """
        pool = list(self._residuals)[-self.noise_window:]
        if len(pool) < NOISE_WARMUP:
            return 0.0
        return self._scale(pool)

    def threshold(self) -> float:
        """The residual magnitude that currently counts as a change point."""
        return self.sensitivity * self.noise_level()
