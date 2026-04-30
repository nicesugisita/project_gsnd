#!/bin/bash
# FastAPI 서버 종료 스크립트

PIDFILE="/home/diquest/gsnd_rag_v4/backend_v3/.uvicorn.pid"

if [ ! -f "$PIDFILE" ]; then
    echo "PID 파일이 없습니다. 프로세스를 직접 확인합니다..."
    PID=$(lsof -ti :18000 2>/dev/null)
    if [ -z "$PID" ]; then
        echo "실행 중인 서버가 없습니다."
        exit 0
    fi
    echo "uvicorn 프로세스 발견 (PID: $PID). 종료합니다..."
    kill "$PID"
    sleep 2
    if kill -0 "$PID" 2>/dev/null; then
        echo "강제 종료합니다..."
        kill -9 "$PID"
    fi
    echo "서버 종료 완료"
    exit 0
fi

PID=$(cat "$PIDFILE")

if ! kill -0 "$PID" 2>/dev/null; then
    echo "서버가 실행 중이 아닙니다 (stale PID: $PID)"
    rm -f "$PIDFILE"
    exit 0
fi

# PID 파일의 프로세스가 실제로 18000 포트를 사용 중인지 확인
if ! lsof -i :18000 -p "$PID" >/dev/null 2>&1; then
    echo "PID $PID 는 포트 18000을 사용하지 않습니다. PID 파일을 제거합니다."
    rm -f "$PIDFILE"
    exit 0
fi

echo "서버 종료 중 (PID: $PID, 포트: 18000)..."
kill "$PID"
sleep 2

if kill -0 "$PID" 2>/dev/null; then
    echo "강제 종료합니다..."
    kill -9 "$PID"
fi

rm -f "$PIDFILE"
echo "서버 종료 완료"
