"""A Gait: a list of positions, each held for a while, then the next.

WHAT THIS IS
------------
The automatic sweep moves between two positions forever, in a sine. A
GAIT is a SEQUENCE: go to 30%, hold for three seconds, go to 70%, hold
for two, go to 20%, hold, and start again.

The CLI has always been able to do this -- ``vera/cli.py``'s ``gait``
command walks a comma-separated list of percentages with a dwell and a
pause between them. But the walk lived in the CLI, so the WebUI could
not do it, and the CLI's version had two things the engine's version
does not: it does not survive a reconnect, and it holds each position
with a plain sleep rather than with the engine's own dwell.

So the walk becomes a first-class thing here, with the same
properties the sweep has: it runs in the engine, it is stopped by
``stop()``, and it reports what it is doing.

THE SHAPE OF A GAIT
-------------------
A gait is a list of STEPS, and a step is a position and a hold:

    [{"percent": 30, "hold": 3.0}, {"percent": 70, "hold": 2.0}]

The position is a percentage of the stroke (0..100) and the hold is in
SECONDS. A step may also name a VIBRATION level, because a gait that
ramps the position and one that ramps the strength are different
things and the operator may want both.

WHY THE ENGINE AND NOT THE CLI
------------------------------
Because the engine's dwell is not a sleep. The sweep's dwell is an
ABSOLUTE time in the future, and it is computed as
``max(0, dwell_until - now)`` -- which used to be
``min(dwell_until, now) - now``, always exactly zero, so the dwell
never happened. A gait that held its positions with a plain sleep
would have the same class of defect: the hold would not be the hold the
operator asked for.

So the gait drives the session the same way the sweep does -- by
commanding a target and letting the sweep's own machinery do the
holding -- and the "hold" is expressed as a target the session keeps
until the gait says otherwise.

THE LOOP
---------
The gait is a REPEATING sequence: when it reaches the end it starts
again, unless the operator asked for it to stop there.

The gait is also cancellable at any point, and cancelling it releases
the session -- so stopping a gait leaves the mechanism at rest, not
where it happened to be.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

#: The bounds on a step.
PERCENT_MIN, PERCENT_MAX = 0.0, 100.0
HOLD_MIN, HOLD_MAX = 0.0, 60.0
VIB_MIN, VIB_MAX = 0, 255

#: How many steps a gait may have. A gait is a sequence an operator
#: types in, so a bound is a guard against a typo turning into an
#: unbounded loop.
MAX_STEPS = 64


class GaitError(ValueError):
    """A gait that cannot be run.

    A subclass of ValueError, so a caller that only knows about
    ValueError still catches it -- the HTTP layer turns it into a 422.
    """


@dataclass
class GaitStep:
    """One step of a gait: a position, a hold, and a vibration level."""

    percent: float = 0.0
    hold: float = 0.0
    vibration: int = 0

    def clamp(self) -> "GaitStep":
        """The step, with every field inside its range."""
        return GaitStep(
            percent=max(PERCENT_MIN, min(PERCENT_MAX, float(self.percent))),
            hold=max(HOLD_MIN, min(HOLD_MAX, float(self.hold))),
            vibration=max(VIB_MIN, min(VIB_MAX, int(self.vibration))),
        )

    def as_dict(self) -> dict:
        return {"percent": round(self.percent, 1),
                "hold": round(self.hold, 2),
                "vibration": self.vibration}


@dataclass
class GaitState:
    """What the gait is doing right now."""

    running: bool = False
    #: The index of the step being held.
    step: int = 0
    #: How many times the sequence has started over.
    laps: int = 0
    #: The steps of the gait, as dictionaries.
    steps: list = field(default_factory=list)
    #: Whether the gait repeats or stops at the end.
    repeat: bool = True
    started_at: float = 0.0
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "step": self.step,
            "laps": self.laps,
            "steps": list(self.steps),
            "repeat": self.repeat,
            "elapsed": round(time.monotonic() - self.started_at, 1)
            if self.started_at else 0.0,
            "error": self.error,
        }


def parse_gait(steps, repeat: bool = True) -> list[GaitStep]:
    """Turn a list of step descriptions into steps, or refuse.

    A step may be:

      * a number -- "30" -- which is a position with no hold;
      * a mapping -- {"percent": 30, "hold": 3} -- which may also
        carry "vibration";
      * a two-element sequence -- [30, 3] -- which is the position and
        the hold.

    The three forms are the three ways an operator is likely to write
    one, and accepting all of them is what makes the thing usable from
    a text field, from JSON and from a form.

    Raises :class:`GaitError` for anything else, and for a value
    outside its range -- so a gait that would be refused later is
    refused NOW, when the operator is looking at it.
    """
    if not isinstance(steps, (list, tuple)):
        raise GaitError("a gait must be a list of steps, not %r"
                        % type(steps).__name__)
    if not steps:
        raise GaitError("a gait must have at least one step")
    if len(steps) > MAX_STEPS:
        raise GaitError("a gait may have at most %d steps, not %d"
                        % (MAX_STEPS, len(steps)))
    out: list[GaitStep] = []
    for i, raw in enumerate(steps):
        out.append(_parse_step(raw, i))
    return out


def _parse_step(raw, index: int) -> GaitStep:
    """One step, in any of the accepted forms.

    A step that is ALREADY a GaitStep is taken as it is -- so a caller
    that parsed a gait can hand the steps straight back, and a gait can
    be re-parsed without changing.
    """
    if isinstance(raw, GaitStep):
        return _check(raw, index)
    if isinstance(raw, bool):
        raise GaitError("step %d is %r; a step is a position, not a flag"
                        % (index, raw))
    if isinstance(raw, (int, float)):
        return _check(GaitStep(percent=_number(raw, index)), index)
    if isinstance(raw, dict):
        unknown = set(raw) - {"percent", "hold", "vibration"}
        if unknown:
            raise GaitError("step %d has unknown field(s) %s; a step is "
                            "percent, hold and vibration"
                            % (index, ", ".join(sorted(unknown))))
        step = GaitStep(
            percent=_number(raw.get("percent", 0.0), index),
            hold=_number(raw.get("hold", 0.0), index),
            vibration=int(raw.get("vibration", 0)),
        )
        return _check(step, index)
    if isinstance(raw, (list, tuple)):
        # A list of numbers is a PAIR -- a position and a hold -- and
        # not a list of steps. A list of MAPPINGS is a list of steps,
        # and the caller meant it as one step's worth.
        #
        # So a list of two is a pair, a list of one is a position, and
        # anything longer is a list of steps -- which is only sensible
        # if every element of it is itself a step. A list of three
        # NUMBERS is three positions with no holds, which is a
        # perfectly good gait.
        if len(raw) == 1:
            return _check(GaitStep(percent=_number(raw[0], index)), index)
        if len(raw) == 2:
            return _check(GaitStep(percent=_number(raw[0], index),
                                   hold=_number(raw[1], index)), index)
        if any(isinstance(x, (dict, GaitStep)) for x in raw):
            # A list that holds a MAPPING is a list of steps, and the
            # caller has put it where a single step goes. That is
            # ambiguous -- it could be one step with a mapping, or
            # several -- so it is refused rather than guessed at.
            #
            # The way to say what was meant is to pass the steps as
            # separate steps: parse_gait([{...}, {...}]).
            raise GaitError(
                "step %d is a list of steps; a step is one position and "
                "one hold -- pass the steps as separate steps, not nested "
                "inside one" % index)
        raise GaitError(
            "step %d has %d elements; a step is a position, or a position "
            "and a hold. A list of three or more numbers is three steps, "
            "so pass them as separate steps." % (index, len(raw)))
    raise GaitError("step %d is %r; a step is a number, a [position, hold] "
                    "pair, or a mapping" % (index, raw))


def _number(value, index: int) -> float:
    """A number, or a GaitError naming the step it was in.

    The conversion is here because a step's position and its hold are
    both numbers, and a non-number in either place has to be REFUSED --
    not crash the caller with a TypeError from float(). A caller that
    gets a GaitError can turn it into a 422; a caller that gets a
    TypeError can only report a 500.
    """
    if isinstance(value, bool):
        raise GaitError("step %d's %r is a flag, not a number"
                        % (index, value))
    try:
        return float(value)
    except (TypeError, ValueError):
        raise GaitError("step %d's %r is not a number"
                        % (index, value)) from None


def _check(step: GaitStep, index: int) -> GaitStep:
    """Refuse a step whose values are outside their ranges."""
    if not (PERCENT_MIN <= step.percent <= PERCENT_MAX):
        raise GaitError("step %d's position must be %g..%g, not %g"
                        % (index, PERCENT_MIN, PERCENT_MAX, step.percent))
    if not (HOLD_MIN <= step.hold <= HOLD_MAX):
        raise GaitError("step %d's hold must be %g..%g seconds, not %g"
                        % (index, HOLD_MIN, HOLD_MAX, step.hold))
    if not (VIB_MIN <= step.vibration <= VIB_MAX):
        raise GaitError("step %d's vibration must be %d..%d, not %d"
                        % (index, VIB_MIN, VIB_MAX, step.vibration))
    return step


def format_gait(steps) -> str:
    """The gait as the text the operator typed, for the UI to show back.

    The format is the ONE the parser accepts first: a comma-separated
    list of "position:hold" pairs. So the text round-trips through
    parse_gait without a second convention.
    """
    parts = []
    for s in steps:
        if s.hold:
            parts.append("%g:%g" % (s.percent, s.hold))
        else:
            parts.append("%g" % s.percent)
    return ",".join(parts)


def parse_gait_text(text: str) -> list[GaitStep]:
    """Parse the comma-separated form the UI's text field uses.

    "30:3, 70, 20:1.5" -- a position, or a position and a hold. The
    hold is optional and defaults to no hold.
    """
    if not isinstance(text, str):
        raise GaitError("a gait must be text, not %r" % type(text).__name__)
    raw = [p.strip() for p in text.split(",")]
    raw = [p for p in raw if p]
    if not raw:
        raise GaitError("a gait must have at least one step")
    steps = []
    for i, part in enumerate(raw):
        if ":" in part:
            pos, _, hold = part.partition(":")
            try:
                steps.append({"percent": float(pos.strip()),
                              "hold": float(hold.strip())})
            except ValueError:
                raise GaitError("step %d (%r) is not a position and a hold"
                                % (i + 1, part)) from None
        else:
            try:
                steps.append({"percent": float(part)})
            except ValueError:
                raise GaitError("step %d (%r) is not a number" % (i + 1, part)) from None
    return parse_gait(steps)
