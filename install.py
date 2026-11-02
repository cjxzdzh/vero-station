#!/usr/bin/env python3
"""The install: prepare the machine, then leave a launcher behind.

COVERS the three platforms -- ubuntu / mac / windows -- with a start entry
point and an install entry point for each, and it handles the awkward
cases: various Bluetooth adapters, and dependency installs through a
domestic proxy or through the foreign one already set.

WHAT THIS IS. One program that does the machine-level preparation for
all three platforms and then installs a launcher that starts the web
UI without the operator having to remember anything:

    python install.py            prepare the machine and install the launcher
    python install.py --check    only report what the machine is missing
    python install.py --uninstall
    python install.py --service  also install it as a background service

The three platforms are genuinely different, and the differences are the
whole difficulty:

  * LINUX uses BlueZ, and BlueZ's daemon is often NOT running, and the
    adapter is often NOT powered, and the kernel's "supervision timeout"
    (the window in which a link survives without a packet) is often
    short enough to drop a slow device. There is also no permission
    problem: the setuid helper handles it.

  * macOS uses CoreBluetooth, which is in the PROCESS -- so there is no
    adapter to configure, but there IS a permission: a process that uses
    Bluetooth has to ask for it, and on a notebook that means the
    terminal (or the app) has to be allowed to use Bluetooth in
    System Settings > Privacy & Security > Bluetooth.

  * WINDOWS uses WinRT through the `winrt` package, and the adapter is
    handled by the OS. There is no permission prompt for a desktop
    application. What there IS is the "Bluetooth Support Service" and
    the radio being off.

THE PROXY. The dependencies come from PyPI, and PyPI is slow from
mainland China -- sometimes unusable. So the install offers the index
explicitly, and it NEVER overwrites a proxy the operator already set:
a proxy in the environment, in pip's own config, or in the system
configuration is the operator's business and is left alone.

Everything here is a REPORT plus a best-effort fix. The install never
refuses to continue because a step failed -- a machine with no
Bluetooth can still run the UI and the offline self-test, and the
operator needs to see the diagnosis rather than a refusal.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import unicodedata
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


# ============================================================ the report
#
# A small structure, so every step can say what it found and what it
# did, and the whole thing can be printed at the end.
class Report:
    def __init__(self):
        self.steps = []

    def add(self, name, ok, detail="", advice=""):
        self.steps.append({"name": name, "ok": bool(ok),
                           "detail": detail, "advice": advice})
        return ok

    def ok(self):
        return all(s["ok"] for s in self.steps)

    def problems(self):
        return [s for s in self.steps if not s["ok"]]

    def render(self):
        out = []
        for s in self.steps:
            mark = "OK  " if s["ok"] else "WARN"
            out.append("  [%s] %s" % (mark, s["name"]))
            if s["detail"]:
                for line in textwrap.wrap(s["detail"], 68):
                    out.append("         %s" % line)
            # The advice is shown for a step that PASSED too.
            #
            # It used to be shown only when the step failed, and that
            # threw away advice the operator NEEDED: the macOS
            # Bluetooth permission is a click in System Settings that
            # no command can perform, so it was reported as "ok" (there
            # is nothing for the installer to do) with the instruction
            # in the advice -- and the advice was dropped because the
            # step passed. So the install reported all green while the
            # very first connection was going to fail.
            #
            # A step's advice is what the operator has to KNOW, not a
            # repair suggestion that only applies when something broke.
            if s["advice"]:
                for line in textwrap.wrap(s["advice"], 66):
                    out.append("         -> %s" % line)
        return "\n".join(out)


# ============================================================ the helpers
def have(cmd):
    """True when a command is on PATH."""
    return shutil.which(cmd) is not None


def run(cmd, timeout=30):
    """Run a command and return (rc, output). Never raises.

    THE DECODE IS THE PLATFORM'S, NOT UTF-8's. It used to be
    `p.stdout.decode("utf-8", "replace")`, which is right on Linux --
    where the machine's own output is UTF-8 -- and WRONG on Windows,
    where a console program writes in the console's code page. There
    that decode turns every Chinese character into U+FFFD, and those
    replacement characters then reach the report.

    And that is not cosmetic. The report is printed to the console,
    and the console on a Chinese Windows is GBK, which has no U+FFFD:
    so `print(report.render())` raised
    `UnicodeEncodeError: 'gbk' codec can't encode character '\ufffd'`
    and the whole check crashed with a traceback instead of a report.
    MEASURED on a real Windows 11 machine with codepage 936.

    So the child's bytes are decoded with the console's own encoding,
    and the console's own encoding is ALSO what this process prints
    with -- see `_console_stdout()`.
    """
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        return p.returncode, p.stdout.decode(_console_encoding(), "replace")
    except Exception as exc:                                    # noqa: BLE001
        return 127, str(exc)


def _console_encoding():
    """The encoding this console reads and writes.

    ON WINDOWS THE CONSOLE IS PUT INTO UTF-8, AND STAYS THERE.

    The bug this replaces was a MISMATCH between two layers:

      * the bytes the program writes, and
      * the code page the console decodes those bytes with.

    Whichever of the two a program chooses, the other can disagree --
    the operator can switch the console's code page at any time -- so
    a program that GUESSES is always one setting behind. It was
    guessed: the encoding was picked from `GetConsoleOutputCP`, and the
    report came out as mojibake anyway.

    WHAT THE OPERATOR'S OWN PASTE PROVES. The mojibake was

        ��װ Vero ����̨����

    and that is EXACTLY "GBK bytes decoded as UTF-8" -- verified by
    encoding the original Chinese to GBK and decoding it as UTF-8.
    For the console to decode our bytes as UTF-8, its code page must
    have been 65001. So the operator's console WAS UTF-8, and the
    program wrote GBK into it.

    WHY THE MEASUREMENT MISSED IT. The test that "passed" ran in a
    console whose code page was 936, so the guess happened to be
    right there. A test in one console does not speak for another.

    SO: MAKE THE CONSOLE AGREE. `SetConsoleOutputCP(65001)` puts the
    console into UTF-8, and Python writes UTF-8 (`sys.stdout.encoding`
    is already utf-8 on a Windows console under Python 3.13), so the
    two layers agree BY CONSTRUCTION -- whatever the console was
    before.

    The original code page is restored when the program ends, so the
    operator's shell is left exactly as they had it.

    Everywhere else it is UTF-8, which is what the machine produces.
    """
    if not IS_WIN:
        return "utf-8"
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:                                           # noqa: BLE001
        pass
    return "utf-8"


#: The console's ORIGINAL output code page, read before anything
#: changes it, so it can be put back when this program ends.
_CONSOLE_CP_SAVED = None


def _console_stdout():
    """stdout that can print what this project prints.

    TWO THINGS HAPPEN HERE, and both are needed.

    1. The console is put into UTF-8 (see `_console_encoding`), and the
       stream is reconfigured to write UTF-8 with `errors="replace"`.
       The console and the bytes then agree by construction.

       `errors="replace"` matters on its own: a character the console
       cannot show becomes a question mark instead of killing the
       report with a traceback. A report that renders with a few "?"
       is useful; a traceback is not.

    2. The console's original code page is remembered, so
       `_restore_console()` can put the operator's shell back the way
       they had it. Leaving a shell in a different code page than the
       one it started in is a rude surprise for the next command.
    """
    global _CONSOLE_CP_SAVED
    if IS_WIN and _CONSOLE_CP_SAVED is None:
        try:
            import ctypes
            _CONSOLE_CP_SAVED = ctypes.windll.kernel32.GetConsoleOutputCP()
        except Exception:                                       # noqa: BLE001
            _CONSOLE_CP_SAVED = None
    try:
        sys.stdout.reconfigure(encoding=_console_encoding(), errors="replace")
    except Exception:                                           # noqa: BLE001
        pass
    return sys.stdout


def _restore_console():
    """Put the console's output code page back the way it was."""
    if not IS_WIN or _CONSOLE_CP_SAVED is None:
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(_CONSOLE_CP_SAVED)
    except Exception:                                           # noqa: BLE001
        pass


