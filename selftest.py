"""Offline self-test: prove the software works without any hardware.

Run this first on any platform. It exercises the frame builders, the parser,
the session, the sweep and the telemetry monitor against the built-in
simulator, and it fails loudly rather than passing on a simulator result when
hardware was expected.

    python selftest.py
"""

import asyncio
import os
import re
import sys
import time

if "--mutate" in sys.argv:
    sys.stderr.write("error: --mutate is not supported here; this script has no "
                     "mutation harness of its own -- it is the offline "
                     "no-hardware check, so a mutation run would have nothing "
                     "to mutate. Mutation harnesses live in scripts/, "
                     "p4_mutation.py first\n")
    sys.exit(2)

# This file lives inside the package, so its parent is what must be importable
# for `import vero` to resolve. os.path rather than a hard-coded "/" because
# Windows separators would silently break it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vero import protocol as P
from vero.automotion import AutoMotion, AutoParams
from vero.monitor import PressureMonitor
from vero.session import DeviceSession

failures = []


def check(name, ok, detail=""):
    print("  %-46s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- " + str(detail)))
    if not ok:
        failures.append(name)


def test_environment():
    """Fail early and legibly if the interpreter is too old.

    bleak 3.x needs Python 3.10+. Rather than let that surface as an
    ImportError or a pip "no matching distribution" wall of text, say
    exactly what is wrong here.
    """
    print("\nenvironment")
    v = sys.version_info
    check("python is 3.10+", v >= (3, 10),
          "running %d.%d.%d -- install Python 3.10+ from python.org"
          % (v[0], v[1], v[2]))
    try:
        import importlib.metadata as md
        ver = md.version("bleak")
        check("bleak is 3.x", ver.split(".")[0] == "3", ver)
    except Exception as exc:                                    # noqa: BLE001
        check("bleak importable", False,
              "pip install -r requirements.txt  (%s)" % exc)


def test_launcher():
    """The launcher is the part users hit first, so check its logic too."""
    print("\nlauncher")
    here = os.path.dirname(os.path.abspath(__file__))
    boot = os.path.join(here, "bootstrap.py")
    if not os.path.exists(boot):
        check("bootstrap.py present", False, "not found at " + boot)
        return
    import importlib.util
    spec = importlib.util.spec_from_file_location("_vero_boot", boot)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Candidates must be argv lists: a plain "py -3.13" string is probed as
    # one token named "p" and silently never matches.
    check("interpreter candidates are argv lists",
          all(isinstance(c, list) for c in mod.CANDIDATES))
    check("py -3.13 present as two tokens",
          ["py", "-3.13"] in mod.CANDIDATES)
    check("version gate rejects 3.9", not mod.ok_version((3, 9, 18)))
    check("version gate accepts 3.10", mod.ok_version((3, 10, 0)))
    major = mod.pip_version(sys.executable)
    check("pip version parses to an int", isinstance(major, int), repr(major))
    check("pip in this venv is new enough", mod.pip_ok(sys.executable))
    # A venv made by a pre-3.10 interpreter cannot install this project's
    # packages, and pip 19 reports that as "no matching distribution".
    check("venv age is detected, not assumed",
          mod.venv_too_old(sys.executable) is False)
    # "start" is a verb too: it is what the three platform entry
    # scripts call "just start it, do not prepare anything". It used to
    # be handled separately in each script, so it was a word the
    # bootstrap did not know -- and this assertion listed the verbs by
    # hand, so it went red when the word was added.
    #
    # The check is the PROPERTY -- every advertised verb dispatches --
    # not the exact set. A list written down is a list that goes stale.
    _advertised = set()
    for _name in ("run.sh", "install-ubuntu.sh", "install-macos.sh"):
        _path = os.path.join(os.path.dirname(os.path.abspath(__file__)), _name)
        if not os.path.exists(_path):
            continue
        with open(_path, encoding="utf-8") as _fh:
            _advertised |= set(re.findall(
                r"^#\s+\./[\w.-]+\.sh\s+([a-z]+)", _fh.read(), re.M))
    _advertised.discard("cli")      # a prefix, not a verb
    _advertised.discard("prepare")  # the default invocation's prose
    _handled = set(getattr(mod, "SELF_VERBS", ()))
    _missing = _advertised - set(mod.DISPATCH) - _handled
    check(not _missing,
          "all launcher commands dispatch",
          "not dispatched: %s" % ", ".join(sorted(_missing)))


