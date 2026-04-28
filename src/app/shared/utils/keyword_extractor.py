from kiwipiepy import Kiwi

_kiwi = Kiwi()

# 연속 명사 bigram 생성 시 제외할 단독 의미 없는 명사 (시군명+이것들은 bigram으로 만들지 않음)
_BIGRAM_SKIP = {"등", "및", "것", "때", "분", "곳", "중", "내", "외", "상", "하"}

# 단위명사 (숫자 + 단위 패턴에 사용)
_COUNTER_FORMS = {"대", "세", "살", "년", "명", "월", "일", "개", "회", "차"}


def extract_nouns(text: str, min_length: int = 2, use_bigram: bool = True) -> list[str]:
    """
    kiwipiepy로 명사를 추출합니다.

    Args:
        text: 분석할 텍스트
        min_length: 최소 명사 길이 (기본 2)
        use_bigram: True이면 연속 명사 bigram도 생성 (기본 True).
                    False이면 개별 명사만 추출 — 키워드 검색어 생성에 적합.
    """
    result = _kiwi.analyze(text)
    tokens = result[0][0]

    noun_tags = {"NNG", "NNP", "SL"}
    seen: set[str] = set()
    keywords: list[str] = []

    def _add(kw: str) -> None:
        if kw not in seen:
            seen.add(kw)
            keywords.append(kw)

    i = 0
    while i < len(tokens):
        tok = tokens[i]

        # 숫자 + 단위명사 패턴 (70대, 60세, 50대 등)
        if tok.tag == "SN" and i + 1 < len(tokens):
            nxt = tokens[i + 1]
            if nxt.tag in ("XSN", "NNB") and nxt.form in _COUNTER_FORMS:
                _add(tok.form + nxt.form)
                i += 2
                continue

        # 명사 처리
        if tok.tag in noun_tags and len(tok.form) >= min_length:
            _add(tok.form)

            # 연속 명사 bigram (use_bigram=True 일 때만)
            if use_bigram and i + 1 < len(tokens):
                nxt = tokens[i + 1]
                if (
                    nxt.tag in noun_tags
                    and len(nxt.form) >= 2
                    and tok.form not in _BIGRAM_SKIP
                    and nxt.form not in _BIGRAM_SKIP
                ):
                    _add(tok.form + nxt.form)

        i += 1

    return keywords