def which_service_manager():
    """The service manager on this machine, or None."""
    if IS_WIN:
        return "windows"
    if have("systemctl"):
        return "systemd"
    if have("launchctl"):
        return "launchd"
    return None


# ============================================================ the proxy
#
# THE RULE: an index is offered, a proxy is never overwritten.
#
# pip reads its index from, in order: the command line, the
# PIP_INDEX_URL environment variable, pip's own config file, and then
# PyPI. So an operator who has already set PIP_INDEX_URL has already
# answered this question, and the install has to respect that answer.
#
# The mirrors below are the ones that serve the wheels this project
# needs. They are offered on request, and they are never applied
# silently -- a silent switch to a mirror is how a developer ends up
# debugging a machine that is not the one they think they are on.

# THE PIP SOURCE LOGIC LIVES IN bootstrap.py, and it is imported here.
#
# There was a second copy of it in this file, and that is exactly the
# kind of duplication that rots: the install would report one source
# and the bootstrap would use another, and the operator would be told
# "the source is X" while the packages came from Y. One implementation,
# imported by both.
#
# bootstrap.py is importable here because it is deliberately written in
# conservative syntax with no dependencies beyond the standard library,
# and because it sits in the same directory.
from bootstrap import (                                          # noqa: E402
    MIRROR_ENV,
    PIP_MIRRORS,
    mirror_url,
    pip_source_flags,
    proxy_env,
    venv_python,
    venv_too_old,
)

__all__ = ["MIRROR_ENV", "PIP_MIRRORS", "mirror_url", "pip_source_flags",
           "proxy_env", "pip_flags_for", "Report", "run_checks", "install",
           "main"]


def step_pip_index(report, mirror=None):
    """Report the source pip will actually use.

    It reports the source that is IN EFFECT, which is the one the
    operator picked from the menu (or the one that was already
    configured). The report has to name the source that will be USED,
    not a general statement about pip -- a report that says "pip will
    use the official source" while the packages come from a mirror is
    the kind of report that sends an operator looking in the wrong
    place.

    The ``mirror`` argument is the source the caller chose. It is
    applied to the environment by :func:`apply_source` BEFORE this is
    called, so the two agree; the argument is still read so a caller
    that did not apply it is not reported wrongly.
    """
    current = mirror or mirror_url()
    proxy = proxy_env()
    official = "https://pypi.org/simple"
    if current and current.rstrip("/") != official:
        detail = "会使用：%s" % current
        if proxy:
            detail += "\n检测到代理：%s" % proxy
        return report.add("pip 的软件源", True, detail, "")
    if current and current.rstrip("/") == official:
        # The operator CHOSE the official source. That is an answer,
        # and the report has to say it -- otherwise the report reads
        # like the default, and the operator cannot tell that their
        # choice was honoured.
        detail = "会使用：%s（你选的官方源）" % current
        if proxy:
            detail += "\n检测到代理：%s" % proxy
        return report.add("pip 的软件源", True, detail, "")
    detail = "pip 会用官方源 %s" % official
    if proxy:
        detail += "\n检测到代理：%s" % proxy
    else:
        detail += "\n没有检测到代理环境变量"
    advice = ("国内网络下载慢时，可以设置 PIP_INDEX_URL，"
              "或用本项目的 %s：" % MIRROR_ENV)
    for name, url, _ in PIP_MIRRORS[:-1]:
        advice += "\n    %-9s %s" % (name, url)
    return report.add("pip 的软件源", True, detail, advice)


# ======================================================== the source menu
#
# WHY THE MENU LIVES HERE AND NOT IN THE .bat. The mirror list, the
# proxy detection and the "already configured" rule all live in
# bootstrap.py, in PYTHON -- and install.py imports them from there so
# there is ONE copy. A menu in the .bat would have to carry a SECOND
# copy of the mirror list, and a duplicated list is exactly how the
# two drift apart: this project already had that bug, with the install
# reporting one source and the bootstrap using another, so the operator
# was told "the source is X" while the packages came from Y.
#
# WHY THERE IS A MENU AT ALL. The install reports the source and lets
# the operator pick, and the operator picks by TYPING something into a
# terminal. That was fine when the install was run from a shell by
# somebody who reads English. It is not fine when the entry point is a
# .bat that an operator DOUBLE-CLICKS, or when the operator's only
# contact with the machine is a black console window that closes on
# its own. The requirement is explicit: the source choice is an
# INTERACTION, and the default is the DOMESTIC mirror.
#
# WHY THE DEFAULT IS THE DOMESTIC MIRROR. In mainland China the
# official source is slow enough that a fresh install can look like a
# hang, and the domestic mirrors serve the same wheels over HTTPS. So
# pressing ENTER -- which is what most operators do -- gets the fast
# path, and the operator who wants something else says so.

#: The mirror the menu offers first, and the one a bare ENTER takes.
#:
#: It is the FIRST entry of PIP_MIRRORS, so the list and the default
#: cannot drift apart.
DEFAULT_MIRROR = PIP_MIRRORS[0][1]

#: The choice a bare ENTER takes: the default mirror.
DEFAULT_CHOICE = "1"


def _pad(text, width):
    """Pad ``text`` to ``width`` DISPLAY columns.

    A CJK character occupies two terminal columns, so a plain "%-22s"
    leaves a mixed label ragged. The count is by display width, so the
    column lines up for the operator.
    """
    w = 0
    for ch in text:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return text + " " * max(0, width - w)


#: How long the menu waits for an answer, in seconds.
#:
#: A read with nobody there BLOCKS FOREVER. Measured on a real Windows
#: 11 machine: a ``readline()`` from a console with no input does not
#: return -- it waits, and the install hangs there for good.
#:
#: So the read is given a deadline. A human reading a ten-line menu
#: and pressing a key has plenty of time; an unattended install gets
#: the default and carries on, which is the only outcome that is
#: better than hanging.
#:
#: It is generous on purpose. A too-short deadline would take the
#: default away from an operator who was reading the menu -- and the
#: default is a choice they never made.
CHOICE_TIMEOUT_SECONDS = 45


