"""cross_encoder_client.rerank_by_cross_encoder 단위 테스트.

실제 서비스(7200) 없이 httpx 클라이언트를 가짜로 주입해
정상 정렬 / 빈입력 / 장애 폴백(WEIGHT 정렬) 3 경로를 검증한다.
"""

import asyncio

import pytest

import app.chat.infra.rag.cross_encoder_client as ce


def _docs():
    # WEIGHT 순서(c, a, b)와 cross-encoder 순서(b, a, c)를 일부러 다르게 둬서
    # 어떤 정렬이 적용됐는지 구분 가능하게 한다.
    return [
        {"CHUNK_ID": "a", "NAME": "A", "CONTENT": "내용 A", "WEIGHT": 0.5},
        {"CHUNK_ID": "b", "NAME": "B", "CONTENT": "내용 B", "WEIGHT": 0.1},
        {"CHUNK_ID": "c", "NAME": "C", "CONTENT": "내용 C", "WEIGHT": 0.9},
    ]


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    """post() 호출 시 미리 정한 응답을 돌려주거나 예외를 던지는 가짜 httpx 클라이언트."""

    def __init__(self, payload=None, exc=None):
        self._payload = payload
        self._exc = exc
        self.is_closed = False

    async def post(self, url, json=None):
        if self._exc is not None:
            raise self._exc
        return _FakeResp(self._payload)


def _run(coro):
    return asyncio.run(coro)


def test_empty_returns_empty():
    assert _run(ce.rerank_by_cross_encoder("q", [])) == []


def test_normal_sorts_by_relevance(monkeypatch):
    # 서비스가 b(0.9) > a(0.5) > c(0.1) 순으로 점수를 줬다고 가정
    payload = {"results": [
        {"index": 1, "relevance_score": 0.9},
        {"index": 0, "relevance_score": 0.5},
        {"index": 2, "relevance_score": 0.1},
    ]}
    monkeypatch.setattr(ce, "_get_ce_client", lambda: _FakeClient(payload=payload))
    out = _run(ce.rerank_by_cross_encoder("q", _docs()))
    assert [d["CHUNK_ID"] for d in out] == ["b", "a", "c"]          # CE 순서
    assert out[0][ce.SCORE_CE] == 0.9                                # 점수 기록 확인
    assert [d["CHUNK_ID"] for d in out] != ["c", "a", "b"]           # WEIGHT 순서가 아님


def test_service_down_falls_back_to_weight(monkeypatch):
    import httpx
    monkeypatch.setattr(
        ce, "_get_ce_client",
        lambda: _FakeClient(exc=httpx.ConnectError("refused")),
    )
    out = _run(ce.rerank_by_cross_encoder("q", _docs()))
    assert [d["CHUNK_ID"] for d in out] == ["c", "a", "b"]           # WEIGHT 내림차순 폴백


def test_empty_results_falls_back_to_weight(monkeypatch):
    monkeypatch.setattr(ce, "_get_ce_client", lambda: _FakeClient(payload={"results": []}))
    out = _run(ce.rerank_by_cross_encoder("q", _docs()))
    assert [d["CHUNK_ID"] for d in out] == ["c", "a", "b"]           # WEIGHT 내림차순 폴백


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
