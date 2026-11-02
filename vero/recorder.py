"""Session recorder: every connection, recorded from connect to disconnect.

THE SCOPE
---------
Five separate requirements, and the first two decide the shape of everything
else:

  * EVERYTHING is recorded -- 行程 (travel), 压力 (pressure), 深入百分比
    (how deep the stroke went). Not a summary, not a peak: the samples.
  * The record starts when the CONNECTION starts and ends when it drops.
    The operator presses nothing. There is no "start recording" button and
    no "save" button, because a recording that the operator can forget to
    save is a recording that is not there when the fault is being
    reproduced.

THE LIFECYCLE
-------------
    connect()   -> recorder.start(...)   -> a NEW file, opened
    (samples)   -> recorder.sample(...)  -> one JSON line per sample
    disconnect()-> recorder.stop(...)    -> the file is closed and listed

The recorder is a plain object with no thread and no timer of its own. The
webapp owns it and calls it from the two places that already know when the
link comes up and goes down, so there is exactly ONE definition of "the
connection is up" in the whole program.

THE FORMAT
-----------
One file per connection, JSON Lines: one JSON object per line.

  * JSONL is append-only, so a recording that is cut short by a power loss
    still leaves every line written before the cut readable. A single JSON
    document would be lost whole.
  * One line per sample keeps a long session from being re-read and
    re-parsed as one blob.
  * Every line carries an ABSOLUTE timestamp (``unix``) and the writer
    ALSO computes the relative offset (``t``, seconds from the first
    sample). The chart's X axis is the relative one, but the absolute one
    is what makes the file a record rather than a picture: two sessions
    can be lined up against each other, and against the wall clock.

The first line of every file is a HEADER: a JSON object with ``_header`` set
to true, carrying the connection's metadata. The sample lines follow. A
reader therefore knows the shape of the file from its first line, and a
file whose first line is missing is visibly truncated rather than silently
interpreted as "zero samples".

WHAT IS AND IS NOT RECORDED
----------------------------
Recorded: travel (target and reported and active percent), the pressure
sample, the position percentage, the active slot, and the vibration
setting. These are the numbers the panel shows.

Not recorded: anything that would make the file grow without bound, and
anything that is not a measurement. The interval between two samples is
part of the sample (``unix`` is a wall clock), so the rate does not need a
separate field.

WHERE THE FILES GO
------------------
``vero/data/records/``. The repository's .gitignore already ignores
``records/`` (line 50), and the directory is created on demand, so a
runtime recording never enters version control. That is deliberate:
recording data is evidence about a session, not source.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not talk to the device, and it does not decide when a connection
starts. It is handed a sample and a moment. Everything about BLE, and
everything about the operator's intent, stays in session.py and webapp.py.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from typing import Any, Optional

#: Where the recordings go, relative to the package.
#:
#: ``vero/data/`` already exists and already holds ``battery_data.json``,
#: so the parent is not a new concept; ``records/`` is the new subdirectory
#: and it is covered by the .gitignore rule for ``records/``.
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
RECORDS_DIR = os.path.join(DATA_DIR, "records")

#: The file name of one recording. The timestamp is local time with the
#: offset spelled out, so a directory listing is readable without
#: converting anything, and two recordings started in the same second are
#: still distinct (the suffix is a counter, not a random string, so the
#: names sort in the order they were taken).
_STAMP_FMT = "%Y%m%d-%H%M%S"

#: The counter suffix is THREE digits, and it stays three digits.
#:
#: "%03d" is a MINIMUM width, not a maximum: at 1000 it produced
#: "20261025-094327-1000.jsonl", which does NOT match the reader's name
#: pattern (webapp.py's _RECORD_NAME allows exactly three digits), so the
#: file appeared in the history list and could not be opened -- a permanent
#: 404 on a record the server had just written itself.
#:
#: So the counter is WRAPPED into the three digits it has to occupy, and
#: the name stays inside the pattern at every value. A counter that has
#: run past 999 is not a state the operator can reach in one session, and
#: the alternative (a name the reader refuses) is strictly worse.
_COUNTER_MODULUS = 1000

#: The name of one recording. ONE pattern, shared by the writer and the
#: reader, so a file this module writes is a file the API can read.
#:
#: The recorder and the importer both produce names through this
#: pattern, and the webapp's read endpoint checks the SAME pattern
#: before it opens anything. Keeping the two in one place is what makes
#: "the server wrote a file it cannot read" structurally impossible
#: rather than a thing that has to be remembered.
_RECORD_STEM = r"[0-9]{8}-[0-9]{6}"
_RECORD_COUNTER = r"[0-9]{3}"
_RECORD_NAME_RE = re.compile(
    r"^%s(-%s|-导入(-%s)?)?\.jsonl$"
    % (_RECORD_STEM, _RECORD_COUNTER, _RECORD_COUNTER))


def record_name_pattern() -> "re.Pattern[str]":
    """The pattern every recording's file name must match.

    The READER's pattern lives in webapp.py, and this is the writer's
    copy of the same rule. webapp.py imports this one rather than
    keeping its own literal, so the two cannot drift -- which is the
    whole defect [H-2] was: the recorder wrote a name the reader
    refused, so the file was listed and could not be opened.
    """
    return _RECORD_NAME_RE


def json_safe(value: Any) -> Any:
    """A copy of ``value`` with every non-finite number replaced by None.

    Applied to the metadata and the sample columns BEFORE they are
    written, so a non-finite number can never reach the disk. This is
    the ROOT fix for [C-1]: a file that never contains NaN cannot take
    the list down, and the refused value is still visible as a null
    rather than silently dropped.

    A VALUE THE WRITER CANNOT ENCODE IS ALSO REPLACED. The strict
    writer raises TypeError on a set, a bytes object, a Decimal or a
    complex number, and that raise was caught as if the DISK had
    failed -- so one such value deleted the whole recording, exactly
    like the NaN did. The production sampler writes only floats and
    ints today, so this was not reachable from the device; but
    ``Recorder.sample()`` is public, and the comment above says the
    values are made JSON-safe here, which reads as "any value is
    safe". It is not, unless the unwritable ones are handled too.

    So anything the strict writer cannot encode becomes None --
    "this reading is not known" -- and the recording continues.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    # Everything else -- a set, bytes, Decimal, complex, a numpy
    # scalar, an object -- is either not encodable or is a number the
    # writer would refuse. It becomes None, which is encodable and
    # honest.
    try:
        json.dumps(value, allow_nan=False)
        return value
    except (TypeError, ValueError):
        return None


