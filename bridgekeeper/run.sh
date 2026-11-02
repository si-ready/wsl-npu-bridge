#!/bin/sh
# Start bridgekeeper in the foreground (default 127.0.0.1:14112).
# Optional env: BRIDGEKEEPER_BIND, BRIDGEKEEPER_LEMOND,
# BRIDGEKEEPER_FLM -- see README.md. It is a foreground process
# with no state to flush; run it under any supervisor (systemd,
# a scheduled task, tmux) and stop it with SIGTERM/SIGINT.
cd "$(dirname "$0")"
if [ ! -x target/release/bridgekeeper ]; then
    echo "binary not built yet; running build.sh first" >&2
    ./build.sh
fi
exec ./target/release/bridgekeeper
