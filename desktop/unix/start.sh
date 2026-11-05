#!/bin/sh
# Starts CodeMonkey (Linux and Raspberry Pi).
#
# Chromium's sandbox needs either user namespaces (most systems) or chrome-sandbox owned by root
# with the setuid bit (see README.txt). Without either, Electron would refuse to start, so the
# sandbox is turned off instead; the window only shows CodeMonkey's own page on 127.0.0.1.
here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
sandbox="$here/chrome-sandbox"
if [ -u "$sandbox" ] && [ "$(stat -c %u "$sandbox" 2>/dev/null)" = 0 ]; then
  exec "$here/CodeMonkey" "$@"
fi
if [ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null)" != 1 ] \
   && unshare -Ur true 2>/dev/null; then
  exec "$here/CodeMonkey" "$@"
fi
exec "$here/CodeMonkey" --no-sandbox "$@"
