#!/bin/bash
cd /opt/tg-torrent-bot
# Prevent multiple instances: check for an existing bot.py process for this user
PIDS=$(pgrep -u "$(whoami)" -f "/opt/tg-torrent-bot/bot.py" || true)
if [ -n "$PIDS" ]; then
    # print the first running PID only and exit
    echo "$PIDS" | head -n1
    exit 0
fi

# Activate venv and start in background, logging to bot.log
. /opt/tg-torrent-bot/.venv/bin/activate
nohup python /opt/tg-torrent-bot/bot.py >> /opt/tg-torrent-bot/bot.log 2>&1 &
disown
echo "tg-torrent-bot started (pid: $!)"
