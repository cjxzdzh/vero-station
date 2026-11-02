@echo off
REM ============================================================
REM  Vero control - Windows launcher.
REM
REM  Usage:
REM    run.bat            start the web UI on http://127.0.0.1:8800
REM    run.bat install    prepare this machine and install the launcher
REM    run.bat service    the same, and register the boot service
REM    run.bat check      report what this machine is missing
REM    run.bat uninstall  remove the launcher and the background service
REM    run.bat start      start the web UI without preparing anything
REM    run.bat test       offline self-test, no hardware needed
REM    run.bat scan       look for the device
REM    run.bat diag       link-lifetime measurement, 5 minutes
REM    run.bat cli ...    raw CLI, e.g. run.bat cli frame --percent 50
REM
REM  All real logic lives in bootstrap.py so that it can be tested on
REM  the machine that writes it. This file only picks an interpreter.
REM
REM  THE PIP SOURCE is never switched silently: the operator's own
REM  PIP_INDEX_URL always wins, and this project's VERO_PIP_INDEX is
REM  used when there is none.
REM
REM    set VERO_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
REM    run.bat install
REM
REM  A proxy in the environment is honoured as it is.
REM
REM  WHY THERE ARE NO PARENTHESIS BLOCKS IN THIS FILE. cmd.exe
REM  parses a whole if-block before it runs any of it, and an
REM  unescaped closing bracket inside an echoed message closes the
REM  block early - the rest of the file then dies with
REM  "the . was unexpected at this position". The message below used to contain one, and the
REM  whole launcher failed to start on a machine that had Python
REM  installed. Everything here uses goto and labels instead, so
REM  there is no block for a bracket to escape from. Keep it that way.
REM ============================================================
setlocal
cd /d "%~dp0"

REM Try the Python launcher pinned to a version first, then plain python.
REM "py" is preferred because it is unambiguous -- bare "python" is
REM often a 2.7 or 3.5 that predates every wheel we need.
set "PY="
for %%C in ("py -3.13" "py -3.12" "py -3.11" "py -3.10" "py -3" "python" "python3") do call :probe %%C

if not defined PY goto :nopython

REM The virtual environment is NOT judged here, and NOT touched here.
REM
REM This file must not delete the .venv. A launcher that destroys the
REM operator's environment as a side effect of being launched is a
REM launcher that destroys work -- and a full reinstall of every
REM dependency follows, on a machine that had just installed them.
REM
REM The rule for an unusable venv belongs to bootstrap.py, which is
REM written in Python so that it can be tested on the machine that
REM writes it. This file only picks an interpreter.

%PY% bootstrap.py run %*
set "RC=%ERRORLEVEL%"
goto :done

REM ------------------------------------------------------------ probe
:probe
if defined PY goto :eof
%~1 -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if errorlevel 1 goto :eof
set "PY=%~1"
goto :eof

REM ------------------------------------------------------------ no python
:nopython
echo.
echo   ERROR: no usable Python found on PATH. Version 3.10 or newer is needed.
echo   Install it from https://www.python.org/downloads/
echo   and tick "Add python.exe to PATH" during setup, then reopen
echo   this terminal.
echo.
pause
exit /b 1

REM ------------------------------------------------------------ done
:done
if "%RC%"=="0" goto :exitok
echo.
echo   Exited with code %RC%.
pause
:exitok
exit /b %RC%
