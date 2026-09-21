#!/bin/bash
# Default to headed Camoufox (Firefox) under Xvfb. Real Cloudflare
# deployments fingerprint headless builds and refuse to mount the Turnstile
# iframe. Running under a virtual X server lets the widget render.
set -e

if [ -z "$DISPLAY" ]; then
    Xvfb :99 -screen 0 1280x900x24 -nolisten tcp >/dev/null 2>&1 &
    export DISPLAY=:99
    sleep 0.5
fi

exec python3 -m app
