#!/bin/sh
# Run lightweight Swift logic checks without SwiftPM.
#
# This is the fallback for machines where `swift test` is blocked by a partial
# Command Line Tools install. It compiles a tiny harness directly with `swiftc`
# against the pure Foundation-based app logic we care about most.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
BUILD_DIR=$(mktemp -d "${TMPDIR:-/tmp}/klippermac-swiftlogic.XXXXXX")
trap 'rm -rf "$BUILD_DIR"' EXIT HUP INT TERM

swiftc \
  "$ROOT/KlipperMac/Sources/KlipperMac/Core/AppFeedback.swift" \
  "$ROOT/KlipperMac/Sources/KlipperMac/Core/CameraFeedback.swift" \
  "$ROOT/KlipperMac/Sources/KlipperMac/Core/Environment.swift" \
  "$ROOT/KlipperMac/Sources/KlipperMac/Core/StackReader.swift" \
  "$ROOT/KlipperMac/Sources/KlipperMac/Core/McuSerial.swift" \
  "$ROOT/KlipperMac/Sources/KlipperMac/Core/Moonraker.swift" \
  "$ROOT/KlipperMac/TestsLogic/SwiftLogicChecks.swift" \
  -o "$BUILD_DIR/swift-logic-checks"

"$BUILD_DIR/swift-logic-checks"
