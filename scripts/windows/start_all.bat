@echo off
REM Start the full AI stack: GPU server + NPU server + Open WebUI + Panel + browser
REM Assumes WSL distro is installed and the repo lives at %USERPROFILE%\llmnpu
setlocal
set BASE=%USERPROFILE%\llmnpu

REM WSL: Open WebUI venv + panel (cd $HOME first: webui writes a secret key to CWD)
wsl -e bash -c "cd ~ && LLMNPU_ROOT=~/llmnpu bash ~/llmnpu/scripts/wsl/openwebui.sh && LLMNPU_ROOT=~/llmnpu bash ~/llmnpu/scripts/wsl/start_panel.sh"
ping -n 22 127.0.0.1 >nul

REM Windows: GPU + NPU servers (they relaunch themselves windowless via hidden.vbs)
cmd /c "%BASE%\scripts\windows\serve_gpu.bat" 30b
ping -n 3 127.0.0.1 >nul
cmd /c "%BASE%\scripts\windows\serve_npu.bat"
ping -n 12 127.0.0.1 >nul

start http://localhost:8188/panel
echo Stack starting: panel http://localhost:8188/panel (webui :3000, GPU :8081, NPU :18181)
endlocal