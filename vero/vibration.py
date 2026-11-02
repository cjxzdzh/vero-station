"""Vibration strength: what the device ACTUALLY did, not what was asked for.

THE PROBLEM THIS SOLVES
------------------------
``/api/target`` takes a vibration level and the A0 control frame carries it
as one byte (offset 5). The device's own telemetry carries a byte back --
the same offset, in the frames the device pushes -- and until now the
project read that byte and called it "vibration" without ever asking what
it MEANS.

The vendor app does ask. It measures a BASELINE of the device's own
vibration byte, subtracts it, and scales the remainder through a 13-point
gain table before drawing a curve. The two halves of that are here:

  * the baseline -- the mean of the vibration bytes the device reports
    while it is not being driven, taken over the first frames the app
    sees (``n1.java:245``, "legacy pi baseline ready");
  * the gain table -- 13 (command, factor) pairs, from the vendor's own
    ``v1.java:95``.

WHY IT MATTERS
--------------
Without the calibration, a vibration level of 128 and a level of 90 are
indistinguishable in the record: both are just "a byte". With it, the
project can say "you asked for 80% and the device's motor delivered
62% of its measured range" -- which is the difference between a control
surface that works and one that silently does not.

WHAT IS NOT HERE, AND WHY
-------------------------
The gain table is a property of the APP's rendering, not of the device.
The app multiplies the raw reading by it to draw a nicer curve. This
module therefore keeps the table as DATA and uses it only where the
question is literally "how strong is this, on the app's scale" -- the
report the operator reads. It does not feed the table into the pressure
decoder: ``scripts/p3c_regression.py`` pins that the amplitude formula in
``pressure.py`` stays untouched, and that is the right call -- the
pressure reading is a different measurement entirely.

The vendor's Bezier baseline (the five control points the app fits to
the first samples) is a RENDERING path too. What is reproduced here is
the part that carries information: the baseline is the MEAN of the
device's own readings, and that is a number the operator can act on.
"""

from __future__ import annotations

from collections import deque

#: The vendor app's own gain table, transcribed from the app's source
#: (``utils/v1.java:95``).
#:
#: Each pair is (command byte, factor). The command byte is what the app
#: writes into the A0 control frame's vibration slot; the factor is what
#: it multiplies the measured remainder by when it draws the curve.
#:
#: The table is not monotonic -- 500 maps to 1.29 and 600 maps to 1.22 --
#: and that is not a transcription error: it is the vendor's own numbers,
#: and the app uses them as they are.
VIBRATION_GAIN_TABLE = (
    (100, 1.00),
    (200, 1.04),
    (300, 1.09),
    (400, 1.12),
    (500, 1.29),
    (600, 1.22),
    (700, 1.25),
    (800, 1.27),
    (900, 1.29),
    (1000, 1.34),
    (1100, 1.55),
    (1200, 1.67),
    (1300, 1.82),
)

#: The largest command byte the table names. The control frame's
#: vibration slot is one byte, so the scale is 0..255 -- the table's
#: 100..1300 range is the APP's internal scale, not the wire scale.
VIBRATION_COMMAND_MAX = 255

#: How many samples the baseline is measured over.
#:
#: The app takes its baseline from the first frames it sees; the exact
#: count is not in the decompiled source, so this is a stated choice
#: rather than a transcription. It is long enough to average out a
#: single noisy frame and short enough that the operator is not waiting
#: seconds for the first reading.
BASELINE_SAMPLES = 7


