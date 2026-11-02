#!/usr/bin/env python3
"""Bootstrap: find a usable Python, build the venv, install, then run.

The Windows launcher is a two-line shim that hands control here, because
shell logic cannot be tested on the machine that writes it, while this file
can. Everything clever lives in Python.

Deliberately conservative syntax (no f-strings, no walrus, no annotations):
this file must be able to run on an OLD interpreter, because its first job
is often to notice that it is one and hand off to a newer one.

    python bootstrap.py run [web|test|scan|diag|cli ...]

Re-executes itself under a 3.10+ interpreter when necessary. bleak 3.x
requires 3.10+, and a pip older than 20 cannot resolve its wheels -- both
failures are far clearer here than inside pip.
"""

import json
import os
import subprocess
import sys

MIN_PY = (3, 10)
HERE = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(HERE, ".venv")

# In order of preference. Each entry is an argv list, not a shell string.
#
# "py -3.13" is the Windows launcher pinned to one version. Bare "python"
# is the last resort and on many machines is a 2.7 or 3.5 install that
# predates every wheel we need, which is why the version gate below exists.
CANDIDATES = [
    ["py", "-3.13"],
    ["py", "-3.12"],
    ["py", "-3.11"],
    ["py", "-3.10"],
    ["py", "-3"],
    ["python"],
    ["python3"],
]


def ok_version(info):
    return info[:2] >= MIN_PY


