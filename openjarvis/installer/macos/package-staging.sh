#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$ROOT/../staging-out}"
APP_NAME="Thozha Setup.app"
VERSION="0.1.0"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This package script requires macOS; no installation changes were made." >&2
  exit 2
fi

[[ "$OUT" = /* ]] || OUT="$PWD/$OUT"
OUT_PARENT="$(dirname "$OUT")"
OUT_NAME="$(basename "$OUT")"
if [[ ! -d "$OUT_PARENT" || -L "$OUT_PARENT" ]]; then
  echo "The staging parent must already exist and must not be a symlink: $OUT_PARENT" >&2
  exit 4
fi
OUT_PARENT="$(cd "$OUT_PARENT" && pwd -P)"
OUT="$OUT_PARENT/$OUT_NAME"
if [[ -e "$OUT" || -L "$OUT" ]]; then
  echo "Refusing to reuse an existing output path. Choose a new, empty staging directory: $OUT" >&2
  exit 4
fi
if ! mkdir -m 700 "$OUT"; then
  echo "Could not create a fresh private staging directory: $OUT" >&2
  exit 4
fi

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/thozha-installer.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/root/Applications/$APP_NAME/Contents/MacOS"
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
APP_TMP="$OUT/.Thozha Setup.app.tmp-$$"
PKG_TMP="$STAGE/ThozhaSetup-$VERSION-unsigned.pkg"
APP_FINAL="$OUT/$APP_NAME"
PKG_FINAL="$OUT/ThozhaSetup-$VERSION-unsigned.pkg"
ditto --norsrc --noextattr --noqtn --noacl "$STAGE/root/Applications/$APP_NAME" "$APP_TMP"
if [[ -e "$APP_FINAL" || -L "$APP_FINAL" ]]; then
  echo "Refusing to replace existing app bundle: $APP_FINAL" >&2
  exit 5
fi
mv -n "$APP_TMP" "$APP_FINAL"
if [[ -e "$APP_TMP" ]]; then
  echo "App bundle could not be placed without overwriting an existing target." >&2
  exit 5
fi
# ditto may emit AppleDouble sidecars on some filesystems. Strip these before
# creating the component package, then verify that the real app signature holds.
find "$APP_FINAL" -name '._*' -delete
if find "$APP_FINAL" -name '._*' -print -quit | grep -q .; then
  echo "AppleDouble metadata remains in the app bundle; refusing to package it." >&2
  exit 6
fi
codesign --verify --deep --strict "$APP_FINAL"
# macOS may attach OS-managed provenance xattrs; pkgbuild can serialize them as
# AppleDouble entries. If that happens, the script refuses the package below.
pkgbuild --component "$APP_FINAL" --identifier com.thozha.setup.staging --version "$VERSION" \
  --install-location /Applications "$PKG_TMP" >/dev/null
if pkgutil --payload-files "$PKG_TMP" | grep -q '\._'; then
  echo "pkgbuild included AppleDouble metadata entries; refusing to publish this package." >&2
  exit 6
fi
EXPANDED="$STAGE/expanded-package"
pkgutil --expand-full "$PKG_TMP" "$EXPANDED"
codesign --verify --deep --strict "$EXPANDED/Payload/$APP_NAME"
if [[ -e "$PKG_FINAL" || -L "$PKG_FINAL" ]]; then
  echo "Refusing to replace existing package: $PKG_FINAL" >&2
  exit 5
fi
mv -n "$PKG_TMP" "$PKG_FINAL"
if [[ -e "$PKG_TMP" ]]; then
  echo "Package could not be placed without overwriting an existing target." >&2
  exit 5
fi
echo "Created local staging artifacts (unsigned for distribution):"
echo "$APP_FINAL"
echo "$PKG_FINAL"
echo "The package contains only the setup app. It does not install or alter OpenJarvis, LaunchAgents, VM images, credentials, or network settings."
