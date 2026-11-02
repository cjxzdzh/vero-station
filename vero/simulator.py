"""A stand-in peripheral so the software can be exercised without hardware.

Used by ``--simulate`` and by the offline self-test. It mimics just enough of
the transport contract for the session and the web UI to run end to end: it
accepts frames, answers the caps query with the same JSON document the real
device sends, and streams 0x29 telemetry once command 54 arrives.

This exists because a report of "verified" against the simulator once passed
for a real-hardware run. Anything that claims a hardware result must say so.
"""

from __future__ import annotations

import asyncio
import time

#: The serial the simulator reports.
#:
#: It used to be "SIM0000000000" here and "sim-device" in the session, so
#: the two paths that populate ``stats.serial`` produced two strings in
#: two different FORMATS -- and the session's own answer was the one the
#: page never saw, because the page reads the session's.
#:
#: One constant, used by both, is the whole fix: the device carries it,
#: the session reports it, and a reader that compares the two sees one
#: value rather than two shapes.
#:
#: It deliberately matches no product prefix (see protocol.PRODUCTS), so
#: the model reads "Unknown" and both capability booleans stay None. That
#: is the honest answer for a simulator, and it is also the safe one: the
#: guard then allows vibration through, so nobody can claim the simulator
#: refused a Mira-only command.
SIM_SERIAL = "sim-device"


class SimulatedDevice:
    """Holds the little bit of device state the UI displays."""

    def __init__(self) -> None:
        self.serial = SIM_SERIAL
        self.firmware = "1.0.11"
        self.power = 15.0
        # Declared here rather than inline in caps_reply() so the simulated
        # session can report the same fields without re-parsing a frame.
        self.rssi = -40
        self.mtu = 255
        self.sensors_armed = False
        self.position = 0.0

    def caps_reply(self) -> bytes:
        """Build the same A5 5A + JSON + CRC envelope the real device sends.

        The CRC comes from the protocol module rather than a local copy -- an
        earlier hand-rolled version stored the bytes big-endian and every
        reply failed verification, which silently blanked RSSI and MTU in the
        status panel.

        The envelope itself now comes from build_framed_json() too, for the
        same reason: the hand-rolled copy declared total_len as
        len(body) + 5 where the payload plus its CRC is 6 bytes past the 5-byte
        header, so the simulator advertised a frame two bytes shorter than it
        emitted, and wrote chunk = 0 where the device writes 2 (bytes 3-4,
        little-endian). Both were measured against a real capture
        (analysis/evidence/hw_retest.json log[0] reply[0], 181 bytes, byte2 =
        181 = whole frame length, bytes 3-4 = 0002) and neither matched. The
        real hardware being the only reference, the simulator follows it.

        Note this is a different frame family from the telemetry frame --
        this one carries JSON and that one carries sensor values -- but
        they agree about byte 2: in BOTH, byte 2 is the length of the
        whole frame. See pressure.py.
        """
        from .protocol import build_framed_json

        return build_framed_json({
            "dev": {
                "sn": self.serial,
                "power": round(self.power, 2),
                "ble_1_fw": self.firmware,
                "mcu1_fw": 200,
                "mcu2_fw": 200,
            },
            # "MTU:" with the colon, matching what the firmware actually
            # emits. The mock used a bare "MTU", which made the simulator the
            # one path that disagreed with real hardware: _ingest_caps() read
            # the bare key, so the fake device filled stats.mtu and a real
            # one never could.
            "ble": {"RSSI": self.rssi, "MTU:": self.mtu},
        })