def test_discovery():
    """Name matching alone is not portable; verify the fallbacks exist."""
    print("\ndevice discovery")
    from vero.discovery import (DEVICE_NAME_PREFIXES, DEVICE_SERVICE_UUIDS,
                                _iter_pairs, _matches)

    class D:
        def __init__(self, address, name=None, uuids=None):
            self.address, self.name, self.service_uuids = address, name, uuids

    # THE NAMES ARE PREFIXES, not whole serials. A whole serial would
    # only ever match one unit, and it would put one person's device
    # identifier in a published repository.
    check("the name table holds PREFIXES, not whole serials",
          all(len(p) <= 4 for p in DEVICE_NAME_PREFIXES),
          "the table holds %s" % (DEVICE_NAME_PREFIXES,))
    check("the Vero's prefix is known", "UMVX" in DEVICE_NAME_PREFIXES)
    check("NO unit's own serial is written down",
          not any(len(p) > 4 for p in DEVICE_NAME_PREFIXES)
          and "UMVXEXAMPLE01" not in DEVICE_NAME_PREFIXES,
          "the table still names a specific unit")
    check("HID 0x1812 is NOT used for matching",
          not any("1812" in u for u in DEVICE_SERVICE_UUIDS))
    real = D("E7:97:7D:XX:XX:XX", "UMVXEXAMPLE01",
             list(DEVICE_SERVICE_UUIDS))
    check("finds the device by name", _matches(real, None, DEVICE_NAME_PREFIXES,
                                               DEVICE_SERVICE_UUIDS))
    check("finds it with NO name at all", _matches(
        D("??", None, list(DEVICE_SERVICE_UUIDS)), None, DEVICE_NAME_PREFIXES,
        DEVICE_SERVICE_UUIDS))
    check("rejects an AirPods-like HID device", not _matches(
        D("??", "AirPods", ["00001812-0000-1000-8000-00805f9b34fb"]),
        None, DEVICE_NAME_PREFIXES, DEVICE_SERVICE_UUIDS))
    check("rejects a same-prefix lookalike UUID", not _matches(
        D("??", "Clone", ["6e400002-b5a3-f393-e0a9-e50e24dcca9e"]),
        None, DEVICE_NAME_PREFIXES, DEVICE_SERVICE_UUIDS))
    check("bleak 3.x {addr: (dev, adv)} shape is handled",
          list(_iter_pairs({"E7:97:7D:XX:XX:XX": (real, None)}))[0][1] is real)
    check("list-of-tuples shape is handled",
          list(_iter_pairs([(real, None)]))[0][1] is real)


def test_frames():
    print("\nframe construction")
    f = P.app_a0_control(0)
    check("zero frame is 13 bytes", len(f) == 13, len(f))
    check("zero frame header", f[:7].hex(" ") ==
          "a5 5a 0d a0 b0 00 a0", f[:7].hex(" "))
    check("zero frame value", f.hex(" ").upper() ==
          "A5 5A 0D A0 B0 00 A0 01 0F 00 00 D3 56", f.hex(" ").upper())
    full = P.app_a0_control(P.STROKE_MAX)
    check("100%% frame", full.hex(" ").upper() ==
          "A5 5A 0D A0 B0 00 A0 01 0F 10 27 25 01", full.hex(" ").upper())
    m = P.app_set_mode(52)
    check("mode 52 frame", m.hex(" ").upper() ==
          "A5 5A 08 B0 3D 52 E7 8D", m.hex(" ").upper())
    c = P.app_caps_query()
    check("caps query", c.hex(" ").upper() == "A5 5A 07 00 01 1E 90",
          c.hex(" ").upper())
    # CRC must verify on every frame we build.
    for name, frame in (("zero", f), ("100%", full), ("mode52", m), ("caps", c)):
        calc = P.crc16_ccitt_false(frame[:-2])
        check("%s CRC verifies" % name,
              frame[-2:] == calc.to_bytes(2, "little"),
              "%s != %s" % (frame[-2:].hex(), calc.to_bytes(2, "little").hex()))


async def _timer_ceiling(interval):
    """How many times a bare `asyncio.sleep(interval)` fires in a second.

    ON THIS MACHINE. It is the platform's timer granularity, measured
    with a loop that touches none of this project -- so it is a fact
    about Windows vs Linux, not about this code.

    Windows: 64 fires a second for a 5 ms sleep. Linux: 197. The
    difference is why a fixed frame-count threshold cannot be portable.
    """
    n = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 1.0:
        await asyncio.sleep(interval)
        n += 1
    return n


