#!/bin/bash
# One-time WSL setup: venv + Open WebUI + panel dependencies.
# Run INSIDE WSL (Ubuntu ARM64):  bash setup_wsl.sh
set -e
ROOT="${LLMNPU_ROOT:-$HOME/llmnpu}"
VENV="${LLMNPU_VENV:-$HOME/openwebui-venv}"

mkdir -p "$ROOT/logs"

# --- python + build deps -----------------------------------------------------
if ! command -v python3 >/dev/null; then
    echo "installing python3 + toolchain..."
    sudo apt-get update && sudo apt-get install -y python3 python3-venv curl
fi

# --- venv --------------------------------------------------------------------
if [ ! -d "$VENV" ]; then
    echo "creating venv at $VENV ..."
    python3 -m venv "$VENV"
fi

# --- packages ----------------------------------------------------------------
# open-webui       : chat UI (also brings fastapi/uvicorn/httpx for the panel)
# websockets       : panel's WebSocket bridge (socket.io proxying)
# bcrypt           : password hashing helper (webui auth maintenance)
echo "installing open-webui + panel deps (this downloads ~1 GB of wheels)..."
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install open-webui websockets bcrypt

echo
echo "OK. Next:"
echo "  1) bash $ROOT/scripts/wsl/openwebui.sh    # start webui"
echo "  2) bash $ROOT/scripts/wsl/start_panel.sh  # start panel"
echo "  (or run start_all.bat on the Windows side for everything)"