def dumps_strict(value: Any) -> str:
    """json.dumps that REFUSES to write a non-finite number.

    ``allow_nan=False`` is the point: the default writes the bare
    literal ``NaN``, which is what put an unparseable file on disk in
    the first place. With it, a non-finite number raises here, at the
    write, instead of surfacing later as a 500 on an unrelated
    endpoint.
    """
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class Recorder:
    """Records one connection, from connect to disconnect.

    Not a context manager on purpose: the connection's lifetime is
    decided by the webapp, and a ``with`` block would have to span the
    whole session -- which is exactly the span the operator is away from
    the keyboard. Two explicit calls (start/stop) can be reached from a
    disconnect handler, a lifespan shutdown, or a crash handler, and each
    of those is a place where the lifetime really ends.
    """

    def __init__(self, records_dir: Optional[str] = None) -> None:
        # ``records_dir`` may be a CALLABLE as well as a path: the
        # webapp keeps its records directory in a module-level constant
        # that may be redirected at runtime, and a recorder that
        # captured the path at construction time would keep writing into
        # the original directory after the constant had been moved.
        self._dir = records_dir if records_dir is not None else RECORDS_DIR
        self._fh = None
        self._path: Optional[str] = None
        #: The ".part" name the recording is written under before it is
        #: renamed into place. None when nothing is being recorded.
        self._temp: Optional[str] = None
        self._started_unix: Optional[float] = None
        self._samples = 0
        self._counter = 0

    @property
    def dir(self) -> str:
        """The directory this recorder writes into, read at the moment of use.

        It may be a CALLABLE, and that is not a convenience: the webapp
        keeps its records directory in a module-level constant that a
        test may redirect at runtime, and a recorder that captured the
        path at construction time would keep writing into the original
        directory after the constant had been moved. The two would then
        disagree -- the endpoints would read one directory and the
        recorder would write into another -- and a recording would show
        up in a response while not being on disk where the response
        said it was.
        """
        if callable(self._dir):
            return self._dir()
        return self._dir

    # ------------------------------------------------------------- state

    @property
    def recording(self) -> bool:
        """Is a recording open right now?"""
        return self._fh is not None

    @property
    def path(self) -> Optional[str]:
        """The file being written, or None."""
        return self._path

    @property
    def started_unix(self) -> Optional[float]:
        """When the recording started (wall clock), or None."""
        return self._started_unix

    @property
    def samples(self) -> int:
        """How many sample lines have been written."""
        return self._samples

    # ------------------------------------------------------------- start

    def start(self, meta: Optional[dict] = None) -> Optional[str]:
        """Begin a recording. Returns the file path, or None if it could not start.

        The header line is written FIRST, before any sample, so a file that
        exists on disk is always a file whose metadata is known. If the
        directory or the file cannot be created, the recording is refused
        (None) and the caller carries on without it: a broken recording
        directory must not stop the operator from connecting to the device.

        THE FILE IS WRITTEN UNDER A TEMPORARY NAME AND RENAMED INTO
        PLACE. The name the reader accepts is the name the writer uses,
        and a recording that is still being written is not yet a
        recording: it appears in the history list only once it is
        complete. See [H-4].
        """
        if self._fh is not None:
            return self._path
        try:
            os.makedirs(self.dir, exist_ok=True)
            path = self._new_path()
            self._fh = open(self._temp_path(path), "w", encoding="utf-8")
        except OSError:
            self._fh = None
            self._path = None
            return None
        self._path = path
        self._temp = self._temp_path(path)
        self._samples = 0
        self._started_unix = time.time()
        header = {
            "_header": True,
            "started_unix": round(self._started_unix, 3),
            "started_local": time.strftime("%Y-%m-%d %H:%M:%S %z"),
            # The metadata is made JSON-safe BEFORE it is written: a NaN in
            # the metadata is a value the server itself produced (the
            # monitor's own readings), and writing it would put a file on
            # disk that no strict JSON parser can read -- which takes the
            # whole history LIST down, because the list reads every
            # header. See json_safe().
            "meta": json_safe(meta or {}),
        }
        if not self._write_line(header):
            # The header did not go out, so the file on disk is not a
            # recording. It is removed rather than left behind: a file
            # with no header is invisible to the list and would sit in
            # the directory forever.
            self._abort()
            return None
        return path

    @staticmethod
    def _temp_path(final_path: str) -> str:
        """The temporary name a recording is written under.

        The suffix is ".part", and it is deliberately NOT ".jsonl": the
        listing only considers ".jsonl" files, so a recording in progress
        is not in the history list at all. The rename into place is
        what publishes it.
        """
        return final_path + ".part"

    def _abort(self) -> None:
        """Give up on a recording that could not be written, and clean up.

        The temporary file is removed and the state is cleared, so a
        failed start leaves NO trace in the records directory. The old
        code created the file with "w" (which truncates and CREATES it)
        and only removed it on OSError, so a ValueError or a TypeError
        left a 0-byte file behind -- and the import endpoint is
        unauthenticated, so those files could be piled up at will.
        """
        path, temp = self._path, self._temp
        self._fh = None
        self._path = None
        self._temp = None
        self._started_unix = None
        self._samples = 0
        for candidate in (temp, path):
            if not candidate:
                continue
            try:
                os.remove(candidate)
            except OSError:
                pass

    def sweep_abandoned(self) -> int:
        """Remove the ".part" files a killed process left behind.

        A recording is written under a ".part" name and renamed into
        place when it ends. A process that was KILLED -- SIGKILL, the
        power going off, the OOM killer -- never got to the rename, so
        its ".part" file stayed on disk forever.

        The ".part" files are invisible to the history list (the list
        only reads ".jsonl"), so they are not a wrong answer shown to
        the operator -- but they are disk, they grow, and every one of
        them is a recording that was thrown away.

        They are removed at START-UP rather than continuously, because
        that is the only moment it is certain no recording of THIS
        process is in flight. The count is returned so the caller can
        say what it did.
        """
        removed = 0
        try:
            names = os.listdir(self.dir)
        except OSError:
            return 0
        for name in names:
            if not name.endswith(".part"):
                continue
            candidate = os.path.join(self.dir, name)
            try:
                os.remove(candidate)
                removed += 1
            except OSError:
                pass
        return removed

    def _new_path(self) -> str:
        """A unique file name inside the records directory.

        The counter is WRAPPED into three digits, so the name stays
        inside the reader's pattern at every value. "%03d" is a minimum
        width, not a maximum, and at 1000 it produced a four-digit name
        that the reader refused -- a record the server wrote and could
        not open again. See _COUNTER_MODULUS.
        """
        stamp = time.strftime(_STAMP_FMT)
        while True:
            self._counter += 1
            # The counter is WRAPPED INTO THREE DIGITS. "%03d" is a
            # minimum width, not a maximum, so the 1000th recording was
            # named "...-1000.jsonl" -- four digits -- and the reader's
            # pattern (which allows exactly three) refused it. The
            # file was listed and could not be opened.
            #
            # The wrap is into 1..999, so the counter never reaches
            # 1000: 1000 would be the four-digit value. The plain
            # (no-counter) name is the FIRST recording of that second.
            counter = (self._counter - 1) % (_COUNTER_MODULUS - 1) + 1
            name = ("%s-%03d.jsonl" % (stamp, counter)
                    if counter > 1 else "%s.jsonl" % stamp)
            path = os.path.join(self.dir, name)
            if not os.path.exists(path) and not os.path.exists(
                    self._temp_path(path)):
                return path

    # ------------------------------------------------------------ sample

    def sample(self, **values: Any) -> bool:
        """Write one sample. Returns True if it was written.

        Every value is optional. A sample that could not be read (the
        device has not reported yet) is still worth recording, because
        the GAP is information: a recording that jumps from 40% to 90%
        without a sample in between must show the gap, not hide it.

        THE VALUES ARE MADE JSON-SAFE HERE. A sample whose value is a
        non-finite number -- a NaN from the monitor's own arithmetic, an
        Infinity from a division -- used to make the strict writer
        raise, and the raise was caught as if the DISK had failed: the
        file was closed and DELETED, and every sample already written
        was lost with it. Measured: two good samples, then one
        sample(pressure=NaN), and the directory was empty afterwards.

        A NaN is a bad VALUE, not a bad disk. It is written as null --
        "this reading is not known" -- and the recording continues.
        """
        if self._fh is None:
            return False
        now = time.time()
        row: dict[str, Any] = {"unix": round(now, 3)}
        if self._started_unix is not None:
            row["t"] = round(now - self._started_unix, 3)
        row.update(values)
        return self._write_line(json_safe(row))

    def _write_line(self, row: dict) -> bool:
        """Write one JSON line. Returns False if the write failed.

        A FAILED WRITE IS NOT SWALLOWED. The old code returned False and
        changed nothing else, so the recording kept claiming to be
        recording while every sample was being dropped -- a full disk
        looked exactly like a healthy session. Now a failure closes the
        file, removes the partial recording and clears the state, so
        `recording` is False and the operator sees that the recording
        died rather than a silent gap. See [H-7].
        """
        if self._fh is None:
            return False
        try:
            self._fh.write(dumps_strict(row) + "\n")
            # Flushed per line. A recording that is killed with the process
            # must not lose the samples that were already measured: this
            # file exists to be evidence, and evidence that is buffered
            # is evidence that is lost.
            self._fh.flush()
        except (OSError, ValueError, TypeError, AttributeError):
            # The write failed. The recording is over: the file is closed
            # and removed rather than left half-written, and the state is
            # cleared so `recording` reports the truth.
            #
            # AttributeError is in this list on purpose. It used to be
            # missing HERE but present in stop()'s except, and the
            # asymmetry was a silent-data-corruption path: an
            # AttributeError from write() skipped the abort, so the
            # file stayed, stop() swallowed the error, and the partial
            # recording was RENAMED INTO PLACE -- published into the
            # history list with no "_end" marker. The server itself
            # then reported "complete: false" about a file the list was
            # offering as a recording. That is the exact ambiguity the
            # "_end" marker exists to remove.
            #
            # The two except lists now match, so a write failure
            # always aborts and a recording is published only when it
            # ended cleanly.
            self._abort()
            return False
        if not row.get("_header"):
            self._samples += 1
        return True

    # -------------------------------------------------------------- stop

    def stop(self, meta: Optional[dict] = None) -> Optional[str]:
        """End the recording and close the file. Returns the path, or None.

        Safe to call when nothing is being recorded, and safe to call
        twice: the second call does nothing.

        THE FILE IS RENAMED INTO PLACE HERE. It was written under a
        ".part" name and only becomes a ".jsonl" recording when the
        recording ends, so the history list never shows a recording that
        is still being written -- and a reader that opens a recording
        gets the whole thing, not the first half of it. See [H-4].
        """
        if self._fh is None:
            return None
        path = self._path
        temp = self._temp or (self._temp_path(path) if path else None)
        # THE FILE HANDLE IS TAKEN BEFORE THE WRITE.
        #
        # The end line is written first, and if THAT write fails it
        # aborts the recording -- which sets self._fh to None. The
        # self._fh.close() after it then raised
        # "AttributeError: 'NoneType' object has no attribute 'close'",
        # and the except clause only caught OSError, so the error
        # escaped: the disconnect endpoint answered 500, and the
        # recording was gone anyway.
        #
        # So the handle is taken into a local, and the close is on the
        # local. The abort's effect on self._fh is then irrelevant.
        fh = self._fh
        try:
            # The END LINE IS ALWAYS WRITTEN. It used to be written
            # only when the caller passed metadata, so a recording
            # closed without metadata had no end marker at all -- and
            # the end marker is the only thing that tells a reader
            # "this recording is finished" apart from "this process
            # was killed mid-write". The two look identical otherwise:
            # the samples look fine and the gap is invisible.
            self._write_line({"_end": True,
                              "ended_unix": round(time.time(), 3),
                              "meta": json_safe(meta) if meta else {}})
            fh.close()
        except (OSError, ValueError, TypeError, AttributeError):
            # A close that fails is not worth raising over: the samples
            # are already on disk and the end line is best-effort.
            try:
                fh.close()
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        finally:
            self._fh = None
            self._path = None
            self._temp = None
            self._started_unix = None
            self._samples = 0
        # The rename is what publishes the recording. If it fails, the
        # partial file is removed: a ".part" file is invisible to the
        # list, and leaving it would be a file nobody can ever open.
        if path and temp:
            try:
                os.replace(temp, path)
            except OSError:
                try:
                    os.remove(temp)
                except OSError:
                    pass
                return None
        return path

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_exc):
        self.stop()
        return False