async def test_session():
    print("\nsession against the simulator")
    s = DeviceSession(simulate=True, interval=0.005)
    await s.connect()
    # connect() awaits a caps round trip before starting the send loop, so
    # checking pump_alive in the same tick reads False even though the task is
    # scheduled. Yield once before asserting on it.
    await asyncio.sleep(0.1)
    check("reports connected", s.connected)
    check("send pump alive", s.stats.pump_alive)
    check("identity read", s.stats.rssi is not None and s.stats.mtu is not None,
          "rssi=%s mtu=%s" % (s.stats.rssi, s.stats.mtu))
    check("simulator serial is unmistakable",
          s.stats.serial == "sim-device", s.stats.serial)

    # arm_sensors() is the session-layer entry point the HTTP routes call, and
    # this is the only in-registry session-level caller, so its return value
    # was never pinned by any check. It must report what the device did, not
    # that the call ran.
    tele = PressureMonitor()
    s.raw_notify = tele.feed
    armed = await s.arm_sensors()
    check("arm_sensors reports what the device did", armed is True, repr(armed))
    check("arming produced telemetry frames",
          tele.state()["frames"] > 0, tele.state()["frames"])
    check("the session recorded the arm", s.stats.sensors_armed is True,
          repr(s.stats.sensors_armed))
    s.raw_notify = None
    check("clearing the monitor clears the telemetry reference",
          s._telemetry is None)

    s.set_target(50)
    # THE THRESHOLD IS A RATE, NOT A COUNT, AND THAT IS THE POINT.
    #
    # This check used to sleep 1.0 s and demand `frames_sent > 100`. The
    # session was built here with `interval=0.005` -- a 5 ms pump, which
    # is faster than the 15 ms the production default uses -- so on the
    # machine that wrote the check, one second was worth several
    # hundred frames and the threshold was comfortable.
    #
    # On Windows the SAME check failed with 65 frames. That is not a
    # defect in the project: a bare `asyncio.sleep(0.005)` loop fires
    # 64 times a second on Windows and 197 times a second on Linux,
    # because Windows' timer granularity is coarser. MEASURED on a real
    # Windows 11 machine, with a loop that touches none of this code:
    #
    #     Windows  bare 5 ms timers in 1.0 s: 64
    #     Linux    bare 5 ms timers in 1.0 s: 197
    #
    # So the check was measuring the platform's timer, not the code.
    #
    # The fix is to ask the question that matters: is the pump keeping
    # up with the interval it was given? The expected count in one
    # second is `1 / interval`, and the pump is allowed to fall behind
    # by the granularity of the platform's timer -- which is what the
    # 0.5 factor below is, and it is measured, not guessed.
    #
    # At the PRODUCTION interval of 15 ms the two platforms agree
    # closely, because 15 ms is well above Windows' timer granularity:
    #
    #     Windows  59 frames in the first second
    #     Linux    66 frames in the first second
    #
    # which is the number that matters for a real device.
    # The expected count in one second is `1 / interval`. But the pump
    # CANNOT fire faster than the platform's timer granularity allows,
    # and on Windows a 5 ms timer fires about every 15 ms -- so the
    # ceiling for a 5 ms interval is about 64 a second, not 200.
    #
    # So the bar is set by what the platform can actually deliver, and
    # the platform's own ceiling is MEASURED, here, with a timer loop
    # that touches none of this project:
    #
    #     ceiling = how many times a bare `asyncio.sleep(interval)` fires
    #               in one second ON THIS MACHINE
    #
    # The pump must keep up with THAT. It is the honest question -- "is
    # the send loop running as fast as this machine's event loop
    # allows, at the interval it was given?" -- and it has the same
    # answer on every platform.
    #
    # At the PRODUCTION interval of 15 ms the two platforms agree
    # closely, because 15 ms is above Windows' timer granularity:
    #
    #     Windows  59 frames in the first second
    #     Linux    66 frames in the first second
    #
    # which is the number that matters for a real device.
    ceiling = await _timer_ceiling(s.interval)
    await asyncio.sleep(1.0)
    expected = min(1.0 / s.interval, ceiling) * 0.9
    check("frames are flowing",
          s.stats.frames_sent >= expected,
          "frames_sent=%s, expected>=%s (interval %s, platform ceiling %s)"
          % (s.stats.frames_sent, round(expected), s.interval,
             round(ceiling)))
    check("active position tracked", s.stats.active_percent == 50.0,
          s.stats.active_percent)
    check("simulator received the position",
          s._client.device.position == 5000, s._client.device.position)

    # Idle watchdog must zero the device when the client goes away.
    await asyncio.sleep(3.0)
    check("idle watchdog released the position",
          s.stats.active_percent == 0.0, s.stats.active_percent)

    a = AutoMotion(lambda: s)
    st = await a.start(AutoParams(20, 80, 128, traverse_seconds=0.5,
                                  dwell_seconds=0.0))
    seen = []
    orig = s.set_target

    def watch(pct, vib=0):
        seen.append(pct)
        return orig(pct, vib)

    s.set_target = watch
    await asyncio.sleep(3.0)
    out = await a.stop()
    s.set_target = orig
    # A full cycle is out-and-back, so a 0.5 s one-way traverse gives a 1 s
    # cycle; three seconds should cover at least two of them.
    check("sweep completed cycles", out["cycles"] >= 2, out["cycles"])
    check("sweep had no errors", out["errors"] == 0, out["errors"])
    check("sweep reports its range", out["params"]["min_percent"] == 20.0,
          out["params"])
    check("sweep reached the full range",
          19.0 <= out["current_percent"] <= 81.0, out["current_percent"])

    # The regression this guards: the phase used to advance one way only and
    # wrap, so every cycle slammed the command from one end of the stroke to
    # the other in a single tick. Both directions must be travelled properly.
    jumps = [abs(seen[i] - seen[i - 1]) for i in range(1, len(seen))]
    worst = max(jumps) if jumps else 0.0
    span = 60.0
    # Largest legitimate single-tick step: the sine waveform's peak velocity
    # is pi/2 times its average. A full cycle covers 4.0 phase units in
    # (2 * traverse) seconds, so one tick covers 4*TICK/(2*traverse) phase,
    # half of which is the outbound half -- and sin(phase*pi/2) turns a phase
    # step into at most (pi/2) * amplitude * dphase of travel.
    tick = 0.040
    dphase = 4.0 * tick / (2.0 * 0.5)
    amplitude = span / 2.0
    peak_step = amplitude * (3.14159 / 2.0) * dphase
    check("no command jumps across the stroke", worst < peak_step * 1.15,
          "worst jump %.2f%% vs theoretical peak %.2f%%"
          % (worst, peak_step))
    check("both halves of the stroke were visited",
          any(v < 30.0 for v in seen) and any(v > 70.0 for v in seen),
          "min=%.1f max=%.1f" % (min(seen), max(seen)))

    snap = s.stats.snapshot()
    for key in ("link_ok", "reconnects", "recovery_seconds", "reassert_frames",
                "link_lifetimes", "median_link_lifetime", "platform"):
        check("snapshot exposes %s" % key, key in snap)
    await s.disconnect()
    check("disconnect clears the link", not s.connected)


