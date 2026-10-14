#!/bin/sh
# Build KlipperMac.app — works with Command Line Tools only (no Xcode needed).
# Compiles every Swift source with swiftc, produces a universal (arm64 +
# x86_64) binary, assembles the .app bundle, ad-hoc signs it.
# Also builds the KlipperCam camera helper and installs the signed binary in
# the bundle's Resources and in ~/.klipperformac/bin (idempotent).
# Output: dist/Klipper for Mac.app
set -e
cd "$(dirname "$0")"

APP="dist/Klipper for Mac.app"
SDK=$(xcrun --sdk macosx --show-sdk-path)
SRCS=$(find Sources -name '*.swift' -not -path 'Sources/KlipperCam/*' | sort)
CAM_SRCS=$(find Sources/KlipperCam -name '*.swift' | sort)
CAM_PLIST="$PWD/Resources/KlipperCam-Info.plist"
FLAGS="-parse-as-library -O -sdk $SDK"
CAM_FLAGS="-O -sdk $SDK"

mkdir -p dist

build_arch() {
    xcrun swiftc $FLAGS -target "$1-apple-macos13.0" $SRCS -o "dist/KlipperMac-$1"
}

# KlipperCam is a bare CLI, so its Info.plist goes into __TEXT for TCC.
build_cam_arch() {
    xcrun swiftc $CAM_FLAGS -target "$1-apple-macos13.0" $CAM_SRCS \
        -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist \
        -Xlinker "$CAM_PLIST" -o "dist/KlipperCam-$1"
}

build_cam() {
    # SwiftPM needs a full Xcode install; Command Line Tools alone cannot run
    # it, so fall back to the swiftc path above.
    if swift build -c release --product KlipperCam >/dev/null 2>&1; then
        echo "    via swift build"
        cp .build/release/KlipperCam dist/KlipperCam-universal
        return 0
    fi
    echo "    via swiftc (no Xcode toolchain for SwiftPM)"
    if build_cam_arch arm64 && build_cam_arch x86_64; then
        lipo -create dist/KlipperCam-arm64 dist/KlipperCam-x86_64 \
            -output dist/KlipperCam-universal
    else
        build_cam_arch "$(uname -m)"
        cp "dist/KlipperCam-$(uname -m)" dist/KlipperCam-universal
    fi
    rm -f dist/KlipperCam-arm64 dist/KlipperCam-x86_64
}

install_cam() {
    # Rename over an old copy: a running helper must not block the install.
    mkdir -p "$(dirname "$1")"
    cp dist/KlipperCam-universal "$1.tmp"
    mv -f "$1.tmp" "$1"
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

echo "==> building KlipperCam"
build_cam
codesign --force --sign - --identifier com.klipperformac.klippercam \
    dist/KlipperCam-universal
install_cam "$APP/Contents/Resources/KlipperCam"
install_cam "$HOME/.klipperformac/bin/KlipperCam"
echo "    helper: $HOME/.klipperformac/bin/KlipperCam"

echo "==> ad-hoc codesign"
codesign --force --sign - "$APP"

if [ -n "${KLIPPERFORMAC_ZIP:-}" ]; then
    echo "==> zipping"
    ZIP="dist/Klipper-for-Mac.zip"
    rm -f "$ZIP"
    ditto -c -k --keepParent "$APP" "$ZIP"
    echo "    zip: $ZIP"
fi

echo "done: $APP  (open it with: open $APP)"
