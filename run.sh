#!/bin/bash
# FastAPI 서버 실행 스크립트

BASE_DIR="/home/diquest/gsnd_rag_v4"
APP_DIR="$BASE_DIR/backend_v2"
PIDFILE="$APP_DIR/.uvicorn.pid"
LOG_DIR="$APP_DIR/logs"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "서버가 이미 실행 중입니다 (PID: $(cat "$PIDFILE"))"
    exit 1
fi

source "$BASE_DIR/venv/bin/activate"
mkdir -p "$LOG_DIR"
cd "$APP_DIR"

SSL_CERT="$APP_DIR/server.crt"
SSL_KEY="$APP_DIR/server.key"

nohup uvicorn main:app --host 0.0.0.0 --port 18000 \
  > "$LOG_DIR/uvicorn.log" 2>&1 &
echo $! > "$PIDFILE"

echo "서버 시작 (PID: $(cat "$PIDFILE"), https://0.0.0.0:18000)"
