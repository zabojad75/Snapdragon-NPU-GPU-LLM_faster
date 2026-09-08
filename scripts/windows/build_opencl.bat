@echo off
REM Build llama.cpp with the Adreno OpenCL backend for Windows ARM64.
REM Prerequisites (one-time):
REM   - VS 2022 Build Tools with ARM64 tools (link.exe for arm64 via vcvarsall arm64)
REM   - LLVM (clang-cl), CMake, Git  ->  C:\Program Files\...
REM   - Qualcomm OpenCL SDK 2.3.2     ->  C:\Qualcomm\OpenCL_SDK\2.3.2
REM   - llama.cpp source             ->  %USERPROFILE%\llmnpu\llama.cpp
REM Produces %USERPROFILE%\llmnpu\pkg-opencl\bin\llama-server.exe
setlocal
set BASE=%USERPROFILE%\llmnpu
cd /d "%BASE%\llama.cpp"
if not exist CMakeUserPresets.json copy docs\backend\snapdragon\CMakeUserPresets.json .

call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat" arm64 >nul 2>&1

set "PATH=C:\Program Files\LLVM\bin;C:\Program Files\CMake\bin;C:\Program Files\Git\bin;%PATH%"

set OPENCL_SDK_ROOT=C:\Qualcomm\OpenCL_SDK\2.3.2
set HEXAGON_SDK_ROOT=
set HEXAGON_TOOLS_ROOT=
set CMAKE_PREFIX_PATH=C:\Qualcomm\OpenCL_SDK\2.3.2

where cl
cmake --preset arm64-windows-snapdragon-release -B build-opencl -D GGML_HEXAGON=OFF -D LLAMA_BUILD_UI=OFF -D "CMAKE_C_STANDARD_LIBRARIES=-lmsvcrt -lucrt -loldnames -lkernel32 -luser32 -lgdi32 -lwinspool -lshell32 -lole32 -loleaut32 -luuid -lcomdlg32 -ladvapi32" -D "CMAKE_CXX_STANDARD_LIBRARIES=-lmsvcrt -lucrt -loldnames -lkernel32 -luser32 -lgdi32 -lwinspool -lshell32 -lole32 -loleaut32 -luuid -lcomdlg32 -ladvapi32"
if errorlevel 1 ( echo CONFIGURE_FAILED & exit /b 1 )
cmake --build build-opencl --config Release --parallel
if errorlevel 1 ( echo BUILD_FAILED & exit /b 1 )
cmake --install build-opencl --prefix "%BASE%\pkg-opencl"
if errorlevel 1 ( echo INSTALL_FAILED & exit /b 1 )
echo BUILD_OK - binaries in %BASE%\pkg-opencl\bin