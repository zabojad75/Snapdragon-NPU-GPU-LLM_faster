@echo off
REM Start GenieX NPU server (Hexagon NPU models, OpenAI-compatible API on :18181)
REM The server runs with NO window (no console, no taskbar entry).
REM --keepalive 999999 : geniex defaults to a 300s idle timeout, which silently
REM kills the NPU server after 5 quiet minutes. Effectively disable it.
setlocal
set BASE=%USERPROFILE%\llmnpu
set GENIEX=%BASE%\geniex\engines\geniex\geniex.exe
set DATA=%BASE%\geniex\models\geniex
if not exist "%BASE%\logs" mkdir "%BASE%\logs"

REM HIDDEN=1 marks the second pass, which actually runs the server
if "%HIDDEN%"=="1" goto :run

set HIDDEN=1
wscript.exe "%BASE%\scripts\windows\hidden.vbs" cmd /c "%BASE%\scripts\windows\serve_npu.bat"
endlocal & exit /b 0

:run
"%GENIEX%" --data-dir "%DATA%" --skip-update serve --host 0.0.0.0:18181 --keepalive 999999 > "%BASE%\logs\geniex_server.log" 2>&1
endlocal