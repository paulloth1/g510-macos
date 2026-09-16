#!/bin/sh
# Install the G510 tools into the current user's account.
#
#   ~/.local/share/g510   this source tree plus its virtualenv
#   ~/.local/bin/g510     command line entry point
#   ~/Applications        G510.app, the menu bar and window UI
#
# Nothing is installed system-wide and no kernel extension is involved.
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"
BUNDLE="$HOME/Applications/G510.app"

echo "==> hidapi"
if ! brew list hidapi >/dev/null 2>&1; then
    brew install hidapi
fi

echo "==> nowplaying-cli (optional, for the Now Playing screen)"
if ! command -v nowplaying-cli >/dev/null 2>&1; then
    brew install nowplaying-cli || echo "    skipped; that screen will fall back"
fi

echo "==> virtualenv"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" -q install -r "$APP_DIR/requirements.txt"

echo "==> command line"
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/g510" <<WRAPPER
#!/bin/sh
APP="$APP_DIR"
exec "\$APP/venv/bin/python" "\$APP/cli.py" "\$@"
WRAPPER
chmod +x "$BIN_DIR/g510"

echo "==> app bundle"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"
cat > "$BUNDLE/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>G510</string>
  <key>CFBundleDisplayName</key><string>G510</string>
  <key>CFBundleIdentifier</key><string>com.g510.app</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>G510</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
</dict>
</plist>
PLIST
cat > "$BUNDLE/Contents/MacOS/G510" <<LAUNCHER
#!/bin/sh
APP="$APP_DIR"
exec "\$APP/venv/bin/python" "\$APP/gui.py"
LAUNCHER
chmod +x "$BUNDLE/Contents/MacOS/G510"
codesign --force --deep -s - "$BUNDLE" >/dev/null 2>&1 || true

echo
echo "Installed."
echo "  g510 --help        what it can do"
echo "  g510 start         run the background agent, and at login"
echo "  open $BUNDLE"
echo
echo "Grant Input Monitoring (to read G-keys) and, for keys/text bindings,"
echo "Accessibility. Check with: g510 permissions"
