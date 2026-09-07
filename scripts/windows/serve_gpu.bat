@echo off
REM Serve a GGUF model on Adreno GPU via llama-server (OpenAI-compatible API on :8081)
REM Usage: serve_gpu.bat [model-key] [ctx-size]
REM   model-key: 30b | 20b (default) | 8b | 12b | 1.5b
REM   ctx-size:  default 16384
setlocal
set BASE=%USERPROFILE%\llmnpu
set PATH=%BASE%\pkg-opencl\bin;%PATH%
set MODELS=%BASE%\models
set PORT=8081
set CTX=%2
if "%CTX%"=="" set CTX=16384

set MODEL=
set ALIAS=
if /I "%1"=="20b"  set MODEL=%MODELS%\gpt-oss-20b-Q4_0.gguf& set ALIAS=gpt-oss-20b
if /I "%1"=="8b"   set MODEL=%MODELS%\qwen3-8b-Q5_0.gguf& set ALIAS=qwen3-8b
if /I "%1"=="12b"  set MODEL=%MODELS%\mistral-nemo-12b-Q4_0.gguf& set ALIAS=mistral-nemo-12b
if /I "%1"=="1.5b" set MODEL=%MODELS%\qwen2.5-coder-1.5b-Q4_0.gguf& set ALIAS=qwen2.5-coder-1.5b
if /I "%1"=="30b"  set MODEL=%MODELS%\qwen3-coder-30b-Q4_0.gguf& set ALIAS=qwen3-coder-30b
if "%MODEL%"=="" set MODEL=%MODELS%\gpt-oss-20b-Q4_0.gguf& set ALIAS=gpt-oss-20b

REM Stop an existing instance first
taskkill /F /IM llama-server.exe >nul 2>&1
ping -n 3 127.0.0.1 >nul

echo Serving %MODEL% on GPU, ctx=%CTX%, port %PORT%
if not exist "%BASE%\logs" mkdir "%BASE%\logs"
start "llama-server-gpu" /min cmd /c "llama-server -m "%MODEL%" --alias %ALIAS% -ngl 99 -c %CTX% --host 0.0.0.0 --port %PORT% --no-webui -fa on > "%BASE%\logs\llama_server.log" 2>&1"
ping -n 3 127.0.0.1 >nul
echo Started. Log: %BASE%\logs\llama_server.log
endlocal