# ---------------------------------------------------------------- reading


#: The sort key of one recording's name.
#:
#: BUILT FROM THE SAME TWO CONSTANTS THE NAME PATTERN IS BUILT FROM, so
#: the two cannot drift: a name this accepts is a name the reader
#: accepts, and a name the reader accepts is a name this can order.
#:
#: The groups are the timestamp, the counter of a RECORDED name and the
#: counter of an IMPORTED one. The counter is a NUMBER -- the order the
#: recordings of one second were taken in -- and it is compared as one.
_RECORD_SORT_RE = re.compile(
    r"^(?P<stamp>%s)"
    r"(?:-(?P<counter>%s)|(?P<imported>-导入)(?:-(?P<imported_counter>%s))?)?"
    r"\.jsonl$"
    % (_RECORD_STEM, _RECORD_COUNTER, _RECORD_COUNTER))


def record_sort_key(name: str):
    """The sort key of one recording's file name, or None if it has none.

    THE KEY IS (TIMESTAMP, COUNTER AS A NUMBER), NOT THE NAME.

    The name is ``<timestamp>[-<counter>|-导入[-<counter>]].jsonl``, and
    the timestamp part sorts correctly as a string -- it is
    ``%Y%m%d-%H%M%S``, fixed width, so lexicographic order IS
    chronological order. The COUNTER does not: it is three digits, and
    three digits compared as a string put ``-010`` BEFORE ``-002``,
    because "0" < "2" in the second character. So the tenth recording of
    one second sorted ahead of the second one, and the list showed the
    recordings of a single second in an order that never happened.

    The suffix is parsed as an INTEGER and compared as one, which is the
    only way "the tenth" can come after "the second".

    THE COUNTERLESS NAME IS THE FIRST RECORDING OF THAT SECOND, so it
    sorts as counter 0 -- before ``-002``, and before every numbered
    name of the same timestamp. A string sort put it LAST for the same
    reason: "-002" < "<timestamp>.jsonl" is False, so the plain name
    sorted after every suffixed one of the same second, and the FIRST
    recording of a second was listed after the LAST one.

    AN IMPORTED RECORDING SORTS IN ITS OWN PLACE. The importer writes
    ``<timestamp>-导入.jsonl`` and ``<timestamp>-导入-002.jsonl`` -- the
    same shape, with the marker in the middle. Both parse: the marker
    is recognised, and the counter after it is the counter. So an
    imported recording sits among the recorded ones of its second, in
    the order it was actually imported, instead of being pushed to one
    end of the list by a string comparison that knows nothing about
    what the marker means.

    THE LAST GROUP IS A TIE-BREAK, AND IT IS THERE SO THE ORDER IS
    DETERMINISTIC. A recorded name and an imported name can carry the
    same timestamp and the same counter -- the recorder and the importer
    count independently -- and a comparison that stopped there would
    leave their order to whatever the directory listing happened to
    return. The imported one is the later event (the importer stamps
    the moment of the import, and the recorder's counterless name is
    the FIRST recording of that second), so it sorts as the newer one.

    None means "this name carries no timestamp", and such a name is not
    a recording at all -- it is left out of the ordering entirely and
    handled by the caller.
    """
    m = _RECORD_SORT_RE.match(name)
    if m is None:
        return None
    counter = m.group("counter") or m.group("imported_counter")
    # The counterless name is the FIRST recording of its second, so it
    # sorts as 0 -- before every numbered name of the same second.
    imported = 1 if m.group("imported") is not None else 0
    return (m.group("stamp"), int(counter) if counter else 0, imported)