def gain_factor(command: int) -> float:
    """The app's gain factor for a command byte.

    Between two table entries the factor is interpolated linearly; below
    the first and above the last it is CLAMPED, because the table
    describes the app's rendering and the answer outside its range is
    "the same as the nearest entry", not "zero" and not "infinity".
    """
    if not VIBRATION_GAIN_TABLE:
        return 1.0
    cmd = int(command)
    if cmd <= VIBRATION_GAIN_TABLE[0][0]:
        return float(VIBRATION_GAIN_TABLE[0][1])
    if cmd >= VIBRATION_GAIN_TABLE[-1][0]:
        return float(VIBRATION_GAIN_TABLE[-1][1])
    for (c0, f0), (c1, f1) in zip(VIBRATION_GAIN_TABLE, VIBRATION_GAIN_TABLE[1:]):
        if c0 <= cmd <= c1:
            if c1 == c0:
                return float(f1)
            t = (cmd - c0) / float(c1 - c0)
            return float(f0) + (float(f1) - float(f0)) * t
    return 1.0


class VibrationCalibrator:
    """Measures the device's own vibration baseline and scales readings.

    Two jobs:

      * ``feed(raw)`` takes the vibration byte the device reports and
        keeps the recent ones, so a baseline can be measured;
      * ``calibrated(raw)`` answers "how strong is this, relative to
        what the device reads when it is doing nothing", which is the
        number that means something.

    THE BASELINE IS MEASURED, NOT ASSUMED. A device's resting reading is
    not zero, and it differs between units and between firmware
    versions. So the baseline is the mean of the readings seen so far,
    and until enough samples have arrived the calibration says "not
    known yet" rather than guessing.
    """

    def __init__(self, baseline_samples: int = BASELINE_SAMPLES):
        self.baseline_samples = max(1, int(baseline_samples))
        self._readings: deque = deque(maxlen=max(64, self.baseline_samples * 8))
        self._baseline: float | None = None
        self._baseline_locked = False

    def reset(self) -> None:
        """Forget the baseline -- for a new link, or a mode change."""
        self._readings.clear()
        self._baseline = None
        self._baseline_locked = False

    def feed(self, raw) -> bool:
        """Take one vibration reading from the device.

        ``raw`` is the byte the device reported (0..255), or None for a
        frame that carried no vibration reading.

        Returns True when this reading completed the baseline.
        """
        if raw is None:
            return False
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return False
        self._readings.append(value)
        if not self._baseline_locked and len(self._readings) >= self.baseline_samples:
            self._baseline = sum(self._readings) / float(len(self._readings))
            self._baseline_locked = True
            return True
        return False

    @property
    def baseline(self) -> float | None:
        """The device's resting vibration reading, or None if not measured."""
        return self._baseline

    @property
    def ready(self) -> bool:
        """Whether enough samples have arrived to trust the baseline."""
        return self._baseline is not None

    def calibrated(self, raw) -> float | None:
        """The reading with the baseline removed, scaled to 0..1.

        None when the reading is missing or the baseline is not measured
        yet. The answer is CLAMPED at 0 -- a reading below the baseline
        is not a negative amount of vibration, it is "no vibration" --
        and it is NOT clamped at the top, because a reading far above
        the baseline is real information.
        """
        if raw is None or self._baseline is None:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        span = float(VIBRATION_COMMAND_MAX) - self._baseline
        if span <= 0.0:
            # The device reads at or above its own maximum at rest, so
            # there is no range to scale into. The honest answer is 0.
            return 0.0
        return max(0.0, (value - self._baseline) / span)

    def report(self, command: int, measured) -> dict:
        """One row describing a commanded level and what came back.

        ``command`` is what the operator asked for (0..255).
        ``measured`` is the byte the device reported, or None.

        The keys are the ones the API and the page use, so nothing has to
        be renamed on the way out.
        """
        cmd = max(0, min(VIBRATION_COMMAND_MAX, int(command)))
        calibrated = self.calibrated(measured)
        return {
            "command": cmd,
            "command_percent": round(cmd * 100.0 / VIBRATION_COMMAND_MAX, 1),
            "measured": None if measured is None else int(measured),
            "baseline": None if self._baseline is None else round(self._baseline, 2),
            "calibrated": None if calibrated is None else round(calibrated, 4),
            "gain": round(gain_factor(cmd), 4),
        }
