"""케이스 1 (진주/김해 아동수당 비교) 단일 end-to-end 스모크.

pipeline_comparison.py 의 _COMP_FINAL_TOP_N 8→10 변경 후, 진주 아동수당이
참조 문서에 포함되는지 확인.
"""
from __future__ import annotations

import sys
import time
import uuid

import httpx

ENDPOINT = "http://localhost:8000/v1/chat/completions"
TIMEOUT = 300.0


def main() -> int:
    user_id = f"smoke_{uuid.uuid4().hex[:8]}"
    conv_id = str(uuid.uuid4())
    msgs = [{"role": "user", "content": "김해 아동수당이랑 진주 아동수당을 비교해줘요"}]

    t0 = time.monotonic()
    r = httpx.post(
        ENDPOINT,
        json={"messages": msgs, "user_id": user_id, "conv_id": conv_id, "stream": False},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    j = r.json()
    final = j["choices"][0]["message"]["content"]
    refs = j.get("referenced_documents") or []
    print(f"[응답] ({time.monotonic()-t0:.1f}s)")
    print("-" * 60)
    print(final[:2000])
    print("-" * 60)
    print(f"\n참조 문서 {len(refs)}개:")
    for d in refs:
        name = d.get("name") or d.get("NAME") or d.get("document_name") or "?"
        print(f"  - {name}")

    # 핵심: 진주 아동수당이 포함되어야 함
    refs_names = [
        (d.get("name") or d.get("NAME") or d.get("document_name") or "")
        for d in refs
    ]
    has_jinju_allowance = any("진주" in n and "아동수당" in n for n in refs_names)
    has_gimhae_allowance = any("김해" in n and "아동수당" in n for n in refs_names)
    print()
    print(f"[검증] 진주 아동수당 참조됨? {has_jinju_allowance}")
    print(f"[검증] 김해 아동수당 참조됨? {has_gimhae_allowance}")

    passed = has_jinju_allowance and has_gimhae_allowance
    print(f"\n결과: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
