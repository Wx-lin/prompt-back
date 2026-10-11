#!/bin/sh
set -eu
Xvfb :99 -screen 0 1280x900x24 -nolisten tcp > /tmp/xvfb.log 2>&1 &
aigc_display_wait=0
while [ ! -S /tmp/.X11-unix/X99 ]; do
  aigc_display_wait=$((aigc_display_wait + 1))
  if [ "$aigc_display_wait" -ge 30 ]; then
    cat /tmp/xvfb.log >&2
    exit 1
  fi
  sleep 0.1
done
exec "$@"