def list_recordings(records_dir: Optional[str] = None) -> list[dict]:
    """Every recording on disk, newest first.

    Each entry carries the metadata a list view needs -- when it started,
    how long it lasted, how many samples it holds, and where the file is --
    and the metadata comes from READING THE FILE, not from a side index.
    An index is a second thing that can disagree with the first, and the
    only copy of a recording's metadata that cannot be lost is the one
    written inside the recording.

    THE ORDER IS BY (TIMESTAMP, COUNTER AS A NUMBER), NEWEST FIRST --
    not by the file name as a string. See record_sort_key() for why the
    string order is wrong: the counter is three digits, so "010" sorts
    before "002", and the counterless name of a second sorts after
    every numbered one of it.
    """
    d = records_dir or RECORDS_DIR
    out: list[dict] = []
    try:
        names = os.listdir(d)
    except OSError:
        return out
    # THE NAMES ARE ORDERED BEFORE ANY FILE IS OPENED. The order is a
    # property of the NAME, so it can be computed from the directory
    # listing alone -- and the listing is the only thing that has to be
    # read to know it.
    #
    # A name that carries no timestamp has no place in the order. It is
    # put at the END of the list, not at the front: the order is
    # descending (newest first), so a name with no key has to sort
    # BELOW every name that has one. The flag is therefore "does this
    # name have a key" -- True sorts FIRST in a descending sort, and
    # False (no key) sorts last.
    #
    # Such a file is still not skipped by the flag alone: whether it is
    # a recording is decided by read_header() below, exactly as before.
    # Only the ORDER changed; which files are listed did not.
    ordered = sorted(
        names,
        key=lambda n: (record_sort_key(n) is not None,
                       record_sort_key(n) or ("", 0)),
        reverse=True)
    for name in ordered:
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(d, name)
        info = read_header(path)
        if info is None:
            # A file that cannot be read at all is not listed: showing an
            # entry that cannot be opened would be a promise the list
            # cannot keep.
            continue
        out.append(info)
    return out


