"""0x29 telemetry frame decoder for the Amorlink Vero (Vex family).

FRAME FORMAT (device -> host, notification characteristic 6e400003)
--------------------------------------------------------------------
A 41-byte A55A frame appears only after the host sends the telemetry command.
The command is built by the app as ``k("B0", 8, ["3D", String.valueOf(0x36)])``
(r0.smali:322 passes 0x36; r1.smali:26101 formats it with String.valueOf, which
is DECIMAL), so the byte on the wire is 0x54 and the frame reads

    A5 5A 08 B0 3D 54 21 ED

WHAT EACH BYTE IS
-----------------
    byte 0-1   A5 5A        header
    byte 2     cmdId        NOT a length. r1.smali:12344-12420 formats it with
                            %02X, and n1.java never validates it -- it only
                            checks the "A55A" prefix and byte 3.
    byte 3     A0           sub-id; selects the branch in n1.java
    byte 4     data id      0xB0 = telemetry, 0x02 = caps JSON,
                            0x0B0x = button events. Same A0 branch, so a
                            decoder that only tests byte 3 reads them as
                            pressure samples.
    byte 5     vibration
    byte 6-8   A0 03 01     fixed
    byte 9..   payload      runs to the end of the frame minus the CRC16
                            (X.substring(len-4) in n1.java:186)
    last 2     CRC16 LE

PAYLOAD (v1.java:131-161)
-------------------------
Values are NOT single bytes. v1.e() takes FOUR hex characters per step --
two bytes -- and returns

    ByteBuffer.wrap(b).order(LITTLE_ENDIAN).getShort() & 0x0FFF

so the range is 0..4095. A 30-byte payload yields fifteen values, which n1.java
consumes with stride 5: five position slots sampled three times.

POSITION (v1.smali d(ArrayList))
--------------------------------
    threshold = isVero ? 800 : 200          # 0x320 / 0xc8
    for i = 4 down to 0:
        if values[i] > threshold: break
    position = clamp(i * 2000, 0, 10000)    # 0x7d0 / 0x2710

Five steps of 20%. Reading the first slot alone, as this module once did,
throws the position away entirely.

CALIBRATION
-----------
After connecting, the device averages seven frames per slot and adds 200 as a
dead zone (n1.smali: div-int then const/16 0xc8 then add-int). Automatic; there
is no calibration control in the app. An idle surface therefore reads near 200,
not 0.
"""
from __future__ import annotations

from .protocol import crc16_ccitt_false

HEADER = 'A55A'
SUB_ID_A0 = 'A0'

#: The telemetry command. Reaches the wire as byte 0x54 because the app
#: formats the integer with String.valueOf (decimal).
CMD_TELEMETRY = 0x36
#: NOTE: kept only for reference. Command 0x34 (the byte 0x52 the app calls
#: mode 52) is the MOTION mode, not a telemetry request. The app sends it once
#: at connect and once more when a 7-frame buffer completes; it is not a
#: per-batch refill. Nothing in the control path sends it.
CMD_MOTION_MODE = 0x34

#: Byte 2 is the LENGTH OF THE WHOLE FRAME.
#:
#: This was WRONG. It used to say "byte 2 is a COMMAND ID, not a
#: length", and that belief cost the last round a real defect: the
#: reassembler assembled to a nominal 37 bytes, so every real frame
#: was cut short, the CRC never matched, and the last four bytes of
#: every frame were thrown away.
#:
#: The real frame, from a live capture
#: (analysis/scripts/find_pressure_byte.py)::
#:
#:     A5 5A <length> A0 B0 <vib> A0 03 01 <values> <crc>
#:
#: 41 bytes on the wire, byte 2 = 0x29 = 41, and the CRC over all 41
#: bytes verifies.
#:
#: The app never validates byte 2 either -- n1.java only checks the
#: "A55A" prefix and byte 3 == "A0" -- so the app does not care what
#: byte 2 says. The DEVICE does: it puts its own length there.
PAYLOAD_OFFSET = 9         # X.substring(18, ...) on hex -> byte 9
VIBRATION_OFFSET = 5       # X.substring(10,12) on hex
DATA_ID_OFFSET = 4         # X.substring(8,10) on hex

