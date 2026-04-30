#!/bin/bash
# FastAPI 서버 실행 스크립트

BASE_DIR="/home/diquest/gsnd_rag_v4"
# 1. 경로를 backend_v2에서 v3로 수정
APP_DIR="$BASE_DIR/backend_v3"
PIDFILE="$APP_DIR/.uvicorn.pid"
LOG_DIR="$APP_DIR/logs"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "서버가 이미 실행 중입니다 (PID: $(cat "$PIDFILE"))"
    exit 1
fi

source "$BASE_DIR/venv/bin/activate"
mkdir -p "$LOG_DIR"
cd "$APP_DIR"

# 2. 메인 파일 위치가 src/app/main.py인 경우에 맞게 수정
# --app-dir 옵션을 사용하여 src 폴더를 기준으로 앱을 찾도록 설정합니다.
nohup uvicorn app.main:app --host 0.0.0.0 --port 18000 \
    --app-dir src \
    > "$LOG_DIR/uvicorn.log" 2>&1 &

echo $! > "$PIDFILE"

echo "서버 시작 (PID: $(cat "$PIDFILE"), https://0.0.0.0:18000)"