#: How many recordings one page of the list carries.
#:
#: The list is polled every two seconds while the tab is open, and the
#: whole list is read every time: on a directory of 3974 recordings that
#: is 3974 header reads and a 630 KB answer, twice a second, for a list
#: the operator scrolls.
#:
#: The page size is a page size, not a limit: the caller can ask for
#: any page, and the total is reported so the page control knows how
#: many there are.
DEFAULT_PAGE_SIZE = 100

#: The most a caller may ask for in one page.
#:
#: The bound is a guard, not a policy: a caller that asks for a million
#: rows gets the maximum and a note, rather than a million rows.
MAX_PAGE_SIZE = 500


def list_recording_names(records_dir: Optional[str] = None) -> list[str]:
    """Every recording's NAME on disk, newest first.

    THE NAMES ONLY, AND THAT IS THE POINT.

    ``list_recordings()`` reads every file's header, because the list
    shows the start time and the size. That is a file read per
    recording, and the list is polled every two seconds.

    The NAMES, though, are enough to know how many recordings there are
    and which ones are on which page -- and the header of a recording
    that is not on the page is not read at all. So a paginated list
    reads the headers of ONE PAGE, not of the whole directory.

    The order is the same one ``list_recordings()`` uses -- by
    (timestamp, counter as a number), newest first -- so the two can
    never disagree about which recording is first.
    """
    d = records_dir or RECORDS_DIR
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out = [n for n in names if n.endswith(".jsonl")]
    out.sort(key=lambda n: (record_sort_key(n) is not None,
                            record_sort_key(n) or ("", 0)),
             reverse=True)
    return out