def probe(cmd):
    """Run `cmd -c 'print(version)'` and return its version tuple.

    cmd is an argv list. Returns None if the command is missing, is not
    Python, or prints something unparseable.
    """
    try:
        out = subprocess.check_output(
            cmd + ["-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
            stderr=subprocess.DEVNULL, timeout=25)
    except Exception:
        return None
    try:
        parts = out.decode("utf-8", "replace").strip().split()
        return tuple(int(x) for x in parts[-1].split("."))
    except Exception:
        return None


def find_interpreter(exclude_self=True):
    """First 3.10+ interpreter on PATH, as (argv, version).

    exclude_self skips the interpreter currently running this script. That
    matters when bootstrap.py is re-executed: without it, a 3.9 process
    would keep handing off to itself forever.
    """
    me = os.path.realpath(sys.executable) if sys.executable else None
    for cmd in CANDIDATES:
        ver = probe(cmd)
        if not ver or not ok_version(ver):
            continue
        if exclude_self and me and os.path.realpath(cmd[0]) == me:
            continue
        return cmd, ver
    return None, None


def describe_interpreter(found, ver):
    """Human label for a found interpreter, safe when ver is None."""
    if not found:
        return "(none)"
    name = " ".join(found)
    return "%s (%s)" % (name, ".".join(str(x) for x in ver)) if ver else name


def too_old_message(here):
    print("")
    print("  ERROR: Python 3.10 or newer was not found.")
    print("")
    print("  Running here : %s" % here)
    print("  This program : %d.%d.%d" % sys.version_info[:3])
    print("")
    print("  The BLE library (bleak 3.x) requires Python 3.10+, so older")
    print("  interpreters cannot install it. pip reports this as a")
    print('  confusing "no matching distribution" error.')
    print("")
    print("  Fix:")
    print("    1. Install Python 3.10+ from https://www.python.org/downloads/")
    print('    2. Tick "Add python.exe to PATH" during setup.')
    print("    3. Close and reopen your terminal, then run this again.")
    print("")
    print("  Debian/Ubuntu : sudo apt install python3.10 python3.10-venv")
    print("  macOS         : brew install python@3.12")
    print("")


def venv_python():
    if os.name == "nt":
        return os.path.join(VENV_DIR, "Scripts", "python.exe")
    return os.path.join(VENV_DIR, "bin", "python")


def venv_too_old(vp):
    """True when the existing .venv was built by a pre-3.10 interpreter.

    An early version of the launcher picked an interpreter with `where
    python`, which ignores the version. That produced a venv carrying an
    ancient pip, and pip 19 reports "no matching distribution" for modern
    wheels -- indistinguishable from a missing package. Rather than
    trying to repair such a venv, say so and rebuild it.
    """
    ver = probe([vp])
    return ver is not None and not ok_version(ver)


def rebuild_venv(vp, reason):
    """Remove the venv, reporting why first. Never silently destroys work."""
    print("")
    print("  Removing the existing virtual environment: %s" % reason)
    import shutil
    try:
        shutil.rmtree(VENV_DIR)
    except Exception as exc:                                    # noqa: BLE001
        print("  ERROR: could not remove it (%s)." % exc)
        print("  Delete the .venv folder by hand, then run this again.")
        print("")
        return False
    return True


def run(cmd, **kw):
    return subprocess.call(cmd, **kw)


def ensure_venv(py_cmd):
    """Create .venv if needed. py_cmd is a full argv list. Returns its path.

    Rebuilds rather than reuses a venv whose interpreter is too old to
    install this project's dependencies.
    """
    vp = venv_python()
    if os.path.exists(vp):
        if not venv_too_old(vp):
            return vp
        if not rebuild_venv(vp, "it was created by a pre-3.10 Python"):
            return None
    print("Creating virtual environment...")
    if run(py_cmd + ["-m", "venv", VENV_DIR]) != 0 or not os.path.exists(vp):
        print("")
        print("  ERROR: could not create the virtual environment.")
        print("    Debian/Ubuntu: sudo apt install python3-10-venv")
        print("")
        return None
    return vp


def parse_pip_version_text(text):
    """The major pip version in `pip --version` output, or None.

    `pip --version` prints: "pip 19.0.3 from /path (python 3.13)".
    The version is the token AFTER the literal "pip" -- matching tokens
    that merely start with "pip" skips the literal itself and never finds
    a version, which silently made every pip look new enough.

    This is the PARSING, split out from the subprocess call so it can be
    fed sample strings. It used to live inside pip_version(), and the
    regression suite carried its own COPY of it -- a copy that is
    correct today and proves nothing: with the production function
    broken to always return None, the copy still passed every one of
    its assertions. A test of a copy is a test of the copy.
    """
    toks = text.split()
    for i, tok in enumerate(toks[:-1]):
        if tok != "pip":
            continue
        major = toks[i + 1].split(".")[0]
        if major.isdigit():
            return int(major)
    return None


def pip_version(vp):
    """Major pip version as an int, or None if it cannot be determined."""
    try:
        out = subprocess.check_output([vp, "-m", "pip", "--version"],
                                      stderr=subprocess.DEVNULL, timeout=60)
    except Exception:
        return None
    return parse_pip_version_text(out.decode("utf-8", "replace"))


def pip_ok(vp):
    """True when the venv's pip is new enough to resolve modern wheels.

    pip 19 cannot see wheels for current bleak/fastapi and fails with the
    same "no matching distribution" text as a genuinely absent package.
    An unreadable version returns True so the upgrade is attempted rather
    than skipped -- upgrading a healthy pip is harmless, trusting a broken
    one is not.
    """
    major = pip_version(vp)
    return True if major is None else major >= 20


#: The pip sources that work from mainland China, and the official one.
#:
#: Each entry is (name, url, needs_trusted_host). The trusted-host flag
#: matters because the mirrors serve over HTTPS with a certificate that
#: pip's bundled CA store does not always have -- and pip then refuses
#: the index with a certificate error that reads like a network fault.
#: The mirrors, and whether they need --trusted-host.
#:
#: The third field is now "needs_trusted_host" and it is FALSE for
#: every HTTPS mirror, because --trusted-host turns OFF the TLS
#: certificate check for that host (pip's InsecureHTTPAdapter calls
#: cert_verify(..., verify=False)) and every one of these serves HTTPS
#: with a publicly trusted certificate.
#:
#: MEASURED, 2026-10-24: tuna, ustc, aliyun, tencent, huawei, bfsu, nju
#: and pku all present a publicly trusted certificate (GlobalSign,
#: Let's Encrypt, ZeroSSL) and install with no extra flags. The wheels
#: are BYTE-IDENTICAL to PyPI's -- bleak 3.0.2's sha256 is
#: 39092feb... on pypi.org, tuna, aliyun and tencent alike -- so there
#: is no supply-chain reason to prefer one over another.
#:
#: NOT LISTED, and why:
#:   * pypi.douban.com  -- DEAD. The TLS handshake completes and every
#:     request then fails; the error is indistinguishable from a
#:     network fault. pypi.doubanio.com only 302-redirects to Tencent.
#:   * mirrors.szu.edu.cn -- unreachable from here (no TCP connect).
#:
#: Huawei LAGS: its fastapi index was one release behind PyPI
#: (0.161.1 vs 0.162.0) when measured. That is fine for the pinned
#: constraint (fastapi>=0.110) but do not expect newest-release parity.
PIP_MIRRORS = [
    ("Tsinghua", "https://pypi.tuna.tsinghua.edu.cn/simple", False),
    ("Aliyun", "https://mirrors.aliyun.com/pypi/simple/", False),
    ("Tencent", "https://mirrors.cloud.tencent.com/pypi/simple", False),
    ("Huawei", "https://repo.huaweicloud.com/repository/pypi/simple", False),
    ("Ustc", "https://mirrors.ustc.edu.cn/pypi/simple", False),
    ("Bfsu", "https://mirrors.bfsu.edu.cn/pypi/web/simple", False),
    ("Nju", "https://mirror.nju.edu.cn/pypi/web/simple", False),
    ("PyPI", "https://pypi.org/simple", False),
]

#: The environment variable that chooses the source.
#:
#: PIP_INDEX_URL is pip's OWN variable, so an operator who has set it
#: has already answered this question. This bootstrap never overrides
#: it: a silent switch to a mirror is how a developer ends up debugging
#: a machine that is not the one they think they are on.
MIRROR_ENV = "VERO_PIP_INDEX"


def proxy_env():
    """The proxy the environment already carries, or None.

    Both the lower-case and the upper-case spellings are checked,
    because pip honours the lower-case ones and most other tools
    honour the upper-case ones -- and a machine with only one of them
    set is a machine where half the tools go through the proxy and half
    do not.

    Nothing here ever WRITES a proxy variable. The proxy is the
    operator's business: it is read, reported, and left alone.
    """
    for name in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
                 "all_proxy", "ALL_PROXY"):
        v = os.environ.get(name)
        if v:
            return "%s=%s" % (name, v)
    return None


