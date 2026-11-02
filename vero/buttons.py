"""Device BUTTON events, and the bindings that turn them into actions.

WHERE THE BUTTON FRAME COMES FROM
----------------------------------
The vendor app dispatches button events inside the SAME ``A55A``/``A0``
branch it uses for telemetry, and it reads the button id out of the hex
string at a fixed position:

    analysis/decompile/jadx-123/sources/com/app/aike/utils/n1.java:308-336

        String substring6 = X.substring(8, 12);
        if (wf.h.N(substring6, "0B04")) { ... r1Var.Q.n("mode"); }
        ...
        if (wf.h.N(substring7, "0B03")) { ... r1Var.U.n("minus"); }
        ...
        if (wf.h.N(substring8, "0B02")) { ... r1Var.S.n("add");  }

``X`` is the UPPERCASE hex string of the whole notification, so the
characters 8..11 are BYTES 4 and 5 -- the 16-bit id
``(byte4 << 8) | byte5``. Those three ids are already named in
``vero/pressure.py``:

    EVENT_DATA_IDS = (0x0B02, 0x0B03, 0x0B04)

so the byte offsets are NOT re-derived here: this module imports
``HEADER``, ``SUB_ID_A0``, ``DATA_ID_OFFSET`` and ``EVENT_DATA_IDS``
from ``vero.pressure`` and reads the frame the way the app reads it.

``0B04`` is the MODE key, ``0B03`` is MINUS and ``0B02`` is ADD (the app's
own name for it; this module calls it ``plus``, because that is what the
key does and what the vendor app's own ``add``/``minus`` pair reads as to
an operator).

WHY THE FRAME WAS INVISIBLE UNTIL NOW
--------------------------------------
``pressure.decode_frame()`` DROPPED these frames on purpose (its
``EVENT_DATA_IDS`` check returns None), because a decoder that only tests
byte 3 would swallow a button event as a pressure sample. The drop was
correct as far as it went -- but it threw the event away, so the device's
own keys could never drive anything. This module is the other half of
that decision: it recognises what pressure.py discards, and nothing else
changes about ``pressure.decode_frame()``'s contract.

THE BUTTON FAMILY
-----------------
The app names three ids. A frame whose 16-bit id at bytes 4..5 has the
HIGH byte ``0x0B`` is a button-frame id, and an id outside the three the
app names is reported as ``unknown`` rather than dropped -- so a device
that grows a fourth key is visible instead of silently mis-read as
telemetry. This is the ONE place where this module goes beyond what the
app's code states, and it is stated here rather than left implicit:
the app's evidence is three ids, and the family rule is this module's
own inference from them.

THE BINDINGS
------------
A binding says "this key runs that action". The bindings live in a JSON
file under ``vero/data/state/``, which the repository's .gitignore already
covers (``state/``, line 51), so a runtime configuration never enters
version control -- the same rule the recordings follow (``records/``,
line 50). The file is written ATOMICALLY: a temporary file in the same
directory, then ``os.replace``. A half-written bindings file would be a
file the server cannot read, and a server that cannot read its bindings
would silently stop responding to the keys.

Every change bumps a VERSION. The page polls, so it has to be able to
tell "the bindings changed" from "the page is stale"; without a counter
the only way to find out is to reload.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from .pressure import (DATA_ID_OFFSET, EVENT_DATA_IDS, HEADER, SUB_ID_A0)

#: Where the bindings live. ``vero/data/`` already exists and already holds
#: ``battery_data.json``; ``state/`` is a new subdirectory under it, and
#: the repository's .gitignore already ignores every ``state/`` directory.
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
STATE_DIR = os.path.join(DATA_DIR, "state")
BINDINGS_FILE = os.path.join(STATE_DIR, "button_bindings.json")

#: The keys the device has, and the names this project gives them.
#:
#: ``plus`` is the vendor app's ``add`` (n1.java dispatches 0B02 to
#: ``r1Var.S.n("add")``), and ``minus`` is the app's own word for 0B03.
BUTTON_MODE = "mode"
BUTTON_PLUS = "plus"
BUTTON_MINUS = "minus"
BUTTON_NAMES = (BUTTON_MODE, BUTTON_PLUS, BUTTON_MINUS)

#: The 16-bit id at bytes 4..5 -> the key's name.
#:
#: The three ids are the vendor app's own (n1.java:308-336); the ORDER is
#: the app's order in the source, and the VALUES are what
#: ``pressure.EVENT_DATA_IDS`` already names.
DATA_ID_TO_BUTTON = {
    EVENT_DATA_IDS[2]: BUTTON_MODE,    # 0x0B04 -> "mode"
    EVENT_DATA_IDS[1]: BUTTON_MINUS,   # 0x0B03 -> "minus"
    EVENT_DATA_IDS[0]: BUTTON_PLUS,    # 0x0B02 -> "add"
}

#: The high byte of the button-id family. An id with this high byte is a
#: button frame even when the low byte is not one the app names; those are
#: reported as ``unknown`` rather than dropped.
BUTTON_FAMILY_HIGH = 0x0B

#: The BRANCHES a key frame may arrive on.
#:
#: ``A1`` is the one the DEVICE uses. Its own firmware builds the frame
#: in the function whose own log line is ``sent Key Value``
#: (analysis/firmware/fw-strings/frame_handlers.txt:1632-1650): it calls
#: the A5 5A builder with the data id ``0xa1`` and a two-byte payload of
#: ``0x0b`` and the key value. The builder
#: (analysis/firmware/fw-strings/control_path.txt:174-190) puts the data
#: id in byte 3, so the frame on the wire is::
#:
#:     A5 5A 08 A1 0B <key> <crc>
#:
#: ``A0`` is kept because the vendor app reads the key id from the same
#: place on either branch -- the hex string's characters 8..11, which are
#: bytes 4 and 5 -- so a device that puts a key id in an ``A0`` frame is
#: read the same way. It is the TELEMETRY branch, and a key id there
#: would be a telemetry frame the app would also read as a key.
#:
#: The previous version accepted ONLY ``A0``, which is why every real key
#: frame was thrown away: the device sends them on ``A1``.
BUTTON_BRANCHES = ("A1", SUB_ID_A0)

#: The shortest frame that still carries BYTES 4 and 5 -- the id itself.
#:
#: This is derived from ``DATA_ID_OFFSET`` rather than hard-coded, so it
#: moves with the constant it depends on. The app's own guard is
#: ``X.length() >= 12`` -- twelve HEX characters, i.e. six BYTES -- so a
#: six-byte frame is the shortest thing the app would even look at.
MIN_BUTTON_FRAME_LENGTH = DATA_ID_OFFSET + 2

#: The action kinds. Every binding names one of these.
ACTION_NONE = "none"
ACTION_TRAVEL = "travel"
ACTION_VIB = "vib"
ACTION_VIB_TOGGLE = "vib_toggle"
ACTION_AUTO_START = "auto_start"
ACTION_AUTO_STOP = "auto_stop"
ACTION_STOP = "stop"
ACTION_MODE = "mode"
#: The device's three NATIVE intensity steps.
#:
#: The vendor app sends exactly three, each a whole frame with a
#: hardcoded CRC. They are the only intensity control that does not
#: depend on the device's reported limits, so they are the fallback
#: when the scaled control frame is ignored.
ACTION_INTENSITY = "intensity"

#: The kinds that take a number after the colon.
NUMERIC_ACTIONS = (ACTION_TRAVEL, ACTION_VIB, ACTION_MODE, ACTION_INTENSITY)

#: The three native intensity ordinals: 0 = low, 1 = medium, 2 = high.
INTENSITY_MIN, INTENSITY_MAX = 0, 2

#: The travel percentage a ``travel:`` action may name.
TRAVEL_MIN, TRAVEL_MAX = 0, 100
#: The vibration level a ``vib:`` action may name.
VIB_MIN, VIB_MAX = 0, 255
#: The level ``vib_toggle`` switches TO when the motor is currently off.
#:
#: A toggle needs a "on" level, and the device has no notion of one --
#: ``require_vibration_allowed()`` refuses a level above 0 on a model
#: without a motor, and any level above 0 is as good as another. 128 is
#: the middle of the 0..255 range.
VIB_TOGGLE_ON = 128


class ActionError(ValueError):
    """An action string that cannot be used.

    A subclass of ValueError so a caller that only knows about ValueError
    still catches it -- the web layer turns it into a 422.
    """


def parse_action(action: str) -> tuple[str, Optional[int]]:
    """Parse one action string into ``(kind, argument)``.

    ``"travel:60"`` -> ``("travel", 60)``; ``"auto_start"`` ->
    ``("auto_start", None)``.

    Raises :class:`ActionError` (a ValueError) when the string names no
    known action, when it carries an argument it must not carry, or when
    the argument is out of range. The ranges are the DEVICE's own: travel
    is a percentage of the stroke, vibration is a 0..255 level, and a
    mode is one of the presets the project can actually put on the wire
    (``protocol.MODE_OPCODES``).

    The argument is parsed as a NUMBER and range-checked here, so a
    binding cannot be stored that would later be refused at execution
    time -- the failure has to be visible when the operator makes it, not
    when the key is pressed.
    """
    if not isinstance(action, str):
        raise ActionError("an action must be a string, not %r"
                           % type(action).__name__)
    text = action.strip()
    if not text:
        raise ActionError("an action must not be empty")
    if ":" in text:
        kind, _, raw_arg = text.partition(":")
        kind = kind.strip()
        raw_arg = raw_arg.strip()
    else:
        kind, raw_arg = text, ""
    if kind not in ACTION_KINDS:
        raise ActionError("unknown action %r; expected one of %s"
                           % (kind, ", ".join(sorted(ACTION_KINDS))))
    if kind in NUMERIC_ACTIONS:
        if not raw_arg:
            raise ActionError("the %r action needs a number, as %r"
                               % (kind, "%s:<n>" % kind))
        try:
            arg = int(raw_arg, 10)
        except ValueError:
            raise ActionError("the %r action needs a whole number, not %r"
                               % (kind, raw_arg)) from None
        if kind == ACTION_TRAVEL:
            if not (TRAVEL_MIN <= arg <= TRAVEL_MAX):
                raise ActionError("travel must be %d..%d, not %d"
                                   % (TRAVEL_MIN, TRAVEL_MAX, arg))
        elif kind == ACTION_VIB:
            if not (VIB_MIN <= arg <= VIB_MAX):
                raise ActionError("vibration must be %d..%d, not %d"
                                   % (VIB_MIN, VIB_MAX, arg))
        elif kind == ACTION_MODE:
            if arg not in MODE_PRESETS:
                raise ActionError("unknown mode %d; expected one of %s"
                                   % (arg, ", ".join(str(m) for m
                                                     in sorted(MODE_PRESETS))))
        else:                                   # ACTION_INTENSITY
            # The three native steps, and no others: the frames are
            # hardcoded, so there is no fourth one to send.
            if not (INTENSITY_MIN <= arg <= INTENSITY_MAX):
                raise ActionError(
                    "intensity must be %d..%d (the device's three native "
                    "steps: low, medium, high), not %d"
                    % (INTENSITY_MIN, INTENSITY_MAX, arg))
        return kind, arg
    if raw_arg:
        raise ActionError("the %r action takes no argument, but %r was given"
                           % (kind, action))
    return kind, None


#: The mode presets a ``mode:`` action may name.
#:
#: Imported from the protocol module rather than written out here, so the
#: set of modes this project can put on the wire has exactly one owner.
def _mode_presets() -> dict:
    from . import protocol as P
    return P.MODE_OPCODES


MODE_PRESETS = _mode_presets()

#: Every action kind, and whether it takes an argument.
ACTION_KINDS = {
    ACTION_NONE: None,
    ACTION_TRAVEL: int,
    ACTION_VIB: int,
    ACTION_VIB_TOGGLE: None,
    ACTION_AUTO_START: None,
    ACTION_AUTO_STOP: None,
    ACTION_STOP: None,
    ACTION_MODE: int,
    ACTION_INTENSITY: int,
}

#: The catalogue the page offers. It is a list of EXAMPLES, not a closed
#: set: ``travel:37`` is a legal action even though no example names it.
#: The examples are the ones an operator is most likely to want.
ACTION_CATALOG = [
    {"action": ACTION_NONE, "kind": ACTION_NONE, "arg": None,
     "label": "不操作"},
    {"action": "travel:0", "kind": ACTION_TRAVEL, "arg": 0,
     "label": "行程 0%（回缩）"},
    {"action": "travel:30", "kind": ACTION_TRAVEL, "arg": 30,
     "label": "行程 30%"},
    {"action": "travel:60", "kind": ACTION_TRAVEL, "arg": 60,
     "label": "行程 60%"},
    {"action": "travel:100", "kind": ACTION_TRAVEL, "arg": 100,
     "label": "行程 100%"},
    {"action": "vib:0", "kind": ACTION_VIB, "arg": 0,
     "label": "振动 0"},
    {"action": "vib:%d" % VIB_TOGGLE_ON, "kind": ACTION_VIB,
     "arg": VIB_TOGGLE_ON, "label": "振动 %d" % VIB_TOGGLE_ON},
    {"action": ACTION_VIB_TOGGLE, "kind": ACTION_VIB_TOGGLE, "arg": None,
     "label": "振动开/关"},
    {"action": ACTION_AUTO_START, "kind": ACTION_AUTO_START, "arg": None,
     "label": "开始自动模式"},
    {"action": ACTION_AUTO_STOP, "kind": ACTION_AUTO_STOP, "arg": None,
     "label": "停止自动模式"},
    {"action": ACTION_STOP, "kind": ACTION_STOP, "arg": None,
     "label": "停止（回缩）"},
    {"action": "intensity:0", "kind": ACTION_INTENSITY, "arg": 0,
     "label": "强度 低档"},
    {"action": "intensity:1", "kind": ACTION_INTENSITY, "arg": 1,
     "label": "强度 中档"},
    {"action": "intensity:2", "kind": ACTION_INTENSITY, "arg": 2,
     "label": "强度 高档"},
] + [{"action": "mode:%d" % m, "kind": ACTION_MODE, "arg": m,
      "label": "模式 %d" % m}
     for m in sorted(MODE_PRESETS)]


def decode_button_event(raw: bytes) -> Optional[dict]:
    """Decode one notification into a button event, or None.

    Returns ``{"button": ..., "data_id": ..., "raw": ...}``:

    * ``button`` -- ``"mode"``, ``"plus"``, ``"minus"`` or ``"unknown"``.
    * ``data_id`` -- the 16-bit id at bytes 4..5, which is what the app
      compares against (``0x0B04`` / ``0x0B03`` / ``0x0B02``).
    * ``raw`` -- the frame's uppercase hex, so a caller can show exactly
      what arrived.

    THE BRANCH IS NOT ``A0``. THIS WAS THE WHOLE BUG.

    The previous version required byte 3 to be ``A0``, on the reading
    that "the button dispatch lives inside the app's ``A0`` branch and
    nowhere else". That reading came from the app's DEcompiled source,
    and it was wrong about the frame the DEVICE sends.

    The DEVICE's own firmware says otherwise, and it says so in code
    (analysis/firmware/fw-strings/frame_handlers.txt:1632-1650, the
    function whose own log line is ``sent Key Value``)::

        void FUN_0000a288(void) {
          local_114 = 0x0b;              // the payload's first byte
          local_113 = FUN_00006bbc();    // the key value
          FUN_00009ebc(0xa1, &local_114, 2, auStack_10c);
          ...
        }

    and the builder it calls (``control_path.txt:174-190``)::

        *param_4 = 0xa5;  param_4[1] = 0x5a;
        param_4[2] = (char)(param_3 + 6);   // 2 + 6 = 8
        param_4[3] = param_1;               // 0xA1
        ...copy 2 payload bytes to param_4[4..5]...
        crc over param_4[0..5], stored LE at param_4[6..7]

    So the frame the device sends for a key press is::

        A5 5A 08 A1 0B <key> <crc_lo> <crc_hi>      (8 bytes)

    **Byte 3 is ``A1``, not ``A0``.** The old decoder rejected every
    one of them, and the live counter showed exactly that: the frames
    arrived, were counted as "other", and were shown as
    ``8B:A55A08A1`` -- the A1 signature, sitting right there in the
    panel while the button card said "0".

    And the app agrees with the firmware, not with the old decoder:
    the app's check is on the HEX STRING's characters 8..11, which are
    BYTES 4 and 5 -- and in the firmware's frame those bytes are
    ``0B`` and the key value. So::

        A5 5A 08 A1 0B 02 ...  ->  the app reads "0B02"  -> the PLUS key
        A5 5A 08 A1 0B 03 ...  ->  the app reads "0B03"  -> the MINUS key
        A5 5A 08 A1 0B 04 ...  ->  the app reads "0B04"  -> the MODE key

    The app's dispatch is gated on "is this an A55A frame of at least
    12 hex characters" -- NOT on byte 3 being A0 (n1.java:142 sets
    ``z10``, and the button dispatch is gated on that flag). The byte-3
    ``A0`` check in the app is the TELEMETRY branch, and the button
    dispatch is a SIBLING of it, not a child.

    So the decoder accepts the branch the device actually uses. ``A0``
    is still accepted -- a device that puts a key id in an ``A0`` frame
    is read too, because the app would read it the same way -- but
    ``A0`` alone is not what a real Vero sends.
    """
    if raw is None or len(raw) < MIN_BUTTON_FRAME_LENGTH:
        return None
    h = bytes(raw).hex().upper()
    if not h.startswith(HEADER):
        return None
    # The branch byte. The device sends its key frames on the A1
    # branch (the firmware's own "sent Key Value" builder) and the
    # vendor app reads the key id from the SAME place on either
    # branch -- bytes 4 and 5 -- so both are accepted.
    if h[6:8] not in BUTTON_BRANCHES:
        return None
    # Bytes 4 and 5, as the 16-bit id the app compares.
    data_id = (raw[DATA_ID_OFFSET] << 8) | raw[DATA_ID_OFFSET + 1]
    if (data_id >> 8) != BUTTON_FAMILY_HIGH:
        return None
    return {
        "button": DATA_ID_TO_BUTTON.get(data_id, "unknown"),
        "data_id": data_id,
        "raw": h,
    }


class ButtonBindings:
    """The stored bindings, and the version that says they changed.

    The class owns the FILE and nothing else: it does not know how an
    action is carried out, and it does not know what a device is. The
    executor is the web layer's job, so this class stays testable on its
    own and the control path stays in one place.

    The store is loaded on construction and re-read on demand, so two
    processes (or a test and the server) do not disagree about what is
    bound for long: every read goes to the file.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or BINDINGS_FILE
        self._bindings: dict[str, str] = {}
        self._version = 0
        self.load()

    # ------------------------------------------------------------ reading

    def load(self) -> dict:
        """Re-read the file. Returns the bindings that were loaded.

        A file that cannot be read -- missing, truncated, or holding
        something that is not a bindings document -- yields EMPTY
        bindings and version 0, never an exception. The bindings are a
        convenience; a broken file must not take the server down.
        """
        try:
            with open(self.path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            self._bindings = {}
            self._version = 0
            return dict(self._bindings)
        if not isinstance(doc, dict):
            self._bindings = {}
            self._version = 0
            return dict(self._bindings)
        raw = doc.get("bindings")
        bindings: dict[str, str] = {}
        if isinstance(raw, dict):
            for name, action in raw.items():
                if name not in BUTTON_NAMES:
                    continue
                if not isinstance(action, str):
                    continue
                try:
                    parse_action(action)
                except ActionError:
                    # A stored action the parser refuses is DROPPED, not
                    # passed on: it cannot be executed, and keeping it
                    # would mean the file and the parser disagree about
                    # what is legal.
                    continue
                bindings[name] = action
        version = doc.get("version")
        self._version = int(version) if isinstance(version, int) else 0
        self._bindings = bindings
        return dict(self._bindings)

    def bindings(self) -> dict:
        """Every binding, read from the file."""
        self.load()
        return dict(self._bindings)

    def get(self, button: str) -> Optional[str]:
        """The action bound to one key, or None."""
        return self.bindings().get(button)

    @property
    def version(self) -> int:
        """The version counter. It rises on every change."""
        self.load()
        return self._version

    # ------------------------------------------------------------ writing

    def set(self, button: str, action: str) -> dict:
        """Bind one key to one action. Returns the new state.

        Both the key and the action are validated BEFORE anything is
        written, so a refused binding leaves the file exactly as it was.
        """
        if button not in BUTTON_NAMES:
            raise ActionError("unknown button %r; expected one of %s"
                               % (button, ", ".join(BUTTON_NAMES)))
        kind, arg = parse_action(action)
        canonical = action if arg is None else "%s:%d" % (kind, arg)
        current = self.bindings()
        current[button] = canonical
        self._write(current)
        return {"button": button, "action": canonical,
                "bindings": dict(current), "version": self._version}

    def remove(self, button: str) -> dict:
        """Unbind one key. Returns the new state.

        Removing a key that is not bound is NOT an error: the state the
        operator asked for is the state that already holds.
        """
        if button not in BUTTON_NAMES:
            raise ActionError("unknown button %r; expected one of %s"
                               % (button, ", ".join(BUTTON_NAMES)))
        current = self.bindings()
        current.pop(button, None)
        self._write(current)
        return {"button": button, "bindings": dict(current),
                "version": self._version}

    def clear(self) -> dict:
        """Unbind every key."""
        # The version is refreshed from the file first, for the same
        # reason set() and remove() do it: the counter must continue
        # from what is ON DISK, not from what this object last saw.
        self.load()
        self._write({})
        return {"bindings": {}, "version": self._version}

    def _write(self, bindings: dict[str, str]) -> None:
        """Write the bindings file ATOMICALLY, and bump the version.

        The temporary file is in the SAME directory as the target, so the
        rename cannot cross a filesystem boundary (and therefore cannot
        fail for that reason), and the target file is never seen
        half-written: either the old content or the new one.
        """
        directory = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            pass
        # The version is read from the file that is on disk NOW, not from
        # the in-memory copy, so two writers cannot both write the same
        # number.
        version = self._version + 1
        doc = {
            "version": version,
            "updated_unix": round(time.time(), 3),
            "bindings": dict(bindings),
        }
        temp = self.path + ".tmp"
        try:
            with open(temp, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False, indent=2,
                          sort_keys=True, allow_nan=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp, self.path)
        except (OSError, TypeError, ValueError):
            # A file that could not be written must not be left behind as
            # a half-written temporary file, and the in-memory state must
            # not claim a change that did not happen.
            try:
                os.remove(temp)
            except OSError:
                pass
            raise
        self._bindings = dict(bindings)
        self._version = version

    # ------------------------------------------------------------ shape

    def as_dict(self) -> dict:
        """The whole state, in the shape the endpoints publish."""
        bindings = self.bindings()
        return {
            "bindings": bindings,
            "version": self._version,
            "buttons": list(BUTTON_NAMES),
            "actions": [dict(a) for a in ACTION_CATALOG],
        }


#: The process-wide store. Both API families read and write THIS one, so
#: the public family and the UI family cannot disagree about what a key
#: does.
_store: Optional[ButtonBindings] = None


def store(path: Optional[str] = None) -> ButtonBindings:
    """The process-wide bindings store.

    A path may be given to REPLACE the store (a test does this, so it
    writes its bindings into a temporary directory rather than into the
    repository). The replacement is permanent for the process.
    """
    global _store
    if path is not None:
        _store = ButtonBindings(path)
    elif _store is None:
        _store = ButtonBindings()
    return _store


def reset_store(path: Optional[str] = None) -> ButtonBindings:
    """Forget the process-wide store and build a new one."""
    global _store
    _store = ButtonBindings(path) if path is not None else None
    return store()
