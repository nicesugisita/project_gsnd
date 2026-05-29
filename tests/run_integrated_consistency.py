# -*- coding: utf-8 -*-
"""통합테스트셋 시트별 일관성 실행 — 담당 시트 번호만 주면 끝.

각 질문을 3회 호출해 gsnd_consistency_* 포맷(stages/diff/변동상세)으로 저장한다.
내부적으로 검증된 run_gsnd_consistency.py 를 그대로 재사용한다.

사전 준비
  1) python tests/split_integrated_testset.py      # tests/integrated_split/*.xlsx 생성(최초 1회)
  2) 서버를 RESPONSE_TRACE_ENABLED=True 로 http://127.0.0.1:8000 에 기동

사용
  python tests/run_integrated_consistency.py 3      # 3번 시트 담당자

출력
  qa_test/gsnd_consistency_sheet{N}_{single|mt}_<타임스탬프>.xlsx
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

ROUNDS = 3
SHEETS = {
    1: ("sheet1_dqp_single.xlsx", False, "DQP 싱글턴 테스트셋"),
    2: ("sheet2_dqp_multi.xlsx",  True,  "DQP 멀티턴 테스트셋(5턴)"),
    3: ("sheet3_gn_set1.xlsx",    False, "경남 테스트셋1 100건"),
    4: ("sheet4_gn_set2.xlsx",    True,  "경남 테스트셋2(3턴)"),
    5: ("sheet5_dqp_extra.xlsx",  False, "DQP 추가생성데이터 510건"),
}


def _usage():
    print("사용: python tests/run_integrated_consistency.py <시트번호 1-5>")
    for k, (f, mt, desc) in SHEETS.items():
        print(f"  {k}: {desc}  ({'멀티턴' if mt else '단일턴'})  → {f}")


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in {str(k) for k in SHEETS}:
        _usage()
        sys.exit(1)
    n = int(sys.argv[1])
    fname, multiturn, desc = SHEETS[n]
    src = ROOT / "tests" / "integrated_split" / fname
    if not src.exists():
        print(f"[error] 분리 파일 없음: {src}")
        print("  먼저 실행: python tests/split_integrated_testset.py")
        sys.exit(1)

    import run_gsnd_consistency as runner

    argv = ["run_gsnd_consistency.py", "--src", str(src),
            "--rounds", str(ROUNDS), "--tag", f"sheet{n}"]
    if multiturn:
        argv += ["--multiturn-only", "--keep-single"]   # 1턴 대화도 포함해 누락 방지
    print(f"[run] 시트{n} ({desc}) | {'멀티턴 replay' if multiturn else '단일턴'} | rounds={ROUNDS}")
    sys.argv = argv
    runner.main()


if __name__ == "__main__":
    main()
