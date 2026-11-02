"""Locating the device, across bleak versions and both BLE stacks.

Two lessons are baked in here.

1. The advertised name is NOT stable. BlueZ exposes an alias
   ("Amorlinkvex"), but that alias is a local label -- it never appears on
   the air. What the scanner actually sees is the device's own serial,
   which the vendor app calls the SN. Matching on the alias therefore
   finds the device on one machine and nowhere else. Name matching is
   kept as a fast path, never as the only path.

2. bleak changed the shape of discover(return_adv=True): older versions
   return a dict keyed by BLEDevice, 1.0-2.x return a list of
   (BLEDevice, AdvertisementData) tuples, and the service_uuids field moved
   between AdvertisementData and the device. `_iter_pairs` normalises all
   of it so callers never branch on a version number.

Service-UUID matching is the reliable identifier: the device advertises
0x1812 (Human Interface Device) plus its two 128-bit custom services, and
those come from the device itself rather than from any local alias.

NO SPECIFIC DEVICE IS NAMED HERE
--------------------------------
This file used to carry a hardcoded tuple of ONE device: its serial, and
its BlueZ alias. That is wrong for a published project in two ways, and
the second one bites hardest:

  * It only ever matched THAT unit. Every other Vero has a different
    serial, so the fallback connect path found nothing for everyone else.
  * It wrote one person's device identifier into a public repository.

The vendor app itself names no device. It identifies a product by the
PREFIX of the serial (DeviceResolver.java:17-21 in app 1.2.3):

    Vero   ALVX, UMVX
    Mira  ALMI, UMMI
    Nexo  ALNX, UMNX
    Wyrh  WYRH

A serial is that prefix plus the unit's own number, so "UMVXEXAMPLE01"
is "UMVX" followed by the unit's own number. Matching on the PREFIX
therefore matches every unit of the family, and no unit's identifier is
ever written down.

So the names below are PREFIXES, matched case-insensitively, and they come
from vero/protocol.py's PRODUCTS table -- the same table the product
detection uses -- so the prefix that decides "this is a Vero" and the
prefix that decides "this is our device" cannot drift apart.

An operator can still narrow the search to one specific unit, by picking it
in the WebUI's scan list or by passing an address. The prefix matching is
only the FALLBACK for "connect to whatever of this family is in range".
"""

import asyncio

from .protocol import PRODUCTS

#: The serial-number PREFIX of each product line, uppercased.
#:
#: Taken from vero/protocol.py's PRODUCTS table, which is the one the
#: product detection uses. The values are uppercase and the comparison is
#: case-insensitive, because the on-air serial is mixed case
#: ("UMVXEXAMPLE01") while the table is written in the app's own form.
DEVICE_NAME_PREFIXES = tuple(
    prefix.upper()
    for prefixes in PRODUCTS.values()
    for prefix in prefixes
)

# Advertised by the device itself, so these are identical on every host.
#
# The 16-bit HID service (0x1812) is deliberately NOT used for matching,
# even though the device advertises it: HID is advertised by AirPods,
# gamepads and dozens of other peripherals, so matching on it would happily
# connect to the wrong gadget.
#
# THE TWO UUIDS ARE NOT EQUAL IN KIND, AND THAT USED TO MATTER.
#
# 6e400001-b5a3-f393-e0a9-e50e24dcca9e is the **Nordic UART Service**.
# It is not this vendor's: Nordic defines it, and it is what every
# Arduino-with-a-BLE-module, every Adafruit Bluefruit, every TI SensorTag,
# every HM-10 serial bridge and a few hundred other boards broadcast. The
# comment here used to call both UUIDs "vendor-specific", and that was
# simply wrong -- the repository's own docs already said so
# (docs/07-hardware.md: "Nordic UART"), and so did the vendor app's own
# source, which uses the same pair (utils/f.java:10,19).
#
# 6f430001-b5a3-f393-e0a9-e50e24dcca9e IS vendor-private. Nothing else
# on earth broadcasts it.
#
# So the two are split below: the private one is a positive identifier on
# its own, and the NUS one is NOT. See NUS_NEEDS_CORROBORATION.
DEVICE_SERVICE_UUIDS = (
    "6e400001-b5a3-f393-e0a9-e50e24dcca9e",
    "6f430001-b5a3-f393-e0a9-e50e24dcca9e",
)

