"""Aike (cn.zhuimeng.amorlink) BLE protocol.

Two layers live here:

1. **Static analysis of the 1.1.5 (57) APK** — the wire format the app speaks:
   ``com.app.aike.utils.q`` (frame builders), ``e0`` (CRC16), the two notify
   parsers, and ``DeviceResolver`` (product-line detection). This is what
   ``build_control`` / ``build_mode`` implement.

2. **What the hardware actually answers** (captured 2026-10-09 from a Vero,
   sn ``UMVXEXAMPLE01``, BLE firmware 1.0.11): the device replies to the app's
   own opcode-7 query with an ``A5 5A`` framed, CRC16-checked **JSON** document,
   not the binary capability frame ``DeviceRuntimeCapsParser`` expects. That
   mismatch is why ``parse_caps`` returns ``None`` against real hardware — use
   ``parse_framed_json`` instead. The vendor evidently ships a newer firmware
   than the app was built against.

WHICH LAYER IS THE CURRENT PATH
--------------------------------
Layer 2 is. Everything the production session reads comes from the
``A5 5A`` JSON envelope: ``parse_framed_json`` and ``parse_device_info`` are
called by ``vero/session.py``, and ``build_framed_json`` is what the
simulator replies with.

The ``5A A5 55`` / ``5A A5 AA`` V13 decoders (``parse_status``,
``parse_caps``) belong to layer 1's successor -- the 1.2.3 app's
B-generation transport -- and have NO production caller here. They are
kept as offline analysis tools and are called from ``analysis/scripts/``
only. The device this project drives does not speak that family at all:
its firmware image contains zero ``5A A5`` byte pairs (see
``analysis/firmware/fw-strings/A55A_SURVEY.md`` section 3, "THE 5A A5 55
FAMILY -- ABSENT"). Each of those two functions says so in its own
docstring.

Pure-python, no dependencies.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------- constants

#: Stroke scale for the verified A0 control frame.
#: DeviceTravelMapper.UI_TRAVEL_MAX is 0x2710 (10000); the app maps its
#: 0..100 slider onto this range, and hardware confirmed 2000/5000/10000
#: give 20%/50%/100% travel.
#:
#: It lives HERE, at the top, rather than next to the frame decoders at the
#: bottom of this file, because build_control()'s default argument needs it:
#: a default of 100 meant "100 units of travel" while every other call site
#: meant "the full 10000-unit stroke", so the same percentage meant two
#: different things depending on which function was asked.
STROKE_MAX = 10000
#: Vibration scale for the same frame (8-bit field at offset 5).
VIBRATION_MAX = 0xFF

SERVICE_PRIMARY = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
SERVICE_SECONDARY = "6f430001-b5a3-f393-e0a9-e50e24dcca9e"

CHAR_WRITE_PRIMARY = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
CHAR_NOTIFY_PRIMARY = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
CHAR_WRITE_SECONDARY = "6f430002-b5a3-f393-e0a9-e50e24dcca9e"
CHAR_NOTIFY_SECONDARY = "6f430003-b5a3-f393-e0a9-e50e24dcca9e"

CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"

#: Frame families.
#:
#: Two families share the ``A5 5A`` prefix and one more starts with
#: ``5A A5``, and nothing in the module used to say which was which. The
#: decoders therefore ran on whatever arrived and produced garbage for the
#: other family:
#:
#:   * ``A5 5A`` ENVELOPE family -- byte 2 is the WHOLE-FRAME LENGTH and
#:     bytes 3-4 are the chunk index. This is what the A-generation
#:     device speaks: the JSON caps reply, and the 0x29 telemetry frames
#:     (whose byte 2 is 0x29, which happens to be the telemetry frame's
#:     LENGTH, and whose bytes 3-4 are NOT a chunk -- the telemetry frame
#:     is a different family that only shares the magic).
#:   * ``5A A5 55`` / ``5A A5 AA`` V13 family -- byte 2 is a DATA_ID.
#:     B-generation only; absent from this device's firmware.
#:
#: The rule that separates them is the magic, and it is checked where it
#: matters: the session only accepts ``A5 5A`` frames as identity, and the
#: V13 decoders only accept ``5A A5`` frames.
FRAME_MAGIC = b"\xa5\x5a"        # "A5" "5A"
FRAME_CRC_LEN = 2
FRAME_HEADER_LEN = 5             # magic(2) + total_len(1) + chunk(2)

#: The V13 family's magic: "5A" "A5". Distinct from FRAME_MAGIC, and the
#: difference is one byte of order -- which is exactly why the two families
#: were being confused.
V13_MAGIC = b"\x5a\xa5"
#: The V13 data ids. 0x55 is a status frame, 0xAA is a capability frame,
#: 0x00 is a motion frame and 0x0A is a mode frame (both written, never
#: read back).
V13_DATA_ID_MOTION = 0x00
V13_DATA_ID_MODE = 0x0A

H0, H1, H2 = 0x5A, 0xA5, 0x55
DATA_ID_STATUS = 85     # 0x55  notify: DeviceV13Status
DATA_ID_PARAMS = 170    # 0xAA  notify: DeviceRuntimeCaps
DATA_ID_CONTROL = 0     # 0x00  write: control frame (q.t)
DATA_ID_MODE = 7        # 0x07  write: mode switch  (q.u)

# Product lines, keyed on the serial-number prefix. App version 1.2.3:
#   jadx-123/sources/com/app/aike/model/DeviceResolver.java:17-21
# All four prefix lists read the same in jadx-123 and smali-123. App 1.1.5
# carries only vero/mira/nexo prefixes -- it has no "WYRH" and no WYRHEXAMPLE02
# at all (see the version note above DEVICE_PROFILES), which is why a citation
# giving a line number but no version directory is ambiguous: the same file
# exists in both decompilations at different line numbers. Cite the version.
PRODUCTS = {
    "Vero":  ("ALVX", "UMVX"),
    "Mira": ("ALMI", "UMMI"),
    "Nexo": ("ALNX", "UMNX"),
    "Wyrh": ("WYRH",),
}
# Nexo is forced onto the B-generation binary transport.
FORCED_B_GENERATION = {"ALNX", "UMNX"}

# Per-model capability table.
#
# EVIDENCE CLASS: static decompilation of the VENDOR APP, version 1.2.3. These
# are the booleans the app's own code branches on, NOT anything the hardware
# reports. The Vero in hand (sn UMVXEXAMPLE01) answers the caps query with an
# A5 5A JSON document carrying only sn / power / ble_1_fw / mcu1_fw / mcu2_fw /
# RSSI / "MTU:" -- it never states its own capabilities, so nothing here can be
# confirmed from the wire. Treat every row as "what the app assumes about this
# model", which is exactly why the UI has to mirror the app: driving a Vero's
# vibration control is not an error against the hardware, it is an error
# against the product line.
#
# VERSION NOTE -- why every citation below names its directory. The app ships
# in two decompiled forms and the profile table changed between them:
#   * jadx-123 / smali-123 = app 1.2.3, DeviceKind has FIVE values
#     (VERO / MIRA / NEXO / WYRH / UNKNOWN), DeviceProfile's constructor takes
#     FIFTEEN parameters, and WYRHEXAMPLE02 exists.
#   * app 1.1.5 has only FOUR DeviceKind values -- no WYRH, no WYRHEXAMPLE02 --
#     and an ELEVEN-parameter constructor, so hasVibration / hasPositionSensors
#     sit at different parameter positions there.
# The 1.1.5 decompilation is not tracked in this repo (its scratch scripts
# point at <research-tree>, outside this workspace), so an
# UNQUALIFIED citation -- file name plus line number, no version directory --
# is unresolvable here: the ledger's citation gate accepts a line that exists
# and is non-empty, which 1.1.5's line 30 also satisfies, while pointing at
# different code. Hence every path below is version-qualified. Wyrh being in
# PRODUCTS but unfindable in a 1.1.5 lookup is exactly this.
#
# Source lines, one per row, all in app 1.2.3:
#     jadx-123/sources/com/app/aike/model/DeviceResolver.java:24  Vero
#     jadx-123/sources/com/app/aike/model/DeviceResolver.java:26  Mira
#     jadx-123/sources/com/app/aike/model/DeviceResolver.java:28  Nexo
#     jadx-123/sources/com/app/aike/model/DeviceResolver.java:30  Wyrh
# Re-verified for all four rows against analysis/decompile/smali-123/.../
# DeviceResolver.smali -- the clinit builds all five profiles in one method with
# reused registers, so the booleans are only readable by tracking register state
# linearly, and 00-INDEX.md:60 says jadx constants "are often wrong" while
# smali is authoritative. Both sources agree: Vero v10=0x0 / v11=0x1,
# Mira v11=0x1 / v12=0x1, Nexo v12=0x0 / v13=0x0, Wyrh v13=0x1 / v14=0x0.
# Corroborated from the other direction in the UI activities: Vero's screen
# calls setVibrationControlsVisible(false) (DeviceControlVexActivity.java:669)
# while Mira's calls setVibrationControlsVisible(true)
# (DeviceControlMiraActivity.java:806).
#
# EVIDENCE TIER PER ROW -- only a Vero was available to test; Mira / Nexo / Wyrh can never
# be confirmed on hardware in this project:
#     Vero  = user-reported "no vibration" AND source False. The only row with
#             hardware-side evidence, and the only one a real Vero will hit.
#     Mira / Nexo / Wyrh = source only. Never executed on real hardware.
# Consequence worth stating plainly: on the one device we own, Vero's
# has_vibration is False, so the ALLOW branch of require_vibration_allowed()
# (recognised model that HAS vibration) is unreachable on hardware and is
# covered only by test doubles. That is a coverage boundary, not a bug -- do
# not read a green suite as "the allow path was verified against a device".
#
# `has_position_sensors` here matches the app's hasPositionSensors flag. It does
# NOT mean our decoder handles that model: the app selects the position-decoding
# threshold per model (controlDeviceType == "Vx" ? 800 : 200, v1.smali:2895/2903
# -- "Vx" = Vero/Nexo, "Mi" = Mira/Wyrh), and monitor.py still hardcodes the
# Vero threshold, so a Mira/Wyrh position reading is computed with Vero's
# threshold and is wrong. Do not read this row as "position is handled".
#
# THE POSITION NOW FLOWS, so the row's truth or falsity CAN reach a reader.
#
# monitor.py's `locate()` is fed by the 0x29 telemetry frames, and the session
# reads the position from that monitor (session.position_percent() ->
# _reported_position()). It used to be that nothing wrote
# `SessionStats.reported_percent` -- so the session had no reported position
# at all, the row's truth or falsity never reached a reader, and the closed
# loop could never close.
#
# So the Mira/Wyrh threshold is now VISIBLE rather than hidden: a Mira's
# position reading is computed with Vero's threshold and is wrong, and a
# reader can now see that it is wrong. That is the honest state of it: the
# threshold is per-model in the app (controlDeviceType == "Vx" ? 800 : 200,
# v1.smali:2895/2903 -- "Vx" = Vero/Nexo, "Mi" = Mira/Wyrh) and this project
# still uses one threshold for every model.
#
# UNKNOWN models get no row at all, and therefore None rather than False -- see
# device_profile() below and session.py's require_vibration_allowed().
DEVICE_PROFILES = {
    "Vero":  {"has_vibration": False, "has_position_sensors": True},
    "Mira":  {"has_vibration": True,  "has_position_sensors": True},
    "Nexo":  {"has_vibration": False, "has_position_sensors": False},
    "Wyrh":  {"has_vibration": True,  "has_position_sensors": False},
}

#: Which models this project can actually DRIVE.
#:
#: The scan list shows only the devices that can be connected and
#: controlled. A device whose model this build cannot drive is not
#: offered, because offering it would offer a device that cannot work.
#:
#: The set is derived from the transport, not from a second hand-written
#: table, because the two would drift:
#:
#:   * A-generation (Vero / Mira / Wyrh) -- the send path writes
#:     :func:`app_a0_control`, the A0 text frame this project transcribed
#:     from the vendor app's smali, and the telemetry path reads the A
#:     generation's 0x29 frames. Both work, and Vero is the model that was
#:     verified on real hardware.
#:
#:   * B-generation (Nexo) -- the send path is NOT wired to the V13
#:     multi-point transport at all. Every write in session.py goes
#:     through :func:`app_a0_control`. Connecting a Nexo therefore
#:     produces a link that comes up and then does nothing, because the
#:     frames the device wants are never sent. That is not a matter of
#:     degree: it is a device that cannot be moved by this build.
#:
#: So "controllable" here means "this build's send path can drive it",
#: NOT "the vendor app can drive it". Wyrh is included on the strength of
#: the A-generation transport alone: it has never been run against real
#: hardware in this project, and its note says so.
#:
#: A model that is NOT in this table -- an unrecognised serial -- is
#: treated as UNKNOWN rather than as controllable, because the whole
#: point of the filter is that a device this build cannot drive must not
#: be offered as one it can. :func:`is_controllable` is the only reader
#: of this table.
CONTROLLABLE_MODELS = ("Vero", "Mira", "Wyrh")

#: The models this build CANNOT drive, with the reason. This is the
#: inverse of :data:`CONTROLLABLE_MODELS`, and it exists so the refusal
#: can say WHY -- "it is a Nexo and the V13 transport is not wired in" is
#: a different message from "we do not recognise it".
NON_CONTROLLABLE_REASONS = {
    "Nexo": ("Nexo 走 B 代（V13 多点打包帧）传输，"
             "而本版本的发送路径未接入该协议，连接后无法控制。"),
}

# Mode opcodes (q.u(int) switch, q.java:534-556). UI passes 52..56.
# Keys are the firmware's HEX values. FUN_000080dc compares against 0x52..0x57
# as raw bytes, and 1.1.5's q.s() emits String.valueOf(mode) as hex text, so
# "82" on the wire means 0x52. Decimal 52 would put byte 0x34 there instead.
#
# The table used to be the IDENTITY map {0x52: 0x52, ... 0x57: 0x57}, which
# claimed to do a conversion and did none. The conversion it was supposed to
# encode is real and is in the app's own code:
#
#   analysis/decompile/jadx-123/sources/com/app/aike/utils/r1.java
#   K0(int i, ...) -- the B-generation mode path:
#       int i10 = 82;
#       switch (i) {
#           case 52: i10 = 82; break;
#           case 53: i10 = 83; break;
#           case 54: i10 = 84; break;
#           case 55: i10 = 85; break;
#           case 56: i10 = 86; break;
#           default:  i10 = clamp(i, 0, 255);
#       }
#       byte[] encode = DeviceV13ModeEncoder.INSTANCE.encode(i10);
#
# So the UI preset 52 is sent to a B-generation device as the BYTE 82
# (0x52), 53 as 83 (0x53), and so on: the UI number and the wire byte
# differ by 30.
#
# The identity map hid that. It also made any assertion built on it
# vacuous: analysis/scripts/verify.py asserts
# `m[9] == P.MODE_OPCODES[mode_id]`, and an identity map is satisfied by
# every input, so the assertion could never fail and was verifying
# nothing.
#
# This table now states the CONVERSION, keyed by the UI preset:
MODE_OPCODES = {
    52: 0x52,   # online                -> 82 (0x52) on the wire
    53: 0x53,   # online wait connect   -> 83 (0x53)
    54: 0x54,   # online Active         -> 84 (0x54)
    55: 0x55,   # online Passage        -> 85 (0x55)
    56: 0x56,   # online interactive     -> 86 (0x56)
}
#: The OTA mode. It has no UI preset -- the app's switch has no case for
#: it, so it falls into the default branch and is sent as itself.
MODE_OPCODE_OTA = 0x57
#: The firmware's own names for the same bytes. This is the SAME byte
#: space as :data:`MODE_OPCODES` and :data:`MODE_NAMES`, and the three
#: tables were three different naming schemes for one byte:
#:
#:   byte 0x54 -> MODE_NAMES says "ramp" (the UI preset's name)
#:             -> FIRMWARE_MODES says "active" (the firmware's name)
#:             -> the UI index is 54
#:
#: They are not interchangeable: one is what the OPERATOR picked and the
#: other is what the FIRMWARE calls the state. The status frame reports
#: the firmware's byte, so the firmware's names are the right ones to
#: show for a reported mode, and the UI preset's names are the right ones
#: to show for a mode the operator chose.
#:
#: 0x57 is "ota-online" in FIRMWARE_MODES and has no UI preset, which is
#: why it is absent from MODE_NAMES and from MODE_OPCODE_TO_UI. That is a
#: fact about the tables, not a gap: the OTA mode is not something the
#: operator picks.
MODE_NAMES = {
    52: "pulse",
    53: "wave",
    54: "ramp",
    55: "steady",
    56: "custom",
}
# The firmware reports a *shifted* opcode in its status frame (UI 52-56 is sent
# as 82-86), so map the reported value back to the preset the UI asked for.
#
# The reported byte for the UI presets is 0x52..0x56 -- the SAME bytes
# MODE_OPCODES maps to. The comment above this table used to claim the
# firmware reports 82-86 (i.e. 0x52-0x56 as decimal 82-86), which reads
# as a different byte range from the one the firmware actually uses
# (FUN_000080dc compares raw bytes against 0x52..0x57). The table below is
# the byte-for-byte truth: the firmware's byte 0x52 IS the UI preset 52.
MODE_OPCODE_TO_UI = {0x52: 52, 0x53: 53, 0x54: 54, 0x55: 55, 0x56: 56}

# Channel identifiers used as the 2nd arg of t(level, channel). The app only
# ever passes 0, but the field is carried on the wire.
CHANNEL_MOTOR = 0
CHANNEL_VIBRATION = 1


# ---------------------------------------------------------------- crc / frame

def crc16(data: bytes) -> int:
    """CRC-16 as implemented by com.app.aike.utils.e0.c(byte[]).

    poly 0x1021, init 0xFFFF, MSB-first, no reflection, then the two result
    bytes are swapped. Self-check: crc16(b"123456789") == 0xB129.

    This is the APP's convention. The device's ``A5 5A`` envelope stores
    the raw CCITT-FALSE value LITTLE-ENDIAN instead, so the same bytes
    read two different ways depending on which side is looking:

        crc16(data)           == byteswap(crc16_ccitt_false(data))
        envelope tail         == crc16_ccitt_false(body).to_bytes(2, "little")

    Both functions live in this module and both have a caller -- this one
    is what the A-generation TEXT protocol appends, and
    :func:`crc16_ccitt_false` is what the envelope's CRC is. They are two
    conventions over one algorithm, not two implementations of it: the
    only difference is the final byte swap, and
    scripts/crc_regression.py asserts exactly that relationship.

    The duplication that used to be here was a THIRD copy of the algorithm
    in a file that had no caller, which was one more chance to pick the
    wrong one.
    """
    return ((crc16_ccitt_false(data) >> 8) & 0xFF) | \
        ((crc16_ccitt_false(data) & 0xFF) << 8)


def little_to_big_endian_hex(value: int, width: int = 4) -> str:
    """e0.h() — format as hex, then swap byte order inside the width.

    WARNING: `width` is the number of HEX CHARACTERS, not bytes. The A0 control
    frame's drive field is 4 BYTES and every call site passed width=4, which
    silently produced 2 bytes and a 12-byte frame where the app sends 13.
    Use bytes_to_be_hex() when you mean bytes.
    """
    text = "%0*X" % (width, value & ((1 << (4 * width)) - 1))
    return "".join(reversed([text[i:i + 2] for i in range(0, len(text), 2)]))


# ---------------------------------------------------------------- builders

def build_handshake() -> bytes:
    """First frame after connect: {0x5A, 0xA5, 0xAA} (q.java:459)."""
    return bytes([H0, H1, 0xAA])


def build_control(level: int, channel: int = 0, elapsed_ms: int = 0,
                  device_max: int = STROKE_MAX, extra: Optional[int] = None,
                  gen: str = "B", vib_raw: int = 0,
                  vib_max: int = 0) -> bytes:
    """Control frame — the motor/vibration level command (q.t(int, int)).

    Read from the decompiled ``q.t(int i, int i10)`` (q.java:495-541). The two
    arguments feed *different* fields, which is easy to get backwards:

    ``i``  — the UI value 0-10000. It never reaches the frame directly; it is
             mapped through ``DeviceTravelMapper.uiToDevice(i, strokeMax)`` and
             lands in the trailing ``int`` at offset 12.
    ``i10``— a 0-255 vibration value, rescaled as
             ``clamp(round(i10 / 255 * vibMax), 0, vibMax)`` into the ``short``
             at offset 4. The app always passes 0 here, so that field is 0
             unless the caller drives vibration explicitly.

    gen="B": 16-byte little-endian frame::

        5A A5 55 00 | vib u16 | chan u16(=1) | elapsed u32 | drive i32

    ``gen="A"`` was removed. It dispatched to :func:`bytes_to_be_hex`, which is
    not defined anywhere in this package, so the branch raised ``NameError`` on
    every call — proof that nothing ever called it. Its field list was also
    wrong: it emitted ``["B0","B0","A0",<vib>,"0F",<drive>]`` where
    :func:`app_a0_control` — transcribed instruction by instruction from
    ``q.smali:4800-5001`` and used by every production write path — emits
    ``["B0",<vib>,"A0","01","0F",<drive>]``. The third element is the constant
    ``"01"``, not a second vibration byte. Use :func:`app_a0_control` for the
    A-generation text frame.

    ``device_max`` is the device's FULL STROKE in stroke units, and it
    defaults to :data:`STROKE_MAX` (10000) — the app's own
    ``DeviceTravelMapper.UI_TRAVEL_MAX``.

    It used to default to 100, which made the default wrong by a factor of
    100: ``build_control(5000)`` produced a drive of 50 rather than 5000, so
    every caller that did not pass ``device_max`` explicitly commanded one
    percent of travel while believing it had commanded half. The unit of
    "percent" was therefore not the same in the two places that accept it
    (:func:`build_control` and :func:`app_a0_control`), and the two scripts
    in analysis/ that call this function passed 100 and 10000 respectively
    -- so their travel conclusions were not comparable.
    """
    if gen.upper() == "B":
        level = max(0, min(10000, int(level)))
        if extra is None:
            limit = device_max if device_max and device_max > 0 else 0
            drive = int((level / 10000) * limit) if limit else level
        else:
            drive = max(0, min(0x7FFFFFFF, int(extra)))
        raw = max(0, min(255, int(vib_raw)))
        if vib_max and vib_max > 0:
            vib_value = max(0, min(vib_max, int((raw / 255.0) * vib_max + 0.5)))
        else:
            vib_value = raw
        return struct.pack(
            "<BBBBHHIi",
            H0, H1, H2, DATA_ID_CONTROL,
            max(0, min(0xFFFF, vib_value)),
            1 if channel is None else max(0, min(0xFFFF, int(channel))),
            max(0, min(0xFFFFFFFF, int(elapsed_ms))),
            drive,
        )

    raise ValueError(
        "gen=%r is not supported; the A-generation text frame is "
        "app_a0_control()" % (gen,))


def build_mode(mode: int, gen: str = "B", device_max: int = 10000) -> bytes:
    """Mode command.

    gen="A" is the app's own frame — identical to app_set_mode(), routed through
    the same builder so there is exactly one implementation.

    gen="B" is the 1.2.3 V13 mode encoder (18 bytes, little-endian).
    """
    if gen.upper() == "A":
        return app_set_mode(mode)
    return build_v13_mode(mode)


def app_a0_control(drive: int, vibration: int = 0) -> bytes:
    """The app's A0 control frame, transcribed from smali instruction by
    instruction (smali-out/com/app/aike/utils/q.smali:4800-5001).

    Six add() calls, verified line by line against q.smali:4802-4960:

        [0] "B0"                                    4802  literal
        [1] e0.h(format("%02X", p2))                4811-4864  vibration
        [2] "A0"                                    4869  literal
        [3] format("%02X", 1)  -> "01"             4878-4907  CONSTANT, no e0.h
        [4] "0F"                                    4913  literal
        [5] e0.h(format("%04X", v2))                4922-4955  drive, byte-swapped

    Element [3] is the constant 1, not a vibration byte -- v5 is set once at
    4407 (const/4 v5, 0x1) and is never rewritten on the text branch. An
    earlier transcription read this slot as a second "B0" and as a vibration
    field; both were wrong, and because the list still had six elements the
    frame length stayed correct, so length-and-CRC self-consistency checks
    never caught it. The wrong bytes sat at offsets 5 and 7.

    Layout:
        A5 5A 0D A0 | B0 | <vib:1> | A0 | 01 | 0F | <drive:2 LE> | crc:2

    BYTE ORDER OF THE DRIVE FIELD: LITTLE-ENDIAN, and this is settled from
    the device's own firmware rather than inferred.

    The comment here used to say ``<drive:2 BE>``, which was wrong, and the
    simulator read the field little-endian -- so the documentation and the
    code disagreed about the same two bytes, and neither could be shown to
    be the device's behaviour.

    The firmware settles it. ``FUN_00009d2c`` is the device's own A0 control
    parser (analysis/firmware/ota/assets/
    BIN_FA004_SRSN_ALMiA_V1.0.2_2506026_Beta.bin, disassembly at file
    offset 0x9d2c -- vaddr == file offset for this image, see
    analysis/firmware/fw-strings/A55A_SURVEY.md):

        9d30  ldrb  r0, [r0, #0]        ; p[0]  == 0xB0 -> mode tag
        9d34  cmp   r0, #176
        9d38  ldrb  r0, [r5, #1]        ; p[1]  -> the vibration byte
        9d3e  ldrb  r0, [r5, #2]        ; p[2]  == 0xA0 -> control tag
        9d66  ldrb  r7, [r5, #3]        ; p[3]  -> the repeat count
        ...
        9d80  ldrb  r1, [r0, #5]        ; low  byte  <- p+5
        9d82  strb  r1, [r3, #0]
        9d84  ldrb  r0, [r0, #6]        ; high byte  <- p+6
        9d86  strb  r0, [r3, #1]
        9d88  ldrh  r0, [r3, #0]        ; the 16-bit value, LITTLE-ENDIAN

    ``p`` is the frame's ``B0`` byte, i.e. frame offset 4, so ``p+5`` and
    ``p+6`` are frame offsets 9 and 10 -- exactly the drive field. The two
    bytes are stored low-byte-first into a stack slot and then read back
    with ``LDRH``, which on this ARM:LE:32:Cortex target assembles the low
    address as the LOW byte. The device therefore reads the drive value
    LITTLE-ENDIAN.

    That is also why the app byte-swaps the hex text: ``e0.h()`` renders
    5000 as "1388" and swaps it to "8813", so the bytes on the wire are
    88 13, and the device's little-endian read of 88 13 gives 0x1388 =
    5000. The swap is not the device's byte order -- it is the app
    cancelling the device's little-endian read out.

    The same conclusion is visible on the wire: a real "1 %" control frame
    (analysis/evidence/hw_retest.json) carries bytes 9..10 = 64 00, and
    little-endian that is 100 while big-endian would be 25600.
    """
    vib = int(vibration) & 0xFF
    items = ["B0", little_to_big_endian_hex(vib, 2), "A0", "01", "0F",
             little_to_big_endian_hex(int(drive) & 0xFFFF, 4)]
    return build_text_frame(13, str_arg="A0", fields=items)


def build_text_frame(frame_len: int, payload: bytes = b"", mode_tag: str = "",
                     fixed_crc: Optional[str] = None,
                     str_arg: str = "", fields: Optional[list] = None) -> bytes:
    """A-generation text frame — a faithful port of ``q.s(str, i, list, fixedCrc)``.

    The decompiled signature is ``q.s(String str, int i, List list, String fixedCrc)``
    and the body is assembled in this exact order (q.java:456-484)::

        body = "A55A"
              + format("%02X", i)      // the FRAME LENGTH, not an opcode
              + str                     // the FIRST string argument
              + each element of list    // in order
        if fixedCrc != null: append fixedCrc
        else:                 append format("%04X", e0.c(body))

    then the whole string is hex-decoded into the bytes written to the
    characteristic. ``fields`` maps to the ``list`` argument — pass it as an
    ordered list of hex strings. ``mode_tag``/``payload`` are the byte-oriented
    convenience form used by :func:`build_caps_query`.

    ``frame_len`` is the LENGTH OF THE WHOLE FRAME, and it is the first
    parameter because that is where it sits on the wire -- byte 2 of an
    ``A5 5A`` frame. It was called ``opcode``, which is what made two call
    sites disagree about the same argument: :func:`app_a0_control` passed
    13 (the frame's length) while :func:`build_caps_query` passed 7 (which
    happens to be both a plausible opcode AND the length of that frame). A
    caller who followed the parameter NAME would have built a frame with a
    length field of 176 and 8 actual bytes.

    Two details come from the smali and are easy to get wrong:

        * ``l()`` is the hex decoder and it throws on an odd-length input.
        * ``e0.c()`` is CRC-16/CCITT-FALSE **with a final byte swap**, so the
          value written into the text is not the raw CRC integer.

    This function used to be defined twice in this module. The two bodies
    were byte-identical (verified by comparing ``ast.unparse`` of the bodies
    with the docstrings stripped) and the signatures matched exactly, so the
    second definition silently shadowed the first and any edit to the first
    had no effect. The surviving docstring carries both originals; the
    duplicate was removed rather than left as a trap.
    """
    parts = ["A55A", "%02X" % (int(frame_len) & 0xFF)]
    if str_arg:
        parts.append(str_arg)
    if mode_tag:
        parts.append(mode_tag)
    if fields:
        parts.extend(str(f) for f in fields)
    if payload:
        parts.append(payload.hex().upper())
    body = "".join(parts)

    if fixed_crc is not None:
        if len(fixed_crc) != 4:
            raise ValueError("fixedCrc must be 4 characters")
        tail = fixed_crc.upper()
    else:
        # THE CRC IS OVER THE DECODED BYTES.
        #
        # The firmware is the authority, and it hashes bytes:
        #
        #     crc = FUN_0000ad44(0xFFFF, frame, payload_len + 4);
        #            // over frame[0:len-2]
        #     frame[len-2] = crc & 0xff; frame[len-1] = crc >> 8;
        #
        # (analysis/firmware/fw-strings/A55A_SURVEY.md, §1 and §5 --
        # five scanners, all of them the same envelope.) And direct
        # recomputation over the frames the device actually ANSWERED
        # confirms it: caps A5 5A 07 00 01 .. 1E 90, mode A5 5A 08 B0
        # 3D 03 .. 33 C7 and mode A5 5A 08 B0 3D 52 .. E7 8D all
        # validate under the BYTE rule and under none of the others.
        #
        # It was changed once to hash the hex TEXT instead, on the
        # strength of analysis/evidence/hw_retest.json -- but those
        # frames were BUILT by the analysis tool, and their tails match
        # the text rule only because the tool's own copy of the CRC had
        # been changed. The device's replies are the only frames that
        # say what the device accepts, and they say bytes.
        decoded = bytes.fromhex(body)
        raw = crc16_ccitt_false(decoded)
        swapped = ((raw >> 8) & 0xFF) | ((raw & 0xFF) << 8)
        tail = "%04X" % swapped
    return bytes.fromhex(body + tail)


# ---------------------------------------------------------------- V13 frames
#
# Recovered from the 1.2.3 decompile (``com.app.aike.model``). The vendor moved
# from the A55A text/CRC framing of 1.1.5 to raw little-endian binary frames with
# a ``data_id`` byte, and reworked the control surface around *position*
# (stroke units) rather than a 0-100 intensity slider.
#
#   DeviceV13MotionEncoder.encodeSingle(vib, tsMs, pos)   -> 16 B
#     ByteBuffer.allocate((count * 8) + 8).order(LITTLE_ENDIAN)
#       put(0x5A) put(0xA5) put(0x55) put(0x00)
#       putShort(vib) putShort(count=1)
#       putInt(tsMs) putInt(pos)
#
#   DeviceV13MotionEncoder.encode(vib, points)           -> 8 + 8*count bytes
#
#   DeviceV13ModeEncoder.encode(mode)                    -> 18 B
#     ByteBuffer.allocate(18).order(LITTLE_ENDIAN)
#       put(0x5A) put(0xA5) put(0x55) put(0x0A)
#       putShort(0xFFFF) putShort(0x0000) put(0xFF) put(mode)
#       putShort(0xFFFF) put(0xFF) put(0xFF) put(0xFF) put(0xFF) putShort(0xFFFF)

# V13_DATA_ID_MOTION and V13_DATA_ID_MODE live at the TOP of this module,
# with the frame-family constants they belong with. They used to be defined
# here, below the V13 frame comment block, while FRAME_MAGIC was above --
# so the two halves of one frame family sat in different parts of the file
# with nothing joining them.


def build_v13_motion(vibration: int, timestamp_ms: int, position: int) -> bytes:
    """DeviceV13MotionEncoder.encodeSingle — 16-byte single-point motion frame.

    ``position`` is a raw stroke unit, not a percentage. The App derives it from
    the trajectory planner; callers that only want a percentage should scale it
    against the device's ``stroke_max`` from the capability frame.
    """
    vib = max(0, min(0xFFFF, int(vibration)))
    ts = max(0, min(0xFFFFFFFF, int(timestamp_ms)))
    pos = max(0, min(0xFFFFFFFF, int(position)))
    return struct.pack("<BBBBHHII", H0, H1, H2, V13_DATA_ID_MOTION,
                       vib, 1, ts, pos)


def build_v13_motion_path(vibration: int, points) -> bytes:
    """DeviceV13MotionEncoder.encode — multi-point trajectory frame.

    ``points`` is an iterable of ``(timestamp_ms, position)`` pairs, at least one.
    """
    pts = list(points)
    if not pts:
        raise ValueError("a motion frame must contain at least one point")
    if len(pts) > 0xFFFF:
        raise ValueError("point count must fit u16")
    body = struct.pack("<BBBBHH", H0, H1, H2, V13_DATA_ID_MOTION,
                       max(0, min(0xFFFF, int(vibration))), len(pts))
    for ts, pos in pts:
        body += struct.pack("<II",
                            max(0, min(0xFFFFFFFF, int(ts))),
                            max(0, min(0xFFFFFFFF, int(pos))))
    return body


def build_v13_mode(mode: int) -> bytes:
    """DeviceV13ModeEncoder.encode — 18-byte mode frame (``data_id`` 0x0A)."""
    m = max(0, min(255, int(mode)))
    return struct.pack("<BBBBHHBBHBBBBH",
                       0x5A, 0xA5, 0x55, V13_DATA_ID_MODE,
                       0xFFFF, 0x0000, 0xFF, m, 0xFFFF,
                       0xFF, 0xFF, 0xFF, 0xFF, 0xFFFF)


# ------------------------------------------------------- firmware mode table
#
# Read out of the device firmware (Ghidra, FUN_000080dc at 0x80dc in
# BIN_FA004_SRSN_ALMiA_V1.0.2_2506026_Beta.bin). The firmware does not accept
# 1.1.5's presets 52-56 blindly: each value is a *state* that opens a one-shot
# gate and then arms a function-pointer pair, and the 1.1.5 app never spoke
# these names. Values and their log strings, verbatim from the decompile:
#
#   0x01  "offline mode"              -> disconnect
#   0x52  "online mode"               -> FUN_00007ba4 -> FUN_00007d24(2)
#   0x53  "online wait connect mode"  -> no action
#   0x54  "online Active mode"        -> FUN_00007928 + 4000 ms delay
#   0x55  "online Passage mode"       -> FUN_00007c70 -> FUN_00007d24(0)
#   0x56  "online interactive mode"   -> FUN_00007c28 -> FUN_00007d24(0)
#   0x57  "OTA ONLINE MODE"           -> FUN_00007c4c
#
# Every handler is guarded by ``if (*state == 0) { *state = 1; ... }`` — the
# firmware only accepts a transition once per boot per mode, so a control tool
# must send a mode frame before motion frames, and repeating the same mode does
# nothing.

FIRMWARE_MODES = {
    0x01: "offline",
    0x52: "online",
    0x53: "wait-connect",
    0x54: "active",
    0x55: "passage",
    0x56: "interactive",
    0x57: "ota-online",
}
# Modes that arm the control transport (FUN_00007d24) rather than just logging.
FIRMWARE_CONTROL_MODES = (0x52, 0x55, 0x56, 0x57)
# The 1.1.5 UI presets map onto the firmware's own names.
#
# This table was `LEGACY_MODE_MAP = {52: 0x52, ..., 56: 0x56}` -- which is
# EXACTLY :data:`MODE_OPCODES`, byte for byte, under a second name. Two
# names for one mapping is two places for it to drift, and this one had
# drifted already: it was written as the UI preset -> wire byte while
# MODE_OPCODES was written as the wire byte -> wire byte, so the same
# conversion existed in two shapes and neither reader could tell which
# one it was looking at.
#
# It had no caller either. The mapping lives in MODE_OPCODES, which is
# keyed by the UI preset (the shape the app's switch uses) and holds the
# wire byte.
LEGACY_MODE_MAP = MODE_OPCODES


def firmware_mode_name(value: int) -> str:
    return FIRMWARE_MODES.get(int(value) & 0xFF, f"mode0x{int(value) & 0xFF:02x}")


def is_firmware_control_mode(value: int) -> bool:
    """True when this mode opcode arms the control transport.

    THE WHITELIST USED TO HAVE NO READER.

    ``FIRMWARE_CONTROL_MODES`` names the four opcodes whose firmware
    handler arms the transport (``FUN_00007d24``) rather than merely
    logging: 0x52 online, 0x55 passage, 0x56 interactive, 0x57 OTA. The
    other three -- 0x01 offline, 0x53 wait-connect, 0x54 active -- do
    not, and the firmware's own comment in the image says a control
    frame is repeated only when the state is in {0x52, 0x55, 0x56}.

    Sending a motion frame while the device is in a mode that does not
    arm the transport is not a no-op: the frame is parsed and the
    counters are advanced, but nothing acts on it, and in the worst case
    the device is put into a state this project cannot drive out of. The
    whitelist is therefore checked BEFORE a mode frame goes out, so a
    wrong opcode is refused at the boundary instead of being discovered
    as "the device did not move".

    0x54 (active) is deliberately NOT in the list. The firmware's own
    comment lists {0x52, 0x55, 0x56} for the repeat, while 0x54's
    handler does the arming plus a 4000 ms delay -- so 0x54 arms the
    transport but on a different schedule. The whitelist is the
    project's conservative reading of "modes a control tool may drive".

    THE ARGUMENT IS THE UI PRESET; THE CHECK IS ON THE WIRE BYTE.

    This module has two mode scales and they are easy to confuse.
    ``MODE_OPCODES`` maps the UI's preset to the byte that goes on the
    wire, and ``app_set_mode`` writes that byte as a two-digit hex
    string: the frame for preset 52 is ``A5 5A 08 B0 3D 52``, so the
    mode byte is at offset 5 and reads 0x52.

    The firmware compares THAT byte against its own table -- 0x52..0x57
    -- so that is the byte the whitelist has to be applied to. The
    helper below maps a preset to its wire byte, and
    :func:`validate_firmware_mode` refuses a preset whose wire byte is
    not one the firmware acts on.
    """
    return (int(value) & 0xFF) in FIRMWARE_CONTROL_MODES


def mode_wire_byte(mode: int) -> int:
    """The byte the firmware will see for a UI mode preset.

    A preset the app can transmit (two decimal digits) is looked up in
    ``MODE_OPCODES``; the OTA opcode has no preset and is its own byte.
    """
    m = int(mode)
    if m in MODE_OPCODES:
        return MODE_OPCODES[m]
    if m == MODE_OPCODE_OTA:
        return MODE_OPCODE_OTA
    # Not a preset the app can transmit. The app renders the mode with
    # String.valueOf() and then requires an even-length hex string, so a
    # single-digit mode is not representable at all. Raise the same way
    # app_set_mode does, so the two cannot disagree.
    text = f"{m:d}"
    if len(text) % 2:
        raise ValueError(
            f"q.l() needs an even hex length; mode {m} renders as {text!r}, "
            "which the app itself cannot transmit")
    # Two digits, but not a preset: the app would transmit it, and the
    # firmware would read the byte. That byte is not in the control set.
    return int(text, 16)


def validate_firmware_mode(value: int) -> int:
    """Return the mode preset, or raise if the firmware would not act on it.

    The check lives here, next to the table it reads, so that the table
    and its reader cannot drift apart -- the same reason
    :func:`firmware_mode_name` sits beside ``FIRMWARE_MODES``.

    The value is checked on the WIRE BYTE, because that is what the
    firmware compares against its own table.
    """
    preset = int(value)
    wire = mode_wire_byte(preset)
    if not is_firmware_control_mode(wire):
        raise ValueError(
            "mode %d puts 0x%02x on the wire, and the firmware only acts "
            "on %s; the control transport is not armed in %s"
            % (preset, wire, firmware_mode_name(wire),
               ", ".join("0x%02x" % m for m in FIRMWARE_CONTROL_MODES)))
    return preset


# ----------------------------------------------------- firmware A55A control
#
# Read out of the device's own nRF52 image (Ghidra, ARM:LE:32:Cortex), which is
# the ground truth the app was reverse engineered from. Two independent receive
# channels scan the same A55A envelope, and they differ in whether they gate on
# a data_id:
#
#   FUN_00005084 (no gate)  -> FUN_000050f8   ← control
#       if (payload[0]    == 0x0B) swap5(payload + 1);   // 5-byte BE→native
#       if (payload[0x0B] == 0x0A) speed = le32(payload + 0x0C);
#
#   FUN_00004f48 (id==0xB1) -> FUN_00004eac  ← config / OTA / text commands
#       payload[0] == 0x0A → sscanf("Data: %s") → command dispatcher
#       payload[0] == 0x0B → len = payload[1]; two length-prefixed blobs
#
#   FUN_00009d2c parses the control payload:
#       payload[0]    == 0xB0 → mode = payload[1]
#       payload[2]    == 0xA0 → repeat payload[3] times when state ∈ {0x52,0x55,0x56}
#
# The two actuators are one store each, so a frame that lands is immediately
# effective:
#       FUN_00008b00(v) → *(DAT_00008b08 + 4) = v    (speed register)
#       FUN_000080b8(m) → *DAT_000080c0      = m    (mode register)
#
# Envelope, per FUN_00004ff4 / FUN_00009ebc:
#       A5 5A | total_len | data_id | payload | crc16 LE
#       total_len = 4 + len(payload) + 2
#       crc covers the first 4 + len(payload) bytes
#
# Note the magic is **A5 5A**; the 1.2.3 app's "V13" binary frames start with
# 5A A5 55 and are not seen by either scanner.


def build_a55a_app(opcode: str, args: list[str] | None = None,
                   fixed_crc: str | None = None) -> bytes:
    """Reproduce the APP's own frame builder, q.s() in 1.1.5.

    Decompiled from jadx-out/sources/com/app/aike/utils/q.java:475-505
    (smali-out/com/app/aike/utils/q.smali:3928):

        StringBuilder sb = new StringBuilder("A55A");
        sb.append(String.format("%02X", length));
        sb.append(opcode);
        for (String arg : args) sb.append(arg);
        if (fixedCrc != null) {
            if (fixedCrc.length() != 4) throw new IllegalArgumentException(...);
            sb.append(fixedCrc.toUpperCase(Locale.ROOT));
        } else {
            sb.append(String.format("%04X", e0.c(hexToBytes(sb.toString()))));
        }
        write(hexToBytes(sb.toString()));

    Two details that matter:
      * the length byte is emitted BEFORE the opcode, and it is a 1-byte length
        of the whole frame — not the firmware scanner's total-length field
      * args are concatenated as TEXT, so every token must already be hex.
        DO NOT coerce digits to int: the app passes "42" meaning 0x42, and
        "3" meaning 0x03 in ff.k.N("3D", String.valueOf(mode)) — that one is
        decimal because String.valueOf() really did produce a decimal string.
        The app's hardcoded fixedCrc values are the ground truth that proves
        which interpretation is right for each frame.
    """
    args = args or []
    body = bytes.fromhex((opcode + "".join(args)).strip())
    # The length byte holds the WHOLE frame, and the whole frame is
    # magic(2) + length(1) + body + crc(2) = len(body) + 5.
    #
    # The guard used to be `len(body) * 2 + 6`, which mixes hex CHARACTER
    # count with bytes and adds a constant that does not belong: it raised
    # for a 125-byte body, whose real frame is 130 bytes and still fits in
    # one length byte, and it let a body whose frame does NOT fit through
    # whenever the arithmetic happened to land under 255. The message
    # described a boundary the caller could not reason about.
    if len(body) + 5 > 0xFF:
        raise ValueError(
            "app frames are length-prefixed with a single byte; this body "
            "would make a %d-byte frame" % (len(body) + 5))
    total = 3 + len(body) + 2
    frame = bytes([0xA5, 0x5A, total & 0xFF]) + body
    if fixed_crc is not None:
        if len(fixed_crc) != 4:
            raise ValueError("fixedCrc 长度必须为4字符")
        # e0.c() returns the byte-SWAPPED CRC; the app appends it as TEXT via
        # String.format("%04X", ...), so the high byte lands first on the wire.
        wire = int(fixed_crc, 16)
        return frame + bytes([(wire >> 8) & 0xFF, wire & 0xFF])
    # THE CRC IS OVER THE DECODED BYTES -- the same rule as
    # build_text_frame's, and for the same reason: the firmware hashes
    # the frame's bytes (see the comment there, and
    # analysis/firmware/fw-strings/A55A_SURVEY.md).
    crc = crc16_ccitt_false(frame)
    swapped = ((crc >> 8) & 0xFF) | ((crc & 0xFF) << 8)
    return frame + bytes([(swapped >> 8) & 0xFF, swapped & 0xFF])


def app_caps_query() -> bytes:
    """q.s("00", 7, N("01"), "1E90") — verified byte-identical to the device's reply
    trigger. The one frame the firmware is known to honour."""
    return build_a55a_app("00", ["01"], fixed_crc="1E90")


# The value the app actually transmits on connect. j.java:78 / j.java:122
# both call K0(52, BleCommandOwner.System.INSTANCE, ...) once the initial
# zero write settles, and K0's switch is keyed on the LEGACY code, so 52 is
# remapped by DeviceV13ModeEncoder to 83 for B-generation parts. This part is
# A-generation (isBGeneration == false for SN UMVXEXAMPLE01), so K0 falls
# through to G0(..., "B0", 8, Q("3D", String.valueOf(52)), ...) and the
# literal 52 is what goes on the wire.
POST_CONNECT_MODE = 52


def app_set_mode(mode: int) -> bytes:
    """q.s("B0", 8, N("3D", String.valueOf(mode)), null) — no fixedCrc, so the
    app computes it.

    String.valueOf() yields DECIMAL, and q.l() then requires an even-length hex
    string, throwing IllegalArgumentException otherwise. So the app can only
    ever send two-digit modes; single-digit values are not representable and
    this function rejects them rather than inventing a byte.
    """
    text = f"{mode:d}"
    if len(text) % 2:
        raise ValueError(
            f"q.l() needs an even hex length; mode {mode} renders as {text!r}, "
            "which the app itself cannot transmit")
    return build_a55a_app("B0", ["3D", text])


def build_caps_query() -> bytes:
    """Capability poll — exactly ``q.s("00", 7, ["01"], "1E90")``.

    The ``1E90`` is a hardcoded CRC in the app, valid only while the body is
    exactly ``A55A070001``; live hardware answers with an ``A5 5A`` framed JSON
    document rather than the binary caps frame the app's parser expects.
    """
    return build_text_frame(7, fields=["01"], str_arg="00", fixed_crc="1E90")


# ------------------------------------------------- fixed-CRC intensity steps
#
# ``q.y(L)v`` sends a hardcoded frame per intensity step. Unlike q.t/q.u these
# do NOT depend on the capability frame, so they are the first thing to try when
# a device ignores the scaled control frames — they are the one path that works
# without knowing strokeMax / vibMax.
#
# ONE feature, ONE scale. ``app_set_intensity`` and ``build_intensity`` used to
# be the same three frames read on two scales that disagreed by one: this
# module's ``build_intensity`` took 1/2/3 while ``app_set_intensity`` took
# 0/1/2, so ``build_intensity(1)`` and ``app_set_intensity(0)`` were the same 11
# bytes and neither function mentioned the other. It stayed invisible because
# the one in-repo caller of ``build_intensity`` wrapped it in try/except and
# printed SKIP for the level that raised -- and level 0 was exactly that level.
#
# The app's ordinal wins (0=low, 1=medium, 2=high) because that is what every
# product caller passes (hw_app_frames.py, probe_status.py, seq_runner.py,
# make_plans.py) and what the p3b/p3c byte assertions already pin.
#
# Bodies and CRCs transcribed from the smali (see the ble-protocol-reverse
# skill, section 8.1):
#   LOW   A55A0BD00A00001442EC23
#   MED   A55A0BD00A00001C4245AA
#   HIGH  A55A0BD00A000028421463

#: preset name -> the two values q.s("D0", 11, N("0A","00","00", X, "42"),
#: FIXED) is handed. The single place the presets are written down.
INTENSITY_PRESETS = {
    "low": ("14", "EC23"),
    "medium": ("1C", "45AA"),
    "high": ("28", "1463"),
}

#: The same three presets as whole frames, for callers that match a hex dump
#: against the known frames. Derived from INTENSITY_PRESETS, never written
#: twice.
INTENSITY_FRAMES = {
    name: build_a55a_app("D0", ["0A", "00", "00", b, "42"], fixed_crc=crc).hex()
    for name, (b, crc) in INTENSITY_PRESETS.items()
}

#: The authoritative level scale: the app's own ordinals.
INTENSITY_ORDINALS = {"low": 0, "medium": 1, "high": 2}

#: Spellings that NAME a preset without being a level. Deliberately holds no
#: bare integer: an alias that can collide with an ordinal is a second scale
#: wearing a disguise, which is precisely what this table just used to be.
#: scripts/crc_regression.py asserts that as a structural rule.
INTENSITY_ALIASES = {
    "0.33": "low", "0.66": "medium",
    "100": "high", "max": "high", "full": "high",
}

_INTENSITY_NAMES = {ordinal: name for name, ordinal in INTENSITY_ORDINALS.items()}


def _intensity_ordinal(level) -> int:
    """Resolve any accepted spelling of a preset to the app's ordinal.

    An int is an ordinal already; a string may be a preset name, one of the
    non-ordinal aliases, or the decimal spelling of an ordinal (``"1"`` -> 1).
    Everything else raises -- including 3 and -1, which on the retired 1-based
    scale meant "high" and "invalid" respectively.
    """
    if isinstance(level, bool) or not isinstance(level, int):
        key = str(level).strip().lower()
        key = INTENSITY_ALIASES.get(key, key)
        if key in INTENSITY_ORDINALS:
            return INTENSITY_ORDINALS[key]
        try:
            level = int(key, 10)
        except ValueError:
            raise ValueError(
                "unknown intensity %r; use the app's ordinal 0/1/2, or "
                "low/medium/high" % (level,)) from None
    if level not in _INTENSITY_NAMES:
        raise ValueError(
            "intensity is the app's ordinal 0/1/2 (low/medium/high), got %r"
            % (level,))
    return level


def app_set_intensity(level: int) -> bytes:
    """q.s("D0", 11, N("0A","00","00", X, "42"), FIXED) — the app sends only
    THREE intensity presets and hardcodes the CRC for each:

        ordinal 0 (low)    0x14  EC23
        ordinal 1 (medium) 0x1C  45AA
        ordinal 2 (high)   0x28  1463

    ``level`` is the app's ordinal (0/1/2), not a 0-100 percentage. This is the
    authoritative scale for the feature: ``build_intensity`` is the same call
    reached by name and returns the same bytes for the same level.
    """
    b, crc = INTENSITY_PRESETS[_INTENSITY_NAMES[_intensity_ordinal(level)]]
    return build_a55a_app("D0", ["0A", "00", "00", b, "42"], fixed_crc=crc)


def build_intensity(level) -> bytes:
    """Name-spelled alias of :func:`app_set_intensity` -- the same call, the
    same frame bytes, the SAME level scale.

    Historically this took 1/2/3 and ``app_set_intensity`` took 0/1/2, which
    made ``build_intensity(1)`` and ``app_set_intensity(0)`` the same 11 bytes.
    Both scales are retired; this is now a thin forwarder so the name survives
    for callers that prefer it, with no second table to drift.
    """
    return app_set_intensity(level)


# ---------------------------------------------------------------- decoders

@dataclass
class DeviceStatus:
    key: int = 0
    mode: int = 0
    battery_raw: int = 0
    temp_raw: int = 0
    cell_raw: int = 0
    speed_mode: Optional[int] = None
    ext_start: int = 0
    ext_len: int = 0
    group_count: int = 0

    @property
    def cell_voltage(self) -> float:
        return self.battery_raw / 1000.0

    @property
    def mode_name(self) -> str:
        ui = self.ui_mode
        return MODE_NAMES.get(ui, f"raw{self.mode}") if ui is not None else f"raw{self.mode}"

    @property
    def ui_mode(self) -> Optional[int]:
        """The UI preset (52-56) that maps to this raw opcode, if any."""
        return MODE_OPCODE_TO_UI.get(self.mode)

    def __str__(self) -> str:
        speed = "-" if self.speed_mode is None else str(self.speed_mode)
        ui = self.ui_mode
        label = self.mode_name if ui is None else f"{self.mode_name}/{ui}"
        return (f"mode={self.mode}[{label}] batt={self.cell_voltage:.2f}V "
                f"temp={self.temp_raw} groups={self.group_count} speed={speed}")


@dataclass
class DeviceCaps:
    firmware: str = ""
    serial: str = ""
    chip_info: int = 0
    motor_max: int = 0
    vib_max: int = 0
    pos_limit: int = 0
    pos_count: int = 0
    stroke_max: int = 0
    raw: bytes = field(default=b"", repr=False)

    @property
    def has_control_transport(self) -> bool:
        return self.motor_max > 0 or self.vib_max > 0

    @property
    def generation(self) -> str:
        # Case-insensitive, matching detect_product(): this used to compare a
        # case-SENSITIVE prefix here while the sibling property went through
        # .upper(), so the same serial could answer "A" for generation and
        # "Vero" for product. One spelling rule per module.
        if any(self.serial.upper().startswith(p)
               for p in FORCED_B_GENERATION):
            return "B"
        return "B" if self.has_control_transport else "A"

    @property
    def product(self) -> str:
        # Delegates rather than re-walking PRODUCTS. This used to be a second,
        # case-sensitive copy of the same prefix walk, so a lowercase serial
        # said "Unknown" here and "Vero" from detect_product(). One table, one
        # walker -- adding a product can no longer land in one place and miss
        # the other.
        return detect_product(self.serial)

    def __str__(self) -> str:
        return (f"{self.product} sn={self.serial!r} fw={self.firmware} "
                f"gen={self.generation} motor_max={self.motor_max} vib_max={self.vib_max} "
                f"stroke={self.stroke_max} pos={self.pos_count}")


def _u8(buf: bytes, offset: int) -> int:
    return buf[offset] & 0xFF


def _u16le(buf: bytes, offset: int) -> int:
    return (_u8(buf, offset + 1) << 8) | _u8(buf, offset)


def parse_status(data: bytes) -> Optional[DeviceStatus]:
    """Decode a 0x5A A5 55 notification (DeviceV13StatusParser.parse).

    OFFLINE ANALYSIS ONLY. This decoder has NO production caller in this
    project and, on the hardware this project actually drives, it has no
    frame to decode either -- see the note below.

    The frame family it parses DOES NOT EXIST on this device. The V13
    frames (``5A A5 55 ...``) are the B-generation transport, and the
    device in hand -- serial prefix UMVx, firmware 1.0.11 -- is
    A-generation. Three independent pieces of evidence say so:

      * The device's own firmware image contains no ``5A A5`` byte pair at
        all. A raw search of
        analysis/firmware/ota/assets/
        BIN_FA004_SRSN_ALMiA_V1.0.2_2506026_Beta.bin (64 KiB) finds 0
        occurrences of ``5A A5`` and 0 of ``5A A5 55``; the A5 5A magic
        appears only as instruction immediates. The same search is recorded
        in analysis/firmware/fw-strings/A55A_SURVEY.md section 3, under the
        heading "THE 5A A5 55 FAMILY -- ABSENT".
      * Every scanner in that firmware looks for ``A5 5A`` and dispatches on
        byte 3 (data_id). Nothing anywhere compares byte 0 against 0x5A.
      * The A-generation device answers the caps query with an ``A5 5A``
        framed JSON document, which this project decodes with
        :func:`parse_framed_json`. A real capture is in
        analysis/evidence/hw_retest.json.

    So this function is kept as an offline tool for the B-generation frame
    family -- the one the vendor app's 1.2.3 build speaks -- and it is
    called from analysis/scripts/ only. It is deliberately NOT wired into
    the session, because there is nothing on the wire for it to read.

    The layout it decodes, from DeviceV13FrameParser.parseStatus
    (analysis/decompile/jadx-123/sources/com/app/aike/model/
    DeviceV13FrameParser.java):

        byte 0..2  0x5A 0xA5 0x55     the V13 family's magic
        byte 3     ext_len           the length of the extension block
        byte 4..5  u16le             the timestamp
        byte 6..7  u16le             the group count
        then the extension block, whose first bytes are:
          +0  key
          +1  mode
          +2  u16le battery millivolts
          +4  temp
          +5  cell (present when ext_len >= 6)
          +6  speed mode (present when ext_len >= 7)

    BYTE 2 IS THE MAGIC'S THIRD BYTE, and this is worth being precise
    about, because the audit got it backwards.

    The audit said the old code "read byte 2 as the magic's third byte and
    compared it against DATA_ID_STATUS -- which is the data_id, not a
    magic byte", and that a V13 MODE frame (data_id 0x0A) was therefore
    accepted as a STATUS frame. That reading is wrong: the app's own
    encoder puts 0x5A 0xA5 0x55 at the head of EVERY V13 frame, and the
    data_id is byte 3.

        DeviceV13MotionEncoder.encode:  put(0x5A) put(0xA5) put(0x55)
                                          put(0x00)   <- the motion data_id
        DeviceV13ModeEncoder.encode:     put(0x5A) put(0xA5) put(0x55)
                                          put(0x0A)   <- the mode data_id

    and DeviceV13FrameParser.parse checks
    `u8(bArr,0) != 90 || u8(bArr,1) != 165 || u8(bArr,2) != 85` -- so byte
    2 == 85 IS the family marker the app checks, and the app calls that
    constant DATA_ID_STATUS because for the frame it parses it is the
    status frame's id. The MODE frame carries the SAME byte 2 and differs
    only at byte 3.

    So byte 2 was never the bug, and the "fix" that moved the check to
    byte 3 would have rejected every real status frame. What WAS wrong is
    that the status frame's own data_id (byte 3, which for a status frame
    is 0x00 per the motion encoder, and which the audit called "ext_len")
    was being read as the extension length.

    The V13 status frame's byte 3 is therefore NOT a length field, and
    the extension block's start cannot be derived from it. What the app
    does (DeviceV13FrameParser.parse, the ext_len > 0 branch) is:

        if (ext_len > 0) {
            if (bArr.length >= ext_len + 8) { length = bArr.length - ext_len; }
            else { length2 = bArr.length - 8; }
        } else { length = bArr.length; }

    i.e. the extension block sits at the END of the frame and its length
    is byte 3, and the frame is only accepted when the block fits.

    This parser now does the same: the block must fit inside the frame,
    and a frame whose block does not fit is REJECTED rather than decoded
    against a wrong base. The old code fell back to `len(data) - 8` when
    the block did not fit, which produced a DeviceStatus claiming
    ext_len=250 inside a 16-byte frame -- any caller slicing by ext_len
    then read past the end of the buffer.
    """
    if len(data) < 8:
        return None
    # Byte 2 is the V13 family's magic byte (0x5A 0xA5 0x55), and it is
    # what the app checks. It was never the bug -- see the docstring.
    if not (_u8(data, 0) == H0 and _u8(data, 1) == H1 and _u8(data, 2) == H2):
        return None
    ext_len = _u8(data, 3)
    groups = _u16le(data, 6)
    base = 8
    if ext_len > 0:
        # The extension block must be INSIDE the frame. The old code fell
        # back to `len(data) - 8` when it did not fit, which produced a
        # DeviceStatus claiming ext_len=250 inside a 16-byte frame -- any
        # caller slicing by ext_len then read past the end of the buffer.
        if len(data) < ext_len + 8:
            return None
        base = len(data) - ext_len
    if ext_len < 5 or len(data) < base + 5:
        return None
    cell_raw = 0
    speed: Optional[int] = None
    if ext_len >= 6:
        cell_raw = _u8(data, base + 5)
    if ext_len >= 7:
        speed = _u8(data, base + 6)
    return DeviceStatus(
        key=_u8(data, base),
        mode=_u8(data, base + 1),
        battery_raw=_u16le(data, base + 2),
        temp_raw=_u8(data, base + 4),
        cell_raw=cell_raw,
        speed_mode=speed,
        ext_start=base,
        ext_len=ext_len,
        group_count=groups,
    )


def parse_caps(data: bytes) -> Optional[DeviceCaps]:
    """Decode a 0x5A A5 AA notification (DeviceRuntimeCapsParser.parse).

    OFFLINE ANALYSIS ONLY, for the same reason as :func:`parse_status`:
    the ``5A A5 55`` / ``5A A5 AA`` frame family does not exist on this
    device (its firmware image contains zero ``5A A5`` byte pairs), and the
    A-generation device answers the caps query with an ``A5 5A`` framed
    JSON document instead -- which :func:`parse_framed_json` and
    :func:`parse_device_info` read. Use those against real hardware.

    The layout, from DeviceRuntimeCapsParser.parse
    (analysis/decompile/jadx-123/sources/com/app/aike/model/
    DeviceRuntimeCapsParser.java):

        byte 0..1  0x5A 0xA5          magic
        byte 2     data_id == 170    0xAA, the PARAMS id
        byte 3..5  firmware version, "x.y.z"
        byte 6..26 the serial, NUL-terminated
        byte 21    chip info
        byte 22..23 u16le motor max
        byte 24..25 u16le vibration max
        byte 26..27 u16le position limit
        byte 28..29 u16le position count
        byte 30..31 u16le stroke max
    """
    if len(data) < 32:
        return None
    if not (_u8(data, 0) == H0 and _u8(data, 1) == H1
            and _u8(data, 2) == DATA_ID_PARAMS):
        return None
    firmware = f"{_u8(data, 3)}.{_u8(data, 4)}.{_u8(data, 5)}"
    raw_sn = data[6:27]
    sn_len = next((i for i, b in enumerate(raw_sn) if b == 0), len(raw_sn))
    serial = raw_sn[:sn_len].decode("ascii", errors="replace")
    return DeviceCaps(
        firmware=firmware,
        serial=serial,
        chip_info=_u8(data, 21),
        motor_max=_u16le(data, 22),
        vib_max=_u16le(data, 24),
        pos_limit=_u16le(data, 26),
        pos_count=_u16le(data, 28),
        stroke_max=_u16le(data, 30),
        raw=data,
    )


def detect_product(name_or_sn: str) -> str:
    for product, prefixes in PRODUCTS.items():
        if any(name_or_sn.upper().startswith(p) for p in prefixes):
            return product
    return "Unknown"


def device_profile(name_or_sn: str) -> Optional[dict]:
    """The vendor app's capability row for this model, or ``None``.

    ``None`` and ``False`` are different answers and must not be collapsed:

    * ``False`` -- the model is identified, and the app's profile for it says
      the capability is absent. ``"Vero"`` has no vibration; a request asking
      for it is wrong and gets refused (HTTP 422).
    * ``None``  -- the model is NOT identified, so we have no row to speak
      with. That is a statement about our knowledge, not about the hardware.
      An unrecognised serial, a session that never got identity, and the
      simulator (sn ``sim-device``, see ``simulator.SIM_SERIAL``) all land
      here, and all three are allowed through.

    The vendor app itself does NOT make this distinction: its
    ``unknownProfile`` reports ``hasVibration = true`` because it hands every
    unrecognised device Mira's UI. That is a UI default, not a statement about
    the connected device, so we deliberately do not copy it -- under-copying
    here would refuse commands against a real device we merely failed to
    identify.

    Callers must treat the returned dict as read-only; it is the module-level
    table, not a per-device record.
    """
    return DEVICE_PROFILES.get(detect_product(name_or_sn))


def profile_capability(name_or_sn: str, capability: str) -> Optional[bool]:
    """One boolean out of :func:`device_profile`, or ``None`` if unknown.

    ``None`` is returned both when the model is unrecognised AND when a
    recognised model has no opinion about the requested capability, so a caller
    cannot confuse "we know it is False" with "we do not know". An unknown
    capability NAME also yields ``None`` rather than raising: the table is data,
    and a typo in a capability name must not become a 500 at a request
    boundary.
    """
    row = device_profile(name_or_sn)
    if row is None:
        return None
    value = row.get(capability)
    return None if value is None else bool(value)


def is_b_generation(name_or_sn: str) -> bool:
    return any(name_or_sn.upper().startswith(p) for p in FORCED_B_GENERATION)


def is_controllable(name_or_sn: str) -> bool:
    """Can this build actually DRIVE the model with this serial?

    True for the A-generation models whose send path this project
    implements (:data:`CONTROLLABLE_MODELS`), and False for everything
    else -- including an UNRECOGNISED serial.

    An unrecognised serial is False, not True, and that is the whole
    point of the function: the scan list shows only the devices this
    build can really control, and offering a device whose model was not
    recognised as "controllable" would offer exactly the device that
    cannot be controlled -- because the model is what decides which
    transport the device speaks, and an unrecognised model is a model
    whose transport is unknown.

    The distinction is not hidden: :func:`non_controllable_reason` says
    WHY a known-but-uncontrollable model was dropped, and the UI shows
    that line to the operator.
    """
    return detect_product(name_or_sn) in CONTROLLABLE_MODELS


def non_controllable_reason(name_or_sn: str) -> str:
    """Why this model was dropped from the scan list, or "".

    Empty for a controllable model. For a KNOWN model that cannot be
    driven, the reason from :data:`NON_CONTROLLABLE_REASONS`. For an
    UNRECOGNISED model, a sentence that says so -- which is a different
    statement from "we know the model and cannot drive it", and the
    operator has to be able to tell them apart.
    """
    product = detect_product(name_or_sn)
    if product in CONTROLLABLE_MODELS:
        return ""
    reason = NON_CONTROLLABLE_REASONS.get(product)
    if reason:
        return reason
    return ("未识别型号：本项目的 BLE 协议只对本厂设备成立，"
            "无法确认这个设备可以被控制。")


# ------------------------------------------------------- live-hardware frames

#: Stroke scale and vibration scale live at the TOP of this module, next to
#: the constants they belong with. They used to be defined down here, below
#: the frame decoders, which meant build_control() -- defined ABOVE this
#: point -- could not use STROKE_MAX as its default argument and had to
#: hard-code 100 instead. A default that is a hundred times off is a bug
#: that hides in a signature.


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE, *no* byte swap — what the device's envelope uses.

    The app's ``e0.c()`` returns the byte-swapped form (that is the value the
    A-generation text protocol appends). The ``A5 5A`` envelope stores the raw
    CCITT result little-endian instead. Both are needed; ``crc16`` above is the
    app-compatible one, this is the hardware one.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= (byte & 0xFF) << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


@dataclass
class DeviceInfo:
    """What a real device reports in its JSON envelope."""

    serial: str = ""
    power: float = 0.0
    ble_fw: str = ""
    mcu1_fw: Optional[int] = None
    mcu2_fw: Optional[int] = None
    rssi: Optional[int] = None
    mtu: Optional[int] = None
    extra: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def product(self) -> str:
        return detect_product(self.serial)

    def __str__(self) -> str:
        fw = f"{self.ble_fw}"
        if self.mcu1_fw is not None:
            fw += f" mcu={self.mcu1_fw}/{self.mcu2_fw}"
        link = ""
        if self.rssi is not None:
            link = f" rssi={self.rssi}dBm"
        if self.mtu is not None:
            link += f" mtu={self.mtu}"
        return f"{self.product} sn={self.serial!r} fw={fw} power={self.power:.2f}V{link}"


def build_framed_json(obj: dict) -> bytes:
    """Wrap a JSON document in the device's A5 5A envelope (verified framing).

    Layout: ``A5 5A | total_len | 0x00 0x02 | utf8-json | crc16_le``
    where ``total_len`` equals the whole frame length including this header, and
    the CRC-16/CCITT-FALSE of everything before the CRC is appended little-endian.
    """
    payload = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    body = FRAME_MAGIC + bytes([0]) + b"\x00\x02" + payload
    total = len(body) + FRAME_CRC_LEN
    body = body[:2] + bytes([total & 0xFF]) + body[3:]
    crc = crc16_ccitt_false(body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def parse_framed_json(data: bytes) -> Optional[tuple[dict, dict]]:
    """Decode an ``A5 5A`` envelope. Returns ``(payload_obj, envelope_meta)``.

    ``envelope_meta`` carries ``total_len``, ``actual_len``, ``chunk``,
    ``crc_expected``, ``crc_actual`` and ``crc_ok``. Returns ``None`` when
    the magic is absent or the JSON body will not parse.

    ``total_len`` is byte 2 and it is the LENGTH OF THE WHOLE FRAME -- the
    device writes the frame's own length there, measured against a real
    capture (analysis/evidence/hw_retest.json: 182 bytes on the wire, byte2
    = 0xB6 = 182). It was carried into the meta and never COMPARED against
    the real length, so a truncated or over-long frame still produced a
    payload as long as the JSON happened to parse. A caller that trusted
    ``total_len`` was trusting a number nothing had checked.

    ``chunk`` is bytes 3-4, and the device writes 0x0002 there. Read
    little-endian that is 512, which is why the value in the meta was 512
    for a device that says 2. The two bytes are read BIG-ENDIAN here, so
    the meta reports the 2 the device actually wrote.
    """
    if len(data) < FRAME_HEADER_LEN + FRAME_CRC_LEN or not data.startswith(FRAME_MAGIC):
        return None
    total_len = data[2]
    # Big-endian, because that is how the device writes it: the capture
    # above has bytes 3-4 = 00 02 and the device's chunk index is 2.
    # Reading them little-endian produced 512 for every real frame.
    chunk = int.from_bytes(data[3:5], "big")
    body = data[5:-FRAME_CRC_LEN]
    crc_actual = int.from_bytes(data[-FRAME_CRC_LEN:], "little")
    crc_expected = crc16_ccitt_false(data[:-FRAME_CRC_LEN])
    meta = {
        "total_len": total_len,
        "actual_len": len(data),
        # Whether the frame's own length field agrees with what arrived.
        # This is the check that was missing: a frame whose length byte
        # says 40 but which carries 182 bytes (or the reverse) is a
        # truncated or concatenated frame, and it used to be decoded as if
        # it were whole.
        "length_ok": total_len == len(data),
        "chunk": chunk,
        "crc_expected": crc_expected,
        "crc_actual": crc_actual,
        "crc_ok": crc_expected == crc_actual,
    }
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload, meta


def parse_device_info(data: bytes) -> Optional[DeviceInfo]:
    """Decode the ``dev``/``ble`` JSON document a real device sends."""
    decoded = parse_framed_json(data)
    if decoded is None:
        return None
    payload, _meta = decoded
    dev = payload.get("dev")
    if not isinstance(dev, dict) or "sn" not in dev:
        return None
    ble_raw = payload.get("ble")
    ble: dict = ble_raw if isinstance(ble_raw, dict) else {}
    known = {"dev", "ble"}
    return DeviceInfo(
        serial=str(dev.get("sn", "")),
        power=float(dev.get("power", 0.0) or 0.0),
        ble_fw=str(dev.get("ble_1_fw", "") or ""),
        mcu1_fw=dev.get("mcu1_fw"),
        mcu2_fw=dev.get("mcu2_fw"),
        rssi=ble.get("RSSI"),
        mtu=ble.get("MTU:"),
        extra={k: v for k, v in payload.items() if k not in known},
        raw=payload,
    )