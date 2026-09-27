#!/usr/bin/env bash
# Build PeachyHead.app (AirPods head motion -> JSON lines) into .run/airpods/.
# A real, signed .app bundle so macOS can ask for the Motion permission.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
APP="$REPO/.run/airpods/PeachyHead.app"
BIN="$APP/Contents/MacOS/PeachyHead"

mkdir -p "$APP/Contents/MacOS"
cp "$HERE/Info.plist" "$APP/Contents/Info.plist"
swiftc -O -target "$(uname -m)-apple-macos14.0" \
  -framework CoreMotion \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$HERE/Info.plist" \
  "$HERE/main.swift" -o "$BIN"
codesign --force --sign - --identifier local.peachy.head "$APP" 2>/dev/null
echo "$BIN"
