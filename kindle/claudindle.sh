#!/bin/sh
# Claudindle - fetch the usage PNG from the server and draw it on the e-ink screen.
# Install to /mnt/us/claudindle/ on the Kindle. Usage: claudindle.sh start|stop|once
#
# Set SERVER to the server's address.
SERVER="${SERVER:-http://192.168.1.104:8080}"
INTERVAL="${INTERVAL:-180}"
DIR=/mnt/us/claudindle
PID=$DIR/claudindle.pid
LOG=$DIR/claudindle.log
IMG=/tmp/claudindle.png

draw() {
    if curl -fsS -m 30 -o "$IMG.new" "$SERVER/usage.png"; then
        mv "$IMG.new" "$IMG"
        eips -c            # full clear avoids e-ink ghosting
        eips -g "$IMG"
        return 0
    fi
    echo "$(date) fetch failed" >> "$LOG"
    # leave the previous image on screen; show a small marker in the corner
    eips 0 39 "offline $(date +%H:%M)"
    return 1
}

loop() {
    # keep the Kindle awake and stop the reader UI from repainting over us
    lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>/dev/null
    eips -c; eips 2 20 "claudindle: connecting to $SERVER"
    while true; do
        if draw; then
            sleep "$INTERVAL"
        else
            # Wi-Fi takes a few seconds to come back after USB eject or wake;
            # retry quickly instead of waiting a whole interval.
            n=0
            until [ $n -ge 12 ] || { sleep 10; n=$((n+1)); draw; }; do :; done
            # after a successful retry, wait a full interval before the next draw
            sleep "$INTERVAL"
        fi
    done
}

kill_loops() {
    # kill every running loop, not just the one in the pid file
    for p in $(ps | grep "claudindle.sh loop" | grep -v grep | awk '{print $1}'); do
        kill "$p" 2>/dev/null
    done
    [ -f "$PID" ] && kill "$(cat "$PID")" 2>/dev/null
    rm -f "$PID"
}

case "$1" in
    start)
        kill_loops
        # Detach so the loop outlives the KUAL menu action.
        if command -v setsid >/dev/null 2>&1; then
            setsid nohup "$0" loop < /dev/null >> "$LOG" 2>&1 &
        else
            nohup "$0" loop < /dev/null >> "$LOG" 2>&1 &
        fi
        echo $! > "$PID"
        echo started
        ;;
    loop)
        echo $$ > "$PID"
        echo "$(date) loop started pid $$ (setsid: $(command -v setsid >/dev/null 2>&1 && echo yes || echo no))" >> "$LOG"
        trap 'lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>/dev/null; exit 0' TERM INT
        loop
        ;;
    stop)
        kill_loops
        lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>/dev/null
        eips -c
        echo stopped
        ;;
    once) draw ;;
    *) echo "usage: $0 start|stop|once|loop"; exit 1 ;;
esac
