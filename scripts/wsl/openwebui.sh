#!/bin/bash
# Start Open WebUI (offline-capable chat UI) -> llama-server (Adreno GPU) + GenieX NPU
# NAT mode: WSL reaches Windows ONLY via the gateway IP (never localhost).
# Paths auto-detect; override with LLMNPU_ROOT / LLMNPU_VENV if non-standard.
ROOT="${LLMNPU_ROOT:-$HOME/llmnpu}"
VENV="${LLMNPU_VENV:-$HOME/openwebui-venv}"
DATA_DIR="${LLMNPU_DATA:-$HOME/openwebui-data}"

HOSTIP=$(ip route show default 2>/dev/null | awk '{print $3}')
if [ -z "$HOSTIP" ]; then
    echo "ERROR: no default route / gateway found"; exit 1
fi
BASE="http://$HOSTIP:8081/v1"
export OPENAI_API_BASE_URL="$BASE"
export OPENAI_API_KEY="sk-dummy"
export WEBUI_AUTH=True
export ENABLE_WEB_SEARCH=True
export WEB_SEARCH_ENGINE=duckduckgo
export WEB_LOADER_ENGINE=safe_web
export DATA_DIR="$DATA_DIR"
mkdir -p "$DATA_DIR" "$ROOT/logs"

# Self-heal: patch persisted DB connections to current gateway IP (NAT mode).
# The gateway IP changes across reboots; Open WebUI persists connections in
# its sqlite DB, so we re-point them at the fresh gateway on every start.
python3 - << PYEOF
import sqlite3, json, re
gw = "$HOSTIP"
db_path = "$DATA_DIR/webui.db"
try:
    db = sqlite3.connect(db_path)
    key = 'openai.api_base_urls'
    row = db.execute('SELECT value FROM config WHERE key=?', (key,)).fetchone()
    if row:
        urls = json.loads(row[0])
        # rebuild each URL cleanly: keep the port, replace host with gateway
        def fix(u):
            m = re.match(r'http://[^:/]+:(8081|18181)/v1', u)
            return f'http://{gw}:{m.group(1)}/v1' if m else u
        patched = [fix(u) for u in urls]
        if patched != urls:
            db.execute('UPDATE config SET value=? WHERE key=?', (json.dumps(patched), key))
            db.commit()
            print(f'webui DB connections -> {patched}')
except Exception as e:
    print('DB patch skipped:', e)
PYEOF

# Open WebUI writes .webui_secret_key to the CWD; pin both so it never
# depends on where this script was launched from (e.g. C:\Windows via wsl -e)
export WEBUI_SECRET_FILE="$HOME/.webui_secret_key"
cd "$HOME"
(setsid nohup "$VENV/bin/open-webui" serve --port 3000 >> "$ROOT/logs/openwebui.log" 2>&1 &)
echo "Open WebUI starting on http://localhost:3000 (backends: $HOSTIP:8081 + :18181)"