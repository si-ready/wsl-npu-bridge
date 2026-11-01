#!/usr/bin/env bash
# authorize-relay.sh -- one-time WSL-side setup for the bridge relays.
#
# Converges ~/.ssh/authorized_keys to the single canonical line that
# host/relay.ps1 generated (authorized-line.txt in the Windows user's
# %LOCALAPPDATA%\wsl-npu-bridge\): any older lines carrying the bridge
# key are removed first, because sshd honors only the FIRST line that
# matches a key -- permissions must live on one line. The line itself
# is tightly restricted: no shell, no agent/X11/PTY, port-forwarding
# only, and only for the two loopback listeners the relays need.
#
#   bash wsl/authorize-relay.sh     (idempotent; safe to re-run)
set -euo pipefail

LINE_FILE=""
for candidate in /mnt/c/Users/*/AppData/Local/wsl-npu-bridge/authorized-line.txt; do
    [ -f "$candidate" ] && LINE_FILE="$candidate" && break
done
if [ -z "$LINE_FILE" ]; then
    echo "authorized-line.txt not found under any Windows profile." >&2
    echo "Run host\\relay.ps1 (or host\\serve.ps1) on Windows first --" >&2
    echo "it generates the key and writes that file." >&2
    exit 1
fi

LINE="$(tr -d '\r' < "$LINE_FILE")"
BLOB="$(echo "$LINE" | awk '{print $3}')"   # fields: options, type, blob, comment

mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys

if grep -qxF "$LINE" ~/.ssh/authorized_keys; then
    echo "bridge authorization already converged -- nothing to do."
    exit 0
fi

grep -vF "$BLOB" ~/.ssh/authorized_keys > ~/.ssh/authorized_keys.new || true
mv ~/.ssh/authorized_keys.new ~/.ssh/authorized_keys
echo "$LINE" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
echo "bridge key authorized (one line, restricted to the relay forwards)."
