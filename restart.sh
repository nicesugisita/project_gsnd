#!/bin/bash
# FastAPI 서버 재기동 스크립트

BASE_DIR="/home/diquest/gsnd_rag_v4"
APP_DIR="$BASE_DIR/backend_v2"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "===== 서버 종료 ====="
bash "$SCRIPT_DIR/shutdown.sh"

echo ""
echo "===== 서버 시작 ====="
setsid bash "$SCRIPT_DIR/run.sh"
sleep 1

echo ""
echo "===== 로그 확인 (Ctrl+C로 로그만 종료, 서버는 유지) ====="
tail -f "$APP_DIR/logs/uvicorn.log"