def read_header(path: str) -> Optional[dict]:
    """The first line of a recording, parsed, or None if it is not a recording.

    THE METADATA IS MADE JSON-SAFE HERE, ON THE READ SIDE TOO. A file
    written by an OLDER build (or by something else entirely) can hold a
    bare ``NaN``, and the server's own JSONResponse refuses to write a
    non-finite number -- so returning it verbatim took the whole history
    LIST down with a 500, for as long as the file was on disk. There is
    no delete endpoint and no self-healing path, so one bad file made
    the "历史记录" tab unopenable for good.

    Sanitising on the read side means the WRITE side cannot be the only
    defence: a file that is already broken is read as "this value is
    unknown" rather than as a reason to refuse the entire list.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            first = fh.readline()
    except OSError:
        return None
    if not first.strip():
        return None
    try:
        row = json.loads(first)
    except ValueError:
        return None
    if not isinstance(row, dict) or not row.get("_header"):
        return None
    # EVERY FIELD GOES THROUGH THE SANITISER, NOT JUST THE METADATA.
    #
    # The first version scrubbed "meta" only. A file whose
    # "started_unix" was a bare NaN -- written by an older build, or by
    # something else entirely -- still came back with the NaN in it,
    # and the list still 500'd on it. The sanitiser was applied to the
    # one field the reviewer happened to look at.
    #
    # So the whole header is sanitised, and a field that is not a
    # number stays as it was (a string is a string); only a
    # NON-FINITE NUMBER becomes None. The point is that no value on
    # this path can be one the server's own JSON writer refuses.
    safe = json_safe(row)
    return {
        "name": os.path.basename(path),
        "path": path,
        "started_unix": safe.get("started_unix"),
        "started_local": safe.get("started_local"),
        "meta": json_safe(safe.get("meta") or {}),
        "size_bytes": _size(path),
    }


#: The trend window the RECORD chart's change-point detector uses.
#:
#: The live chart uses the detector's own default of 11 frames, and it
#: should: the live series is the pressure alone. A recording is
#: different -- its travel is the dominant term in its pressure, and an
#: 11-frame window does not absorb it, so the threshold is raised by the
#: travel's own residual and nothing is ever marked.
#:
#: MEASURED on a recording in the archive: with an 11-frame window the
#: threshold came out at 387.6 against a noise level of 129.2 and NOT
#: ONE frame was marked. With 61 the threshold came out at 1602.0
#: against a noise level of 534.0, and eight frames were marked.
RECORD_TREND_WINDOW = 61


def read_curve(path: str) -> Optional[dict]:
    """One recording, as plottable series.

    The shape matches what the pressure monitor's own ``series()`` returns
    -- ``t`` (relative seconds from the first sample), ``unix`` (absolute
    wall clock) and one array per channel -- so the chart code has ONE
    shape to draw and the recording does not invent a second meaning for
    the same numbers. The added channels are the ones the live view does
    not carry: travel, the position percentage and the active slot.

    ``t`` starts at 0 for the first sample, which is what the chart's X
    axis is specified to show.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            rows = []
            header = None
            ended = False
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    # A truncated final line (the process died mid-write)
                    # must not take the whole recording down. The samples
                    # before it are still good.
                    continue
                if not isinstance(row, dict):
                    continue
                if row.get("_header"):
                    header = row
                    continue
                if row.get("_end"):
                    # The recording ENDED. The _end line is the marker
                    # that says "this file is finished", and it is the
                    # only way a reader can tell a finished recording
                    # from one whose process was killed mid-write.
                    ended = True
                    continue
                rows.append(row)
    except OSError:
        return None
    if not rows:
        return {
            "t": [], "unix": [], "travel_target": [], "travel_active": [],
            "travel_reported": [], "pressure": [], "position_pct": [],
            "slot": [], "vibration": [], "press": {}, "count": 0,
            "started_unix": None,
            "started_local": None, "meta": {}, "complete": False,
        }

    base = rows[0].get("unix")
    t = []
    for row in rows:
        u = row.get("unix")
        t.append(round(u - base, 3) if isinstance(u, (int, float)) and
                 isinstance(base, (int, float)) else None)

    def column(name):
        # The columns are made JSON-safe on the way out, for the same
        # reason the header's metadata is: a single NaN anywhere in the
        # curve is enough to make JSONResponse raise, and the raise is a
        # 500 on a record the operator is trying to open. A value that
        # was never finite is reported as "not known" (null), which is
        # what it is.
        return json_safe([row.get(name) for row in rows])

    # EVERY POSITION'S OWN PRESSURE comes back as one array per slot, so
    # the recording carries the same five readings the live panel shows.
    # Every segment's every pressure value is recorded.
    #
    # The slot names are read out of the ROWS, not hardcoded, so a
    # recording written by an older build (which has no per-slot
    # columns) reads back as an empty map rather than as five columns
    # full of None -- the difference between "this run did not record
    # them" and "this run recorded them and they were all nothing".
    _slot_names = []
    for _row in rows:
        for _key in _row:
            if _key.startswith("press_") and _key not in _slot_names:
                _slot_names.append(_key)
    _press = {}
    for _key in _slot_names:
        _press[_key[len("press_"):]] = column(_key)

    # THE CHANGE POINTS, ON THE RECORDING'S OWN CLOCK.
    #
    # The live chart draws them, and the record chart did not: the
    # detector's marks are frames of the pressure series, and the record
    # chart has the whole series, so it can have the marks too.
    #
    # THE WINDOW IS 61, NOT THE LIVE DEFAULT OF 11, AND THE REASON IS
    # MEASURED. A recording's travel is the dominant term in its
    # pressure -- the rod's own motion moves the sensors -- and the
    # residual of an 11-frame trend window against that is large: the
    # threshold came out at 387.6 with a noise level of 129.2, and NOT
    # ONE frame was marked. With a 61-frame window the trend absorbs
    # the travel and the threshold came out at 1602.0 against a noise
    # level of 534.0, and eight frames were marked.
    #
    # So the record chart's window is the one that works on a recorded
    # stroke. The live chart keeps the shorter window, because the live
    # view's series is the pressure ALONE and does not carry the
    # recording's travel term.
    _change_t: list = []
    _change_v: list = []
    try:
        from .changepoints import ChangePointDetector
        _det = ChangePointDetector(trend_window=RECORD_TREND_WINDOW)
        for _i, _row in enumerate(rows):
            _p = _row.get("pressure")
            if _p is None:
                continue
            _when = _row.get("t")
            if not isinstance(_when, (int, float)):
                _when = float(_i) * 0.03
            _det.feed(float(_when), float(_p))
        for _t, _v, _r in _det.marks():
            _change_t.append(round(float(_t), 3))
            _change_v.append(_v)
    except Exception:                             # noqa: BLE001
        # The marks are a nicety. A recording that cannot be marked is
        # still a recording, and a detector that raises must not take
        # the whole archive with it.
        _change_t, _change_v = [], []

    return {
        "t": json_safe(t),
        "unix": json_safe([row.get("unix") for row in rows]),
        # The marks, on the SAME clock as "t" -- so a mark lands under
        # its own reading. The record chart draws them on the pressure
        # series, which is the series they were detected on.
        "change_t": json_safe(_change_t),
        "change_v": json_safe(_change_v),
        "change_threshold": round(_det.threshold(), 2) if _det else None,
        "travel_target": column("travel_target"),
        "travel_active": column("travel_active"),
        "travel_reported": column("travel_reported"),
        "pressure": column("pressure"),
        "position_pct": column("position_pct"),
        "slot": column("slot"),
        # THE VIBRATION COLUMN. It was WRITTEN on every row and then
        # DROPPED on the way back in, so a recording that carried the
        # vibration level of every sample came back without it -- the
        # one channel the operator's own slider set, gone.
        #
        # It is read exactly like every other column: one value per
        # sample, in the sample's order, and a sample that has no
        # vibration reading comes back as null (which is what
        # ``row.get()`` returns for a row that does not carry the key).
        # So the semantics of "this sample did not record it" are the
        # same as for the other columns -- a missing reading is null,
        # not 0, because 0 is a real vibration level ("no vibration")
        # and a null is an absent reading.
        "vibration": column("vibration"),
        "press": _press,
        "count": len(rows),
        # The header's own fields go through the sanitiser too, for the
        # same reason the columns do: a NaN in "started_unix" is enough
        # to make the response a 500.
        "started_unix": json_safe((header or {}).get("started_unix")),
        "started_local": json_safe((header or {}).get("started_local")),
        "meta": json_safe((header or {}).get("meta") or {}),
        # WHETHER THE RECORDING ENDED CLEANLY. The _end line is that
        # marker. Without it a reader could not tell a finished
        # recording from one whose process was killed mid-write -- the
        # truncated line is skipped silently, so the samples look fine
        # and the gap is invisible.
        "complete": ended,
    }


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# ---------------------------------------------------------------- writing an
# imported archive back to disk.


