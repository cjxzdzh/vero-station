"""Voltage to battery percent, matching the app -- and the 1.2.3 smoothing.

THE CONVERSION
--------------
The device only reports a pack voltage (``dev.power`` in the caps JSON,
around 14.5-15.0 V). The app turns that into a percent like this:

    n1.java:296     parse dev.power
    n1.java:306     BatteryVoltageNormalizer.legacyPackVoltageToCell
                    -> pack / 4.0                (LEGACY_CELL_COUNT)
    utils/h.java:0  nearest table row by |table.v - cell|, then
                    round(row.percent * 100)
    BatteryVoltageNormalizer.normalizeMappedPercent
                    anything above 95 -> 100, then clamp to 0..100

So it is a lookup against a measured discharge curve, not a threshold ladder.
The curve is ``assets/battery_data.json`` from the APK: 122 points, byte for
byte identical between 1.1.5 and 1.2.3.

    100%  at or above   4.0609 V per cell  (16.24 V pack)
      0%  at or below   3.2719 V per cell  (13.09 V pack)

Note the plateau: the five rows computing to 96/97/98/99/100% all collapse to
100 after FULL_AT, so the battery reads full well below the top of the curve.

Nearest-neighbour is the app's algorithm and is kept rather than interpolated.
The curve is steep at both ends, so interpolation would invent precision the
measurements do not support, and matching the app means a number here agrees
with the number in the manufacturer's app.

THE SMOOTHING (1.2.3)
---------------------
1.1.5 converted each frame on its own, so the icon jittered with every noisy
reading. 1.2.3 wraps the same conversion in a filter:

  * samples a 10 s window (max 512 samples)
  * rejects voltages outside 0.0-10.0 V per cell
  * takes the MEDIAN of the window, not the newest frame
  * waits for 3 samples and a 1 s settle before the first reading
  * re-evaluates at most every 2 s
  * an INCREASE is never published (blocks the charge rebound)
  * a DECREASE needs 2 consecutive agreeing evaluations

The conversion itself is identical between versions, so smoothing changes how
steady the display is, never what a given voltage means. It is applied here
because a reading that flickers 35/37/35/37 is not a useful battery gauge.
"""

from __future__ import annotations

import bisect
import statistics
import time

#: Cells in series in the pack, per BatteryVoltageNormalizer.LEGACY_CELL_COUNT.
CELL_COUNT = 4.0

#: Percent at or above which the app shows a full battery.
FULL_AT = 95

#: The app's discharge curve, (cell volts, fraction 0-1), 122 points,
#: straight from assets/battery_data.json.
_CURVE = (
    (3.0000, -0.049123), (3.0264, -0.047260), (3.1036, -0.039965),
    (3.1560, -0.032520), (3.1947, -0.024968), (3.2254, -0.017333),
    (3.2502, -0.009634), (3.2719, -0.001880), (3.2908, 0.005923),
    (3.3079, 0.013769), (3.3234, 0.021653), (3.3379, 0.029574),
    (3.3516, 0.037526), (3.3640, 0.045509), (3.3761, 0.053522),
    (3.3872, 0.061564), (3.3975, 0.069630), (3.4074, 0.077721),
    (3.4173, 0.085834), (3.4263, 0.093969), (3.4353, 0.102126),
    (3.4440, 0.110305), (3.4523, 0.118505), (3.4604, 0.126723),
    (3.4685, 0.134962), (3.4762, 0.143217), (3.4840, 0.151493),
    (3.4917, 0.159788), (3.4995, 0.168100), (3.5072, 0.176431),
    (3.5150, 0.184783), (3.5227, 0.193150), (3.5305, 0.201536),
    (3.5382, 0.209939), (3.5460, 0.218359), (3.5537, 0.226800),
    (3.5615, 0.235259), (3.5689, 0.243736), (3.5763, 0.252230),
    (3.5835, 0.260743), (3.5903, 0.269269), (3.5968, 0.277814),
    (3.6033, 0.286374), (3.6092, 0.294949), (3.6151, 0.303537),
    (3.6204, 0.312139), (3.6256, 0.320753), (3.6306, 0.329378),
    (3.6349, 0.338017), (3.6393, 0.346665), (3.6430, 0.355321),
    (3.6467, 0.363988), (3.6501, 0.372663), (3.6535, 0.381350),
    (3.6566, 0.390040), (3.6594, 0.398737), (3.6625, 0.407441),
    (3.6653, 0.416155), (3.6684, 0.424875), (3.6709, 0.433600),
    (3.6737, 0.442333), (3.6768, 0.451069), (3.6799, 0.459815),
    (3.6830, 0.468568), (3.6864, 0.477330), (3.6898, 0.486099),
    (3.6941, 0.494877), (3.6979, 0.503667), (3.7025, 0.512465),
    (3.7065, 0.521273), (3.7115, 0.530092), (3.7165, 0.538923),
    (3.7211, 0.547765), (3.7264, 0.556620), (3.7313, 0.565486),
    (3.7363, 0.574364), (3.7419, 0.583256), (3.7472, 0.592160),
    (3.7524, 0.601077), (3.7580, 0.610006), (3.7636, 0.618949),
    (3.7689, 0.627905), (3.7744, 0.636873), (3.7803, 0.645855),
    (3.7856, 0.654851), (3.7915, 0.663859), (3.7974, 0.672882),
    (3.8033, 0.681918), (3.8092, 0.690968), (3.8151, 0.700033),
    (3.8213, 0.709112), (3.8278, 0.718207), (3.8340, 0.727316),
    (3.8405, 0.736439), (3.8470, 0.745580), (3.8541, 0.754735),
    (3.8612, 0.763907), (3.8684, 0.773097), (3.8761, 0.782304),
    (3.8839, 0.791531), (3.8919, 0.800775), (3.9000, 0.810039),
    (3.9084, 0.819323), (3.9167, 0.828628), (3.9254, 0.837951),
    (3.9338, 0.847294), (3.9419, 0.856659), (3.9499, 0.866042),
    (3.9580, 0.875442), (3.9657, 0.884863), (3.9735, 0.894302),
    (3.9815, 0.903760), (3.9908, 0.913235), (4.0008, 0.922735),
    (4.0125, 0.932261), (4.0271, 0.941820), (4.0438, 0.951415),
    (4.0609, 0.961052), (4.0773, 0.970728), (4.0922, 0.980440),
    (4.1102, 0.990191), (4.1530, 1.000000),
)