#: The ONE service UUID that identifies this vendor's device by itself.
#:
#: A private 128-bit UUID is chosen by whoever registers it, so this one
#: says "this is one of ours" and nothing else says it.
VENDOR_SERVICE_UUID = "6f430001-b5a3-f393-e0a9-e50e24dcca9e"

#: The Nordic UART Service -- a PUBLIC standard, not a vendor identifier.
#:
#: Kept as its own name so the code says what it means at every use site.
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"

#: A NUS match ALONE is not enough to call a device ours.
#:
#: The device advertises BOTH services, and the private one is the one that
#: identifies it. But a host that cannot read the advertisement's service
#: list -- or a device whose firmware advertises only the NUS -- used to be
#: rescued by matching NUS alone. That rescue is a real hole: an HM-10
#: serial bridge or a TI SensorTag broadcasts the same NUS UUID, so the
#: scan offered them as "本厂设备" and the connect path would have written
#: motor-control frames into a stranger's BLE peripheral.
#:
#: So the NUS branch now needs CORROBORATION -- see _matches.
#:
#: The corroboration that is available without a working name is the HID
#: service 0x1812, which this device advertises (analysis/FIRMWARE-REPORT.md
#: "真机 GATT 布局" lists 00001801) and which no NUS-only board does.
NUS_NEEDS_CORROBORATION = True

#: The HID service the device advertises alongside its own two. Used ONLY
#: to corroborate a NUS match, never as an identifier on its own: HID is
#: advertised by AirPods, gamepads and dozens of other peripherals.
HID_SERVICE_UUID = "00001812-0000-1000-8000-00805f9b34fb"


def _iter_pairs(devs):
    """Yield (address, device, advertisement) across bleak return shapes.

    The shapes actually observed, not just the documented ones:

      * bleak 3.0.2   -> ``{address_str: (BLEDevice, AdvertisementData)}``
      * bleak 1.x-2.x -> ``[(BLEDevice, AdvertisementData), ...]``
      * some builds   -> ``{BLEDevice: AdvertisementData}``

    Getting this wrong fails silently and expensively: iterating a dict
    yields its KEYS, so a mismatch produces a screen of devices with no name,
    no address and no RSSI, and nothing ever matches. The address is yielded
    separately because in the first shape it lives only in the key.

    Yields ``(address, device_or_None, adv_or_None)``; either of the last two
    may be None, but never both.
    """
    if isinstance(devs, dict):
        for key, value in devs.items():
            if isinstance(value, tuple) and len(value) == 2 \
                    and hasattr(value[0], "address"):
                dev, adv = value
                yield getattr(dev, "address", key), dev, adv
            elif hasattr(value, "address"):
                yield key, None, value               # {dev: adv}
            else:
                yield key, None, value               # {addr: adv}
        return
    for item in devs or []:
        if isinstance(item, tuple) and len(item) == 2 \
                and hasattr(item[0], "address"):
            yield item[0].address, item[0], item[1]
        elif hasattr(item, "address"):
            yield item.address, item, None


def _names_of(dev, adv):
    out = set()
    for src in (getattr(dev, "name", None),
                getattr(adv, "local_name", None) if adv else None,
                getattr(dev, "details", None)):
        if isinstance(src, str) and src:
            out.add(src)
    return out


def _uuids_of(dev, adv):
    out = set()
    for src in (getattr(adv, "service_uuids", None) if adv else None,
                getattr(dev, "service_uuids", None)):
        for u in src or ():
            out.add(str(u).lower())
    return out


def _uuid_key(u):
    """Canonical comparison key for a BLE UUID string.

    BLE UUIDs appear in several forms and which one a stack hands you is not
    guaranteed: full 128-bit, 32-bit shorthand ("6e400001"), and 16-bit
    shorthand ("1812", "0x1812") all show up in the wild. Reduce them all to
    the full lowercase form so comparison is a plain set intersection.
    """
    s = str(u).strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if len(s) == 4:                       # 16-bit shorthand
        return "0000%s-0000-1000-8000-00805f9b34fb" % s
    if len(s) == 8:                       # 32-bit shorthand
        return "%s-0000-1000-8000-00805f9b34fb" % s
    return s


