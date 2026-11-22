#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$ROOT/../staging-out}"
APP_NAME="Thozha Setup.app"
VERSION="0.1.0"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/thozha-installer.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This package script requires macOS; no installation changes were made." >&2
  exit 2
fi

mkdir -p "$OUT" "$STAGE/root/Applications/$APP_NAME/Contents/MacOS"
swift build -c release --package-path "$ROOT"
BIN="$ROOT/.build/release/ThozhaSetup"
[[ -x "$BIN" ]] || { echo "Build did not produce the setup app executable." >&2; exit 3; }
ditto --norsrc --noextattr --noqtn --noacl "$BIN" "$STAGE/root/Applications/$APP_NAME/Contents/MacOS/ThozhaSetup"
cat > "$STAGE/root/Applications/$APP_NAME/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>ThozhaSetup</string>
<key>CFBundleIdentifier</key><string>com.thozha.setup.staging</string>
<key>CFBundleName</key><string>Thozha Setup</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.1.0</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
printf 'APPL????' > "$STAGE/root/Applications/$APP_NAME/Contents/PkgInfo"
chmod 0755 "$STAGE/root/Applications/$APP_NAME/Contents/MacOS/ThozhaSetup"
codesign --force --deep --sign - "$STAGE/root/Applications/$APP_NAME"
# Keep only ordinary bundle files; AppleDouble metadata is not part of the app.
find "$STAGE/root" -name '._*' -delete
mkdir -p "$OUT"
rm -rf "$OUT/$APP_NAME" "$OUT/ThozhaSetup-$VERSION-unsigned.pkg"
pkgbuild --component "$STAGE/root/Applications/$APP_NAME" --identifier com.thozha.setup.staging --version "$VERSION" \
  --install-location /Applications "$OUT/ThozhaSetup-$VERSION-unsigned.pkg" >/dev/null
ditto --norsrc --noextattr --noqtn --noacl "$STAGE/root/Applications/$APP_NAME" "$OUT/$APP_NAME"
find "$OUT/$APP_NAME" -name '._*' -delete
echo "Created local staging artifacts (unsigned for distribution):"
echo "$OUT/$APP_NAME"
echo "$OUT/ThozhaSetup-$VERSION-unsigned.pkg"
echo "The package contains only the setup app. It does not install or alter OpenJarvis, LaunchAgents, VM images, credentials, or network settings."
