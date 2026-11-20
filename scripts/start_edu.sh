#!/usr/bin/env bash
# 启动（或重启）教师工作台。
# 模型密钥从 .envs/edu.env 读取：.envs 指向仓库外的 ../.envs，且已在 .gitignore 里，密钥不会进仓库。
# 密钥文件只需创建一次，内容是一行：EDU_LLM_API_KEY=...
# 用法：scripts/start_edu.sh            （默认端口 7870）
#       EDU_PORT=7880 scripts/start_edu.sh
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE="${EDU_ENV_FILE:-.envs/edu.env}"
PORT="${EDU_PORT:-7870}"
LOG="/tmp/edu-server-$PORT.log"

if [ -f "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
else
  echo "没有找到 $ENV_FILE：服务照常启动，但模型功能关闭。" >&2
fi

# 端口上已有服务就先停掉（重启）
OLD=$(ss -ltnp 2>/dev/null | grep ":$PORT " | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2 || true)
if [ -n "$OLD" ]; then
  echo "停止端口 $PORT 上的旧服务（进程 $OLD）"
  kill "$OLD"
  for _ in 1 2 3 4 5; do ss -ltn 2>/dev/null | grep -q ":$PORT " || break; sleep 1; done
fi

nohup .envs/harness/bin/python -m uvicorn edu_app.api:app --host 127.0.0.1 --port "$PORT" > "$LOG" 2>&1 &
echo "已启动，进程 $!，日志 $LOG"

for _ in $(seq 1 15); do
  if curl -s -m 2 -o /dev/null "http://127.0.0.1:$PORT/"; then
    READY=$(curl -s -m 5 "http://127.0.0.1:$PORT/api/meta" | grep -o '"model_ready":[a-z]*' || true)
    echo "服务已就绪：http://127.0.0.1:$PORT  $READY"
    exit 0
  fi
  sleep 1
done
echo "15 秒内没有响应，查看日志：tail -30 $LOG" >&2
exit 1
