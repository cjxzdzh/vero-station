"""Command line interface: move the device without the browser.

Windows-friendly on purpose -- no colour codes, no curses, and every failure
prints a sentence that says what to do next rather than a traceback.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import protocol as P
from .automotion import AutoMotion, AutoParams
from .session import DeviceSession


async def _with_session(args, body):
    s = DeviceSession(simulate=False, address=args.target)
    try:
        print("connecting to %s ..." % (args.target or "the device"))
        await s.connect()
    except Exception as exc:                               # noqa: BLE001
        print("could not connect: %s" % exc)
        print("  - is the device on, and not connected to a phone?")
        print("  - Windows: Settings > Bluetooth should be on")
        print("  - run 'scan' to see what is actually advertising")
        return 1

    print("connected. serial=%s fw=%s power=%s rssi=%s mtu=%s"
          % (s.stats.serial, s.stats.firmware, s.stats.power,
             s.stats.rssi, s.stats.mtu))
    if s.stats.identity_error:
        print("  identity query failed: %s" % s.stats.identity_error)
    try:
        return await body(s)
    finally:
        await s.disconnect()


async def cmd_move(args):
    async def body(s):
        print("holding %d%% for %.1f s" % (args.percent, args.seconds))
        s.set_target(args.percent, args.vibration)
        # THE CLI HAS NO PAGE, so it sends no heartbeat -- and the idle
        # guard therefore treated it as an absent client and retracted
        # the position 2.5 s in. Measured, before this fix: `move
        # --percent 100 --seconds 30` reported "holding 100% for 30.0 s"
        # and the position was at 0.0 from t=2.0 s onwards, for the rest
        # of the 30 s.
        #
        # So the CLI claims the same hold the public API's stroke route
        # claims. It is ended by the release below, which the CLI always
        # makes -- so the hold cannot outlive the command.
        hold_for_client = getattr(s, "hold_for_client", None)
        if hold_for_client is not None and args.percent > 0:
            hold_for_client()
        try:
            await asyncio.sleep(args.seconds)
        finally:
            await s.release()
        print("done. frames=%d reconnects=%d recovery=%.2fs"
              % (s.stats.frames_sent, s.stats.reconnects,
                 s.stats.recovery_seconds))
        return 0
    return await _with_session(args, body)


async def cmd_sweep(args):
    async def body(s):
        a = AutoMotion(lambda: s)
        p = AutoParams(min_percent=args.min, max_percent=args.max,
                       vibration=args.vibration,
                       traverse_seconds=args.traverse,
                       dwell_seconds=args.dwell,
                       feedback=not getattr(args, "no_feedback", False))
        st = await a.start(p)
        print("sweeping %d%%..%d%%, vibration %d, one way %.2f s (sine wave)"
              % (args.min, args.max, args.vibration, args.traverse))
        try:
            await asyncio.sleep(args.seconds)
        finally:
            await a.stop()
            await s.release()
        print("done. cycles=%d frames=%d reconnects=%d recovery=%.2fs"
              % (st.get("cycles", 0), s.stats.frames_sent, s.stats.reconnects,
                 s.stats.recovery_seconds))
        return 0
    return await _with_session(args, body)


async def cmd_info(args):
    async def body(s):
        print()
        print("  serial   : %s" % s.stats.serial)
        print("  firmware : %s" % s.stats.firmware)
        print("  power    : %s V" % s.stats.power)
        print("  RSSI     : %s dBm" % s.stats.rssi)
        print("  MTU      : %s" % s.stats.mtu)
        print("  armed    : %s" % s.stats.sensors_armed)
        print()
        return 0
    return await _with_session(args, body)


async def cmd_frame(args):
    d = int(round(args.percent / 100.0 * P.STROKE_MAX))
    f = P.app_a0_control(d, args.vibration)
    print("percent %d%% -> drive %d" % (args.percent, d))
    print("hex: %s" % f.hex(" ").upper())
    return 0


# --- the ten commands the prototype offered and this one did not ------------
# --- Brought over 2026-10-19 because telemetry's census check ("and offers
# --- every command the prototype offered") was red on a real gap, not a
# --- stale reading: the prototype's parser names eleven subcommands and this
# --- one named five. Ported, not rewritten -- each keeps the prototype's
# --- argument names, defaults and help text, so a muscle-memory script works
# --- against either. What CHANGED is the substrate, and it is stated per
# --- command rather than glossed: the prototype drove a raw BleakClient and
# --- an aikectl.control.StreamController; this one drives a DeviceSession,
# --- whose only motion writer is set_target(). That makes three of the ten
# --- (gait, ramp, move's sustained hold) closed-loop against the device's own
# --- reported position where the prototype was open-loop, and it means there
# --- is no mode writer and no TUI on this side -- see cmd_mode and cmd_tui for
# --- what they do instead of faking.

_STOP_FRAMES = 3


def _hexs(data: bytes) -> str:
    return data.hex(" ").upper()


async def _quiet_frame(args, frame, label):
    """Write one frame that needs no live link to have been open long.

    Reports failure the way this module reports every failure: a sentence
    saying what happened, not a traceback.
    """
    async def body(s):
        try:
            await s._write(frame, response=False)          # noqa: SLF001
        except Exception as exc:                          # noqa: BLE001
            print("%s failed: %s" % (label, exc))
            return 1
        print("%s: %s" % (label, _hexs(frame)))
        return 0
    return await _with_session(args, body)


async def cmd_caps(args):
    """Print the capability query frame -- no link, like `frame`."""
    return await _quiet_frame(args, P.build_caps_query(), "caps query")


async def cmd_frames(args):
    """Print control frames without connecting, like `frame` but wider."""
    print("caps query  : %s" % _hexs(P.build_caps_query()))
    print("handshake   : %s" % _hexs(P.build_handshake()))
    print()
    print("-- generation A (1.1.5, A55A text + CRC16) --")
    for name in ("low", "medium", "high"):
        print("intensity %-7s: %s" % (name, _hexs(P.build_intensity(name))))
    # A-generation control goes through app_a0_control, not build_control.
    # build_control(gen="A") was REMOVED on purpose -- it dispatched to a
    # helper that does not exist in this package, so the branch raised
    # NameError on every call, and its field list disagreed with the frame the
    # app actually transmits. The prototype's `frames` called it anyway; here
    # the real A0 frame is printed instead of a broken one.
    drive = int(round(args.level / 100.0 * P.STROKE_MAX))
    print("control A-gen     : %s"
          % _hexs(P.app_a0_control(drive, args.vib)))
    for m in (52, 53, 55):
        try:
            print("mode %d A-gen    : %s" % (m, _hexs(P.app_set_mode(m))))
        except ValueError as exc:
            print("mode %d A-gen    : UNREPRESENTABLE (%s)" % (m, exc))
    print()
    print("-- generation B (1.2.3 DeviceV13, little-endian binary) --")
    f = P.build_v13_motion(args.vib, 0, args.pos)
    print("motion pos=%-6d: %s  (%dB)" % (args.pos, _hexs(f), len(f)))
    pts = ([(i * 20, i * args.pos // 5) for i in range(5)]
           if args.pos else [(0, 0)] * 5)
    g = P.build_v13_motion_path(args.vib, pts)
    print("motion path(5)    : %s  (%dB)" % (_hexs(g), len(g)))
    for m in (52, 53, 55):
        print("mode %d B-gen    : %s" % (m, _hexs(P.build_v13_mode(m))))
    return 0


async def cmd_probe(args):
    """Connect, ask for capabilities, print what came back."""
    async def body(s):
        print()
        print("  serial   : %s" % s.stats.serial)
        print("  product  : %s" % getattr(s.stats, "product", "?"))
        print("  firmware : ble %s, mcu %s" % (s.stats.firmware, s.stats.mcu_firmware))
        print("  supply   : %.3f V" % s.stats.power)
        if s.stats.rssi is not None:
            print("  rssi     : %s dBm" % s.stats.rssi)
        if s.stats.mtu is not None:
            print("  mtu      : %s" % s.stats.mtu)
        if s.stats.identity_error:
            print("  identity : %s" % s.stats.identity_error)
        return 0
    return await _with_session(args, body)


async def cmd_status(args):
    """The prototype aliased probe to status; keep both names."""
    return await cmd_probe(args)


async def cmd_watch(args):
    """Stream notifications until interrupted."""
    async def body(s):
        print("watching (Ctrl-C to stop)")
        try:
            while True:
                await asyncio.sleep(0.5)
        except (KeyboardInterrupt, asyncio.CancelledError):
            return 0
    return await _with_session(args, body)


async def cmd_mode(args):
    """Switch mode.

    DeviceSession has no mode writer -- it issues exactly one mode frame, the
    POST_CONNECT_MODE, inside its handshake, and nothing else writes one. So
    this prints what it WOULD send and says plainly that it cannot send it,
    rather than silently dropping the command or reaching into a private
    method. The frame itself is real and is built by the same protocol module
    the handshake uses.

    The prototype's help offered 52-56 (A-gen) or a raw 0-255 (B-gen). The
    half of that which does not exist on this device is rejected by
    app_set_mode rather than faked: it renders single-digit modes as an
    odd-length hex string, which the app's own q.l() throws on, so there is
    no byte to send. The message says which constraint, not just that it
    failed.
    """
    try:
        frame = P.app_set_mode(args.mode)
    except ValueError as exc:
        print("mode %s cannot be transmitted: %s" % (args.mode, exc))
        print()
        print("  this build's session has no mode writer: it sends one mode")
        print("  frame (mode %d) as part of its connect handshake and"
              % P.POST_CONNECT_MODE)
        print("  exposes no other.")
        return 1
    print("mode %d: %s" % (args.mode, _hexs(frame)))
    print()
    print("  this build's session has no mode writer: it sends one mode")
    print("  frame (mode %d) as part of its connect handshake and exposes no"
          % P.POST_CONNECT_MODE)
    print("  other. the frame above is shown, not sent.")
    return 0


async def cmd_gait(args):
    """Walk a list of stroke percentages, holding each for a fixed dwell."""
    levels = [float(x) for x in args.levels.split(",")]

    async def body(s):
        print("gait %s dwell=%ss pause=%ss" % (levels, args.dwell, args.pause))
        for i, pct in enumerate(levels):
            print("  [%d/%d] %.0f%%" % (i + 1, len(levels), pct))
            s.set_target(pct, args.vibration)
            await asyncio.sleep(args.dwell)
            if i + 1 < len(levels):
                await asyncio.sleep(args.pause)
        await s.release()
        print("done. frames=%d reconnects=%d recovery=%.2fs"
              % (s.stats.frames_sent, s.stats.reconnects,
                 s.stats.recovery_seconds))
        return 0
    return await _with_session(args, body)


async def cmd_ramp(args):
    """Sweep 0 -> peak -> 0."""
    async def body(s):
        async def leg(seconds, up):
            steps = max(1, int(seconds / 0.1))
            for i in range(steps + 1):
                pct = args.peak * (i / steps) if up else args.peak * (1 - i / steps)
                s.set_target(pct, args.vibration)
                await asyncio.sleep(seconds / steps)
            return

        print("ramp 0 -> %.0f%% -> 0  (up=%ss down=%ss hold=%ss)"
              % (args.peak, args.seconds, args.seconds, args.hold))
        await leg(args.seconds, True)
        await asyncio.sleep(args.hold)
        await leg(args.seconds, False)
        await s.release()
        print("done. frames=%d reconnects=%d recovery=%.2fs"
              % (s.stats.frames_sent, s.stats.reconnects,
                 s.stats.recovery_seconds))
        return 0
    return await _with_session(args, body)


async def cmd_stop(args):
    """Return to rest and close the link."""
    async def body(s):
        zero = P.app_a0_control(0)
        for _ in range(_STOP_FRAMES * 3):
            try:
                await s._write(zero, response=False)          # noqa: SLF001
            except Exception as exc:                          # noqa: BLE001
                print("write failed: %s" % exc)
                break
            await asyncio.sleep(0.02)
        await s.release()
        print("zero frame x%d: %s" % (_STOP_FRAMES * 3, _hexs(zero)))
        print("device retracts on its own within ~5 s")
        return 0
    return await _with_session(args, body)


def cmd_tui(args):
    """The prototype's TUI has no counterpart here.

    It drove the raw client and printed each notification as it arrived; this
    package routes notifications through PressureMonitor and has no interactive
    console over them. Rather than ship a console that looks like the one it
    replaced and does less, say what the equivalent is and exit non-zero so a
    script cannot mistake this for a working command.
    """
    print("tui: not available in this build.", file=sys.stderr)
    print("the prototype's console printed raw notifications through a raw")
    print("bleak client; here they go through PressureMonitor. Use `watch`")
    print("for a live view, or `vera web` for the interactive surface.")
    return 2


async def cmd_scan(args):
    """List nearby BLE devices, flagging the one we care about.

    Prints every advertiser with its name and signal strength, so a failed
    run says whether the radio is working and whether the device is on the
    air -- instead of just "no match".
    """
    from bleak import BleakScanner

    from .discovery import (_iter_pairs, _matches, _names_of,
                            DEVICE_NAME_PREFIXES, DEVICE_SERVICE_UUIDS)

    print("scanning %.0f s ..." % args.seconds)
    try:
        raw = await BleakScanner.discover(timeout=args.seconds,
                                          return_adv=True)
    except Exception as exc:                               # noqa: BLE001
        print("scan failed: %s" % exc)
        return 1

    rows = []
    for addr, dev, adv in _iter_pairs(raw):
        names = sorted(n for n in _names_of(dev, adv) if n)
        rssi = getattr(adv, "rssi", None) if adv else None
        is_ours = _matches(dev, adv, DEVICE_NAME_PREFIXES, DEVICE_SERVICE_UUIDS)
        rows.append((addr, names, rssi, is_ours))

    if not rows:
        print("  no BLE devices advertised at all.")
        print("  Check: adapter enabled, and the device powered on.")
        return 1

    for addr, names, rssi, ours in sorted(rows):
        mark = "<<< THIS DEVICE" if ours else ""
        label = ", ".join(names) if names else "(no name advertised)"
        strength = "%s dBm" % rssi if rssi is not None else "?"
        print("  %-18s  %-34s  %6s  %s"
              % (addr, label, strength, mark))

    print("%d device(s) seen, %d match this project"
          % (len(rows), sum(1 for r in rows if r[3])))
    if not any(r[3] for r in rows):
        print()
        print("  The device is NOT advertising. If it is powered on, it is")
        print("  almost certainly already connected to a phone: BLE allows")
        print("  only one central at a time. Force-quit the vendor app.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="vero",
        description="Control a Vero / Amorlink device over BLE")
    ap.add_argument("--target", default=None,
                    help="MAC address; omit to scan by name")
    sub = ap.add_subparsers(dest="cmd")

    m = sub.add_parser("move", help="hold one position")
    m.add_argument("--percent", type=float, default=50.0)
    m.add_argument("--vibration", type=int, default=0)
    m.add_argument("--seconds", type=float, default=30.0)

    s = sub.add_parser("sweep", help="move between two positions")
    s.add_argument("--min", type=float, default=10.0)
    s.add_argument("--max", type=float, default=70.0)
    s.add_argument("--vibration", type=int, default=0)
    s.add_argument("--traverse", type=float, default=4.0,
                   help="seconds for one way; a full cycle takes twice this")
    s.add_argument("--dwell", type=float, default=0.0,
                   help="pause at each end; 0 keeps the turnaround smooth")
    s.add_argument("--no-feedback", action="store_true",
                   help="do not correct against reported device position")
    s.add_argument("--seconds", type=float, default=60.0)

    i = sub.add_parser("info", help="read device identity and telemetry")
    f = sub.add_parser("frame", help="show the bytes for a level, no link")
    f.add_argument("--percent", type=float, default=50.0)
    f.add_argument("--vibration", type=int, default=0)

    sc = sub.add_parser("scan", help="look for the device")
    sc.add_argument("--seconds", type=float, default=15.0)
    sc.add_argument("--match", default="amorlink")

    # --- the prototype's eleven, kept at their original names and defaults ---
    # --- so the census above (`_old - _new` empty) is satisfied by real
    # --- commands rather than by a renamed alias.

    sub.add_parser("probe", help="connect and print device info")

    sub.add_parser("status", help="query state once")

    w = sub.add_parser("watch", help="stream notifications")
    w.add_argument("--seconds", type=float, default=0.0,
                   help="0 watches until interrupted")

    sub.add_parser("caps", help="print the capability query frame")

    fr = sub.add_parser("frames",
                        help="print control frames without connecting")
    fr.add_argument("--pos", type=int, default=2000,
                    help="position, 0-10000")
    fr.add_argument("--vib", type=int, default=1, help="vibration, 0-255")
    fr.add_argument("--level", type=int, default=50, help="A-gen level 0-100")

    md = sub.add_parser("mode", help="switch mode")
    md.add_argument("mode", type=int, help="52-56 (A-gen) or 0-255 (B-gen)")

    gt = sub.add_parser("gait",
                        help="hold a sequence of stroke percentages")
    gt.add_argument("levels", help="comma separated percentages, e.g. 20,50,100")
    gt.add_argument("--dwell", type=float, default=5.0, help="seconds per level")
    gt.add_argument("--pause", type=float, default=2.0, help="gap between levels")
    gt.add_argument("--vibration", type=int, default=0, help="vibration, 0-255")

    rp = sub.add_parser("ramp",
                        help="sweep 0 -> peak -> 0 with live streaming")
    rp.add_argument("--peak", type=float, default=100.0,
                    help="peak stroke percentage 0-100")
    rp.add_argument("--seconds", type=float, default=2.0,
                    help="seconds for each direction")
    rp.add_argument("--hold", type=float, default=1.0,
                    help="seconds to dwell at the peak")
    rp.add_argument("--vibration", type=int, default=0, help="vibration, 0-255")

    sub.add_parser("stop", help="zero and disconnect")
    sub.add_parser("tui", help="interactive console")

    args = ap.parse_args(argv)
    cmd = args.cmd or "info"

    if cmd in ("frame", "frames", "caps"):
        # No link needed for either frame printer; `caps` still opens one so a
        # failure to reach the device is reported as such.
        if cmd == "frame":
            return asyncio.run(cmd_frame(args))
        if cmd == "frames":
            return asyncio.run(cmd_frames(args))
        return asyncio.run(cmd_caps(args))
    if cmd == "tui":
        return cmd_tui(args)
    if cmd == "scan":
        args.target = None
        return asyncio.run(cmd_scan(args))
    if cmd == "probe":
        return asyncio.run(cmd_probe(args))
    if cmd == "status":
        return asyncio.run(cmd_status(args))
    if cmd == "watch":
        return asyncio.run(cmd_watch(args))
    if cmd == "mode":
        return asyncio.run(cmd_mode(args))
    if cmd == "gait":
        return asyncio.run(cmd_gait(args))
    if cmd == "ramp":
        return asyncio.run(cmd_ramp(args))
    if cmd == "stop":
        return asyncio.run(cmd_stop(args))
    if cmd == "move":
        return asyncio.run(cmd_move(args))
    if cmd == "sweep":
        return asyncio.run(cmd_sweep(args))
    return asyncio.run(cmd_info(args))


if __name__ == "__main__":
    sys.exit(main())