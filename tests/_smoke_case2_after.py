"""케이스 2 (병원비 지원) 단일 end-to-end 스모크.

classification_recommended_prompt.txt 수정 후 동일 시나리오 재현:
  user → "병원비 지원 제도 있나요?"
  bot  → "어느 시군에 거주하고 계신가요?" (clarification)
  user → "창원"
  bot  → 기대: [서비스 1] [서비스 2] ... (fallback 문구 금지)
"""
from __future__ import annotations

import json
import sys
import time
import uuid

import httpx

ENDPOINT = "http://localhost:8000/v1/chat/completions"
TIMEOUT = 300.0
FALLBACK = "제공된 문서에서는 해당 내용을 확인할 수 없습니다."


def post(client: httpx.Client, messages: list[dict], user_id: str, conv_id: str) -> dict:
    payload = {"messages": messages, "user_id": user_id, "conv_id": conv_id, "stream": False}
    r = client.post(ENDPOINT, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def main() -> int:
    user_id = f"smoke_{uuid.uuid4().hex[:8]}"
    conv_id = str(uuid.uuid4())
    msgs: list[dict] = [{"role": "user", "content": "병원비 지원 제도 있나요?"}]

    t0 = time.monotonic()
    r1 = post(httpx.Client(), msgs, user_id, conv_id)
    reask = r1["choices"][0]["message"]["content"]
    print(f"[1] reask ({time.monotonic()-t0:.1f}s): {reask}")

    msgs.append({"role": "assistant", "content": reask})
    msgs.append({"role": "user", "content": "창원"})

    t1 = time.monotonic()
    r2 = post(httpx.Client(), msgs, user_id, conv_id)
    final = r2["choices"][0]["message"]["content"]
    refs = r2.get("referenced_documents") or []
    print(f"[2] final ({time.monotonic()-t1:.1f}s):")
    print("-" * 60)
    print(final)
    print("-" * 60)
    print(f"\n참조 문서 {len(refs)}개:")
    for d in refs:
        name = d.get("name") or d.get("NAME") or d.get("document_name") or "?"
        print(f"  - {name}")

    # 검증
    has_fallback = FALLBACK in final
    has_service_block = "[서비스" in final or "서비스 1" in final or "서비스1" in final
    print()
    print(f"[검증] fallback 문구 포함? {has_fallback} (False여야 통과)")
    print(f"[검증] [서비스 N] 블록 포함? {has_service_block} (True여야 통과)")
    print(f"[검증] referenced_documents 비어있지 않음? {len(refs) > 0}")

    passed = (not has_fallback) and has_service_block and len(refs) > 0
    print(f"\n결과: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
