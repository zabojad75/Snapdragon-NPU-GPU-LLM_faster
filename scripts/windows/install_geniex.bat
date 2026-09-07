@echo off
REM Download + install GenieX CLI (Qualcomm, BSD-3-Clause) for Windows ARM64.
REM Pinned to v0.6.1; adjust GENIEX_VER if a newer release is out.
REM Installs to %USERPROFILE%\llmnpu\geniex
setlocal
set GENIEX_VER=0.6.1
set BASE=%USERPROFILE%\llmnpu
set URL=https://github.com/qualcomm/GenieX/releases/download/v%GENIEX_VER%/geniex-cli-setup-windows-arm64-v%GENIEX_VER%.exe
set OUT=%TEMP%\geniex-cli-setup-windows-arm64-v%GENIEX_VER%.exe

if exist "%BASE%\geniex\engines\geniex\geniex.exe" (
    echo GenieX already installed at %BASE%\geniex - skipping.
    endlocal & exit /b 0
)

echo Downloading GenieX installer v%GENIEX_VER% (~65 MB)...
curl -L --fail --retry 5 --retry-delay 3 -o "%OUT%" "%URL%"
if errorlevel 1 ( echo DOWNLOAD_FAILED & endlocal & exit /b 1 )

echo Running installer (follow its prompts; install into %BASE%\geniex)...
"%OUT%"
if errorlevel 1 ( echo INSTALLER_FAILED & endlocal & exit /b 1 )

if exist "%BASE%\geniex\engines\geniex\geniex.exe" (
    echo GenieX installed OK.
) else (
    echo NOTE: installer may have used a different location. If so, move the
    echo       geniex folder to %BASE%\geniex or set LLMNPU_WIN_ROOT.
)
endlocal