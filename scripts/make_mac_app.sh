#!/usr/bin/env bash
# Build "dist/YT Downloader.app": a small launcher that runs ./run-mac.sh from this folder.
# Move it to /Applications (or the Dock) yourself. Keep this folder where it is: the app points here.
set -e
cd "$(dirname "$0")/.."
REPO="$(pwd)"
APP="dist/YT Downloader.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>YT Downloader</string>
  <key>CFBundleDisplayName</key><string>YT Downloader</string>
  <key>CFBundleIdentifier</key><string>local.ytdownloader</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>YT Downloader</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/YT Downloader" <<LAUNCH
#!/usr/bin/env bash
# Started from Finder: no terminal, so failures are shown in a dialog and logged next to the library.
cd "$REPO" || { osascript -e 'display alert "YT Downloader" message "The app folder was moved: build the app again with scripts/make_mac_app.sh."'; exit 1; }
mkdir -p data
./run-mac.sh >> data/mac-app.log 2>&1 || osascript -e 'display alert "YT Downloader could not start" message "See data/mac-app.log in the app folder."'
LAUNCH
chmod +x "$APP/Contents/MacOS/YT Downloader"

# Icon: the red download badge from the web page, drawn at every size macOS needs.
ICONSET="$(mktemp -d)/icon.iconset"; mkdir -p "$ICONSET"
SVG="$(mktemp -d)/icon.svg"
cat > "$SVG" <<'SVGEOF'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024"><rect x="64" y="64" width="896" height="896" rx="200" fill="#d93a32"/><path d="M512 260v380m0 0-170-170m170 170 170-170M330 780h364" fill="none" stroke="#fff" stroke-width="80" stroke-linecap="round" stroke-linejoin="round"/></svg>
SVGEOF
if qlmanage -t -s 1024 -o "$(dirname "$SVG")" "$SVG" >/dev/null 2>&1 && [ -f "$SVG.png" ]; then
  for s in 16 32 128 256 512; do
    sips -z $s $s "$SVG.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
    sips -z $((s*2)) $((s*2)) "$SVG.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/icon.icns" || echo "(no icon: iconutil failed)"
else
  echo "(no icon: couldn't render it)"
fi
echo "Built $APP — move it to /Applications if you like. It starts the app from $REPO."
