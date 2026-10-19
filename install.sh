#!/usr/bin/env sh
set -eu

APP_DIR="$HOME/.local/share/corne-keymap-editor"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"

mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR"
cp "$(dirname "$0")/corne_keymap_editor.py" "$APP_DIR/corne_keymap_editor.py"
cp "$(dirname "$0")/run.sh" "$APP_DIR/run.sh"
cp "$(dirname "$0")/corne-keymap-editor.svg" "$APP_DIR/corne-keymap-editor.svg"
chmod +x "$APP_DIR/corne_keymap_editor.py"
chmod +x "$APP_DIR/run.sh"
ln -sfn "$APP_DIR/run.sh" "$BIN_DIR/corne-keymap-editor"

cat > "$DESKTOP_DIR/corne-keymap-editor.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Corne Keymap Editor
Comment=Edit ZMK Corne keymaps and combos
Exec=$BIN_DIR/corne-keymap-editor %f
Icon=$APP_DIR/corne-keymap-editor.svg
Terminal=false
StartupNotify=true
StartupWMClass=CorneKeymapEditor
Categories=Development;Utility;
MimeType=text/plain;
EOF
chmod +x "$DESKTOP_DIR/corne-keymap-editor.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
fi

printf '%s\n' "Installed Corne Keymap Editor."
printf '%s\n' "Launch it from your application menu or run: $BIN_DIR/corne-keymap-editor"
printf '%s\n' "If Tkinter is missing on Debian/Ubuntu, run: sudo apt install python3-tk"