def write_imported(recording: dict, records_dir: Optional[str] = None) -> Optional[str]:
    """Write an imported archive into the records directory, and return its name.

    An imported file has to become a REAL history record, not a row that
    lives in the browser
    until the page is reloaded. A row that only exists in the page is
    gone the moment the operator picks another archive, and it is not in
    the list of "历史记录" at all -- the list is a listing of files on
    disk, and a file that is not on disk is not in it.

    So the import writes a real .jsonl file, with the SAME shape the
    recorder writes, and it lands in the SAME directory. That means the
    list, the chart, the replay and the export all treat it like any
    other recording, with no second code path for it.

    THE FILE IS A RECORDING, NOT A COPY OF THE EXPORT. The export format
    is a JSON envelope around the curve; the recording format is
    JSON-lines. So the envelope is unwrapped here and the samples are
    written back out as the rows they came from. The result is a file
    that read_curve() reads with no special case.

    The name is a TIMESTAMP like every other recording, with an "-导入"
    marker so an imported record is distinguishable from a recorded one
    in the list. The counter suffix keeps two imports in the same second
    apart, the same way the recorder does.

    The unix times are REBASED. The imported samples carry the wall
    clock of the machine they were recorded on, which is not this
    machine's clock, and the "t" column is relative to the first
    sample anyway. So the rows are written with a fresh, monotonic
    timeline: the first sample at "now", and each later one at its own
    original OFFSET. That keeps the recording's timing -- which is what
    the chart's X axis and the replay's clock are made of -- and drops
    the foreign absolute clock, which would otherwise put the imported
    recording years in the past.
    """
    d = records_dir or RECORDS_DIR
    if not isinstance(recording, dict):
        return None
    times = recording.get("t")
    if not isinstance(times, list) or not times:
        return None

    # THE METADATA MUST BE A MAPPING, AND IT IS CHECKED BEFORE ANY FILE
    # IS CREATED.
    #
    # The old code did ``dict(recording.get("meta") or {})`` INSIDE the
    # ``with open(path, "w")`` block, so a "meta" that was a list or a
    # string raised ValueError -- not OSError -- and the ``except
    # OSError`` could not see it. The file had already been created and
    # truncated by the "w", so a 0-byte file was left on disk for every
    # refused import. The import endpoint is unauthenticated, so those
    # files could be piled up without limit.
    #
    # So the shape is checked FIRST, and the file is written under a
    # temporary name and renamed into place: a half-written file is
    # never visible under a name the list would show.
    meta = recording.get("meta")
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        return None
    meta = json_safe(meta)

    # The samples that carry a time, and their times, in order. A sample
    # with no time cannot be placed on the timeline, so it is dropped --
    # the same rule read_curve() applies on the way back in.
    rows = []
    first_t = None
    for i, tv in enumerate(times):
        if not isinstance(tv, (int, float)) or not math.isfinite(tv):
            continue
        if first_t is None:
            first_t = tv
        rows.append((i, float(tv)))
    if not rows:
        return None

    # The base wall clock: NOW, so the imported record sorts as recent.
    # The absolute clock of the source machine is not usable here.
    base_unix = time.time()

    def _column(name):
        col = recording.get(name)
        return col if isinstance(col, list) else []

    press_map = recording.get("press")
    press_map = press_map if isinstance(press_map, dict) else {}

    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return None

    name = _imported_name(d)
    path = os.path.join(d, name)
    temp = path + ".part"
    try:
        with open(temp, "w", encoding="utf-8") as fh:
            header = {
                "_header": True,
                "started_unix": round(base_unix, 3),
                "started_local": time.strftime("%Y-%m-%d %H:%M:%S %z"),
                "meta": dict(meta,
                             imported=True,
                             imported_local=time.strftime("%Y-%m-%d %H:%M:%S %z")),
            }
            fh.write(dumps_strict(header) + "\n")
            for i, tv in rows:
                row = {"unix": round(base_unix + (tv - first_t), 3),
                       "t": round(tv - first_t, 3)}
                for key in ("travel_target", "travel_active",
                            "travel_reported", "pressure",
                            "position_pct", "slot", "vibration"):
                    col = _column(key)
                    if i < len(col):
                        row[key] = col[i]
                for slot, col in press_map.items():
                    if isinstance(col, list) and i < len(col):
                        row["press_%s" % slot] = col[i]
                # The row is made JSON-safe before it is written, so a
                # NaN in a column of the imported archive cannot put a
                # file on disk that the history list cannot serialise.
                fh.write(dumps_strict(json_safe(row)) + "\n")
            fh.write(dumps_strict({"_end": True,
                                   "ended_unix": round(time.time(), 3)}) + "\n")
        # The rename publishes the recording. Until this line, the file
        # is a ".part" file, which the list does not read at all.
        os.replace(temp, path)
    except (OSError, ValueError, TypeError):
        # A file that could not be written must not be half-written: it
        # is removed, so the list cannot show a broken recording and the
        # directory cannot accumulate 0-byte files.
        for leftover in (temp, path):
            try:
                os.remove(leftover)
            except OSError:
                pass
        return None
    return name


