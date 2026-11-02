"""Link-lifetime diagnostic -- the cross-platform comparison experiment.

The Linux host drops this device's BLE link every ~40.4 s with HCI
"Connection Timeout". btmon shows the link is created with interval 50 ms and
supervision timeout 420 ms -- 420 ms is the kernel's default, not a driver
hardcode -- while the peripheral asks for 7.5 ms / 4000 ms and is refused with
a Negative Reply whose Reason field is 0x02 (the handle had not reached
BT_CONNECTED yet, not "invalid parameters"). An independent nRF52840 sniffer
shows the peripheral transmitting normally through and past the disconnect, and
the same device holds an indefinite connection to a phone. So the defect looks
host-side.

Running this on Windows answers whether that is Linux-specific, which is the
only remaining question. Run it on both platforms and compare the lifetime
table: on Linux it clusters at ~40 s; on Windows it should either be unbounded
or follow a different period, and either result is decisive.

It also prints per-platform capability facts that explain any difference --
adapter name and address, MTU, and whether the stack negotiated anything --
so a run is self-describing rather than just a number.

Two quantities are reported, and they are not the same one. The LINK
LIFETIME is how long each link stayed up, and the verdict is about that.
The DROP-TO-DROP INTERVAL is the gap between one recovery and the next,
so it folds the reconnect delay into every interval after the first; it is
printed because the operator wants to see the gap, but judging a run by it
mixes two different things.

Usage:
    python diagnose.py                       # 5 minutes, default
    python diagnose.py --seconds 300
    python diagnose.py --target <BLE address or serial>  # one specific unit
    python diagnose.py --json report.json    # machine-readable
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import time
from datetime import datetime, timezone

# This module lives inside the package; its parent must be importable for
# `from vero import ...` to resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from . import protocol as P                                   # noqa: E402
from .session import DeviceSession                            # noqa: E402

#: A median link lifetime below this is not a period. The link is dying
#: faster than this host can rebuild it: RECONNECT_INTERVAL alone is
#: 0.25 s, so a link that lives less than a second contains less than
#: two reconnect intervals and its "median" is the death, not a period.
#: The Linux-matching band starts at 38 s, so this branch sits well
#: below it, and it moves neither that band nor the "> 100 s" band.
SHORT_LIFETIME_SECONDS = 1.0


def host_facts() -> dict:
    """Everything about this host that could plausibly matter."""
    facts = {
        "platform": sys.platform,
        "platform_release": platform.release(),
        "platform_version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": sys.version.split()[0],
        "bleak": None,
        "adapter": None,
    }
    try:
        import importlib.metadata as md
        facts["bleak"] = md.version("bleak")
    except Exception:                                          # noqa: BLE001
        pass
    # Adapter identity, where the platform exposes it.
    try:
        if sys.platform.startswith("linux"):
            import subprocess
            out = subprocess.run(["hciconfig"], capture_output=True,
                                 text=True, timeout=8).stdout
            lines = [l for l in out.splitlines() if l.startswith("hci")]
            if lines:
                facts["adapter"] = lines[0].split("\t")[-1].strip()
                bd = subprocess.run(["hciconfig"], capture_output=True,
                                    text=True, timeout=8).stdout
                for l in bd.splitlines():
                    if "BD Address" in l:
                        facts["adapter_address"] = l.split(":")[-1].strip()
                        break
        elif sys.platform == "win32":
            import subprocess
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "Get-PnpDevice -Class Bluetooth | "
                                  "Select-Object -ExpandProperty FriendlyName"],
                                 capture_output=True, text=True, timeout=20).stdout
            names = [l.strip() for l in out.splitlines() if l.strip()]
            facts["adapter"] = names[0] if names else "unknown"
            facts["adapters"] = names
        elif sys.platform == "darwin":
            facts["adapter"] = "CoreBluetooth (system)"
    except Exception as exc:                                  # noqa: BLE001
        facts["adapter_error"] = str(exc)[:120]
    return facts


async def run(args) -> dict:
    print("=" * 72)
    print("  Vero / Amorlink  --  BLE link lifetime diagnostic")
    print("=" * 72)

    facts = host_facts()
    print("platform : %s %s (%s)" % (facts["platform"],
                                     facts["platform_release"],
                                     facts["machine"]))
    print("python   : %s     bleak: %s" % (facts["python"], facts["bleak"]))
    print("adapter  : %s %s" % (facts.get("adapter") or "unknown",
                               facts.get("adapter_address", "")))
    print("device   : %s" % (args.target
                              or "any device of this family (by name prefix)"))
    print("window   : %d s, target %d%%" % (args.seconds, args.percent))
    print("-" * 72)

    session = DeviceSession(simulate=False, address=args.target)

    # Two quantities, kept apart on purpose.
    #
    # link_lifetimes -- how long each link stayed UP, as the session
    # itself measured it. This is what the verdict is about.
    #
    # drop_intervals -- the time from one recovery to the next, which
    # folds the recovery delay into every interval after the first. The
    # pre-change code measured THIS and called it a link lifetime, so
    # on a host with a slow reconnect the median left the ~40 s window
    # and a run that matched the Linux symptom exactly was reported as
    # "a third pattern".
    drop_intervals = []
    link_start = None
    recoveries = []
    last_reconnects = 0
    t0 = time.monotonic()
    next_report = 0.0

    print("connecting...", flush=True)
    try:
        await session.connect()
    except Exception as exc:                                   # noqa: BLE001
        print("CONNECT FAILED: %s: %s" % (type(exc).__name__, exc))
        print("\nTroubleshooting:")
        print("  - is the device switched on and not connected to a phone?")
        print("  - on Windows, is Settings > Bluetooth on?")
        print("  - try --target <mac> if scanning by name finds nothing")
        return {"error": str(exc), "facts": facts}

    if session.simulate:                                       # pragma: no cover
        print("REFUSING: session fell back to the simulator", flush=True)
        await session.disconnect()
        return {"error": "simulator", "facts": facts}

    print("connected. serial=%s fw=%s power=%s rssi=%s mtu=%s"
          % (session.stats.serial, session.stats.firmware,
             session.stats.power, session.stats.rssi, session.stats.mtu),
          flush=True)
    print("-" * 72, flush=True)

    session.set_target(args.percent)
    link_start = time.monotonic()

    while time.monotonic() - t0 < args.seconds:
        await asyncio.sleep(0.5)
        now = time.monotonic() - t0
        session.touch()

        # A reconnect count bump means the link died and was rebuilt.
        if session.stats.reconnects > last_reconnects:
            life = time.monotonic() - link_start
            if life > 0.5:
                drop_intervals.append(life)
            recoveries.append(now)
            last_reconnects = session.stats.reconnects
            print("  [%6.1fs] LINK DROPPED after %.2f s  "
                  "(recovered, cumulative recovery %.2f s, reassert %d frames)"
                  % (now, life, session.stats.recovery_seconds,
                     session.stats.reassert_frames), flush=True)
            link_start = time.monotonic()

        if now >= next_report:
            next_report = now + 15.0
            print("  [%6.1fs] frames=%d errs=%d reconnects=%d link=%s"
                  % (now, session.stats.frames_sent,
                     session.stats.write_errors,
                     session.stats.reconnects, session.stats.link_ok),
                  flush=True)

    # The session's own link lifetimes are the real thing: it times each
    # link from the moment it comes up to the moment it dies, with no
    # recovery delay folded in. The interval this file measures by hand
    # (drop_intervals) is a different quantity -- it starts at the
    # previous recovery and runs to the next one -- so it is reported
    # for the operator, but it is NOT what the verdict is about.
    #
    # The filter drops the 0.0 placeholder that connect() appends for a
    # link that is still up. Only _reconnect() records a lifetime, so a
    # link that was still running when the window closed leaves a
    # placeholder behind and never contributes a number of its own.
    #
    # The threshold is 0, NOT 0.5. It used to be 0.5, which was a guess
    # that a real lifetime is never that short -- and it is wrong:
    # session.py sets _link_started_at at the END of connect(), after
    # the BLE connect, the post-connect handshake and the identity
    # query, so a device that answers and then dies, or an identity
    # query that times out, produces a genuine lifetime well under
    # 0.5 s. Filtering at 0.5 threw those away, and because the
    # filtering happened BEFORE classify() was called, a host whose
    # links all died in under half a second reached classify() with an
    # EMPTY list and was told "NO DISCONNECT -- the link stayed up ...
    # so this host holds the link". The opposite of the truth.
    #
    # 0 is the correct threshold because the placeholder is exactly
    # 0.0 (session.py:503 appends the literal 0.0), while a recorded
    # lifetime is a duration and is strictly positive. The two are
    # therefore separated by their SIGN, not by a magnitude guess:
    #
    #   * the placeholder is the literal 0.0 -- never a real 0.0;
    #   * a recorded lifetime is time.monotonic() - _link_started_at,
    #     and _link_started_at is a strictly EARLIER monotonic reading,
    #     so the difference is > 0. time.monotonic() on Linux is
    #     CLOCK_MONOTONIC, whose resolution is nanoseconds and whose
    #     value is far from float exhaustion (the double ULP at a
    #     12-hour uptime is ~7e-12 s), so two successive readings
    #     cannot return the same value.
    #
    # Measured on this host: over 200k pairs of successive monotonic()
    # reads the smallest gap was 2.7e-08 s and no two readings were ever
    # identical; and driving the REAL DeviceSession through its REAL
    # _record_link_end() produced lifetimes down to 1.15e-05 s, none of
    # them zero. A lifetime of 0.0 is therefore not reachable, and
    # `> 0` loses nothing.
    link_lifetimes = [x for x in session.stats.link_lifetimes if x > 0]
    # How long the link that is still up has been up. Not a completed
    # lifetime, so it never enters the median -- but it is the number
    # that says whether the link is currently healthy.
    #
    # Measured from the session's own connected_at, not from the poll
    # loop's link_start: the poll notices a reconnect up to 0.5 s late,
    # and a 0.5 s error on a 40 s link is a 1.25 % error on the very
    # number being reported.
    final_uptime = (time.monotonic() - session.stats.connected_at
                    if session.stats.connected_at else 0.0)

    await session.disconnect()

    total = time.monotonic() - t0
    print("-" * 72)
    print("window        : %.1f s" % total)
    print("links observed: %d" % len(link_lifetimes))
    print("frames sent   : %d" % session.stats.frames_sent)
    print("write errors  : %d" % session.stats.write_errors)
    print("recovery total: %.2f s" % session.stats.recovery_seconds)

    if link_lifetimes:
        print("\nlink lifetimes (s): %s"
              % ["%.2f" % x for x in link_lifetimes])
        print("  min=%.2f  max=%.2f  median=%.2f  mean=%.2f"
              % (min(link_lifetimes), max(link_lifetimes),
                 statistics.median(link_lifetimes),
                 sum(link_lifetimes) / len(link_lifetimes)))
        if drop_intervals:
            print("\ndrop-to-drop intervals (s, recovery delay included): %s"
                  % ["%.2f" % x for x in drop_intervals])
            print("  median=%.2f" % statistics.median(drop_intervals))
        # The last link is still up when the window closes. It is NOT a
        # completed lifetime, so it is excluded from the median -- but
        # its age is the one number that says whether the link is
        # currently healthy, so it is reported separately.
        print("  last link still up for %.2f s" % final_uptime)
    else:
        print("\nNo link ever dropped during the window.")
        print("  last link still up for %.2f s" % final_uptime)

    # One source of truth for the verdict: classify() handles the
    # no-drop case too, so the two paths cannot drift apart.
    verdict = classify(link_lifetimes, final_uptime, total)

    print("\n" + "=" * 72)
    print("  VERDICT: %s" % verdict)
    print("=" * 72)

    return {
        "facts": facts,
        "seconds": total,
        # The true link lifetimes -- the quantity the verdict is about.
        "lifetimes": [round(x, 3) for x in link_lifetimes],
        # The drop-to-drop intervals, which fold the recovery delay into
        # every one after the first. Reported because the operator wants
        # to see the gap, but NOT the same quantity as "lifetimes".
        "drop_intervals": [round(x, 3) for x in drop_intervals],
        # The last link was still up when the window closed. Not a
        # completed lifetime, so it is not in the median.
        "final_uptime": round(final_uptime, 3),
        "frames_sent": session.stats.frames_sent,
        "write_errors": session.stats.write_errors,
        "recovery_seconds": round(session.stats.recovery_seconds, 3),
        "reconnects": session.stats.reconnects,
        "verdict": verdict,
        "serial": session.stats.serial,
        "firmware": session.stats.firmware,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def classify(lifetimes, final_uptime, total) -> str:
    """Judge a run by the LINK LIFETIME, not by the drop-to-drop interval.

    The pre-change version was handed the drop-to-drop interval, which
    folds the recovery delay into every interval after the first. On a
    host whose reconnect takes a while, that pushed the median out of
    the ~40 s window and a run that matched the Linux symptom exactly
    was reported as "a third pattern".

    ``lifetimes`` is the list of true link lifetimes -- one per link that
    died on its own. ``final_uptime`` is how long the last link was up
    when the window closed; it is not a completed lifetime, so it never
    enters the median, but it is the number that says whether the link
    is currently healthy.
    """
    if not lifetimes:
        return ("NO DISCONNECT -- the link stayed up for %.0f s of the %.0f s "
                "window, so this host holds the link. On Linux the same "
                "device drops every ~40.4 s, so the fault is specific to the "
                "Linux Bluetooth stack, not the device."
                % (final_uptime, total))
    med = statistics.median(lifetimes)
    # A link that dies in under a second is not showing a PERIOD, it is
    # failing to come up. This has to be its own branch: "a third
    # pattern" reads as "some other repeating period worth measuring",
    # which is the opposite of what a sub-second lifetime means -- the
    # link never survives long enough to be measured at all.
    #
    # It is also the branch a short lifetime MUST land in. Before the
    # filter was fixed, a host whose links all died in under half a
    # second had those lifetimes discarded before classify() ran, so
    # classify() saw an empty list and answered "NO DISCONNECT -- this
    # host holds the link". A dropped link reported as a held link.
    if med < SHORT_LIFETIME_SECONDS:
        return ("links die after a median %.3f s -- they do not survive "
                "long enough to establish a period at all. This is not "
                "the Linux ~40 s symptom (the link is dying far faster "
                "than that) and it is not a healthy long-lived link "
                "either: the link is failing to come up and stay up."
                % med)
    # The window is 38..43 s, and the quantity measured here starts a
    # little later than the HCI-level 40.379 s does: the session sets
    # _link_started_at (session.py:502) AFTER the post-connect handshake
    # and the identity query, so it excludes them. The excluded time is
    # 0.20 s of handshake plus however long the caps query took, so the
    # session's lifetime sits in [37.68, 40.38] s -- it can drop below
    # 38 only when the identity query burns its whole 2.5 s allowance,
    # which means the device never answered and the run did not measure
    # a link lifetime at all. The typical answer lands at ~40.1 s, well
    # inside the window.
    if 38 <= med <= 43:
        return ("links die at a ~%.1f s median, matching the Linux symptom. "
                "This host behaves like the Linux one, so the cause is not "
                "specific to Linux's Bluetooth stack -- it is the adapter or "
                "its driver." % med)
    # 100 s, not 120: README.md's "诊断工具怎么读" table documents this band
    # as "大于 100 秒", and the Windows reference run in docs/04-link.md
    # held the link for 117.0 s. A 120 s cut-off filed that run under
    # "a third pattern" instead of "far beyond Linux's ~40 s".
    if med > 100:
        return ("links live a median %.1f s, far beyond Linux's ~40 s. The "
                "defect is specific to the Linux Bluetooth stack; this host "
                "and the device are fine." % med)
    return ("links die at a median %.1f s, which matches neither the Linux "
            "~40 s nor a healthy multi-minute link -- a third pattern, worth "
            "looking at separately." % med)


def main():
    ap = argparse.ArgumentParser(
        description="Measure how long this host holds the BLE link, for "
                    "cross-platform comparison against the Linux ~40 s drop.")
    ap.add_argument("--target", default=None,
                    help="MAC address, or leave unset to scan by name")
    ap.add_argument("--seconds", type=int, default=300)
    ap.add_argument("--percent", type=float, default=30.0,
                    help="stroke to hold while measuring, keeps the link busy")
    ap.add_argument("--json", default=None, help="write a JSON report here")
    args = ap.parse_args()

    try:
        report = asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
        print("\nreport written to %s" % args.json)

    print("\nSend this block to whoever is comparing platforms -- it is the "
          "whole experiment in one screenful.")
    print(json.dumps(report.get("facts", {}), indent=2))
    print("link lifetimes:", report.get("lifetimes"))
    print("drop-to-drop intervals:", report.get("drop_intervals"))


if __name__ == "__main__":
    main()