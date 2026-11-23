#!/bin/bash
# Keyboard and composer geometry check for Jarvis Home.
# Usage: scripts/ui/keyboard-check.sh <adb-serial>
# Opens the keyboard from the Home composer, verifies the composer and Send button sit above the IME's top edge,
# types a long message (not sent), closes the keyboard, and verifies the layout returns. Never sends a request.
# The serial is mandatory: this script never runs an unqualified adb command.
set -euo pipefail
SERIAL="${1:?usage: keyboard-check.sh <adb-serial>}"
PKG="com.jarvis.companion.debug"
OUT="${TMPDIR:-/tmp}/jarvis-keyboard-check-$(date +%s)"
mkdir -p "$OUT"
adb_s() { adb -s "$SERIAL" "$@"; }

dump_ui() { adb_s shell uiautomator dump /sdcard/jarvis-ui.xml >/dev/null 2>&1; adb_s shell cat /sdcard/jarvis-ui.xml | tr '>' '\n'; }
bounds_of() { # $1 = dump, $2 = content-desc text
  echo "$1" | grep -F "$2" | grep -oE 'bounds="\[[0-9]+,[0-9]+\]\[[0-9]+,[0-9]+\]"' | head -1 | grep -oE '[0-9]+' | tr '\n' ' '
}
ime_top() { # IME source frame top from the window manager (the keyboard's visible top edge)
  adb_s shell dumpsys window 2>/dev/null | grep -oE 'InsetsSource id=[0-9]+ type=ime frame=\[[0-9]+,[0-9]+\]' | head -1 | grep -oE '\[[0-9]+,[0-9]+\]' | tail -1 | tr -d '[]' | cut -d, -f2
}
fail() { echo "FAIL: $1"; echo "artifacts: $OUT"; exit 1; }

adb_s shell am start -n "$PKG/com.jarvis.companion.MainActivity" >/dev/null
sleep 4
X=$(dump_ui)
read -r cx1 cy1 cx2 cy2 <<< "$(bounds_of "$X" 'Message to Jarvis')"
[ -n "${cy1:-}" ] || fail "composer not found on Home"
echo "composer at rest: $cx1,$cy1 - $cx2,$cy2"
adb_s shell input tap $(( (cx1 + cx2) / 2 )) $(( (cy1 + cy2) / 2 ))
sleep 3
adb_s exec-out screencap -p > "$OUT/keyboard-open.png"
X=$(dump_ui)
read -r cx1 cy1 cx2 cy2 <<< "$(bounds_of "$X" 'Message to Jarvis')"
read -r sx1 sy1 sx2 sy2 <<< "$(bounds_of "$X" 'Send to Jarvis')"
TOP=$(ime_top)
[ -n "$TOP" ] || fail "keyboard frame not reported (is the keyboard open?)"
echo "keyboard top: $TOP  composer bottom: $cy2  send bottom: ${sy2:-?}"
[ "$cy2" -le "$TOP" ] || fail "composer is hidden behind the keyboard"
[ -n "${sy2:-}" ] && { [ "$sy2" -le "$TOP" ] || fail "Send button is hidden behind the keyboard"; }

# Long message: typed only, never sent.
adb_s shell input text 'Jarvis%sshould%sstay%susable%swhen%sthe%smessage%sis%slong%sand%swraps%sacross%smultiple%slines%sso%sthe%scomposer%sand%ssend%sbutton%sremain%svisible%sabove%sthe%skeyboard.'
sleep 2
X=$(dump_ui)
read -r cx1 cy1 cx2 cy2 <<< "$(bounds_of "$X" 'Message to Jarvis')"
TOP=$(ime_top)
echo "after long text: composer bottom $cy2, keyboard top $TOP"
[ "$cy2" -le "$TOP" ] || fail "long text pushed the composer behind the keyboard"
adb_s exec-out screencap -p > "$OUT/keyboard-long-text.png"

# Close the keyboard with Back (the keyboard consumes it; the app stays open).
adb_s shell input keyevent KEYCODE_BACK
sleep 2
adb_s exec-out screencap -p > "$OUT/keyboard-closed.png"
X=$(dump_ui)
if echo "$X" | grep -qF 'Message to Jarvis'; then echo "keyboard closed; composer still present"; else fail "composer disappeared after closing the keyboard"; fi
adb_s shell dumpsys activity activities | grep -q "$PKG" || fail "app left the foreground"
echo "PASS keyboard-check on $SERIAL (artifacts: $OUT)"
