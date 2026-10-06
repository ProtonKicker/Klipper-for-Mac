#!/bin/sh
# Build KlipperMac.app — works with Command Line Tools only (no Xcode needed).
# Compiles every Swift source with swiftc, produces a universal (arm64 +
# x86_64) binary, assembles the .app bundle, ad-hoc signs it.
# Output: dist/KlipperMac.app
set -e
cd "$(dirname "$0")"

APP="dist/KlipperMac.app"
SDK=$(xcrun --sdk macosx --show-sdk-path)
SRCS=$(find Sources -name '*.swift' | sort)
FLAGS="-parse-as-library -O -sdk $SDK"

mkdir -p dist

build_arch() {
    xcrun swiftc $FLAGS -target "$1-apple-macos13.0" $SRCS -o "dist/KlipperMac-$1"
}

echo "==> compiling"
if build_arch arm64 && build_arch x86_64; then
    LIPO_INPUTS="dist/KlipperMac-arm64 dist/KlipperMac-x86_64"
else
    echo "    multi-arch failed, building host arch only"
    HOST=$(uname -m)
    build_arch "$HOST"
    LIPO_INPUTS="dist/KlipperMac-$HOST"
fi

echo "==> assembling $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
lipo -create $LIPO_INPUTS -output "$APP/Contents/MacOS/KlipperMac"
rm -f dist/KlipperMac-arm64 dist/KlipperMac-x86_64 dist/KlipperMac-"$(uname -m)"
cp Resources/Info.plist "$APP/Contents/Info.plist"
if [ -f Resources/AppIcon.icns ]; then
    cp Resources/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
fi
printf 'APPL????' > "$APP/Contents/PkgInfo"

echo "==> ad-hoc codesign"
codesign --force --sign - "$APP"

echo "done: $APP  (open it with: open $APP)"