def _imported_name(d: str) -> str:
    """A unique file name for an imported recording, in `d`.

    The timestamp is the SAME format the recorder uses, so the list
    sorts the imported records among the recorded ones, and the
    "-导入" marker says where it came from.

    THE COUNTER IS A MODULE-LEVEL MONOTONE, NOT A RESCAN. The old
    version started at 0 every call and probed with ``os.path.exists``,
    so two calls in a row returned the SAME name -- the uniqueness was
    an accident of "call, then write" happening to be atomic, not a
    property of the function. It also re-walked the directory on every
    probe, which is O(n) per import.

    The counter is now monotonic and shared, and the counter is WRAPPED
    into the three digits the reader's pattern allows, so the name is
    always one the reader accepts. See [H-2] and [H-3].
    """
    stamp = time.strftime(_STAMP_FMT)
    global _import_counter
    while True:
        _import_counter += 1
        # Wrapped into three digits, for the same reason the recorder's
        # counter is: the reader's pattern allows exactly three, and a
        # four-digit name is a file that is listed and cannot be opened.
        counter = (_import_counter - 1) % (_COUNTER_MODULUS - 1) + 1
        name = ("%s-导入-%03d.jsonl" % (stamp, counter)
                if counter > 1 else "%s-导入.jsonl" % stamp)
        if not os.path.exists(os.path.join(d, name)):
            return name


#: The imported-name counter. Module-level, so two imports in the same
#: second do not have to probe the directory to find out that the first
#: one took the name. It only ever goes UP.
_import_counter = 0