def _uuid_matches(seen, wanted):
    """True when any wanted UUID is present in the seen set.

    Full 128-bit UUIDs compare exactly. Short forms cannot be expanded:
    "6e400001" does not determine the remaining 96 bits, and a vendor UUID
    like 6e400001-b5a3-f393-... is NOT 6e400001-0000-1000-8000-00805f9b34fb.
    A short form can arrive on EITHER side -- the stack may report the
    advertisement UUID in short form while our table holds the full one --
    so the shorter of the two is matched as a leading prefix.
    """
    for want in wanted:
        raw_w = str(want).strip().lower().removeprefix("0x")
        for seen_u in seen:
            raw_s = str(seen_u).strip().lower().removeprefix("0x")
            if len(raw_w) == len(raw_s):
                if raw_w == raw_s:
                    return True
                continue
            short, long_ = (raw_w, raw_s) if len(raw_w) < len(raw_s) \
                else (raw_s, raw_w)
            if len(short) in (4, 8) and long_.startswith(short):
                return True
            if _uuid_key(raw_w) == _uuid_key(raw_s):
                return True
    return False


def _matches(dev, adv, names, services):
    """True when this advertiser is the device we want.

    Names are a fast path only. Service UUIDs come from the device itself,
    so they hold on any host regardless of local aliases.

    THE NAMES ARE PREFIXES, NOT WHOLE NAMES. Every unit of a product
    line advertises a serial that STARTS with that line's prefix
    ("UMVX" for a Vero), and the rest of the serial is the unit's own
    number. Comparing the whole name against a fixed string would only
    ever match one unit; comparing the PREFIX matches every unit of the
    family and never writes a unit's identifier down.

    The comparison is case-insensitive because the on-air serial is mixed
    case ("UMVXEXAMPLE01") while the prefix table is uppercase.

    A SERVICE UUID IS NOT A SINGLE KIND OF EVIDENCE, AND THE KIND
    DECIDES WHETHER IT IS ENOUGH ON ITS OWN.

    The vendor's private service (6f430001) is a positive identifier:
    nobody else on earth broadcasts it, so seeing it means "this is one
    of ours" and the answer is yes.

    The Nordic UART Service (6e400001) is a PUBLIC STANDARD. It is
    broadcast by every BLE serial bridge and every Nordic-based sensor
    board there is. Treating it as an identifier on its own is what let
    the scan offer an HM-10 and a TI SensorTag as "本厂设备", with the
    note saying the vendor service had confirmed it -- and the connect
    path would then have written motor-control frames into a stranger's
    peripheral.

    So a NUS match now needs CORROBORATION. The corroboration used here
    is the HID service 0x1812, which this device advertises alongside
    its own two (analysis/FIRMWARE-REPORT.md, "真机 GATT 布局") and
    which a bare NUS bridge does not. HID alone still never matches: it
    is advertised by AirPods and gamepads, so it is evidence, not an
    identifier.

    WHAT THIS COSTS. The rescue case this weakens is "a host that cannot
    read the advertisement's service list". On such a host the device
    would no longer be found by UUID alone. That is the right trade: the
    alternative was writing control frames into whatever answered, and
    a name is still a fast path that works on every host that can read
    one. The operator can also always name the device by address, which
    the connect path honours once the family test passes.
    """
    if names:
        prefixes = {str(n).upper() for n in names}
        for name in _names_of(dev, adv):
            up = name.upper()
            if up in prefixes:
                return True
            if any(up.startswith(p) for p in prefixes):
                return True
    if not services:
        return False
    seen = _uuids_of(dev, adv)
    # Which of the two services the CALLER is asking about. The set is a
    # parameter, not a constant, so the split is applied to it rather than
    # to the module's defaults: a caller that asks only about the NUS gets
    # NUS semantics, and one that asks about neither gets neither.
    asked = {str(s).lower() for s in services}
    # The private service, on its own, is enough.
    if VENDOR_SERVICE_UUID.lower() in asked \
            and _uuid_matches(seen, (VENDOR_SERVICE_UUID,)):
        return True
    if NUS_SERVICE_UUID.lower() not in asked:
        return False
    if not _uuid_matches(seen, (NUS_SERVICE_UUID,)):
        return False
    if not NUS_NEEDS_CORROBORATION:
        return True
    # A NUS match needs a second, independent piece of evidence that the
    # advertiser is this device and not some other board that happens to
    # speak the same public UART protocol.
    return _uuid_matches(seen, (HID_SERVICE_UUID,))


