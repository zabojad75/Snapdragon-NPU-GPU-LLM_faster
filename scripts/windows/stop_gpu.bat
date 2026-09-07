@echo off
REM Stop the GPU llama-server
taskkill /F /IM llama-server.exe >nul 2>&1
echo llama-server stopped.