"""The OTA channel: the firmware asks for firmware, the host sends it.

WHAT THIS IS
------------
The device can be told to update its own firmware over BLE. The
mechanism is entirely in the firmware and the vendor app -- this project
had NEITHER: ``protocol.py`` carried one constant,
``MODE_OPCODE_OTA = 0x57``, and no code at all.

THE PROTOCOL, FROM THE VENDOR APP'S OWN SOURCE
----------------------------------------------
The device REQUESTS a chunk, over the notification channel, and the host
answers. The request is an A55A frame whose body is:

    F8 A1 424C4531 <offset:4 bytes> <length:1 byte> <crc:2 bytes>

  * ``F8`` -- the OTA opcode. The firmware's own dispatch table sends
    data_id 0xF8 to the OTA path (A55A_SURVEY.md: "0x7480 (gate 0xF8)
    -> the OTA path").
  * ``A1`` -- "give me a chunk". The other two the firmware sends are
    ``F800424C4531`` and ``F801424C4531`` -- ``424C4531`` is ``"BLE1"``
    in ASCII, and the 00/01 say "chunk 0" and "chunk 1" of the
    handshake. They are the START of a transfer, and the host answers
    them by beginning one.
  * ``<offset:8>`` -- where in the file the chunk starts, as EIGHT hex
    characters, BIG-ENDIAN. The app parses it as four bytes and
    reassembles them big-endian, so the wire form is a big-endian
    32-bit offset.
  * ``<length:2>`` -- how many bytes the chunk is, as two hex
    characters.

And the ANSWER is the app's own frame builder with the opcode ``A2``:

    A2 424C4531 <offset:8> <length:2> <payload>

which is the same A55A envelope every other frame uses, with
``424C4531`` -- ``"BLE1"`` -- after the opcode, then the chunk's
payload, then the offset and the length it answers.

(The offset and the length come BEFORE the payload, because the
payload is the longest element and the app's list is built in the order
the firmware reads. That order is not a guess: it is the order the
app's own list is built in -- m.smali:597-736, where the list gains
"A2", then "424C4531", then the offset, then the length, and then the
firmware data.)

WHY THE FRAME IS BUILT THE WAY IT IS
-------------------------------------
The app's builder is ``k()`` (r1.java:922), and it is the SAME builder
the A55A control frames use: ``"A55A" + length + opcode + args +
CRC``, where the CRC is the app's own CRC-16/CCITT-FALSE over the
decoded bytes. So the OTA answer is not a new frame family -- it is an
A55A frame with a different opcode.

WHAT IS NOT HERE
----------------
THE FIRMWARE ITSELF. The repository has 43 MB of OTA payload in
``analysis/firmware/ota/`` but NO firmware image for this device: the
files there are the app's own download manifest and the web assets the
app fetches, not a Vero firmware.

So the channel is implemented and the PAYLOAD is not here, and the
project says so rather than pretending. A caller supplies the file.

THE SAFETY RULES
----------------
Firmware is the one thing this project does that can brick a device, so
the rules are strict:

  * the mode is checked BEFORE anything is sent. The firmware only
    accepts a control frame in the OTA mode (0x57), and a control frame
    in the wrong mode is counted by the firmware and ignored;
  * the frame is checked before it goes out -- the offset and the length
    have to be inside the file, and the length has to fit in the frame;
  * nothing is written to the device until the operator says so. The
    channel is READ-ONLY by default: it will tell the operator what the
    device is asking for, and it will not answer until it is told to.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

#: The opcode the firmware uses for the OTA path.
OTA_OPCODE = "F8"

#: The "give me a chunk" sub-opcode, INCLUDING the OTA opcode in front
#: of it -- so it can be compared against the frame's own prefix.
OTA_REQUEST_CHUNK = "F8A1"

#: The opcode the ANSWER carries.
OTA_ANSWER_OPCODE = "A2"

#: ``"BLE1"`` in ASCII -- the tag that identifies the BLE firmware
#: channel, in both the request and the answer.
OTA_TAG = "424C4531"

#: The opcode the firmware uses to start the transfer, and the one that
#: asks for the first chunk.
OTA_START = "F800424C4531"
OTA_CONTINUE = "F801424C4531"

#: The largest chunk that fits in one A55A frame.
#:
#: The frame is length-prefixed with ONE byte, and the length byte
#: covers the whole frame: magic(2) + length(1) + body + crc(2). The
#: body is the opcode(1) + the tag(4) + the offset(4) + the
#: length(1) + the payload. So the payload is at most 255 - 5 - 10 -
#: 1 = 239 bytes.
MAX_CHUNK = 239

#: The largest offset the firmware can ask for: 32 bits, unsigned.
MAX_OFFSET = 0xFFFFFFFF


class OtaError(ValueError):
    """An OTA request that cannot be answered.

    A subclass of ValueError, so a caller that only knows about
    ValueError still catches it -- the HTTP layer turns it into a 422.
    """


@dataclass
class OtaRequest:
    """What the device asked for."""

    #: Where in the file the chunk starts.
    offset: int = 0
    #: How many bytes the chunk is.
    length: int = 0
    #: Whether this is the START of a transfer.
    start: bool = False
    #: The raw request, for the record.
    raw: str = ""

    def as_dict(self) -> dict:
        return {"offset": self.offset, "length": self.length,
                "start": self.start, "raw": self.raw}


@dataclass
class OtaState:
    """What the OTA channel knows.

    The channel is READ-ONLY until it is told to answer: it records what
    the device asked for, and it will not send a chunk until the
    operator says so. That is the difference between a channel that can
    brick a device and one that cannot.
    """

    #: The device's own mode, as the status frame reports it.
    mode: int | None = None
    #: Whether the device is in the OTA mode -- the mode in which a
    #: control frame is accepted at all.
    in_ota_mode: bool = False
    #: The requests the device has made, newest last.
    requests: list = field(default_factory=list)
    #: The chunks that were sent.
    sent: int = 0
    #: The bytes that were sent.
    bytes_sent: int = 0
    #: The last error, if any.
    error: str = ""
    #: Whether the channel will ANSWER. False until the operator says
    #: so.
    armed: bool = False
    #: The file the chunks are read from, if one has been set.
    path: str = ""
    #: The size of that file.
    size: int = 0

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "in_ota_mode": self.in_ota_mode,
            "requests": list(self.requests[-8:]),
            "request_count": len(self.requests),
            "sent": self.sent,
            "bytes_sent": self.bytes_sent,
            "error": self.error,
            "armed": self.armed,
            "path": os.path.basename(self.path) if self.path else "",
            "size": self.size,
        }


def parse_request(text: str) -> OtaRequest | None:
    """Parse the device's OTA request, or None if it is not one.

    The request is the A55A frame's BODY as text, which is how the app
    reads it: the app takes the notification's bytes, renders them as
    upper-case hex, and prefix-checks the string.

    A request is one of:

      * ``F8A1<offset:8><length:2>`` -- give me a chunk;
      * ``F800424C4531`` -- start the transfer;
      * ``F801424C4531`` -- continue the transfer.
    """
    if not text:
        return None
    s = str(text).strip().upper()
    # THE MATCH IS A "CONTAINS", NOT A "STARTS WITH".
    #
    # The app checks the request with ``wf.h.N(X2, "F8A1424C4531")``,
    # and wf.h.N is a CONTAINS (it is indexOf under the name): the
    # notification is rendered as a hex string and the marker is looked
    # for ANYWHERE in it.
    #
    # So the frame's own A55A envelope is part of the string the app
    # searches, and the OTA marker is somewhere inside it. Matching the
    # start of the string instead would never match a real request.
    if OTA_START in s:
        return OtaRequest(start=True, raw=s)
    if OTA_CONTINUE in s:
        return OtaRequest(raw=s)
    if OTA_REQUEST_CHUNK not in s:
        return None
    # THE OFFSET AND THE LENGTH ARE AT THE END OF THE FRAME.
    #
    # The app reads them from the END, not from just after the
    # sub-opcode: ``X2.substring(X2.length() - 14, X2.length() - 6)``
    # for the offset and ``X2.substring(X2.length() - 6, X2.length() - 4)``
    # for the length.
    #
    # Over the HEX STRING, 14 characters is SEVEN BYTES, so the last
    # seven bytes of the frame are:
    #
    #     [offset:4][length:1][crc:2]
    #
    # and the LENGTH IS ONE BYTE. The app reads two hex characters and
    # parses them, which is one byte's worth -- and its answer frame
    # puts the length in the same place, as ``"%02x"``.
    #
    # So the request frame is:
    #
    #     A55A <op:1> F8 A1 424C4531 <offset:4> <length:1> <crc:2>
    #
    # and the opcode is the payload length plus 22 -- the app's
    # ``parseInt2 + 22`` -- because the OTA frame is NOT the
    # A-generation shape, where the byte after the magic is a LENGTH.
    # Reading the offset from the start of the payload instead would be
    # wrong for every real request.
    # The slices are relative to the END, so the frame's CRC has to be
    # there for them to land on the right bytes. A frame without one
    # is not a frame the app would read, so it is not a request.
    if len(s) < 16:
        return None
    offset_hex = s[-14:-6]
    length_hex = s[-6:-4]
    try:
        offset = int(offset_hex, 16)
        length = int(length_hex, 16)
    except ValueError:
        return None
    return OtaRequest(offset=offset, length=length, raw=s)


def build_answer(chunk: bytes, offset: int, length: int) -> bytes:
    """The answer frame for one chunk.

    The app's own list, in the app's own order:
    ``"A2"``, ``"424C4531"``, the payload, the offset, the length.

    The frame is the A55A envelope the app's builder produces, with the
    CRC the app computes over the decoded bytes.
    """
    from .protocol import build_a55a_app
    if offset < 0 or offset > MAX_OFFSET:
        raise OtaError("the offset must be 0..%d, not %d"
                       % (MAX_OFFSET, offset))
    if length < 0 or length > 0xFF:
        raise OtaError("the length must be 0..255, not %d" % length)
    if len(chunk) > MAX_CHUNK:
        raise OtaError("a chunk may be at most %d bytes, not %d"
                       % (MAX_CHUNK, len(chunk)))
    # THE APP'S ORDER: the opcode, the tag, the offset, the length,
    # and THEN the payload.
    #
    # The app builds its list in that order (m.smali:597-736): "A2",
    # then "424C4531", then the offset, then the length, and the
    # firmware data LAST -- because the payload is the longest
    # element, and the firmware reads the frame in that order.
    return build_a55a_app(
        OTA_ANSWER_OPCODE,
        [OTA_TAG, "%08X" % offset, "%02X" % length, chunk.hex().upper()])


def read_chunk(path: str, offset: int, length: int) -> bytes:
    """The bytes the device asked for, out of the firmware file.

    A short read -- the file is shorter than the request -- is a
    refusal, not a short chunk: the device asked for bytes that are not
    there, and answering with fewer would tell it the file ends
    somewhere it does not.
    """
    if offset < 0 or length < 0:
        raise OtaError("the offset and the length must not be negative")
    if length == 0:
        return b""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read(length)
    except OSError as exc:
        raise OtaError("cannot read the firmware: %s" % exc) from None
    if len(data) < length:
        raise OtaError(
            "the firmware file is %d bytes at the offset, and the device "
            "asked for %d -- the file does not hold what was asked for"
            % (len(data), length))
    return data
