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
FLAGS="-parse-as-library -O -sdk $SDK -Xlinker -rpath -Xlinker @executable_path/../Frameworks"
CAM_FLAGS="-O -sdk $SDK"

mkdir -p dist

target_for_arch() {
    # Per-arch floors: Intel can run macOS 10.15; Apple Silicon cannot, so the
    # arm64 slice targets 11.0. The bundled CLI runtimes keep matching floors.
    case "$1" in
        x86_64) echo "$1-apple-macos10.15" ;;
        arm64)  echo "$1-apple-macos11.0" ;;
        *)      echo "$1-apple-macos11.0" ;;
    esac
}

build_arch() {
    xcrun swiftc $FLAGS -warnings-as-errors -target "$(target_for_arch "$1")" \
        $SRCS -o "dist/KlipperMac-$1"
}

# KlipperCam is a bare CLI, so its Info.plist goes into __TEXT for TCC.
build_cam_arch() {
    xcrun swiftc $CAM_FLAGS -target "$(target_for_arch "$1")" $CAM_SRCS \
        -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist \
        -Xlinker "$CAM_PLIST" -o "dist/KlipperCam-$1"
}

build_cam() {
    # Always compile both slices directly: SwiftPM produces only the host
    # architecture, which would strand the camera feature on the other CPU.
    echo "    via swiftc (universal, no SwiftPM toolchain required)"
    if build_cam_arch arm64 && build_cam_arch x86_64; then
        lipo -create dist/KlipperCam-arm64 dist/KlipperCam-x86_64 \
            -output dist/KlipperCam-universal
    else
        echo "    universal camera helper build failed; keeping host architecture"
        HOST=$(uname -m)
        build_cam_arch "$HOST"
        cp "dist/KlipperCam-$HOST" dist/KlipperCam-universal
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

echo "==> embedding Swift Concurrency runtime (Catalina/10.15 back-deploy)"
FRAMEWORKS="$APP/Contents/Frameworks"
mkdir -p "$FRAMEWORKS"
SWIFTC=$(xcrun -f swiftc)
CONC_LIB="$(dirname "$(dirname "$SWIFTC")")/lib/swift/macosx/libswift_Concurrency.dylib"
if [ ! -f "$CONC_LIB" ]; then
    # Command Line Tools layout (no full Xcode): legacy concurrency lib location
    CONC_LIB=/Library/Developer/CommandLineTools/usr/lib/swift-5.5/macosx/libswift_Concurrency.dylib
fi
if [ -f "$CONC_LIB" ]; then
    cp "$CONC_LIB" "$FRAMEWORKS/libswift_Concurrency.dylib"
    codesign --force --sign - "$FRAMEWORKS/libswift_Concurrency.dylib"
else
    echo "WARNING: libswift_Concurrency.dylib not found in toolchain; Catalina will still crash" >&2
fi

echo "==> building KlipperCam"
build_cam
codesign --force --sign - --identifier com.klipperformac.klippercam \
    dist/KlipperCam-universal
install_cam "$APP/Contents/Resources/KlipperCam"
echo "    helper bundled inside app Resources"

echo "==> bundling CLI (per-arch CPython + sources)"
APP_ABS="$PWD/$APP"
cd ..
RUNTIMES=.build/pythons
if [ ! -x "$RUNTIMES/cpython-3.12-macos-x86_64-none/bin/python3.12" ] ||
   [ ! -x "$RUNTIMES/cpython-3.12-macos-aarch64-none/bin/python3.12" ]; then
    ./scripts/fetch-python-runtimes.sh
fi
./scripts/bundle-cli.sh "$APP_ABS"
cd KlipperMac

echo "==> ad-hoc codesign"
codesign --force --sign - "$APP"

if [ -n "${KLIPPERFORMAC_ZIP:-}" ]; then
    echo "==> zipping"
    ZIP="dist/Klipper-for-Mac.zip"
    rm -f "$ZIP"
    ditto --norsrc -c -k --keepParent "$APP" "$ZIP"
    echo "    zip: $ZIP"
fi

echo "done: $APP  (open it with: open $APP)"
