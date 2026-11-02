#!/bin/sh
# Build bridgekeeper. Zero dependencies, so this works fully
# offline: cargo never needs the network. Runs the unit tests
# first -- they are the gate for the hand-rolled JSON surface.
set -e
cd "$(dirname "$0")"
export PATH="$HOME/.cargo/bin:$PATH"
cargo test --release
cargo build --release
echo "built: $(pwd)/target/release/bridgekeeper"
ls -la target/release/bridgekeeper