#: The file the installer's menu writes the chosen source to.
#:
#: It is here, in bootstrap.py, and not in install.py, because
#: bootstrap.py is what READS the source -- the installer only writes
#: it. A second copy of the path in two files is how the two drift
#: apart, so it is derived here from the same name.
SOURCE_FILE = os.path.join(HERE, ".vero-pip-index")


def _source_file_url():
    """The source the installer's menu chose, or None.

    It is a FILE and not only an environment variable, because the
    install runs in a CHILD process: ``bootstrap.py`` starts
    ``install.py`` and waits for it, so a change to the environment
    inside install.py does NOT travel back up to the bootstrap -- and
    the bootstrap's own dependency install would go back to the
    default source.
    """
    try:
        with open(SOURCE_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    return line
    except OSError:
        pass
    return None


def mirror_url():
    """The source to install from, or None for pip's own default.

    The order is: the operator's own PIP_INDEX_URL, then this
    project's VERO_PIP_INDEX, then the source the installer MENU chose,
    then pip's own config file. The first one wins in each pair,
    because the operator's setting is the more specific statement of
    intent.

    The menu's choice comes BEFORE pip's own config file on purpose:
    the operator picked it in THIS project's own menu, and a stale
    pip config on the machine should not override what they were
    just asked and answered.
    """
    for name in ("PIP_INDEX_URL", MIRROR_ENV):
        v = os.environ.get(name)
        if v:
            return v.strip()
    chosen = _source_file_url()
    if chosen:
        return chosen
    # PIP_EXTRA_INDEX_URL is a source too -- pip prints both in
    # "Looking in indexes:", and it searches them with NO priority
    # between them.
    #
    # It is read here for the REPORT, not to be used as the index: a
    # mirror added as an extra index can supply a HIGHER version than
    # PyPI, and pip takes the best match across all of them. That is
    # the dependency-confusion risk, and it is why the project's own
    # mirror switches the index rather than adding to it.
    # pip's own config: the operator may have put it there.
    for path in pip_config_files():
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("index-url") and "=" in line:
                        return line.split("=", 1)[1].strip()
        except OSError:
            continue
    return None


def pip_config_files():
    """Every config file pip itself would read, on THIS platform.

    WHY PIP'S OWN API, AND NOT A LIST OF PATHS. The first version of
    this hard-coded four paths, and three of them were wrong on the
    platforms this project runs on:

      * macOS's user config is at
        ~/Library/Application Support/pip/pip.conf -- the two paths
        that were listed do not exist there, so an operator who had
        run `pip config set global.index-url ...` on their Mac was
        INVISIBLE to this, and the install silently used a different
        source than the one pip would have used.
      * Windows's user config is at %APPDATA%/pip/pip.ini, and the
        global one at C:/ProgramData/pip/pip.ini. Only the global
        one was listed.
      * Linux's global config also lives at /etc/xdg/pip/pip.conf,
        which was not listed.

    So the list was a guess about where pip looks, and a wrong guess
    is worse than none: it made the install report one source while
    pip used another.

    pip knows the answer, and it is platform-dependent (it comes from
    appdirs), so pip is asked. The list below is the FALLBACK for the
    case where pip cannot be imported -- which is the case that
    matters, because this file runs BEFORE the venv exists and
    sometimes before pip does.

    The paths are yielded in pip's own order (global, then user, then
    site) and whether a file EXISTS is not filtered here: the caller
    opens what is there, and the list is also what the tests compare
    against pip's own.
    """
    try:
        from pip._internal.configuration import Configuration
        for _kind, _paths in Configuration(isolated=False).iter_config_files():
            for _p in _paths:
                if _p:
                    yield _p
        return
    except Exception:                                          # noqa: BLE001
        pass
    # The fallback: the paths pip uses on the platforms this project
    # runs on, in pip's own order.
    home = os.path.expanduser("~")
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        paths = [
            os.path.join("C:\\ProgramData", "pip", "pip.ini"),
            os.path.join(appdata, "pip", "pip.ini"),
            os.path.join(home, "pip", "pip.ini"),
            os.path.join(sys.prefix, "pip.ini"),
        ]
    elif sys.platform == "darwin":
        paths = [
            "/Library/Application Support/pip/pip.conf",
            os.path.join(home, "Library", "Application Support", "pip", "pip.conf"),
            os.path.join(home, ".config", "pip", "pip.conf"),
            os.path.join(home, ".pip", "pip.conf"),
            os.path.join(sys.prefix, "pip.conf"),
        ]
    else:
        paths = [
            "/etc/xdg/pip/pip.conf",
            "/etc/pip.conf",
            os.path.join(home, ".config", "pip", "pip.conf"),
            os.path.join(home, ".pip", "pip.conf"),
            os.path.join(sys.prefix, "pip.conf"),
        ]
    for path in paths:
        yield path


def pip_source_flags():
    """The pip flags for the source that is in effect.

    THE TRUSTED-HOST IS NOT ADDED, AND THAT IS THE POINT.

    --trusted-host does not mean "this host is OK". It mounts pip's
    InsecureHTTPAdapter, whose cert_verify() calls
    super().cert_verify(conn, url, verify=False) -- it turns OFF the
    TLS certificate check for that host. So adding it to every mirror
    is not a convenience: it is removing the protection that stops a
    man in the middle from serving a different wheel.

    And it is not needed. Measured on the mirrors this project lists:
    tuna, ustc, aliyun, tencent, huawei, bfsu, nju and pku all serve
    HTTPS with a PUBLICLY TRUSTED certificate (GlobalSign, Let's
    Encrypt, ZeroSSL), and pip installs from every one of them with no
    flags at all. pip >= 24.2 verifies against the system CA store plus
    truststore, so a normal machine -- including a current Windows --
    needs nothing extra.

    The two cases that DO need it are the ones an operator has to ask
    for, and they are named in the message when they happen:
      * an http:// index (pip IGNORES an untrusted http index and says
        "no versions", which reads exactly like a missing package), and
      * an HTTPS index whose certificate the machine cannot verify --
        an intercepting corporate proxy, or an out-of-date CA store.

    So the flags carry the index and nothing else. A certificate
    failure is reported by pip with a message that says exactly what
    it is, and the operator can add the flag themselves.
    """
    url = mirror_url()
    if not url:
        return []
    if url.rstrip("/") == "https://pypi.org/simple":
        return []
    # An http:// index is the ONE case where pip needs --trusted-host
    # to look at it at all. It is added here because without it the
    # index is silently ignored.
    if url.lower().startswith("http://"):
        host = url.split("://", 1)[-1].split("/", 1)[0]
        return ["--index-url", url, "--trusted-host", host]
    return ["--index-url", url]


def ensure_deps(vp):
    # Upgrade only when pip is genuinely too old. Doing it unconditionally
    # makes every single run pay for a pointless reinstall.
    major = pip_version(vp)
    if major is not None and major < 20:
        print("Upgrading pip %d -> newest (pip %d cannot see modern wheels)..."
              % (major, major))
        # The SAME source as the install below.
        #
        # The first version of this upgraded pip with no flags, so a
        # machine that had only VERO_PIP_INDEX set (and not
        # PIP_INDEX_URL) upgraded pip from pypi.org and then installed
        # the dependencies from the mirror -- two sources, and the
        # slow one was the one that had to succeed first.
        if run([vp, "-m", "pip", "install", "--upgrade", "pip"]
               + pip_source_flags()) != 0:
            print("  WARNING: pip upgrade failed; trying the install anyway.")
    probe_deps = subprocess.call([vp, "-c", "import bleak, fastapi, uvicorn"],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
    if probe_deps == 0:
        return True
    flags = pip_source_flags()
    if flags:
        print("Installing dependencies from %s ..." % flags[1])
    else:
        print("Installing dependencies (first run only, needs internet)...")
    if run([vp, "-m", "pip", "install", "-r",
            os.path.join(HERE, "requirements.txt")] + flags) != 0:
        print("")
        print("  ERROR: dependency install failed.")
        v = pip_version(vp)
        if v is not None and v < 20:
            print("  Your pip is %d, which is too old to install these"
                  " packages." % v)
            print("  Run:  %s -m pip install --upgrade pip" % vp)
        else:
            print("  Check your internet connection, then run this again.")
            print("")
            print("  In mainland China the official source can be very slow.")
            print("  Use a mirror:")
            for name, url, _ in PIP_MIRRORS[:-1]:
                print("    %s %s" % (name, url))
            print("  for example:")
            print("    %s \"%s\""
                  % (MIRROR_ENV, PIP_MIRRORS[0][1]))
            print("  or, for one run only:")
            print("    VERO_PIP_INDEX=%s ./run.sh" % PIP_MIRRORS[0][1])
        print("")
        return False
    return True


#: The verbs that start the WEB UI, and that therefore read the host
#: and the port from the environment rather than carrying them.
#:
#: "web" and "start" used to disagree about that: "web" read
#: VERO_HOST and VERO_PORT, and "start" -- the verb the three platform
#: entry scripts actually call -- carried them as literals. An operator
#: who set VERO_PORT got the boot service on that port and
#: "./run.sh start" on 8800, with nothing saying so. Both verbs now
#: take the same path.
#:
#: The list is separate from DISPATCH because the verbs in DISPATCH
#: carry their own argv, and these two carry none: the host and the
#: port are computed at the moment they are needed.
WEB_VERBS = ("web", "start")

DISPATCH = {
    "test": ["selftest.py"],
    "scan": ["run_cli.py", "scan"],
    "diag": ["run_diagnose.py", "--seconds", "300", "--json", "report.json"],
    # "start" is what the three platform entry scripts call "just
    # start it, do not prepare anything". It used to be handled by
    # each script separately (a shell `if` in install-ubuntu.sh, a
    # `goto` in install-windows.bat), so the same word meant the same
    # thing in four places with four copies of the rule. Here it is
    # one entry, and the scripts just pass the word on.
}

#: The verbs that are handled in the bootstrap itself rather than
#: dispatched to a script. "install" is here because it has to run
#: BEFORE the venv exists -- it is what creates it.
#:
#: "service" is here too, because the three platform entry scripts
#: advertise it ("./install-ubuntu.sh service", and the same on macOS
#: and Windows) and because it means "install AND register the boot
#: service". It was missing, so "./run.sh service" fell through to the
#: dispatch table, missed there too, and printed "Unknown command
#: 'service'; starting the web UI instead." -- which silently started
#: the UI instead of doing what the operator asked for.
SELF_VERBS = ("install", "check", "uninstall", "service")


def do_run(argv):
    os.chdir(HERE)

    # Step 1: a 3.10+ interpreter.
    if ok_version(sys.version_info[:3]):
        py_cmd = [sys.executable]
    else:
        found, ver = find_interpreter()
        if not found:
            too_old_message("%d.%d.%d" % sys.version_info[:3])
            return 1
        print("This Python is %d.%d.%d, too old. Handing off to %s."
              % (sys.version_info[0], sys.version_info[1], sys.version_info[2],
                 describe_interpreter(found, ver)))
        return run(found + [os.path.abspath(__file__)] + ["run"] + argv)

    print("Using Python %d.%d.%d" % sys.version_info[:3])

    # Step 2: the verbs that must run before there is a venv.
    #
    # "install" creates the venv and the dependencies, so it CANNOT be
    # dispatched to a script that runs inside the venv -- it is the
    # thing that makes the venv. So it runs with the system
    # interpreter, and the installer itself creates the venv.
    verb = argv[0] if argv else ""
    if verb in SELF_VERBS:
        args = list(argv[1:]) if argv else []
        flags = []
        for a in args:
            flags.append(a)
        return run([sys.executable, os.path.join(HERE, "install.py"), verb]
                   + flags)

    # Step 3: venv.
    vp = ensure_venv(py_cmd)
    if not vp:
        return 1

    # Step 4: dependencies.
    if not ensure_deps(vp):
        return 1

    # Step 5: run the requested piece.
    if argv and argv[0] == "cli":
        return run([vp, "run_cli.py"] + argv[1:])
    key = argv[0] if argv else "web"
    if key in WEB_VERBS:
        # Expose the UI deliberately: loopback unless VERO_HOST says otherwise.
        # The port comes from the environment, like the host and like the
        # launcher does. It was hardcoded in the dispatch table for the
        # "start" verb, so the THREE start paths disagreed about the
        # port: the systemd/launchd unit and the launcher honoured it
        # (VERO_PORT, or --port at install time), while this one always
        # used 8800. An operator who installed with --port 9000 got a
        # service on 9000 and a "./run.sh start" on 8800, with nothing
        # saying so.
        host = os.environ.get("VERO_HOST", "127.0.0.1")
        port = os.environ.get("VERO_PORT", "8800")
        return run([vp, "start_web.py", "--host", host, "--port", port])
    if key not in DISPATCH:
        print("Unknown command %r; starting the web UI instead." % key)
        key = "web"
        host = os.environ.get("VERO_HOST", "127.0.0.1")
        port = os.environ.get("VERO_PORT", "8800")
        return run([vp, "start_web.py", "--host", host, "--port", port])
    return run([vp] + DISPATCH[key])


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "run":
        return do_run(argv[1:])
    # Invoked with no recognised verb: behave like "run web".
    return do_run(argv if argv else ["web"])


if __name__ == "__main__":
    sys.exit(main())