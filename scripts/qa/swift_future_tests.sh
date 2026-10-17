#!/bin/sh
# Parse the future SwiftPM test files even on machines where `swift test`
# itself is blocked by the local Apple toolchain state. This keeps the
# Package.swift-side tests from silently rotting while QA still runs through
# the direct swiftc logic harness.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)

srcs=$(find "$ROOT/KlipperMac/Sources" -name '*.swift' | sort)
tests=$(find "$ROOT/KlipperMac/Tests/KlipperMacTests" -name '*.swift' | sort)

[ -n "$srcs" ]
[ -n "$tests" ]

printf '%s\n%s\n' "$srcs" "$tests" | xargs swiftc -parse
