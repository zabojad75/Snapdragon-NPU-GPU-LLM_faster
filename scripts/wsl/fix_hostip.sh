#!/bin/bash
# Keep the opencode config pointed at the Windows host.
# In NAT mode the WSL gateway IP changes across reboots; this rewrites the
# baseURL of the llamacpp (8081) and geniex-npu (18181) providers to match.
GW=$(ip route show default | awk '{print $3}')
[ -z "$GW" ] && exit 1
CFG="${OPENCODE_CONFIG:-$HOME/.config/opencode/opencode.jsonc}"
[ -f "$CFG" ] || { echo "no opencode config at $CFG (install opencode first)"; exit 0; }
if grep -qE "http://[0-9.]+:(8081|18181)/v1" "$CFG" && ! grep -q "http://$GW:8081/v1" "$CFG"; then
    sed -i -E "s|http://[0-9.]+:(8081\|18181)/v1|http://$GW:\1/v1|g" "$CFG"
    echo "opencode config -> $GW:8081 + $GW:18181"
fi