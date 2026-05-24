"""RRF A/B probe — search both-pool 질의를 동시에 쏘아 Step5 top_docs 정렬을 비교.

RRF는 search_v2 의 both-pool 경로(center+tel 병합)에서 top_docs 정렬만 바꾼다.
이 스크립트는 LLM 최종응답이 아니라 그 *직전* Step5 정렬을 보려는 것이므로,
각 요청을 동시에 발사하고 LLM 응답/타임아웃은 신경 쓰지 않는다.
정렬은 서버 로그에서 conv_id 로 수확한다.

사용:
    python tests/rrf_ab_probe.py ON     # 플래그 true 인 서버에 발사 (conv_id=ABON-N)
    python tests/rrf_ab_probe.py OFF    # 플래그 false 인 서버에 발사 (conv_id=ABOFF-N)
"""
from __future__ import annotations

import sys
import threading

import httpx

ENDPOINT = "http://localhost:8000/v1/chat/completions"

# both-pool(ambiguous) 를 노리는 search 의도 질의 — 시설/관청 구분이 모호한 "어디로/누구에게" 류
QUERIES = [
    "창원시 복지 도움 받으려면 어디로 가야 하나요?",
    "김해시 복지 상담은 어디서 받을 수 있나요?",
    "진주시 복지 관련 문의는 어디로 하면 되나요?",
    "양산시 노인 복지 도움 받을 수 있는 곳 알려줘",
    "거제시 장애인 복지 어디서 도와주나요?",
    "통영시 복지 지원 받으려면 어디에 연락해요?",
]


def fire(tag: str, i: int, q: str) -> None:
    cid = f"AB{tag}-{i}"
    payload = {"messages": [{"role": "user", "content": q}],
               "user_id": f"ab_{tag.lower()}", "conv_id": cid, "stream": False}
    try:
        httpx.post(ENDPOINT, json=payload, timeout=90)
    except Exception as e:  # LLM 타임아웃 등은 무관 — Step5 는 이미 로그됨
        print(f"  [{cid}] (client) {type(e).__name__}")
    else:
        print(f"  [{cid}] done")


def main() -> None:
    tag = (sys.argv[1] if len(sys.argv) > 1 else "ON").upper()
    print(f"[ab] tag={tag} → {len(QUERIES)}개 질의 동시 발사")
    threads = [threading.Thread(target=fire, args=(tag, i, q))
               for i, q in enumerate(QUERIES, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"[ab] tag={tag} 발사 완료 — 서버 로그에서 conv_id=AB{tag}-* 수확")


if __name__ == "__main__":
    main()