def is_ours(dev, adv, names=DEVICE_NAME_PREFIXES,
            services=DEVICE_SERVICE_UUIDS) -> bool:
    """Is this advertiser one of the devices this project drives?

    THE ONE ANSWER, FOR EVERY PATH. The scan list, the connect path
    and the CLI's scan all have to answer the same question -- "is
    this one of ours?" -- and they used to answer it three ways:

      * the CONNECT path matched the advertised name's PREFIX or the
        vendor service UUID;
      * the SCAN LIST looked only at the name, so a device that
        advertised the vendor UUID but no usable name was HIDDEN from
        the list -- while the connect path would have taken it.

    So on a host where the device's name is not usable -- BlueZ with
    a local alias, CoreBluetooth with an empty cached name, WinRT with
    no local name -- the device was connectable but INVISIBLE. The
    operator's only way in was to know the address and type it.

    The name is a fast path, never the only path: the vendor service
    UUIDs come from the device itself, so they hold on any host. That
    is why this function exists and why the scan list calls it.

    Returns a bool. Callers that need to know WHY use
    :func:`non_controllable_reason`.
    """
    return _matches(dev, adv, names, services)


def describe(addr, dev, adv, redact_address=True):
    """One-line description for humans and logs.

    THE ADDRESS IS REDACTED BY DEFAULT. This line goes into the error
    message the WebUI shows and the API returns, so an address in it
    reaches whoever can reach the API -- and the addresses in a scan are
    OTHER PEOPLE'S devices: the neighbours' headphones, the colleague's
    watch, the phone in the next room.

    A MAC is personal equipment even when it is not yours, so the
    summary says how many devices were seen, what they advertised
    themselves as, and how strong the signal was -- everything an
    operator needs to tell "the radio is off" from "the device is
    connected to a phone" -- and nothing that identifies a card.

    The redacted form keeps the OUI, which is the vendor's block and
    says who made the adapter, and blanks the device bytes.
    """
    names = sorted(n for n in _names_of(dev, adv) if n)
    _addr = addr or (getattr(dev, "address", None) or "?")
    if redact_address:
        _addr = _redact_address(_addr)
    return "%s  %s  rssi=%s" % (
        _addr,
        ", ".join(names) or "(no name advertised)",
        getattr(adv, "rssi", None) if adv else "?")


def _redact_address(addr):
    """A MAC with its device bytes blanked. Non-MACs pass through."""
    if not isinstance(addr, str):
        return "?"
    parts = addr.split(":")
    if len(parts) == 6 and all(len(p) == 2 for p in parts):
        return ":".join(parts[:3] + ["XX", "XX", "XX"])
    return addr


#: Human-readable summary of the most recent failed scan, for error messages.
#: A bare "not found" is close to useless: it looks identical whether the
#: radio is off, the device is connected to a phone, or the match is wrong.
_last_scan = []


def last_scan():
    """List of one-line descriptions seen during the last failed search."""
    return list(_last_scan)


async def scan(seconds=15.0):
    """Return every advertising device, WITH its advertisement data.

    Devices that only carry an address string (some stacks omit the object)
    are returned as a light stand-in so callers still get an address.

    Why the advertisement is kept
    -----------------------------
    The signal strength lives on the ADVERTISEMENT, not on the device:
    bleak's ``BLEDevice`` has ``__slots__ = ("address", "name", "details")``
    and no ``rssi`` field at all. So a scan that threw the advertisement
    away could not report a signal strength no matter what the caller did
    afterwards -- the number was already gone.

    That is why this returns ``(device, advertisement)`` pairs rather than
    bare devices. The previous shape returned only the device, which is
    what made "list the nearby devices with their signal strength" a
    feature the WebUI could not have.

    The shape is kept flat and per-version normalised by ``_iter_pairs``,
    so a caller that only wants the device can take ``pair[0]``.
    """
    from bleak import BleakScanner
    raw = await BleakScanner.discover(timeout=seconds, return_adv=True)
    seen, out = set(), []
    for addr, dev, adv in _iter_pairs(raw):
        if addr in seen:
            continue
        seen.add(addr)
        out.append((dev if dev is not None else _AddressOnly(addr), adv))
    return out