#: X.substring(len-4) drops the trailing CRC16, so the payload runs to the end
#: of the frame minus two bytes -- the frame length, not a header field.
CRC_LENGTH = 2

#: Shortest frame v1.e() can parse: the payload slice starts at byte 9, so a
#: frame needs at least 10 bytes plus its CRC.
MIN_FRAME_LENGTH = 12

#: Sibling sub-commands of A0 that carry something else entirely. n1.java
#: dispatches on byte 4 within the same A0 branch, so a decoder that only
#: checks byte 3 would swallow button events as pressure samples.
#:
#: The BUTTON ids, though, do NOT come on the A0 branch. The device's
#: own firmware builds its key frames with the data id 0xA1 (the
#: function whose own log line is "sent Key Value" --
#: analysis/firmware/fw-strings/frame_handlers.txt:1632-1650), so they
#: never reach this decoder's A0 branch at all. They are named here
#: because the IDS are the same, and the vendor app reads them out of
#: the same place (the hex string's characters 8..11). The decoder for
#: them is vero/buttons.py, and it accepts the A1 branch.
CAPS_DATA_ID = 0x02
EVENT_DATA_IDS = (0x0B02, 0x0B03, 0x0B04)

#: v1.e() consumes FOUR hex characters (two bytes) per step and returns
#: ``ByteBuffer.LITTLE_ENDIAN.getShort() & 0x0FFF`` (v1.java:131-161). Reading
#: single bytes misses the high nibble entirely: with the observed constants
#: 0x0A and 0x0D sitting in the second byte, that is +2560 and +3328.
VALUE_MASK = 0x0FFF

#: Smallest ATT payload this link negotiates, so a telemetry frame is always
#: delivered in more than one notification. The app never calls requestMtu --
#: MTU 23 is a hardcoded constant (r1.smali:45706, const/16 v8, 0x17) -- and
#: n1.java:141-142 processes every notification independently, prefix-checking
#: only the "A55A" head. Nothing reassembles fragments, on either side.
ATT_PAYLOAD = 23
FRAGMENT_THRESHOLD = ATT_PAYLOAD

#: A telemetry frame is worth this many bytes. 7 header + 14 two-byte values +
#: 2 CRC. Used only to recognise a fragment as "this is a partial copy of a
#: whole frame", never to reject one: the exact frame size has not been
#: confirmed on hardware, so any cutoff here could silently drop real samples.
NOMINAL_TELEMETRY_LENGTH = 37

#: Five slots per sensor group, three groups per frame (n1.java divides the
#: parsed list by five).
SENSOR_SLOTS = 5
BATCH = 7
NORMALISE_OFFSET = 200


