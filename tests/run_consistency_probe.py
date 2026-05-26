"""단계별 일관성 진단 러너 — 같은 질문을 N회 순차 호출 → 단계 비교 xlsx.

전제:
- 서버가 RESPONSE_TRACE_ENABLED=True 로 떠 있어야 함. (각 호출의 단계 산출이
  response_trace.jsonl 에 쌓임 — stage_trace.py 가 검색식까지 채워 넣음)
- 검색식 단계는 모듈 전역 thread-safe 싱크라, '순차' 호출이어야 요청별 귀속이 정확.
  이 러너는 의도적으로 직렬(다음 호출 전 이전 호출 완료 대기)로 던진다.

사용:
    # 기본 질문 3종 × 5회
    python tests/run_consistency_probe.py
    # 질문/횟수 지정
    python tests/run_consistency_probe.py --n 5 --q "창원 노인 복지 추천해줘" --q "양산 청년 일자리"

호출이 끝나면 export_stage_consistency 를 자동 실행해 xlsx 를 만든다.
"""
from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
ENDPOINT = "http://localhost:8000/v1/chat/completions"
TIMEOUT = 300.0
USER_ID = "qa_consistency_probe"

_DEFAULT_QUESTIONS = [
    "창원 노인 복지 추천해줘",
    "임플란트 지원 받고 싶어",
    "양산 청년 일자리 알려줘",
]


def post_chat(client: httpx.Client, question: str, conv_id: str) -> str:
    payload = {
        "messages": [{"role": "user", "content": question}],
        "user_id": USER_ID,
        "conv_id": conv_id,
        "stream": False,
    }
    r = client.post(ENDPOINT, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    try:
        return data["choices"][0]["message"]["content"]
    except Exception:
        return ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5, help="질문당 반복 호출 횟수")
    ap.add_argument("--q", action="append", dest="questions", help="질문 (여러 번 지정 가능)")
    ap.add_argument("--endpoint", default=ENDPOINT)
    ap.add_argument("--no-export", action="store_true", help="호출만 하고 xlsx 생성 생략")
    args = ap.parse_args()

    questions = args.questions or _DEFAULT_QUESTIONS
    print(f"[probe] 질문 {len(questions)}종 × {args.n}회 = {len(questions) * args.n}회 순차 호출")
    print(f"[probe] endpoint={args.endpoint}")
    print("[probe] ⚠ 서버가 RESPONSE_TRACE_ENABLED=True 로 떠 있어야 단계가 기록됩니다.\n")

    with httpx.Client() as client:
        for q in questions:
            for i in range(1, args.n + 1):
                conv = f"probe-{uuid.uuid4().hex[:10]}"
                t0 = time.monotonic()
                try:
                    ans = post_chat(client, q, conv)
                    el = round(time.monotonic() - t0, 1)
                    print(f"[{i}/{args.n}] {el}s chars={len(ans)} :: {q[:24]}", flush=True)
                except Exception as e:
                    print(f"[{i}/{args.n}] ERROR {type(e).__name__}: {e} :: {q[:24]}", flush=True)
                # 순차 보장: 다음 호출은 이전 완료 후에만 (검색식 귀속 정확성)

    if args.no_export:
        print("\n[probe] --no-export → xlsx 생략. 직접 실행: python tests/export_stage_consistency.py")
        return

    print("\n[probe] 단계 비교 xlsx 생성 중...")
    # 같은 인터프리터에서 익스포터 main 호출
    sys.argv = [sys.argv[0]]  # 익스포터가 기본 경로(Config.RESPONSE_TRACE_DIR) 사용
    try:
        from tests.export_stage_consistency import main as export_main
    except ImportError:
        sys.path.insert(0, str(ROOT))
        from export_stage_consistency import main as export_main  # type: ignore
    export_main()


if __name__ == "__main__":
    main()