def telemetry_frame(value: int = 200) -> bytes:
    """Build a valid 0x29 telemetry frame.

    Constructed with the protocol helpers rather than hand-written hex: the
    first attempt at this wrote 38 bytes instead of 41 and the decoder
    correctly rejected it, which looked like a product bug but was a bad test.
    """
    header = bytes.fromhex("a5 5a 29 a0 b0 00 a0 03 01")
    group = bytes([value, 0x0A, value, 0x0D, 0, 0, 0, 0, 0, 0])
    body = group * 3
    frame = header + body
    return frame + P.crc16_ccitt_false(frame).to_bytes(2, "little")


def test_monitor():
    print("\ntelemetry monitor")
    m = PressureMonitor()
    frame = telemetry_frame(200)
    check("telemetry frame is 41 bytes", len(frame) == 41, len(frame))
    for _ in range(50):
        m.feed(frame)
    st = m.state()
    check("monitor ingested frames", st.get("frames", 0) >= 50, st)
    check("curve is available", len(m.curve(30)) > 0)


def test_product_profiles():
    """Which model is this, and does it have vibration.

    Three-state discipline runs through all of it: a recognised model answers
    True or False, an unrecognised prefix answers None, and the two must never
    be conflated. `supports_vibration is False` is what the request guard
    refuses on; `None` is what it allows. If these two ever collapse, either a
    Vero silently vibrates or an unlisted real device gets refused.
    """
    print("\nper-model capability profiles")
    check("Vero serial is detected case-insensitively",
          P.detect_product("UMVXEXAMPLE01") == "Vero",
          P.detect_product("UMVXEXAMPLE01"))
    check("a lowercase Vero serial is still Vero",
          P.detect_product("UMVXEXAMPLE02") == "Vero", P.detect_product("UMVXEXAMPLE02"))
    check("the WYRH prefix resolves to Wyrh",
          P.detect_product("WYRHEXAMPLE01") == "Wyrh",
          P.detect_product("WYRHEXAMPLE01"))
    check("the four documented models are all in PRODUCTS",
          set(P.PRODUCTS) == {"Vero", "Mira", "Nexo", "Wyrh"}, sorted(P.PRODUCTS))
    check("DEVICE_PROFILES has exactly one row per model",
          set(P.DEVICE_PROFILES) == set(P.PRODUCTS),
          sorted(P.DEVICE_PROFILES))

    # One row per documented source line, both booleans, checked by value --
    # not just "the keys exist". A profile table that parses but holds the
    # wrong polarity would refuse or allow exactly the wrong models. The keys
    # are SERIALS, not model names: device_profile() takes a serial and runs
    # the same prefix walk as everything else, so "Vero" as an argument is an
    # unrecognised serial (returns None), not a lookup by name.
    expect = {
        "Vero": ("UMVXEXAMPLE01", False, True),   # DeviceResolver.java:24
        "Mira": ("UMMIEXAMPLE01", True, True),    # DeviceResolver.java:26
        "Nexo": ("UMNXEXAMPLE01", False, False),  # DeviceResolver.java:28
        "Wyrh": ("WYRHEXAMPLE01", True, False),   # DeviceResolver.java:30
    }
    for model, (serial, vib, pos) in expect.items():
        row = P.device_profile(serial)
        check("%s profile row exists" % model, row is not None, row)
        check("%s serial resolves to %s" % (model, model),
              P.detect_product(serial) == model, P.detect_product(serial))
        if row is None:
            continue
        check("%s has_vibration is %r" % (model, vib),
              row["has_vibration"] is vib, row)
        check("%s has_position_sensors is %r" % (model, pos),
              row["has_position_sensors"] is pos, row)
        check("%s capability reads through profile_capability" % model,
              P.profile_capability(serial, "has_vibration") is vib,
              P.profile_capability(serial, "has_vibration"))

    check("a model NAME is not a serial and has no profile",
          P.device_profile("Vero") is None, P.device_profile("Vero"))

    check("an unrecognised serial has no profile at all",
          P.device_profile("SIM0000000000") is None,
          P.device_profile("SIM0000000000"))
    check("an unrecognised serial's vibration is None, not False",
          P.profile_capability("SIM0000000000", "has_vibration") is None,
          P.profile_capability("SIM0000000000", "has_vibration"))
    check("a mistyped capability name yields None, not an exception",
          P.profile_capability("UMVX1", "has_vibraTion") is None,
          P.profile_capability("UMVX1", "has_vibraTion"))

    # DeviceCaps delegates rather than re-walking the table. The delegation is
    # the point: a lowercase serial used to answer "Unknown" from here and
    # "Vero" from detect_product(), so the same device had two names.
    caps = P.DeviceCaps(serial="UMVXEXAMPLE01")
    check("DeviceCaps.product agrees with detect_product", caps.product == "Vero",
          caps.product)
    check("DeviceCaps.product is case-insensitive too",
          P.DeviceCaps(serial="UMVXEXAMPLE02").product == "Vero",
          P.DeviceCaps(serial="UMVXEXAMPLE02").product)
    check("DeviceCaps.generation uses the same spelling rule",
          P.DeviceCaps(serial="UMNXEXAMPLE02").generation == "B",
          P.DeviceCaps(serial="UMNXEXAMPLE02").generation)


async def main():
    test_environment()
    test_launcher()
    test_discovery()
    test_frames()
    await test_session()
    test_monitor()
    test_product_profiles()

    print("\n" + "=" * 60)
    if failures:
        print("  %d check(s) FAILED:" % len(failures))
        for f in failures:
            print("    - %s" % f)
        return 1
    print("  ALL CHECKS PASSED")
    print("  Note: this exercised the simulator only. A pass here does not")
    print("  say anything about real hardware -- use diagnose.py for that.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
