#!/usr/bin/env sh
set -eu
rm -rf "$HOME/.local/share/corne-keymap-editor"
rm -f "$HOME/.local/bin/corne-keymap-editor"
rm -f "$HOME/.local/share/applications/corne-keymap-editor.desktop"
printf '%s\n' "Corne Keymap Editor removed."
