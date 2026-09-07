@echo off
REM Start GenieX NPU server (Hexagon NPU models, OpenAI-compatible API on :18181)
REM --keepalive 999999 : geniex defaults to a 300s idle timeout, which silently
REM kills the NPU server after 5 quiet minutes. Effectively disable it.
setlocal
set BASE=%USERPROFILE%\llmnpu
set GENIEX=%BASE%\geniex\engines\geniex\geniex.exe
set DATA=%BASE%\geniex\models\geniex
if not exist "%BASE%\logs" mkdir "%BASE%\logs"
start "geniex-npu" /min cmd /c ""%GENIEX%" --data-dir "%DATA%" --skip-update serve --host 0.0.0.0:18181 --keepalive 999999 > "%BASE%\logs\geniex_server.log" 2>&1"
echo GenieX server starting on http://0.0.0.0:18181 (log: %BASE%\logs\geniex_server.log)
endlocal