class _AddressOnly:
    """Stand-in for a device object a stack did not provide."""

    def __init__(self, address):
        self.address = address
        self.name = None


async def find_device(address=None, names=DEVICE_NAME_PREFIXES,
                       services=DEVICE_SERVICE_UUIDS, timeout=25.0):
    """Locate the device by address, else by name or service UUID.

    Returns a BLEDevice, or None. On failure `scan_seen` holds a human
    summary of what was actually advertising, because "not found" without
    that context is the least useful error message there is.

    AN ADDRESS IS A HINT, NOT A VERDICT.
    -------------------------------------
    The address path used to return whatever the stack had at that
    address, without asking whether the device there was one of ours.
    That is a hole with a real consequence: the scan list only ever
    offers devices this build can drive, but a caller that passes an
    address directly could point the whole control path at a
    HEADPHONE -- and the send path would then write control frames to
    it.

    So the address narrows the SEARCH and the family test still
    decides. A device at the given address whose advertised name or
    service UUIDs do not match is not returned: the search continues
    by name, and the caller gets the same "not found" it would get for
    a device that is simply absent.

    WHY THE SCAN, AND NOT ``find_device_by_address``. That helper
    returns the DEVICE and throws the ADVERTISEMENT away, so the family
    test would have nothing to look at -- and a device whose name is
    only known from its advertisement would look like a stranger. The
    scan keeps both, so the address and the family are checked against
    the same evidence the scan list uses. One scan, one decision, and
    the two paths cannot disagree about which devices are ours.
    """
    from bleak import BleakScanner

    raw = await BleakScanner.discover(timeout=timeout, return_adv=True)
    want = str(address).strip().lower() if address else None

    for _addr, dev, adv in _iter_pairs(raw):
        if not _matches(dev, adv, names, services):
            continue
        # The family test passed. If the caller named an address, the
        # device at THAT address is the one they meant.
        if want is None:
            return dev
        _real = getattr(dev, "address", None) or _addr or ""
        if str(_real).strip().lower() == want:
            return dev

    del _last_scan[:]
    for addr, dev, adv in _iter_pairs(raw):
        _last_scan.append(describe(addr, dev, adv))
    return None


async def find_device_by_address(address, timeout=10.0):
    """Find a device by its address, WITHOUT asking for an advertisement.

    WHY THIS EXISTS. `find_device` finds a device by SCANNING for it,
    and a scan can only see a device that is ADVERTISING. On Windows the
    OS holds BLE links of its own, so a device the OS is connected to
    does not advertise -- and that is exactly the state the operator is
    in after a disconnect/reconnect cycle:

        the operator clicks 断开, then 连接 again
        -> Windows' own Bluetooth panel shows the device CONNECTED
        -> the scan finds nothing, because the device is not advertising
        -> this page shows 未连接

    Both surfaces are telling the truth about DIFFERENT things: the OS
    is connected at the LINK level, and the page is not connected at
    the GATT level.

    THIS FUNCTION ASKS THE STACK FOR A KNOWN ADDRESS INSTEAD. On
    Windows that goes through WinRT's device cache, which knows a
    device the OS holds a link to, so it can answer for a device that is
    not advertising. On Linux it is the same advertising scan by
    address -- it costs one more scan and finds nothing more, so it is
    harmless there.

    IT IS NOT A SUBSTITUTE FOR THE FAMILY TEST. A device at that
    address is returned only if it is OURS: the name has to match the
    product-line prefixes. The point of the address is to NARROW THE
    SEARCH, never to skip the check -- otherwise a caller that passed
    an address could point the whole control path at a HEADPHONE.
    """
    want = str(address or "").strip().lower()
    if not want:
        return None
    from bleak import BleakScanner

    dev = await BleakScanner.find_device_by_address(str(address).strip(),
                                                     timeout=timeout)
    if dev is None:
        return None
    # THE FAMILY TEST, on the device the stack handed back. The
    # advertisement may be absent -- a quiet device has none -- so the
    # NAME is what decides.
    name = getattr(dev, "name", None)
    if not name:
        return None
    if not any(str(name).upper().startswith(p.upper())
               for p in DEVICE_NAME_PREFIXES):
        return None
    return dev
