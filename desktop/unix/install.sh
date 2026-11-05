#!/bin/sh
# Adds CodeMonkey to the applications menu, for you only (nothing is written outside your home folder).
set -e
here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$apps"
cat > "$apps/codemonkey.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=CodeMonkey
Comment=AI coding assistant that runs on your own computer
Exec="$here/start.sh" %U
Icon=$here/codemonkey.png
Terminal=false
Categories=Development;
StartupWMClass=CodeMonkey
EOF
chmod +x "$apps/codemonkey.desktop"
echo "CodeMonkey is now in your applications menu (under Programming / Development)."
if ! command -v python3 >/dev/null 2>&1; then
  echo "It needs Python 3.10 or newer, which is not installed: sudo apt install python3"
elif ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  echo "It needs Python 3.10 or newer; this system has $(python3 --version)."
fi