class SimulatedTransport:
    """Matches the small surface DeviceSession uses from a bleak client."""

    def __init__(self, device: SimulatedDevice | None = None) -> None:
        self.device = device or SimulatedDevice()
        self.is_connected = False
        self.writes = 0
        #: How many frames arrived with a CRC the device would have
        #: dropped. Zero is the healthy value; anything else means the
        #: host built a frame the device would not accept.
        self.crc_failures = 0
        self._notify_cb = None
        self._task = None
        self._stop = False

    @staticmethod
    def _crc_ok(raw: bytes) -> bool:
        """Does this frame carry the CRC the device expects?

        The rule is the firmware's: CRC-16/CCITT-FALSE over the
        decoded bytes of frame[0:len-2], stored little-endian in the
        last two bytes.

        A frame that is too short to carry a CRC is accepted -- the
        caller decides what to do with a runt, and the CRC check is
        about the rule, not about the frame's shape.
        """
        if len(raw) < 5:
            return True
        from .protocol import crc16_ccitt_false
        body = raw[:-2]
        crc = crc16_ccitt_false(body)
        want = bytes([crc & 0xFF, (crc >> 8) & 0xFF])
        return raw[-2:] == want

    async def connect(self) -> None:
        self.is_connected = True
        self._stop = False
        self._task = asyncio.create_task(self._stream())

    async def disconnect(self) -> None:
        self._stop = True
        self.is_connected = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # The stream task was cancelled. Swallowed on purpose: the
                # disconnect() that cancelled it is waiting for it, and the
                # tuple form this replaces DID catch CancelledError (an
                # except clause catches a class whatever it inherits
                # from), so this is not a behaviour change.
                pass
            except Exception:                          # noqa: BLE001
                pass
            self._task = None

    async def start_notify(self, _char, callback) -> None:
        self._notify_cb = callback

    async def write_gatt_char(self, _char, data, response: bool = False):
        raw = bytes(data)
        self.writes += 1
        # THE CRC IS CHECKED, LIKE THE DEVICE CHECKS IT.
        #
        # The simulator used to look only at the magic, the data_id and
        # the drive bytes -- never at the CRC. So a frame with a
        # WRONG CRC was accepted and the mechanism moved, and every
        # test that drives the simulator was blind to the CRC rule.
        #
        # That blindness is not hypothetical: the CRC rule was
        # implemented backwards for a while and the whole suite stayed
        # green, because the simulator -- the thing every test drives
        # -- did not care.
        #
        # The rule is the one the firmware uses: CRC-16/CCITT-FALSE
        # over the decoded bytes of frame[0:len-2], stored little-endian
        # in the last two bytes. See protocol.build_text_frame and
        # analysis/firmware/fw-strings/A55A_SURVEY.md.
        #
        # A frame whose CRC does not match is DROPPED, the way the
        # firmware drops it -- and the drop is counted, so a test can
        # see it happened.
        if raw[:2] == b"\xa5\x5a" and len(raw) >= 5:
            if not self._crc_ok(raw):
                self.crc_failures += 1
                return None
        # The A0 control frame: A5 5A | len | A0 | B0 | vib | A0 | 01 | 0F |
        # <drive:2> | crc:2. The data_id byte is at offset 3 (0xA0).
        if raw[:2] == b"\xa5\x5a" and raw[3:4] == b"\xa0" \
                and len(raw) >= 11:
            # The drive value is a LITTLE-ENDIAN uint16 at byte offset 9.
            #
            # The byte order is not a guess: the device's own firmware
            # reads it that way. FUN_00009d2c in
            # analysis/firmware/ota/assets/
            # BIN_FA004_SRSN_ALMiA_V1.0.2_2506026_Beta.bin (file offset
            # 0x9d2c) loads the low byte from p+5 and the high byte from
            # p+6 -- where p is the frame's B0 byte, i.e. frame offset 4 --
            # and reassembles them with LDRH, which is little-endian on this
            # ARM:LE:32:Cortex target. See the full citation in
            # protocol.app_a0_control().
            #
            # protocol.py's docstring used to claim big-endian here, which
            # meant the simulator and the documentation disagreed about the
            # same two bytes and neither could be shown to be the device's
            # behaviour. Both now say little-endian, and the firmware is
            # the reason.
            self.device.position = int.from_bytes(raw[9:11], "little")
        elif raw[:3] == b"\xa5\x5a\x07":
            # The caps query. Mirror the real device: it answers by PUSHING
            # on the notify channel, never on the write-response channel.
            # Returning it here instead would let the simulator pass while
            # every real connection reported an empty identity.
            #
            # The branch matches the REQUEST frame's shape: byte 2 is 0x07,
            # the length of "A5 5A 07 00 01" plus its two CRC bytes. The
            # device's ANSWER has byte 2 = 0xB6 (182), its own length --
            # 182 bytes on the wire in the real capture
            # (analysis/evidence/hw_retest.json).
            #
            # It used to be worth noting that this branch does not recognise
            # the ANSWER shape, so a device that pushed its caps reply
            # through the write path would go unanswered here while the
            # real device answered. That remains true -- the simulator
            # answers a QUERY, and a query is what the host sends. What
            # changed is that the distinction is now stated rather than
            # left as a coincidence of byte values.
            if self._notify_cb is not None:
                await asyncio.sleep(0.05)
                try:
                    self._notify_cb(None, self.device.caps_reply())
                except Exception:                 # noqa: BLE001
                    pass
        elif raw[:5] == b"\xa5\x5a\x08\xb0\x3d" and raw[5:6] == b"\x54":
            # The A-generation telemetry command. r0.smali:322 passes 0x36 to
            # T0(); K0() formats it with String.valueOf (decimal), so the byte
            # on the wire is 0x54. 0x52 would be the motion mode.
            self.device.sensors_armed = True
        return None

    async def _stream(self) -> None:
        """Emit 0x29 telemetry continuously once armed."""
        try:
            while not self._stop:
                await asyncio.sleep(0.05)
                if self.device.sensors_armed and self._notify_cb is not None:
                    import math
                    # Values must be written as 12-bit little-endian words,
                    # because v1.e() reads two bytes per step and masks to
                    # 0x0FFF. Building single-byte payloads produced readings
                    # around 2560, which is a different quantity entirely.
                    # Five position slots x three sub-samples. Only the slots
                    # at or above the touched step carry a value, which is what
                    # makes the position quantiser resolve a step.
                    t = time.monotonic()
                    depth = int(2.5 + 2.4 * abs(math.sin(t * 0.35)))
                    slots = [0, 0, 0, 0, 0]
                    for k in range(depth + 1):
                        slots[k] = int(2200 + 700 * math.sin(t * 2.3 + k))
                    values = slots * 3
                    payload = bytearray()
                    for v in values:
                        payload += bytes([v & 0xFF, (v >> 8) & 0x0F])
                    frame = bytearray(b"\xa5\x5a\x29\xa0\xb0\x00\xa0\x03\x01")
                    frame += payload
                    from .protocol import crc16_ccitt_false
                    frame += crc16_ccitt_false(bytes(frame)).to_bytes(2, "little")
                    try:
                        self._notify_cb(None, bytes(frame))
                    except Exception:                 # noqa: BLE001
                        pass
        except asyncio.CancelledError:
            raise