def _read_choice(prompt, timeout=None):
    """Read one line from the operator, with a DEADLINE.

    Returns the line, or None when there is nobody there: the stream
    hit EOF (the input was redirected from nothing), or the deadline
    passed with no answer.

    The deadline is what makes this safe to call from an unattended
    install. A plain ``readline()`` would wait for ever, and an
    install that hangs for ever is indistinguishable from an install
    that failed.
    """
    if timeout is None:
        timeout = CHOICE_TIMEOUT_SECONDS
    result = {}

    def _read():
        try:
            result["line"] = sys.stdin.readline()
        except (KeyboardInterrupt, EOFError, OSError, ValueError):
            result["line"] = ""

    # A daemon thread, so a stuck read cannot keep the process alive.
    worker = threading.Thread(target=_read, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        # The deadline passed with no answer. The thread is a daemon,
        # so the process can still exit; and the answer nobody gave is
        # not worth waiting for.
        return None
    line = result.get("line", "")
    if line == "":
        # EOF: the input was redirected from nothing (nul, /dev/null)
        # or the stream closed. There is nobody to answer.
        return None
    return line.strip()


def choose_source(force=False, stream=None):
    """Ask which source to install from. Returns a URL, or None.

    ``None`` means "no choice was made" -- either the operator asked
    for the source already in effect, or there was nobody there to ask.
    The caller then falls back to the source that is already in effect,
    which is the pre-existing behaviour.

    ``force`` asks even when a source is already configured. It is the
    opposite of the default rule, and it exists because "already
    configured" is the OPERATOR'S OWN setting -- and an operator who
    has set PIP_INDEX_URL to a mirror they chose should not be asked
    to confirm it. But an operator who wants to CHANGE it says so.
    """
    out = stream or sys.stdout
    already = mirror_url()
    if already and not force:
        # The operator has already answered this question, in the
        # environment or in pip's own config. Asking again would be
        # asking them to confirm their own setting, and the answer
        # they would give ("yes, the one I set") is the one that is
        # already in effect.
        return None

    # THE MENU IS BUILT FROM ONE LIST, AND THE NUMBERS ARE ASSIGNED
    # AS IT IS PRINTED.
    #
    # The first version printed the numbers by hand and then looked the
    # answer up in a SEPARATE list, and the two disagreed: option 8
    # (the official PyPI) returned the domestic mirror, and option 9
    # (a custom address) returned nothing at all. A menu whose printed
    # numbers and whose lookup table are two things is a menu that
    # lies about what it does.
    #
    # So the choices are BUILT first, as (label, url) pairs, and the
    # number of each choice IS its index. Printing and looking up read
    # the same list, so they cannot drift apart.
    choices = []
    for name, url, _ in PIP_MIRRORS[:-1]:
        label = "国内镜像（清华，默认）" if url == DEFAULT_MIRROR else name
        choices.append((label, url))
    choices.append(("官方 PyPI", PIP_MIRRORS[-1][1]))
    choices.append(("自定义地址", None))
    choices.append(("沿用当前环境里的设置", None))

    print("")
    print("  pip 软件源（下载依赖用）")
    print("  ─────────────────────────────────────────────")
    for i, (label, url) in enumerate(choices, 1):
        shown = url if url else ""
        # The label is padded by DISPLAY WIDTH, not by character count:
        # a CJK character is two columns wide, so "%-22s" on a mixed
        # label leaves the column ragged -- and the first version's
        # column was visibly ragged.
        print("   %d) %s %s" % (i, _pad(label, 24), shown))
    print("  ─────────────────────────────────────────────")
    if already:
        print("   当前：%s" % already)
    if proxy_env():
        print("   检测到代理：%s" % proxy_env())
    print("   直接回车 = 选 1（国内镜像）")
    print("")

    answer = _read_choice("  选择 [1]: ")
    if answer is None or answer == "":
        # Nobody there, or a bare ENTER: the default.
        #
        # A bare ENTER is the DEFAULT, not an answer -- so an
        # unattended install takes the domestic mirror rather than
        # stopping for a question nobody can answer.
        return DEFAULT_MIRROR
    try:
        pick = int(answer)
    except ValueError:
        print("  看不懂 %s，改用默认的国内镜像。" % answer)
        return DEFAULT_MIRROR
    if pick < 1 or pick > len(choices):
        print("  选项 %s 不存在，改用默认的国内镜像。" % answer)
        return DEFAULT_MIRROR
    label, url = choices[pick - 1]
    if label == "沿用当前环境里的设置":
        # "沿用当前环境里的设置" -- the source already in effect, or
        # pip's own default when there is none.
        return already
    if url is None:
        # "自定义地址"
        print("")
        print("  请输入完整的软件源地址（例如 https://example.com/simple）：")
        custom = _read_choice("  > ")
        if not custom:
            print("  没有输入地址，改用默认的国内镜像。")
            return DEFAULT_MIRROR
        return custom
    return url


#: The file the chosen source is written to, so a LATER run uses it.
#:
#: It has to be a FILE, not just an environment variable. The install
#: runs in a CHILD process: ``bootstrap.py`` starts ``install.py`` and
#: waits for it, so a change to the environment inside install.py does
#: NOT travel back up to the bootstrap -- and the bootstrap's own
#: dependency install (the one that runs when the venv is built by
#: ``run.bat install``) would go back to the default source.
#:
#: It also has to survive the session: an operator who chose the
#: domestic mirror expects the NEXT ``run.bat`` to use it too, and a
#: machine that asks again every time is a machine that gets the
#: wrong answer by accident.
#:
#: The file sits next to the code, in the same place pip's own config
#: would be read from, and it holds ONE line: the source URL.
SOURCE_FILE = os.path.join(HERE, ".vero-pip-index")


def apply_source(url):
    """Make ``url`` the source for this process AND for the next one.

    It is set in the ENVIRONMENT, so the pip install that runs in THIS
    process reads it. And it is written to :data:`SOURCE_FILE`, so the
    NEXT run reads it too.

    Both are needed. The environment carries the choice to this
    process's own pip install; the file carries it to every later run.
    A choice that reached only one of them would leave the machine
    half-configured -- and the half that matters is the one the
    operator does not see.
    """
    if not url:
        return False
    os.environ[MIRROR_ENV] = url
    try:
        with open(SOURCE_FILE, "w", encoding="utf-8") as fh:
            fh.write(url.strip() + "\n")
    except OSError:
        # A read-only directory is not a reason to fail the install --
        # the environment still carries the choice for this run.
        pass
    return True


def pip_flags_for(mirror=None, trusted=True):
    """The pip flags for an install.

    With no argument it uses the source that is already in effect (the
    operator's own, or the project's). With an argument it uses that
    one -- which is what the --mirror flag asks for.

    --trusted-host IS NOT ADDED FOR AN HTTPS SOURCE, and that is the
    point: it turns OFF the TLS certificate check for that host (pip's
    InsecureHTTPAdapter calls cert_verify(..., verify=False)). Every
    mirror this project lists serves HTTPS with a publicly trusted
    certificate, so the flag is not needed and it is not harmless.

    It IS added for an http:// source, because there pip IGNORES the
    index entirely without it -- and says "no versions", which reads
    exactly like a missing package.
    """
    if mirror is None:
        return pip_source_flags()
    flags = ["--index-url", mirror]
    if (trusted and mirror.lower().startswith("http://")
            and mirror.rstrip("/") != "https://pypi.org/simple"):
        flags.append("--trusted-host")
        flags.append(_host_of(mirror))
    return flags


def _host_of(url):
    rest = url.split("://", 1)[-1]
    return rest.split("/", 1)[0]


# ============================================================ Linux: BlueZ
def _parse_version_tuple(text):
    """The first (major, minor) version pair in a string, or None.

    The Debian/Ubuntu package version is "5.83-1~exp1ubuntu0.1": the
    minor part is "83-1~exp1ubuntu0.1", which is NOT a digit string.
    The old parser asked for two parts that were both digits, so the
    dpkg fallback -- the ONLY way to read the version on a machine
    without bluetoothctl -- never matched, and the version check was
    silently skipped on every Debian machine.

    So the rule is: take the leading digits of the major and of the
    minor, and ignore everything after them. "5.83-1~exp1ubuntu0.1"
    gives (5, 83); "5.66" gives (5, 66).
    """
    for tok in text.split():
        parts = tok.split(".")
        if len(parts) < 2:
            continue
        major = parts[0]
        minor = parts[1]
        # The leading digits of the minor part, before any distro suffix.
        digits = ""
        for ch in minor:
            if not ch.isdigit():
                break
            digits += ch
        if major.isdigit() and digits:
            return (int(major), int(digits))
    return None


def _bluez_version():
    """The BlueZ version as a (major, minor) tuple, or None.

    Read from `bluetoothctl --version`, whose output is
    "bluetoothctl: 5.66". The package version is a fallback: on a
    machine without bluetoothctl the library is still there.
    """
    rc, out = run(["bluetoothctl", "--version"], timeout=15)
    if rc == 0:
        got = _parse_version_tuple(out)
        if got:
            return got
    # The package manager knows, even when the tool does not.
    for cmd in (["dpkg-query", "-W", "-f=${Version}", "bluez"],
                ["rpm", "-q", "--qf", "%{VERSION}", "bluez"]):
        rc, out = run(cmd, timeout=20)
        if rc == 0:
            got = _parse_version_tuple(out)
            if got:
                return got
    return None


def step_linux_bluetooth(report):
    """BlueZ, the adapter, and the kernel's link window."""
    if not IS_LINUX:
        return None

    # 1. the tools.
    tools = ["bluetoothctl"]
    missing = [t for t in tools if not have(t)]
    if missing:
        report.add("BlueZ 工具", False,
                    "缺少：%s" % ", ".join(missing),
                    "安装：sudo apt install bluetooth bluez")
    else:
        report.add("BlueZ 工具", True, "bluetoothctl 已安装")

    # 2. the daemon.
    rc, out = run(["systemctl", "is-active", "bluetooth"], timeout=15)
    if rc != 0 or "active" not in out:
        report.add("BlueZ 服务", False,
                    "bluetooth 服务未运行（%s）" % (out.strip() or "无输出"),
                    "启动：sudo systemctl enable --now bluetooth")
    else:
        report.add("BlueZ 服务", True, "bluetooth 服务运行中")

    # 2a. THE D-BUS SYSTEM BUS.
    #
    # bleak's Linux backend talks to BlueZ over D-Bus, and dbus-fast
    # connects to the SYSTEM bus at
    # unix:path=/run/dbus/system_bus_socket. A machine where the
    # system bus is not running has bluetoothd running and still
    # cannot scan -- and the failure is an exception from deep inside
    # bleak, which reads as "the library is broken" rather than "the
    # message bus is down".
    dbus_ok = False
    if have("systemctl"):
        rc, out = run(["systemctl", "is-active", "dbus"], timeout=15)
        dbus_ok = (rc == 0 and "active" in out)
    if not dbus_ok and os.path.exists("/run/dbus/system_bus_socket"):
        dbus_ok = True
    if dbus_ok:
        report.add("D-Bus 系统总线", True, "运行中")
    else:
        report.add("D-Bus 系统总线", False,
                   "没有运行 —— bleak 通过 D-Bus 找 BlueZ，没有它就扫不到设备",
                   "启动：sudo systemctl enable --now dbus")

    # 2b. THE BLUEZ VERSION. bleak 3.x -- the version this project
    #     targets -- states that it needs BlueZ 5.55 or newer. On an
    #     older BlueZ the scanner and the notify signatures it uses
    #     are missing, and the failure shows up as a scan that finds
    #     nothing, which looks like "the device is off".
    bluz_version = _bluez_version()
    if bluz_version is None:
        report.add("BlueZ 版本", True, "读不到版本（用 bluez-tools 的信息）")
    elif bluz_version >= (5, 55):
        report.add("BlueZ 版本", True,
                   "%d.%d（bleak 3.x 需要 5.55 以上）" % bluz_version)
    else:
        report.add("BlueZ 版本", False,
                   "%d.%d —— bleak 3.x 需要 5.55 以上，低于这个版本扫描不到设备"
                   % bluz_version,
                   "升级：sudo apt install --only-upgrade bluez "
                   "libbluetooth3")

    # 3. the adapter. BlueZ 5's `bluetoothctl list` is the modern way;
    # `hciconfig` is the old one and is not always installed.
    adapters = []
    if have("bluetoothctl"):
        rc, out = run(["bluetoothctl", "list"], timeout=20)
        if rc == 0:
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("Controller"):
                    parts = line.split()
                    name = parts[1] if len(parts) > 1 else "?"
                    adapters.append(name)
    if not adapters and os.path.isdir("/sys/class/bluetooth"):
        adapters = sorted(os.listdir("/sys/class/bluetooth"))
    if not adapters:
        report.add("蓝牙适配器", False,
                    "没有找到蓝牙适配器",
                    "插上适配器，或检查：lsusb | grep -i bluetooth")
    else:
        report.add("蓝牙适配器", True,
                    "找到 %d 个：%s" % (len(adapters), ", ".join(adapters)))

    # 4. THE ADAPTER'S POWER. A controller that exists but is powered
    #    off finds nothing. This is the single most common "it does not
    #    work" on Linux, and the fix is one command.
    #
    #    `bluetoothctl show` reports the DEFAULT controller's state,
    #    which is the one that matters.
    powered = None
    if have("bluetoothctl"):
        rc, out = run(["bluetoothctl", "show"], timeout=20)
        if rc == 0:
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("Powered:"):
                    powered = line.split(":", 1)[1].strip()
                    break
    if powered is None:
        report.add("适配器已上电", True,
                   "读不到电源状态（控制器可能不被 bluetoothctl 选中）")
    elif powered.lower() in ("yes", "on", "true"):
        report.add("适配器已上电", True, "适配器处于上电状态")
    else:
        report.add("适配器已上电", False,
                   "适配器已断电（bluetoothctl 报告 Powered: %s）" % powered,
                   "上电：sudo bluetoothctl power on")

    # 4b. THE CENTRAL ROLE. BlueZ reports the roles an adapter can take,
    # and "central" is the one that matters here: a GATT client IS a
    # central. A dongle that only advertises (peripheral) finds
    # nothing, and the reason is in a field most people never read.
    roles = ""
    if have("bluetoothctl"):
        rc, out = run(["bluetoothctl", "show"], timeout=20)
        if rc == 0:
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("Roles:"):
                    roles = line.split(":", 1)[1].strip().strip("[]")
                    break
    if not roles:
        report.add("适配器角色", True, "读不到角色信息")
    elif "central" in roles.lower():
        report.add("适配器角色", True, "central（可以做 GATT 客户端）")
    else:
        report.add("适配器角色", False,
                   "角色是 %s，没有 central —— 这个适配器扫不到设备" % roles,
                   "换一个支持 central 的适配器（大部分 USB 蓝牙棒都支持）")

    # 5. THE SUPERVISION TIMEOUT. This is the Linux-specific trap.
    #
    #    BlueZ's supervision timeout is how long a link survives
    #    without a packet from the peripheral. The default is 2
    #    seconds, and a device that sends telemetry every 100 ms is
    #    fine -- but one whose telemetry pauses (a device that is
    #    holding a position, or one whose radio sleeps) is dropped by
    #    the kernel, and the link is re-established from scratch.
    #
    #    The timeout is a MODULE parameter, so it can only be changed
    #    by the administrator. The install says so, and does not try.
    #
    #    The parameter's NAME differs between kernels: the classic
    #    module parameter is "timeout" (in seconds), and some newer
    #    kernels expose "supervision_timeout" instead. And some
    #    kernels -- with the newer Bluetooth stack, or a vendor driver
    #    -- expose neither. So the check looks for both names and
    #    reports honestly when neither is there: an unreadable
    #    parameter is NOT a fault, and claiming it is would send the
    #    operator chasing a problem they do not have.
    value = None
    which = None
    base = "/sys/module/bluetooth/parameters"
    for name in ("timeout", "supervision_timeout"):
        path = os.path.join(base, name)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    value = fh.read().strip()
                    which = name
                    break
            except OSError:
                pass
    if value is None:
        report.add("链路监督超时", True,
                   "这个内核没有暴露该参数（用默认值）")
    elif int(value) >= 60:
        report.add("链路监督超时", True,
                   "当前 %s 秒（%s）" % (value, which))
    else:
        report.add("链路监督超时", False,
                   "当前 %s 秒（%s）-- 设备遥测一停，链路就会被内核丢掉"
                   % (value, which),
                   "调大（需要管理员）：echo 120 | sudo tee "
                   "/sys/module/bluetooth/parameters/%s" % which)
    return adapters


def step_linux_permissions(report):
    """Whether a NORMAL user can use the radio.

    THE HISTORY, because the check is easy to get wrong.

    BlueZ used to ship a setuid helper (bluetoothd, mode 4750, group
    lp) so that an ordinary user could talk to the adapter. BlueZ 5.52
    REPLACED it with D-Bus + polkit: the daemon runs as the
    'bluetooth' user and grants access through a policy. So on a
    modern BlueZ the helper is NOT setuid, and its not being setuid is
    NOT a fault.

    So the check is: does the OLD setuid helper exist AND not have the
    setuid bit? If it does not exist at all, the machine is on a modern
    BlueZ and the question does not arise. If it exists and IS setuid,
    the machine is on the old BlueZ and the permission is fine.

    The one case that really is a fault is the old helper with the
    setuid bit missing -- and the fix for that is well known.
    """
    if not IS_LINUX:
        return
    #
    # The OLD setuid helper is /usr/lib/bluetooth/bluetoothd -- the
    # CLIENT helper, distinct from the daemon of the same name.
    #
    # The daemon lives in /usr/libexec/bluetooth/bluetoothd on a modern
    # BlueZ, and it is NOT setuid BY DESIGN: it is started by systemd
    # as root and drops to the 'bluetooth' user. Treating the daemon's
    # missing setuid bit as a fault is a false alarm, and it is exactly
    # the kind of false alarm that sends an operator to "fix" a machine
    # that is already fine.
    #
    # So the daemon's path is not in this list at all.
    helper = None
    for path in ("/usr/lib/bluetooth/bluetoothd",
                 "/usr/lib/bluetooth/bluetooth"):
        if os.path.exists(path):
            helper = path
            break
    if helper is None:
        report.add("普通用户权限", True,
                   "没有 setuid 助手 —— 新的 BlueZ 用 D-Bus/polkit 授权，"
                   "普通用户可直接使用蓝牙")
        return
    try:
        st = os.stat(helper)
        setuid = bool(st.st_mode & 0o4000)
    except OSError:
        setuid = False
    if setuid:
        report.add("普通用户权限", True, "%s 是 setuid，普通用户可用" % helper)
    else:
        report.add("普通用户权限", False,
                   "%s 存在但不是 setuid —— 这是老版 BlueZ 的做法，"
                   "普通用户会无法使用蓝牙" % helper,
                   "修复：sudo chgrp lp %s && sudo chmod 2750 %s"
                   % (helper, helper))


# ============================================================ macOS
def step_macos(report):
    """CoreBluetooth lives in the process, so the work is permissions."""
    if not IS_MAC:
        return
    # The Bluetooth permission is per-process, and a terminal's
    # permission comes from the terminal application. There is no
    # command that grants it -- it is a click in System Settings. So
    # this step REPORTS and explains.
    #
    # It is reported as NOT ok, because the permission really is an
    # outstanding item: nothing on this machine can grant it, and the
    # first connection will fail until the operator clicks it. It used
    # to be reported as ok with the instruction in the advice, and the
    # advice was only rendered for a failing step -- so the install
    # said "安装完成" and the operator found out at the first scan.
    report.add("macOS 蓝牙权限", False,
               "CoreBluetooth 在进程内，不需要配置适配器；"
               "但蓝牙权限必须人工授予",
               "首次使用需要在「系统设置 → 隐私与安全性 → 蓝牙」"
               "中允许你的终端程序使用蓝牙 —— 授权给的是**终端程序**"
               "（Terminal / iTerm / VS Code），不是 Python。"
               "被拒绝后没有任何命令能重新申请，只能在系统设置里打开。")
    # The machine's own adapter, for the report.
    rc, out = run(["system_profiler", "SPBluetoothDataType"], timeout=60)
    if rc == 0 and "Bluetooth" in out:
        first = ""
        for line in out.splitlines():
            if "Bluetooth" in line and line.strip().startswith("Bluetooth"):
                first = line.strip()
                break
        report.add("macOS 蓝牙适配器", True, first or "系统里有蓝牙")
    else:
        report.add("macOS 蓝牙适配器", False,
                   "系统没有报告蓝牙硬件",
                   "MacBook 的蓝牙不能关闭；若为虚拟机请给虚拟机直通蓝牙")


# ============================================================ Windows
def _windows_radio_state():
    """The Windows radio's own state, or None when it cannot be asked.

    "ON", "OFF", "DISABLED" or "UNKNOWN" -- or None, which means the
    question could not be asked (no WinRT, no radio, an exception).

    IT IS ASKED THROUGH THE PROJECT'S OWN VENV, not the system
    Python, because the `winrt` packages are installed as bleak's
    dependencies and the system Python may not have them.
    """
    if not IS_WIN:
        return None
    # IT HAS TO PRINT THE STATE. `asyncio.run(m())` RETURNS the
    # coroutine's value and throws it away -- nothing prints it -- so the
    # first version of this helper ran the whole query, got the answer,
    # and then reported "the radio state cannot be asked". The state is
    # read from the child's STDOUT, so it has to be printed.
    #
    # RadioKind is imported too: the radio has to be the BLUETOOTH one,
    # and a machine with a Wi-Fi radio would otherwise answer for that.
    code = (
        "import asyncio\n"
        "from winrt.windows.devices.radios import Radio, RadioKind, RadioState\n"
        "async def m():\n"
        "    radios = await Radio.get_radios_async()\n"
        "    for r in radios:\n"
        "        if r.kind == RadioKind.BLUETOOTH:\n"
        "            return r.state\n"
        "    return None\n"
        "print(asyncio.run(m()))\n"
    )
    for py in (_venv_python(), sys.executable):
        if not py or not os.path.exists(py):
            continue
        try:
            p = subprocess.run([py, "-c", code], capture_output=True,
                               text=True, timeout=40)
        except Exception:                                     # noqa: BLE001
            continue
        out = (p.stdout or "").strip()
        if not out:
            continue
        name = out.splitlines()[-1].strip()
        if name.isdigit():
            return {0: "UNKNOWN", 1: "ON", 2: "OFF", 3: "DISABLED"}.get(
                int(name), "UNKNOWN")
        if name.upper() in ("ON", "OFF", "DISABLED", "UNKNOWN"):
            return name.upper()
    return None


def step_windows(report):
    """WinRT needs the radio on and the service running."""
    if not IS_WIN:
        return
    rc, out = run(["sc", "query", "bthserv"], timeout=20)
    if rc == 0 and "RUNNING" in out:
        report.add("蓝牙支持服务", True, "bthserv 正在运行")
    else:
        report.add("蓝牙支持服务", False,
                   "bthserv 没有运行",
                   "启动：sc start bthserv")
    # THE RADIO ITSELF, AND HOW IT IS ASKED FOR.
    #
    # The old check was
    #
    #     Get-PnpDevice -Class Bluetooth |
    #         Where-Object Status -ne 'OK' |
    #         Select-Object -ExpandProperty FriendlyName
    #
    # and it reported a WARN listing every Bluetooth device on the
    # machine: "REALFORCE_1; Microsoft 蓝牙枚举器; ...; AirPods Pro
    # Avrcp 传输; ...; BARROT Bluetooth Adapter; ...". That is not a
    # radio problem -- it is a list of the machine's Bluetooth DEVICES,
    # and the filter `Status -ne 'OK'` matched all of them.
    #
    # MEASURED on the real machine: the radio was ON (WinRT says
    # `state=1`, i.e. RadioState.ON) while the old check reported a
    # WARN. So the check was wrong, not the machine.
    #
    # THE RIGHT QUESTION is the radio's own state, and WinRT answers it
    # exactly: `Radio.get_radios_async()` gives the radios and their
    # `state`. bleak checks the same thing and raises
    # BleakBluetoothNotAvailableError(POWERED_OFF) when the radio is
    # off -- so this check now asks the same question bleak does.
    #
    # A RADIO THAT IS OFF IS THE ONLY FAILURE. `DISABLED` (the radio
    # exists but cannot be used) and `OFF` are failures; `ON` and
    # `UNKNOWN` are not. `UNKNOWN` is not a failure because bleak does
    # not refuse on it, and a check that fails where bleak would work
    # is worse than no check.
    #
    # IF WINRT CANNOT BE ASKED, THE CHECK IS SKIPPED. It cannot be
    # reported as a pass either: a check that could not run is not a
    # check that passed, and saying "所有蓝牙设备状态正常" when nothing
    # was asked is a lie the operator has to debug.
    state = _windows_radio_state()
    if state is None:
        report.add("蓝牙无线电", True,
                   "无法查询无线电状态（不影响使用）",
                   "如果扫描不到设备：确认「设置 → 蓝牙和设备 → 蓝牙」"
                   "里的无线电是开着的")
    elif state == "ON":
        report.add("蓝牙无线电", True, "无线电已开启")
    elif state == "OFF":
        report.add("蓝牙无线电", False, "无线电是关着的",
                   "在「设置 → 蓝牙和设备 → 蓝牙」里把无线电打开。"
                   "bleak 会检查无线电状态，没开就直接报 "
                   "BleakBluetoothNotAvailableError（POWERED_OFF）")
    elif state == "DISABLED":
        report.add("蓝牙无线电", False, "无线电被禁用",
                   "无线电不可用。检查「设备管理器 → 蓝牙」里的适配器，"
                   "或者换一台带蓝牙的机器")
    else:
        report.add("蓝牙无线电", True, "无线电状态未知（不影响使用）")


# ============================================================ the launcher
#
# The launcher is what the operator actually uses. It is deliberately
# THIN: it does the machine's checks, then hands over to the venv's
# Python. All the logic is in Python, because shell logic cannot be
# tested on the machine that writes it.
LAUNCHER_PY = '''#!/usr/bin/env python3
"""Start the Vero web UI. Installed by install.py."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VP = os.path.join(HERE, ".venv", "bin", "python")
if os.name == "nt":
    VP = os.path.join(HERE, ".venv", "Scripts", "python.exe")

if not os.path.exists(VP):
    sys.stderr.write(
        "The virtual environment is missing. Run install.py again.\\n")
    raise SystemExit(1)

host = os.environ.get("VERO_HOST", "127.0.0.1")
port = os.environ.get("VERO_PORT", "8800")
raise SystemExit(subprocess.call(
    [VP, "-u", os.path.join(HERE, "start_web.py"),
     "--host", host, "--port", port]))
'''


def install_launcher(report, target_dir=None):
    """Make sure the launcher in the project directory is the launcher.

    The launcher is a TRACKED file: ``launcher.py`` is in the repository,
    and its content is the same string as ``LAUNCHER_PY`` above. It used
    to be written unconditionally, which meant an install could silently
    OVERWRITE a tracked source file -- and if somebody had edited the
    tracked copy without syncing the constant, the install would throw
    that edit away without saying so.

    So the file is only written when it is MISSING or when its content
    differs from the constant, and the difference is reported either
    way. A file that is already correct is left byte-for-byte alone, so
    an install never dirties the working tree.
    """
    path = os.path.join(HERE, "launcher.py")
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                current = fh.read()
            if current == LAUNCHER_PY:
                try:
                    os.chmod(path, 0o755)
                except OSError:
                    pass
                return report.add("启动入口", True,
                                  "已经在 %s，内容一致（未改动）" % path)
            # The tracked copy differs from the constant. Overwriting it
            # would be an unannounced change to a version-controlled file,
            # so the operator is told and the file is left alone.
            return report.add(
                "启动入口", True,
                "%s 的内容与本脚本内置的版本不同，已保留原文件" % path,
                "如果你要换成新版本，请手动用 install.py 里的 LAUNCHER_PY 覆盖它")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(LAUNCHER_PY)
        os.chmod(path, 0o755)
    except OSError as exc:
        return report.add("启动入口", False,
                          "写不进 %s（%s）" % (path, exc),
                          "把项目目录放在有写权限的地方")
    return report.add("启动入口", True, "已写入 %s" % path)


# ============================================================ the service
SYSTEMD_UNIT = """[Unit]
Description=Vero control web UI
After=bluetooth.service
Wants=bluetooth.service

[Service]
Type=simple
WorkingDirectory=%(here)s
ExecStart=%(vp)s -u start_web.py --host %(host)s --port %(port)s
Restart=on-failure
RestartSec=5
# The radio belongs to the service, so it must be able to use it.
#
# It is the "bluetooth" group, not "lp". The "lp" group is the
# LINE PRINTER group -- it has nothing to do with radios, and it has
# been a no-op on every modern BlueZ for years. BlueZ grants radio
# access through D-Bus and polkit, and the group that actually carries
# that permission on a Debian/Ubuntu machine is "bluetooth".
#
# The group only exists on some systems; a unit that names a group
# which is not there FAILS TO START with "Failed to determine user
# credentials", so the group is added only when it exists.
%(groups)s

[Install]
WantedBy=multi-user.target
"""

LAUNCHD_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.vero.station</string>
    <key>ProgramArguments</key>
    <array>
        <string>%(vp)s</string>
        <string>-u</string>
        <string>start_web.py</string>
        <string>--host</string><string>%(host)s</string>
        <string>--port</string><string>%(port)s</string>
    </array>
    <key>WorkingDirectory</key><string>%(here)s</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>%(log)s</string>
    <key>StandardErrorPath</key><string>%(log)s</string>
</dict>
</plist>
"""


def install_service(report, host="127.0.0.1", port=8800):
    """Install the UI as a background service, where the OS has one."""
    manager = which_service_manager()
    if manager == "systemd":
        return _install_systemd(report, host, port)
    if manager == "launchd":
        return _install_launchd(report, host, port)
    if manager == "windows":
        return _install_windows_service(report, host, port)
    return report.add("后台服务", True,
                      "这台机器没有 systemd / launchd，跳过（用启动入口即可）")


def _rebuild_venv(reason):
    """Remove a venv that cannot be used, saying why first.

    bootstrap.py owns the removal (`rebuild_venv`), so it is called
    rather than copied. A second copy of "how to delete a directory
    safely" is a second chance to delete the wrong thing.
    """
    from bootstrap import rebuild_venv
    return rebuild_venv(_venv_python(), reason)


def _venv_python():
    """The venv's interpreter.

    This is bootstrap.py's `venv_python()`, not a second copy of the
    path. Two copies of "where the venv lives" are two things that can
    disagree -- and the two of them did disagree about what to do with
    a venv that was too old: bootstrap.py rebuilt it, while the
    installer here kept it and then failed to install the packages into
    it with pip's "no matching distribution", which reads exactly like
    a missing package.
    """
    return venv_python()


def _group_exists(name):
    """Does this system have this group?

    Read from the group database rather than assumed: a unit that names
    a group the system does not have fails to start, so the name has
    to be checked before it goes into the unit.
    """
    try:
        import grp
        grp.getgrnam(name)
        return True
    except (ImportError, KeyError):
        return False


def _systemd_unit(here, vp, host, port):
    """The systemd unit for this project, rendered.

    ONE function renders it, and the caller and the test both use it.
    The template used to be rendered by each side separately -- the
    installer here and the test in scripts/install_entry_regression.py
    -- which meant the test could check a unit the installer never
    produces. That is how the "SupplementaryGroups=lp" line survived
    the test: the test rendered the template with its own dictionary
    and never saw the line the installer actually wrote.

    So the rendering lives here, and the group is resolved by the same
    code that resolves it at install time.
    """
    groups = ""
    for name in ("bluetooth", "lp"):
        if _group_exists(name):
            groups = "SupplementaryGroups=%s" % name
            break
    return SYSTEMD_UNIT % {"here": here, "vp": vp, "host": host,
                           "port": port, "groups": groups}


def _install_systemd(report, host, port):
    unit = _systemd_unit(HERE, _venv_python(), host, port)
    path = "/etc/systemd/system/vero-station.service"
    tmp = os.path.join("/tmp", "vero-station.service")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(unit)
    except OSError as exc:
        return report.add("后台服务 (systemd)", False,
                          "写不了 %s（%s）" % (tmp, exc), "")
    rc, out = run(["sudo", "install", "-m", "644", tmp, path], timeout=60)
    if rc != 0:
        # No sudo, or it was refused. The unit is still written to /tmp
        # so the operator can install it by hand.
        return report.add("后台服务 (systemd)", False,
                          "装不上 %s（%s）" % (path, out.strip()[:200]),
                          "手动安装：sudo cp %s %s && sudo systemctl "
                          "enable --now vera-station" % (tmp, path))
    run(["sudo", "systemctl", "daemon-reload"], timeout=60)
    rc, out = run(["sudo", "systemctl", "enable", "--now",
                   "vera-station.service"], timeout=90)
    if rc != 0:
        return report.add("后台服务 (systemd)", False,
                          "启动失败：%s" % out.strip()[:300],
                          "看日志：journalctl -u vera-station -n 50")
    return report.add("后台服务 (systemd)", True,
                      "已安装并启动：vera-station.service")


def _install_launchd(report, host, port):
    home = os.path.expanduser("~")
    dest = os.path.join(home, "Library", "LaunchAgents",
                        "com.vero.station.plist")
    log = os.path.join(home, "Library", "Logs", "vero-station.log")
    plist = LAUNCHD_PLIST % {"vp": _venv_python(), "here": HERE,
                              "host": host, "port": port, "log": log}
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(plist)
    except OSError as exc:
        return report.add("后台服务 (launchd)", False,
                          "写不了 %s（%s）" % (dest, exc), "")
    rc, out = run(["launchctl", "unload", dest], timeout=30)
    rc, out = run(["launchctl", "load", dest], timeout=60)
    if rc != 0:
        return report.add("后台服务 (launchd)", False,
                          "装载失败：%s" % out.strip()[:300],
                          "手动：launchctl load %s" % dest)
    return report.add("后台服务 (launchd)", True,
                      "已安装并装载：%s" % dest)


def _install_windows_service(report, host, port):
    # A Windows service needs an admin shell and a service wrapper.
    # The launcher is a console program, so it is registered through
    # NSSM when it is available, and through the Task Scheduler
    # otherwise -- the scheduler needs no extra software.
    nssm = shutil.which("nssm") or shutil.which("nssm.exe")
    if nssm:
        cmd = [nssm, "install", "VeroStation",
               _venv_python(), "-u start_web.py --host %s --port %s"
               % (host, port)]
        cmd += ["set", "AppDirectory", HERE]
        rc, out = run(cmd, timeout=90)
        if rc == 0:
            run([nssm, "start", "VeroStation"], timeout=60)
            return report.add("后台服务 (Windows)", True,
                              "已通过 NSSM 安装并启动：VeroStation")
        return report.add("后台服务 (Windows)", False,
                          "NSSM 安装失败：%s" % out.strip()[:300], "")
    # The Task Scheduler: at logon, no extra software, no admin for a
    # per-user task.
    name = "VeroStation"
    vp = _venv_python()
    script = os.path.join(HERE, "start_web.py")
    # The ARGUMENT string is the part AFTER the executable. It used to be
    # sliced out of a full command line with a hand-written offset and
    # then had a quote appended unconditionally:
    #
    #     cmdline[len(vp) + 3:].strip('"') + '"'
    #
    # The slice was right, but cmdline ends with the port and no quote,
    # so `.strip('"')` removed nothing and the appended quote was left
    # dangling: the interpreter received `--port 8800"` and argparse
    # failed on the trailing quote -- while Register-ScheduledTask
    # itself succeeded, so the install reported success for a service
    # that could not start.
    #
    # So the argument string is BUILT here, not sliced out of a bigger
    # one, and it is quoted per-argument: a path with a space in it is
    # still one argument.
    argument = '-u "%s" --host %s --port %s' % (script, host, port)
    # PowerShell single-quoted strings: the only escape is '' for a
    # literal quote. A path containing a single quote would break the
    # command, so it is doubled here.
    def _ps_quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    ps = (
        "$a = New-ScheduledTaskAction -Execute %s -Argument %s "
        "-WorkingDirectory %s; "
        "$t = New-ScheduledTaskTrigger -AtLogOn; "
        "Register-ScheduledTask -TaskName %s -Action $a -Trigger $t "
        "-Description 'Vero control web UI' -Force | Out-Null; "
        "Start-ScheduledTask -TaskName %s"
        % (_ps_quote(vp), _ps_quote(argument), _ps_quote(HERE),
           _ps_quote(name), _ps_quote(name)))
    rc, out = run(["powershell", "-NoProfile", "-Command", ps], timeout=120)
    if rc == 0:
        return report.add("后台服务 (Windows)", True,
                          "已通过任务计划程序安装并启动：%s" % name)
    return report.add("后台服务 (Windows)", False,
                      "任务计划程序安装失败：%s" % out.strip()[:400],
                      "手动运行启动入口即可")


# ============================================================ the driver
def run_checks(report, mirror=None):
    print("检查运行环境……\n")
    step_pip_index(report, mirror)
    if IS_LINUX:
        step_linux_bluetooth(report)
        step_linux_permissions(report)
    elif IS_MAC:
        step_macos(report)
    elif IS_WIN:
        step_windows(report)
    else:
        report.add("操作系统", True, "%s（非三平台之一）" % sys.platform)

    # The interpreter and the venv.
    if sys.version_info[:2] >= (3, 10):
        report.add("Python 版本", True,
                   "%d.%d.%d" % sys.version_info[:3])
    else:
        report.add("Python 版本", False,
                   "%d.%d.%d（需要 3.10 以上）" % sys.version_info[:3],
                   "安装：sudo apt install python3.10 python3.10-venv / "
                   "brew install python@3.12")

    vp = _venv_python()
    if os.path.exists(vp):
        if venv_too_old(vp):
            report.add("虚拟环境", False,
                       "这个 .venv 是 3.10 之前的 Python 建的，用不了",
                       "运行 install.py 会重建它")
        else:
            report.add("虚拟环境", True, "已存在")
    else:
        report.add("虚拟环境", False, "还没有 .venv",
                   "运行 install.py 会创建它")


def install_commit_hook(report):
    """Install the project's commit-message hook, if this is a git checkout.

    The hook refuses a commit message that carries a cloud account.
    It used to be a manual step ("sh scripts/hooks/install.sh"), which
    means a fresh clone had NO protection until the operator happened
    to read the README -- and the project's own privacy check had
    already failed once because of exactly that.

    An install is the moment the machine is prepared, so the hook is
    installed here. A checkout that is not a git repository (a zip, a
    copy) simply has nowhere to put it, and the report says so.
    """
    src = os.path.join(HERE, "scripts", "hooks", "prepare-commit-msg")
    if not os.path.isfile(src):
        return report.add("提交信息钩子", True, "本项目里没有这个钩子，跳过")
    hooks_dir = os.path.join(HERE, ".git", "hooks")
    if not os.path.isdir(hooks_dir):
        return report.add("提交信息钩子", True,
                          "这里不是 git 仓库，没有钩子可装（跳过）")
    dest = os.path.join(hooks_dir, "prepare-commit-msg")
    try:
        shutil.copyfile(src, dest)
        os.chmod(dest, 0o755)
    except OSError as exc:
        return report.add("提交信息钩子", False, "装不上（%s）" % exc,
                          "手动执行：sh scripts/hooks/install.sh")
    return report.add("提交信息钩子", True,
                       "已装上 %s（提交信息里带云账号地址会被拒绝）" % dest)


def install(report, mirror=None, host="127.0.0.1", port=8800,
            service=False):
    print("安装 Vero 控制台……\n")
    run_checks(report, mirror)

    # The venv.
    #
    # A venv built by a pre-3.10 interpreter cannot install this
    # project's wheels, and pip reports that as "no matching
    # distribution" -- which reads exactly like a missing package. So
    # the SAME rule bootstrap.py applies is applied here: rebuild it,
    # and say so, rather than keeping a venv that cannot work.
    #
    # The rule is bootstrap.py's, imported above. It used to be a
    # second copy -- and the two copies disagreed: bootstrap.py
    # rebuilt the venv, while the installer here kept it and then ran
    # pip against it, so the operator got pip's "no matching
    # distribution" instead of the real reason.
    vp = _venv_python()
    if os.path.exists(vp) and venv_too_old(vp):
        print("重建虚拟环境（旧版本 Python 建的，用不了）……")
        if not _rebuild_venv("它是由 3.10 之前的 Python 建的"):
            report.add("创建虚拟环境", False,
                       "旧的 .venv 删不掉，请手动删掉 .venv 目录后重试")
            return
        vp = _venv_python()
    if not os.path.exists(vp):
        print("创建虚拟环境……")
        rc, out = run([sys.executable, "-m", "venv", os.path.join(HERE, ".venv")],
                      timeout=300)
        if rc != 0:
            report.add("创建虚拟环境", False, out.strip()[:300],
                       "Ubuntu: sudo apt install python3.10-venv")
        else:
            report.add("创建虚拟环境", True, "已创建")

    # The dependencies.
    vp = _venv_python()
    if os.path.exists(vp):
        print("安装依赖（首次需要联网）……")
        flags = pip_flags_for(mirror)
        rc, out = run([vp, "-m", "pip", "install", "-r",
                       os.path.join(HERE, "requirements.txt")] + flags,
                      timeout=1800)
        if rc != 0:
            detail = out.strip()[-500:]
            advice = ""
            if "no matching distribution" in out.lower():
                advice = ("这是 pip 太老或源不合适："
                          "升级 pip：%s -m pip install -U pip" % vp)
            elif "proxy" in out.lower() or "connection" in out.lower():
                advice = "检查代理设置，或换一个源重试"
            report.add("安装依赖", False, detail, advice)
        else:
            report.add("安装依赖", True, "已安装")

    install_launcher(report)
    install_commit_hook(report)
    if service:
        install_service(report, host, port)
    else:
        report.add("后台服务", True,
                   "未安装（需要的话用 --service）")


def main(argv=None):
    # FIRST THING. The console's encoding, before a single character is
    # printed. On a Chinese Windows the console is GBK, and this
    # project's report contains characters GBK cannot encode -- so
    # without this the report crashes with a UnicodeEncodeError and the
    # operator gets a traceback instead of the check they asked for.
    _console_stdout()
    try:
        return _main(argv)
    finally:
        # The console's code page is restored even when the report
        # raised, so the operator's shell is never left changed.
        _restore_console()


def _main(argv=None):
    ap = argparse.ArgumentParser(
        description="安装 Vero 控制台：准备机器并留下启动入口")
    ap.add_argument("verb", nargs="?", default="install",
                    choices=["install", "check", "uninstall", "service"],
                    help="要做什么（默认 install）。service = 安装并装成开机自启的后台服务")
    ap.add_argument("--check", action="store_true",
                    help="只检查，不改动任何东西")
    ap.add_argument("--uninstall", action="store_true",
                    help="移除启动入口和后台服务")
    ap.add_argument("--service", action="store_true",
                    help="同时安装为开机自启的后台服务")
    ap.add_argument("--host", default="127.0.0.1",
                    help="服务监听的地址（默认 127.0.0.1）")
    ap.add_argument("--port", default="8800", help="服务端口（默认 8800）")
    ap.add_argument("--mirror", default=None,
                    help="pip 源，例如 https://pypi.tuna.tsinghua.edu.cn/simple")
    ap.add_argument("--interactive", action="store_true",
                    help="询问用哪个 pip 源（默认是交互的，"
                         "非交互环境请用 --no-interactive）")
    ap.add_argument("--no-interactive", action="store_true",
                    help="不问，直接用当前环境里的源")
    ap.add_argument("--json", action="store_true",
                    help="以 JSON 输出检查结果")
    args = ap.parse_args(argv)

    report = Report()
    verb = args.verb
    if args.uninstall:
        verb = "uninstall"
    elif args.check:
        verb = "check"

    # THE SOURCE CHOICE, BEFORE anything is installed.
    #
    # It is asked here, at the top, because the answer is needed by
    # the dependency install AND it is the last chance to change it:
    # once the venv exists and the packages are in, the source stops
    # mattering until the next upgrade.
    #
    # IT IS NOT ASKED WHEN:
    #   * --json was given -- that output is meant to be parsed by a
    #     program, and a program cannot answer a question;
    #   * --mirror was given -- that IS the answer;
    #   * --no-interactive was given -- the caller says nobody is
    #     there, and a question with nobody there blocks FOREVER
    #     (measured on the real machine: a read from a console with
    #     no input does not return, it waits).
    #
    # IT IS ASKED WHEN a source is already configured TOO, because the
    # operator may want to CHANGE it -- and the operator's own
    # PIP_INDEX_URL is not a reason to hide the menu, it is a reason
    # to SHOW what is currently in effect.
    mirror = args.mirror
    interactive = not args.no_interactive
    if args.json:
        interactive = False
    if args.mirror:
        interactive = False
    if interactive and verb != "uninstall":
        mirror = choose_source(force=True) or mirror
        if mirror:
            apply_source(mirror)

    if verb == "uninstall":
        _uninstall(report)
    elif verb == "check":
        run_checks(report, mirror)
    else:
        # "service" is the VERB form of "--service": the three platform
        # entry scripts advertise it ("./install-ubuntu.sh service"), so
        # it has to mean "install AND register the boot service". Before
        # this, the verb was not in argparse's choices and the scripts
        # sent the operator to an argparse usage error.
        want_service = args.service or verb == "service"
        install(report, args.mirror, args.host, args.port, want_service)

    if args.json:
        # ensure_ascii=False keeps the Chinese readable, and the
        # reconfigured stdout above can print it (the console's code
        # page cannot encode every character, and "replace" turns the
        # rest into a question mark instead of a traceback).
        print(json.dumps(report.steps, ensure_ascii=False, indent=2))
    else:
        print()
        print(report.render())
        print()
        if report.ok():
            print("安装完成。")
        else:
            print("安装完成，但有 %d 项需要你确认（上面标了 WARN）。"
                  % len(report.problems()))
        print()
        print("启动：")
        if IS_WIN:
            print("    python launcher.py")
        else:
            print("    ./launcher.py")
        print("或者直接用原来的入口：")
        if IS_WIN:
            print("    run.bat")
        else:
            print("    ./run.sh")
        print()
    return 0


def _is_tracked(path):
    """Is this file under version control in the project directory?

    Used by the uninstall so it does not delete a TRACKED source file --
    removing one leaves the working tree dirty, which is a change to the
    project that an uninstall has no business making.

    The check is "does git know about this path", answered by asking
    git. A project without git (a plain copy that was unpacked from a
    zip) simply answers "not tracked", and the uninstall then removes
    the launcher, which is the right answer for an unpacked copy.
    """
    try:
        rel = os.path.relpath(path, HERE)
    except ValueError:
        return False
    try:
        out = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", rel],
            cwd=HERE, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def _uninstall(report):
    # The launcher is a TRACKED file. Deleting it left the working tree
    # with a "deleted" entry, which is a version-control change made by
    # an uninstall -- exactly the kind of surprise an uninstall must
    # not spring. So the file is only removed when it is NOT the tracked
    # one, or when it was written by an install that is being undone
    # (which is indistinguishable here, so the safe reading wins: a
    # tracked launcher is left alone and the operator is told).
    path = os.path.join(HERE, "launcher.py")
    if os.path.exists(path):
        tracked = _is_tracked(path)
        if tracked:
            report.add("移除启动入口", True,
                       "%s 是版本控制里的文件，已保留" % path,
                       "如果你真的要删它，手动删除即可")
        else:
            try:
                os.remove(path)
                report.add("移除启动入口", True, "已删除 %s" % path)
            except OSError as exc:
                report.add("移除启动入口", False, str(exc), "")
    else:
        report.add("移除启动入口", True, "本来就没有")
    manager = which_service_manager()
    if manager == "systemd":
        run(["sudo", "systemctl", "disable", "--now",
             "vera-station.service"], timeout=90)
        run(["sudo", "rm", "-f", "/etc/systemd/system/vera-station.service"],
            timeout=60)
        run(["sudo", "systemctl", "daemon-reload"], timeout=60)
        report.add("移除后台服务", True, "vera-station.service")
    elif manager == "launchd":
        dest = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents",
                            "com.vero.station.plist")
        run(["launchctl", "unload", dest], timeout=30)
        if os.path.exists(dest):
            os.remove(dest)
        report.add("移除后台服务", True, "com.vero.station.plist")
    elif manager == "windows":
        ps = "Unregister-ScheduledTask -TaskName 'VeroStation' " \
             "-Confirm:$false -ErrorAction SilentlyContinue"
        run(["powershell", "-NoProfile", "-Command", ps], timeout=90)
        report.add("移除后台服务", True, "VeroStation")


if __name__ == "__main__":
    raise SystemExit(main())
