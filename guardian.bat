@echo off
REM =====================================================================
REM  Planme single resident-process launcher (replaces old start/stop bats)
REM  Usage:
REM    guardian.bat            -> start the guardian (resident, no window)
REM    guardian.bat start      -> same as above
REM    guardian.bat stop       -> graceful stop (writes stop flag)
REM    guardian.bat status     -> is it running? next trigger time
REM    guardian.bat test       -> self-check paths/ports, starts nothing
REM    guardian.bat install    -> autostart at logon via Task Scheduler
REM    guardian.bat uninstall  -> remove that scheduled task
REM  NOTE: keep this file pure ASCII (GBK/CP936 safe on Windows).
REM =====================================================================
SETLOCAL
SET "PY=D:\Tools\Miniforge3\envs\planme\python.exe"
SET "PYW=D:\Tools\Miniforge3\envs\planme\pythonw.exe"
SET "ROOT=%~dp0"
IF NOT EXIST "%PYW%" SET "PYW=%PY%"
cd /d "%ROOT%"

SET "ARG=%~1"
IF "%ARG%"==""        GOTO START
IF /I "%ARG%"=="start"     GOTO START
IF /I "%ARG%"=="stop"      GOTO FORE
IF /I "%ARG%"=="status"    GOTO FORE
IF /I "%ARG%"=="test"      GOTO FORE
IF /I "%ARG%"=="install"   GOTO FORE
IF /I "%ARG%"=="uninstall" GOTO FORE
echo Usage: guardian.bat [start^|stop^|status^|test^|install^|uninstall]
GOTO :EOF

:START
start "" "%PYW%" "%ROOT%planme_guardian.py" run
echo Guardian started (resident, no window). Logs: guardian.log
GOTO :EOF

:FORE
"%PY%" "%ROOT%planme_guardian.py" %*
GOTO :EOF
