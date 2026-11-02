"""Reassembling a telemetry frame that the BLE link split in half.

THE PROBLEM
-----------
A telemetry frame is longer than one ATT payload can carry -- the
link's MTU is 23 bytes -- so it arrives in two notifications. The
project counted that (``pressure.py`` has ``ATT_PAYLOAD = 23`` and a
``FRAGMENT_THRESHOLD``) and then did nothing about it.

``pressure.decode_frame()`` decoded whatever it was given. Given the
first half it decoded the first half; given the second half it decoded
that too. The frame's own CRC is over the WHOLE frame, so BOTH halves
failed it, and both were counted as "the frame did not verify" -- which
is a real measurement, and it was measuring the reassembly that never
happened.

So the numbers disagreed with each other. The frame count was twice the
number of samples, the curve showed half as many readings as the
counter said, and the recorder wrote half-frames to disk as if they
were frames.

The app does the same thing -- ``n1.java`` processes every notification
independently, prefix-checking only the "A55A" head, and nothing
reassembles fragments on either side. So the app has the same blind
spot. The app has no recorder; this project does.

THE FRAME'S LENGTH IS IN THE FRAME
---------------------------------
A telemetry frame is::

    A5 5A <length> A0 B0 <vib> A0 03 01 <values> <crc>

and byte 2 is the LENGTH OF THE WHOLE FRAME. The real frame was
recovered from a live capture
(``analysis/scripts/find_pressure_byte.py``): 41 bytes on the wire,
byte 2 = 0x29 = 41, and the CRC over the whole 41 bytes verifies.

This was WRONG in the previous revision of this module, which said
"byte 2 is the COMMAND ID (0x36), NOT a length" and assembled to a
nominal 37 bytes. The consequence was not subtle: every frame was cut
at 37, the CRC -- which is over all 41 -- never matched, and the last
four bytes of every frame were thrown away. The live dashboard said
``crc_fail: 4880`` and ``reassembly_dropped: 4906``, which is every
frame and every notification.

So the frame's own header is the authority, and this module believes
it. The nominal length survives only as the fallback for a frame whose
header does not carry a usable length -- and the fallback is used ONLY
when the header's number is not usable, never in preference to it.

WHAT THE REASSEMBLER DOES
-------------------------
  * a notification that starts with the magic and no frame is in
    progress STARTS a frame;
  * a notification that does NOT start with the magic, while a frame is
    in progress, is its CONTINUATION -- the magic is at the frame's
    head, which arrived earlier, and the tail is just bytes;
  * a notification that starts with the magic while a frame is in
    progress means the old frame was abandoned -- a loss, and it is
    COUNTED, because a stream that loses frames silently looks like a
    stream that has nothing to say;
  * a notification that does not start with the magic and no frame is
    in progress is not a fragment of anything.
"""

from __future__ import annotations

#: The header of every frame this project speaks.
HEADER_MAGIC = b"\xa5\x5a"

#: The shortest frame that could be a frame at all.
MIN_FRAME_LENGTH = 12

#: The length a telemetry frame is assembled to when its header does
#: not carry a usable one.
#:
#: 7 header + 14 two-byte values + 2 CRC.
#:
#: This is a FALLBACK, not the target. A real telemetry frame carries
#: its own length in byte 2 (0x29 = 41), and the frame is assembled to
#: THAT. The fallback is here for a frame whose header does not say
#: something usable -- and it is never preferred over a length the
#: frame states itself.
NOMINAL_FRAME_LENGTH = 37

#: The offset of the length byte in the frame header.
LENGTH_OFFSET = 2

#: The smallest length a frame may claim and still be believable.
#:
#: The header is 9 bytes and the CRC is 2, so a frame shorter than 11
#: cannot be a whole frame. A frame that claims less than this is
#: treated as not stating a length, and the fallback is used.
MIN_CLAIMED_LENGTH = 11

#: The longest frame the protocol can carry, so a corrupt header
#: cannot make the buffer wait forever.
MAX_CLAIMED_LENGTH = 255

#: How many fragments the buffer will hold before it gives up on a
#: frame. The longest frame the protocol can carry is 255 bytes (the
#: length byte is one byte), and the shortest fragment is 1 byte, so
#: 260 is above the ceiling. The limit is here so a corrupt stream
#: cannot make the buffer grow without bound.
MAX_FRAGMENTS = 260