_CELL_V = [v for v, _ in _CURVE]

# ---------------------------------------------------------------- smoothing
# Defaults from DeviceBatteryEstimator.Config (1.2.3), read off the synthetic
# Config constructor's bitmask: 10000L, 512, 3, 1000L, 2000L, 0.0, 10.0, 2.

WINDOW_SECONDS = 10.0
MAX_WINDOW_SAMPLES = 512
MIN_INITIAL_SAMPLES = 3
INITIAL_STABILITY_DELAY_S = 1.0
EVALUATION_INTERVAL_S = 2.0
MIN_CELL_VOLTS = 0.0
MAX_CELL_VOLTS = 10.0
DECREASE_CONFIRMATIONS = 2


def pack_to_cell(pack_volts: float) -> float:
    """Pack voltage -> per-cell voltage."""
    return pack_volts / CELL_COUNT


def cell_to_percent(cell_volts: float) -> int:
    """Per-cell voltage -> 0-100, by nearest table row then the app's clamp."""
    # bisect on the ascending voltage list, then compare the two neighbours.
    i = bisect.bisect_left(_CELL_V, cell_volts)
    if i <= 0:
        row = _CURVE[0]
    elif i >= len(_CURVE):
        row = _CURVE[-1]
    else:
        lo, hi = _CURVE[i - 1], _CURVE[i]
        row = lo if abs(lo[0] - cell_volts) <= abs(hi[0] - cell_volts) else hi
    pct = round(row[1] * 100)
    if pct > FULL_AT:
        pct = 100
    return max(0, min(100, pct))


def pack_to_percent(pack_volts: float) -> int:
    return cell_to_percent(pack_to_cell(pack_volts))


