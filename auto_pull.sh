#!/bin/bash
cd /root/fakabot

# 1. 加载环境变量（cron 不会读 .bashrc，必须显式 source）
set -a
[ -f /root/fakabot/.env ] && . /root/fakabot/.env
set +a

# 2. 没有 Token 就别启动，写日志退出，避免刷屏
if [ -z "$BOT_TOKEN" ]; then
    echo "$(date '+%F %T') [auto_pull] 缺少 BOT_TOKEN，跳过启动" >> /root/fakabot/bot.log
    exit 1
fi

# 3. 拉代码
git fetch --all
git reset --hard origin/main

# 4. 装依赖（用绝对路径，cron 的 PATH 很干净）
/usr/bin/pip3 install -r requirements.txt -q 2>> /root/fakabot/bot.log

# 5. 精确杀 fakabot 进程，绝不碰其他 bot
if [ -f /root/fakabot/bot.pid ]; then
    kill -9 "$(cat /root/fakabot/bot.pid)" 2>/dev/null || true
    rm -f /root/fakabot/bot.pid
fi
pkill -9 -f "/root/fakabot/bot.py" 2>/dev/null || true

# 6. 启动并记录 PID
nohup /usr/bin/python3 -u /root/fakabot/bot.py >> /root/fakabot/bot.log 2>&1 &
echo $! > /root/fakabot/bot.pid