def decode_frame(raw: bytes) -> dict | None:
    """Decode one device notification.

    Returns None unless it is a pressure-bearing A55A/A0 frame. The length,
    the payload slice and the value extraction all follow the app's own code
    rather than a fixed-size reading of a captured frame.

    Two integrity facts are *measured and reported* on every frame, never acted
    on. Both are returned as fields rather than used as gates, because neither
    can be trusted as a rejection criterion until a telemetry frame has been
    captured end to end and its CRC recomputed against real bytes:

    ``crc_ok``      whether the trailing two bytes match CRC-16/CCITT-FALSE over
                    the rest of the frame. The four frames that have been
                    confirmed (caps, mode 0x52, mode 0x03, intensity) all
                    match; a 41-byte telemetry frame has not.
    ``is_fragment`` whether this notification is shorter than one ATT payload
                    can carry, which means it is part of a larger frame.

    Rejecting on either would be a guess dressed as a check. The app itself
    validates neither -- n1.java only prefix-checks "A55A" -- so rejecting here
    would diverge from the reference implementation, and accepting silently is
    what makes the counters worth having.
    """
    h = raw.hex().upper()
    if len(raw) < MIN_FRAME_LENGTH or not h.startswith(HEADER):
        return None
    # Byte 3 selects the branch inside n1.java's A55A handler.
    if h[6:8] != SUB_ID_A0:
        return None
    data_id = raw[DATA_ID_OFFSET]
    # Same A0 branch, different consumers: the caps JSON reply and the button
    # events. Treating them as pressure samples produces a flat or bogus trace.
    if data_id == CAPS_DATA_ID or h[8:12] in ('%04X' % v for v in EVENT_DATA_IDS):
        return None

    vibration = raw[VIBRATION_OFFSET]
    payload = raw[PAYLOAD_OFFSET:len(raw) - CRC_LENGTH]
    if len(payload) < 2:
        return None

    # Integrity measurement. See the docstring: reported, not enforced.
    stored_crc = int.from_bytes(raw[-CRC_LENGTH:], 'little')
    crc_ok = crc16_ccitt_false(raw[:-CRC_LENGTH]) == stored_crc
    is_fragment = len(raw) < FRAGMENT_THRESHOLD

    # v1.e(): two bytes little-endian per step, masked to 12 bits.
    values = [
        int.from_bytes(payload[i:i + 2], 'little') & VALUE_MASK
        for i in range(0, len(payload) - 1, 2)
    ]
    if not values:
        return None

    # Five slots per group; a trailing partial group is kept, because the app
    # feeds it in rather than dropping it (n1.java builds a short list when
    # the count is not a multiple of five).
    groups = [values[g:SENSOR_SLOTS * (g + 1)]
              for g in range((len(values) + SENSOR_SLOTS - 1) // SENSOR_SLOTS)]
    groups = [g for g in groups if g]

    return {
        'raw': h,
        'vibration': vibration,
        'data_id': data_id,
        'values': values,
        'groups': groups,
        'value_count': len(values),
        'crc_ok': crc_ok,
        'is_fragment': is_fragment,
        'frame_len': len(raw),
    }


class PressureAccumulator:
    """LEGACY -- kept for the offline self-test only, not in the control path.

    This mirrors the app's 7-frame averaging but assumes the old single-byte
    payload model, which the hardware does not use. Anything reading live
    frames goes through PressureMonitor, which uses decode_frame().
    """
    """Reproduces the app's 7-frame batching and +200 normalisation."""

    def __init__(self, slots: int = SENSOR_SLOTS):
        self.slots = slots
        self.buffer: list[dict] = []
        self.acc = [0] * slots
        self.batch_count = 0
        self.completed: list[list[float]] = []
        self.samples: list[list[int]] = []

    def add(self, frame: dict) -> list[float] | None:
        """Feed one frame; returns the normalised vector when a batch completes."""
        groups = frame.get('groups')
        if not groups:
            return None
        self.samples.append(list(groups[0]))
        self.buffer.append(frame)
        for g in groups:
            for i, v in enumerate(g[:self.slots]):
                self.acc[i] += v
        if len(self.buffer) < BATCH:
            return None
        n_groups = len(groups)
        denom = self.batch_count + BATCH
        self.batch_count = denom
        out = [self.acc[i] / (denom * n_groups) + NORMALISE_OFFSET
               for i in range(self.slots)]
        self.completed.append(out)
        self.buffer.clear()
        self.acc = [0] * self.slots
        return out

    def per_slot_means(self) -> list[float] | None:
        if not self.samples:
            return None
        n = len(self.samples)
        return [sum(s[i] for s in self.samples) / n for i in range(self.slots)]

    def per_slot_ranges(self) -> list[tuple[int, int]] | None:
        if not self.samples:
            return None
        return [(min(s[i] for s in self.samples),
                 max(s[i] for s in self.samples)) for i in range(self.slots)]
