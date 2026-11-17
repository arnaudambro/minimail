#!/bin/bash
# Build Minimail.app (with its notification agent) and install it in ~/Applications.
set -euo pipefail
cd "$(dirname "$0")"
swift build -c release
BIN="$(swift build -c release --show-bin-path)"
if [ -d Resources/AppIcon.iconset ]; then
  iconutil -c icns Resources/AppIcon.iconset -o Resources/AppIcon.icns
fi

plist() { # name id executable extra
cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$1</string>
  <key>CFBundleDisplayName</key><string>$1</string>
  <key>CFBundleIdentifier</key><string>$2</string>
  <key>CFBundleExecutable</key><string>$3</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.2.0</string>
  <key>CFBundleVersion</key><string>2</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
  $4
</dict>
</plist>
PLIST
}

APP="build/Minimail.app"
AGENT="$APP/Contents/Library/LoginItems/MinimailAgent.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$AGENT/Contents/MacOS" "$AGENT/Contents/Resources"
cp "$BIN/Minimail" "$APP/Contents/MacOS/Minimail"
cp "$BIN/MinimailAgent" "$AGENT/Contents/MacOS/MinimailAgent"
cp Resources/AppIcon.icns "$APP/Contents/Resources/"
cp Resources/AppIcon.icns "$AGENT/Contents/Resources/"
plist Minimail dev.minimail.app Minimail '<key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
  <key>CFBundleURLTypes</key><array><dict><key>CFBundleURLName</key><string>dev.minimail.app</string><key>CFBundleURLSchemes</key><array><string>minimail</string></array></dict></array>' > "$APP/Contents/Info.plist"
plist Minimail dev.minimail.agent MinimailAgent '<key>LSUIElement</key><true/>' > "$AGENT/Contents/Info.plist"
codesign --force -s - "$AGENT"
codesign --force -s - "$APP"

if [ "${1:-}" != "--no-install" ]; then
  DEST="$HOME/Applications/Minimail.app"
  mkdir -p "$HOME/Applications"
  rm -rf "$DEST"
  cp -R "$APP" "$HOME/Applications/"
  LSREG=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
  "$LSREG" -f "$DEST" "$DEST/Contents/Library/LoginItems/MinimailAgent.app" || true
  echo "Installed $DEST"
fi
