@echo off
REM ============================================================
REM  Vero control -- Windows installer and launcher.
REM
REM  THIS IS THE ENTRY POINT an operator uses -- double-click it.
REM
REM  Usage (from a terminal):
REM    install-windows.bat              prepare the machine, then start the UI
REM    install-windows.bat check        report what is missing, change nothing
REM    install-windows.bat service      also install it as a boot task
REM    install-windows.bat uninstall  remove the launcher and the background service
REM    install-windows.bat start        just start it, no preparation
REM
REM  WHAT WINDOWS NEEDS. bleak 3.x talks to the radio through WinRT,
REM  and WinRT is part of the OS -- so there is no driver to install
REM  and no adapter to configure. What there IS:
REM
REM    * the "Bluetooth Support Service", bthserv, has to be running,
REM    * the radio has to be ON, in Settings then Bluetooth,
REM    * bleak 3.x needs Windows 11, version 22000 or newer.
REM
REM  THE PYTHON. Windows has no python by default, and the "python"
REM  on PATH is often a 2.7 or a 3.5 from years ago. So the launcher
REM  tries the versioned "py" launcher first, and says exactly what to
REM  install when there is nothing usable.
REM
REM  THE PIP SOURCE IS ASKED FOR. The install puts a menu up and
REM  waits for an answer, and the DEFAULT is the domestic mirror
REM  (Tsinghua) -- so pressing ENTER gets the fast path, because in
REM  mainland China the official source is slow enough to look like a
REM  hang. The menu offers every mirror this project knows, the
REM  official PyPI, a custom address, and "keep what is already set".
REM
REM  The choice is remembered, so the next run uses it too.
REM
REM  The operator's own PIP_INDEX_URL still wins over everything:
REM
REM    set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
REM
REM  A proxy in the environment is honoured as it is -- pip reads
REM  http_proxy / https_proxy itself.
REM
REM  WHY THERE ARE NO PARENTHESIS BLOCKS IN THIS FILE. cmd.exe
REM  parses a whole if-block before it runs any of it, and an
REM  unescaped closing bracket inside an echoed message closes the
REM  block early -- the rest of the file then dies with
REM  "the . was unexpected at this position" and prints nothing at
REM  all. That happened here: the message about needing Python
REM  3.10 or newer carried one. Everything below uses goto and
REM  labels instead, so there is no block for a bracket to escape
REM  from. scripts/check_bat_files.py enforces that.
REM ============================================================
setlocal
cd /d "%~dp0"

REM The verb, and the REST of the arguments.
REM
REM THE REST IS BUILT FROM %2, NOT FROM %*. `shift` moves the %1..%9
REM placeholders along, but it does NOT touch %* -- %* is always the
REM WHOLE argument list, whatever has been shifted.
REM
REM So this line used to be
REM
REM     set "REST=%*"
REM
REM which, for "install-windows.bat check", gave REST=[check] -- and
REM the verb was then passed TWICE: once as %VERB% and once inside
REM %REST%. install.py took the first as the verb and rejected the
REM second:
REM
REM     install.py: error: unrecognized arguments: check
REM
REM MEASURED on a real Windows 11 machine.
REM
REM %2..%9 is the rest, and %* is only used when there are more than
REM eight arguments -- which is a different thing.
set "VERB=%~1"
if "%VERB%"=="" set "VERB=install"
set "REST=%2 %3 %4 %5 %6 %7 %8 %9"
REM Trim the trailing space the empty placeholders leave behind.
set "REST=%REST: =%"

REM ---------------------------------------------------------------- python
REM
REM "py -3.13" is preferred over bare "python": the version is pinned,
REM so the venv is never built by an interpreter that predates every
REM wheel this project needs.
set "PY="
for %%C in ("py -3.13" "py -3.12" "py -3.11" "py -3.10" "py -3" "python" "python3") do call :probe %%C

if not defined PY goto :nopython

REM ---------------------------------------------------------------- radio
REM
REM The service and the radio are checked and reported; the radio is
REM not touched, because turning it on needs the Settings app and the
REM user's consent.
call :btserv

REM ---------------------------------------------------------------- the go
if /i "%VERB%"=="start" goto :start

REM THE SOURCE MENU.
REM
REM install.py asks which pip source to use, and the DEFAULT is the
REM domestic mirror -- so an operator who just presses ENTER gets the
REM fast path. The menu lives in install.py, not here, because the
REM mirror list and the "already configured" rule live in Python and a
REM SECOND copy here would rot.
REM
REM THE READ HAS A DEADLINE, so an unattended install cannot hang.
REM Measured on a real Windows 11 machine: a readline() from a console
REM with nobody at the keyboard does not return -- it waits for ever.
REM So install.py gives the read a deadline (45 s) and takes the
REM default when nobody answers. Verified: a stdin held open with no
REM answer returns the domestic mirror after 45 s, and the install
REM carries on.
REM
REM --json and --mirror already mean "do not ask", and the service
REM path never reaches this file at all -- the service runs
REM start_web.py directly.
set "INTERACTIVE=--interactive"

%PY% install.py %VERB% %REST% %INTERACTIVE%
set "RC=%ERRORLEVEL%"
if /i not "%VERB%"=="install" goto :done
if not "%RC%"=="0" goto :done

echo.
echo   Starting ...
echo.
%PY% bootstrap.py run web
goto :done

:start
REM "start" is the bootstrap's own verb, so it is passed straight
REM through instead of being interpreted here.
%PY% bootstrap.py run start %REST%
set "RC=%ERRORLEVEL%"
goto :done

REM ---------------------------------------------------------------- probe
:probe
if defined PY goto :eof
%~1 -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if errorlevel 1 goto :eof
set "PY=%~1"
goto :eof

REM ---------------------------------------------------------------- bthserv
:btserv
sc query bthserv >nul 2>&1
if errorlevel 1 goto :btoff
echo   NOTE: the Bluetooth Support Service, bthserv, is running.
goto :eof
:btoff
echo.
echo   NOTE: the Bluetooth Support Service, bthserv, is not running.
echo   Start it with:  sc start bthserv
echo   It needs an administrator terminal.
echo.
goto :eof

REM ---------------------------------------------------------------- no python
:nopython
echo.
echo   ERROR: no usable Python found on PATH. Version 3.10 or newer is needed.
echo   Install it from https://www.python.org/downloads/
echo   and tick "Add python.exe to PATH" during setup, then reopen
echo   this terminal.
echo.
pause
exit /b 1

REM ---------------------------------------------------------------- done
:done
if not "%RC%"=="0" goto :failed
exit /b 0
:failed
echo.
echo   Exited with code %RC%.
pause
exit /b %RC%