class BatteryEstimator:
    """The 1.2.3 filter around :func:`pack_to_percent`.

    Feed it every pack voltage the device reports; ``percent`` is what the app
    would be showing. The conversion is unchanged from 1.1.5 -- this only
    decides *when* to publish a new number and which sample to trust.

    Reading ``percent`` before enough samples have arrived returns None rather
    than a number derived from one frame, because one frame is exactly the
    jitter 1.2.3 was written to remove.
    """

    def __init__(self, window_seconds: float = WINDOW_SECONDS,
                 max_samples: int = MAX_WINDOW_SAMPLES,
                 min_initial_samples: int = MIN_INITIAL_SAMPLES,
                 initial_stability_delay_s: float = INITIAL_STABILITY_DELAY_S,
                 evaluation_interval_s: float = EVALUATION_INTERVAL_S,
                 decrease_confirmations: int = DECREASE_CONFIRMATIONS,
                 clock=None):
        self.window_seconds = window_seconds
        self.max_samples = max_samples
        self.min_initial_samples = min_initial_samples
        self.initial_stability_delay_s = initial_stability_delay_s
        self.evaluation_interval_s = evaluation_interval_s
        self.decrease_confirmations = decrease_confirmations
        self.samples: list[tuple[float, float]] = []   # (wall clock, cell V)
        self.displayed: int | None = None
        self.first_sample_at: float | None = None
        self.last_evaluation_at: float | None = None
        self.consecutive_decreases = 0
        self.rejected = 0
        self.reason = "waiting for the first reading"
        # Injectable clock so a caller (or a test) can drive the filter on a
        # synthetic timeline without every method having to agree on one.
        self._clock = clock or time.monotonic
        self._max_seen: float | None = None

    # ------------------------------------------------------------------ feed
    def feed(self, pack_volts: float | None,
             now: float | None = None) -> None:
        if pack_volts is None:
            return
        try:
            cell = pack_to_cell(float(pack_volts))
        except (TypeError, ValueError):
            return
        if not (MIN_CELL_VOLTS <= cell < MAX_CELL_VOLTS):
            # The app rejects out-of-range samples before they reach the window,
            # so a bogus reading cannot drag the median.
            self.rejected += 1
            return
        now = self._clock() if now is None else now
        if self.first_sample_at is None:
            self.first_sample_at = now
        self.samples.append((now, cell))
        cutoff = now - self.window_seconds
        self.samples = [s for s in self.samples if s[0] >= cutoff]
        if len(self.samples) > self.max_samples:
            self.samples = self.samples[-self.max_samples:]

    def reset(self) -> None:
        self.samples.clear()
        self.displayed = None
        self.first_sample_at = None
        self.last_evaluation_at = None
        self.consecutive_decreases = 0
        self.rejected = 0
        self._max_seen = None
        self.reason = "waiting for the first reading"

    # ------------------------------------------------------------- evaluate
    def _window_median(self) -> float:
        return statistics.median(s[1] for s in self.samples)

    def evaluate(self, now: float | None = None) -> int | None:
        """Advance the filter and return the percent it would display.

        `now` is accepted for tests but must come from the same clock as
        :meth:`feed`. Comparing a caller-supplied timestamp against the
        monotonic clock that ``feed`` stamped the window with is how a
        "settling" state can outlive every sample ever fed.
        """
        now = self._clock() if now is None else now
        # Never let the clock go backwards: a caller-supplied timestamp older
        # than the window's newest sample would otherwise make the settle and
        # interval gates unreachable.
        if self._max_seen is not None and now < self._max_seen:
            now = self._max_seen
        self._max_seen = now
        if len(self.samples) < self.min_initial_samples:
            self.reason = ("waiting for %d samples (%d so far)"
                           % (self.min_initial_samples, len(self.samples)))
            return self.displayed
        if (self.displayed is None and self.first_sample_at is not None
                and now - self.first_sample_at < self.initial_stability_delay_s):
            self.reason = "settling before the first reading"
            return None
        if (self.last_evaluation_at is not None
                and now - self.last_evaluation_at < self.evaluation_interval_s):
            self.reason = "not due yet"
            return self.displayed

        self.last_evaluation_at = now
        candidate = cell_to_percent(self._window_median())

        if self.displayed is None:
            self.displayed = candidate
            self.consecutive_decreases = 0
            self.reason = "initial reading"
            return candidate
        if candidate > self.displayed:
            # Rising readings are suppressed outright: plugging the device in
            # briefly lifts the voltage and the gauge must not jump back up.
            self.consecutive_decreases = 0
            self.reason = "increase suppressed"
            return self.displayed
        if candidate == self.displayed:
            self.consecutive_decreases = 0
            self.reason = "unchanged"
            return self.displayed

        self.consecutive_decreases += 1
        if self.consecutive_decreases >= self.decrease_confirmations:
            self.displayed = candidate
            self.consecutive_decreases = 0
            self.reason = "decrease confirmed"
            return candidate
        self.reason = ("drop pending confirmation (%d/%d)"
                       % (self.consecutive_decreases, self.decrease_confirmations))
        return self.displayed

    @property
    def percent(self) -> int | None:
        return self.displayed

    def state(self) -> dict:
        cell = self._window_median() if self.samples else None
        return {
            "percent": self.displayed,
            "samples": len(self.samples),
            "rejected": self.rejected,
            "median_cell_volts": round(cell, 4) if cell is not None else None,
            "immediate_percent": (cell_to_percent(cell)
                                 if cell is not None else None),
            "raw_pack_volts": round(cell * CELL_COUNT, 3) if cell is not None
                              else None,
            "reason": self.reason,
        }


def battery(pack_volts: float | None) -> dict | None:
    """Un-smoothed report for a single pack voltage, or None when unknown.

    The immediate conversion, with nothing filtered. This is what a caller
    asking "what does this voltage mean" wants; :class:`BatteryEstimator` is
    what a caller asking "what does the device currently show" wants.

    ``percent`` is what the app would display for this voltage alone. The
    matched table row is reported too, so a surprising number can be checked
    against the curve instead of taken on faith.
    """
    if pack_volts is None:
        return None
    try:
        pack = float(pack_volts)
    except (TypeError, ValueError):
        return None
    cell = pack_to_cell(pack)
    pct = cell_to_percent(cell)
    row_v, row_pct = min(_CURVE, key=lambda r: abs(r[0] - cell))
    return {
        "pack_volts": round(pack, 3),
        "cell_volts": round(cell, 4),
        "percent": pct,
        "table_cell_volts": row_v,
        "table_percent": round(row_pct * 100),
        "in_range": _CURVE[0][0] <= cell <= _CURVE[-1][0],
        "cell_count": int(CELL_COUNT),
    }