#!/bin/bash
# llmnpu Panel — one-page control for the whole local AI stack.
# Serves the panel on 127.0.0.1:8188 (toolbar + embedded Open WebUI).
ROOT="${LLMNPU_ROOT:-$HOME/llmnpu}"
VENV="${LLMNPU_VENV:-$HOME/openwebui-venv}"

pkill -f "panel[.]py" 2>/dev/null
sleep 1
mkdir -p "$ROOT/logs"
(setsid nohup "$VENV/bin/python" "$ROOT/panel/panel.py" > "$ROOT/logs/panel.log" 2>&1 &)
sleep 3
curl -s -o /dev/null --max-time 4 -w "panel: %{http_code}\n" http://127.0.0.1:8188/ || echo "panel: FAILED"