class Reassembler:
    """Collects the fragments of one frame and hands back the whole.

    ``feed`` takes one notification and returns the complete frame when
    this notification finished it, or None. The frame is returned
    BYTES -- the caller decodes it with the same decoder everything
    else uses, so a reassembled frame is not a second kind of frame.
    """

    def __init__(self, max_fragments: int = MAX_FRAGMENTS,
                 nominal_length: int = NOMINAL_FRAME_LENGTH):
        self._buf = bytearray()
        self.max_fragments = max(2, int(max_fragments))
        # The length the frame is ASSEMBLED to. See _finish() for why it
        # is the nominal one and not a length the frame carries.
        self.nominal_length = max(MIN_FRAME_LENGTH, int(nominal_length))
        # How many frames were PUT BACK TOGETHER -- that is, how many
        # took more than one notification -- and how many were
        # discarded because they never arrived whole. Both are worth
        # counting: a device that sends half a frame every time looks
        # exactly like a device that is silent, and only the counters
        # tell them apart.
        #
        # A frame that arrived WHOLE is not counted as reassembled: it
        # was not reassembled, it was simply decoded.
        self.completed = 0
        self.discarded = 0
        # The fragments that arrived for the frame in progress.
        self.fragments = 0

    def reset(self) -> None:
        """Forget the frame in progress."""
        self._buf = bytearray()
        self.fragments = 0

    @property
    def pending(self) -> int:
        """How many bytes of the frame in progress have arrived."""
        return len(self._buf)

    def feed(self, raw: bytes) -> bytes | None:
        """Take one notification. Returns the whole frame when it is done.

        None means "not yet, or not a frame at all".
        """
        if not raw:
            return None
        data = bytes(raw)
        if self._buf:
            # A frame is in progress. Is this notification its
            # CONTINUATION, or a NEW frame that arrived while the old
            # one was still unfinished?
            #
            # The magic tells them apart. A CONTINUATION is bytes at
            # offsets 3 and onwards of the frame, and it does NOT start
            # with the magic -- the magic is at the frame's head, which
            # arrived earlier. A NEW frame DOES start with the magic.
            #
            # So: a continuation is APPENDED, and a new frame means the
            # old one was abandoned -- which is a loss, and it is
            # counted.
            if data.startswith(HEADER_MAGIC):
                self.discarded += 1
                self.reset()
            else:
                self._buf.extend(data)
                self.fragments += 1
                if self.fragments > self.max_fragments:
                    self.discarded += 1
                    self.reset()
                    return None
                return self._finish()
        if not data.startswith(HEADER_MAGIC):
            # Not the head of a frame, and no frame is in progress, so
            # this is something else entirely.
            return None
        self._buf.extend(data)
        self.fragments += 1
        return self._finish()

    def _target_length(self) -> int:
        """How many bytes the frame in the buffer is to be assembled to.

        The frame says so itself: byte 2 is the LENGTH OF THE WHOLE
        FRAME. A real telemetry frame is 41 bytes and says 0x29 = 41.

        The nominal length is the fallback, used only when the header
        does not carry a usable number -- a frame that claims less than
        a whole frame, or more than the protocol can carry.

        WHY THIS MATTERS: the previous revision used the nominal length
        unconditionally. Every real frame was cut at 37 bytes, so the
        CRC -- which covers all 41 -- never matched, and the last four
        bytes of every frame were thrown away. The live counters said
        ``crc_fail: 4880`` and ``reassembly_dropped: 4906``.
        """
        if len(self._buf) > LENGTH_OFFSET:
            claimed = self._buf[LENGTH_OFFSET]
            if MIN_CLAIMED_LENGTH <= claimed <= MAX_CLAIMED_LENGTH:
                return claimed
        return self.nominal_length

    def _finish(self) -> bytes | None:
        """Is the frame in the buffer complete? If so, take it out.

        The frame's length is IN the frame -- byte 2 carries it -- so
        that is what the frame is assembled to. See the module
        docstring.
        """
        total = self._target_length()
        if len(self._buf) < total:
            # Not all of it yet.
            return None
        if len(self._buf) > total:
            # More than the frame needs. The extra bytes belong to the
            # NEXT frame, which the link has already started -- and
            # that is possible, because a notification can carry the
            # tail of one frame and the head of the next.
            frame = bytes(self._buf[:total])
            rest = bytes(self._buf[total:])
            _was_split = self.fragments > 1
            self._buf = bytearray()
            self.fragments = 0
            if _was_split:
                self.completed += 1
            # The rest may itself be a complete frame, if the link
            # packed two into one notification. That is handled by
            # feeding it back, but ONLY if it starts with the magic --
            # otherwise it is dropped, and the drop is counted.
            if rest.startswith(HEADER_MAGIC):
                self._buf.extend(rest)
                self.fragments = 1
            elif rest:
                self.discarded += 1
            return frame
        frame = bytes(self._buf)
        _was_split = self.fragments > 1
        self._buf = bytearray()
        self.fragments = 0
        if _was_split:
            self.completed += 1
        return frame

    def state(self) -> dict:
        """The counters, for a panel that wants to say why."""
        return {
            "reassembled": self.completed,
            "reassembly_dropped": self.discarded,
            "reassembly_pending": self.pending,
        }
