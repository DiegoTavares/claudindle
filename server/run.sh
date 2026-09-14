#!/bin/sh
# Start the server without systemd (e.g. as an unprivileged user on a Pi).
# Safe to call repeatedly: exits if already running. Use from cron:
#   @reboot /home/<user>/claudindle/server/run.sh
#   */5 * * * * /home/<user>/claudindle/server/run.sh
cd "$(dirname "$0")" || exit 1
PY=${PY:-python3}; [ -x .venv/bin/python ] && PY=.venv/bin/python
pgrep -f "claudindle.py serve" >/dev/null && exit 0
export CLAUDINDLE_TZ="${CLAUDINDLE_TZ:-America/Vancouver}"
setsid nohup "$PY" claudindle.py serve --port "${PORT:-8080}" < /dev/null >> claudindle.log 2>&1 &
