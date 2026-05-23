# ContextualQueryRewriter — Python 이식용 핸드오프 문서

RAG 시스템의 사용자 질문을 검색 채널별로 3-way 분리하여 재작성하는 컴포넌트.
Java/Spring 구현을 Python으로 이식할 수 있게 정리.

---

## 1. 목적

사용자 질문을 1회 LLM 호출로 3가지 형태로 분리한다.

| 출력 필드 | 용도 | 다운스트림 |
|----------|------|-----------|
| `vectorQuery` | 벡터 임베딩 검색용. 제외 entity 단어 제거 (anchor 케이스 제외) | VectorSearcher / FAISS / Milvus |
| `llmQuery` | LLM 답변용. "X 제외 Y" 의도 명시 자연어 | LLMAnswer (OpenAI, Gemini 등) |
| `mustNotKeywords` | 키워드 검색 must_not 절 | OpenSearch / Elasticsearch Bool Query |
| `anchorEntities` | 비교 기준점 (vec에 단어 유지) | (정보용) |
| `exclusionIntent` | 4-way 분류 결과 | 후속 노드 분기 |

해결하는 문제:
- 벡터 검색에 NOT 연산자 없음 → "의료급여 외 지원" 질문에 vec="의료급여 외 지원" 그대로 보내면 의료급여 문서 retrieve됨
- "X 말고 Y" 형태가 항상 X 정보 불필요는 아님 — "아반떼 말고 비슷한 가격대" 는 아반떼 가격대 알아야 비교 가능

---

## 2. Exclusion Intent 분류 (핵심 설계)

5가지 intent로 분류:

### NONE
- exclusion 패턴 없음
- 자명 쿼리 또는 멀티턴 ellipsis 해소만
- 예) `Spring Boot 사용법`, `작년은?` (history 있는 멀티턴)

### PURE_EXCLUSION
- entity 정보 불필요, 단순 제외
- 패턴: `X 빼고/말고 + 구체적 다른 Y` (수식어·비교어 없음)
- 예) `삼성전자 빼고 SK하이닉스 매출` → `SK하이닉스 매출`
- 결과: vec에 entity 단어 X, mustNotKeywords=[X], anchorEntities=[]

### ANCHOR_COMPARISON
- entity가 검색 기준점, 비교 수식어 동반
- 패턴: `X 말고/빼고/보다 + [비슷한/같은/다른/유사한/보다 형용사] Y`
- 예) `아반떼 말고 비슷한 가격대 다른 차종` → vec에 "아반떼" 유지 (가격대 알아야 비교)
- 결과: vec에 entity 단어 O, mustNotKeywords=[] (검색 차단 안 함), anchorEntities=[X]
- llmQuery는 "X 제외 비슷한 Y"로 명시 → LLM이 답변 시 X 자체는 추천 안 함

### RESIDUAL_CATEGORY
- "X 외 기타 Y" 잔여 카테고리
- 예) `의료급여 외 기타 지원` → vec="기초생활수급자 기타 지원 혜택"
- 결과: vec에 entity 단어 X (+ "기타/다른" 추가), mustNotKeywords=[X]

### SUBSTITUTION
- 대체, X 정보 불필요
- 패턴: `X 대신/아니라 Y`
- 예) `현대차 대신 기아 판매량` → `기아 판매량`
- 결과: vec에 entity 단어 X, mustNotKeywords=[X]

---

## 3. 출력 DTO (Python 표현)

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

class ExclusionIntent(Enum):
    NONE = "NONE"
    PURE_EXCLUSION = "PURE_EXCLUSION"
    ANCHOR_COMPARISON = "ANCHOR_COMPARISON"
    RESIDUAL_CATEGORY = "RESIDUAL_CATEGORY"
    SUBSTITUTION = "SUBSTITUTION"

@dataclass
class QueryRewriteResult:
    original_query: str
    vector_query: str
    llm_query: str
    must_keywords: List[str] = field(default_factory=list)
    must_not_keywords: List[str] = field(default_factory=list)
    anchor_entities: List[str] = field(default_factory=list)
    exclusion_intent: ExclusionIntent = ExclusionIntent.NONE
    rewritten: bool = False
    has_exclusion: bool = False
```

---

## 4. 시스템 프롬프트 (verbatim)

### 4.1 OUTPUT_SPEC (공통 — 두 프롬프트 마지막에 concat)

> 모든 예시는 **변수형(X/Y/A/B/E/T)**. 구체 도메인 entity 없음 — few-shot leakage 방지 + 일반화.

```
EXCLUSION INTENT CLASSIFICATION (가장 중요):

같은 "X 말고/빼고/외 Y" 표현이라도 X 정보가 검색에 필요한지에 따라 다르게 처리한다.
5가지 의도 중 1개로 분류:

※ 아래 예시에서 X, Y, A, B는 임의의 명사/엔티티 변수. 도메인 무관(회사/제품/지표/지역/장르 등).
  실제 사용자 입력은 다양한 도메인의 구체 명사로 들어온다.

(1) NONE — exclusion 패턴 없음. 단순/자명 쿼리 또는 멀티턴 ellipsis 해소만.
    예) "[주제 T] 사용법", 멀티턴 "T-1년은?" (history 있을 때 ellipsis 해소)

(2) PURE_EXCLUSION — entity 정보 불필요. 단순 제외 후 다른 Y만 원함.
    예) "A 빼고 B의 [지표]", "장르 X 말고 장르 Y 추천"
    특징: "X 빼고/말고 + 구체적 다른 Y" (수식어·비교어 없음).
    → vectorQuery: entity 단어 없음. mustNotKeywords: [X]. anchorEntities: [].

(3) ANCHOR_COMPARISON — entity가 검색 기준점. 비교 수식어 동반.
    예) "X 말고 비슷한 [속성] 다른 Y" (X의 속성값 알아야 비교)
        "X 빼고 다른 [같은 카테고리] Y" (X 카테고리 비교)
        "X보다 [형용사] Y" (X 기준값 알아야 비교)
    특징: "X 말고/빼고/보다 + [비슷한/같은/다른/유사한/보다 형용사] Y".
    "다른"은 anchor 신호일 가능성 큼 (X 카테고리 내에서 다른 항목 찾기).
    → vectorQuery: entity 단어 유지 (검색 anchor). mustNotKeywords: [] (검색 차단 안 함).
      anchorEntities: [X]. llmQuery: "X 제외 비슷한/다른 Y"로 의도 명시.

(4) RESIDUAL_CATEGORY — 잔여 카테고리. "X 외 기타 Y".
    예) "X 외 기타 [카테고리] Y", "X 이외의 [카테고리] Y"
    특징: "X 외/이외 + (선택) 기타/다른 Y".
    → vectorQuery: entity 단어 없음, "기타/다른" 등 잔여 표현 포함.
      mustNotKeywords: [X]. anchorEntities: [].

(5) SUBSTITUTION — 대체. X 불필요.
    예) "X 대신 Y의 [지표]", "X가 아니라 Y"
    특징: "X 대신/아니라 Y".
    → vectorQuery: entity 단어 없음. mustNotKeywords: [X]. anchorEntities: [].

CRITICAL OUTPUT FORMAT — STRICT JSON ONLY:
```json
{
  "exclusionIntent": "NONE|PURE_EXCLUSION|ANCHOR_COMPARISON|RESIDUAL_CATEGORY|SUBSTITUTION",
  "vectorQuery": "벡터 검색용 쿼리 (intent에 따라 entity 유지/제거)",
  "llmQuery": "LLM 답변용 쿼리 (의도 명시적 자연어)",
  "mustKeywords": ["positive 검색 키워드 배열 2~5개"],
  "mustNotKeywords": ["must_not 절 키워드. ANCHOR_COMPARISON/NONE은 []"],
  "anchorEntities": ["검색 anchor entity. ANCHOR_COMPARISON에서만 채움. 그 외 []"],
  "hasExclusion": true|false
}
```

RULES (반드시 준수):
- PURE_EXCLUSION / RESIDUAL_CATEGORY / SUBSTITUTION: vectorQuery에 제외 entity 단어 절대 X.
- ANCHOR_COMPARISON: vectorQuery에 anchor entity 단어 O. mustNotKeywords []. anchorEntities에 entity O.
- llmQuery는 모든 intent에서 자연어 의도 명시 ("X 제외", "X 말고" 그대로).
- hasExclusion: NONE이면 false, 나머지는 true.
- 자명 쿼리(NONE): vectorQuery=llmQuery=원본, 모든 배열 [].

Output ONLY the JSON.
```

### 4.2 MULTI_TURN_SYSTEM_PROMPT (full)

```
You are a multi-turn conversation query rewriter for a Retrieval-Augmented Generation (RAG) system.

ROLE: Rewrite the user's latest question into a STANDALONE search query that can be
understood and searched without seeing any prior turn.

HARD RULES (must follow exactly):
1. INJECT MISSING NOUNS: If the latest question relies on prior context (pronouns, ellipsis,
   trailing particles like "는?", "은?", "도?", short fragments, or any reference word),
   you MUST inject the relevant noun phrase(s) from the history into the rewritten query.
   At least ONE concrete noun from the history MUST appear in the output unless the
   question is clearly a new topic.
2. RESOLVE PRONOUNS: "그것", "그거", "그", "위에서 말한", "거기에", "그 회사",
   "the same", "it", "there" → replace with the actual entity from history.
3. RESOLVE ELLIPSIS: short fragments like "T-1년은?", "올해는?", "변형-X는?",
   "지역-Y는?" → expand by carrying over the predicate/topic from the previous turn.
   예) "T-1년은?" with prior "E의 T년 [지표]" → "E의 T-1년 [지표]"
       ("변형-X는?" with prior "원본의 [속성]" → "변형-X의 [속성]")
4. RESOLVE NEGATION ACROSS TURNS: "X 말고", "X 빼고" → drop X, keep the new target, and
   carry over the missing topic from history.
5. PRESERVE LANGUAGE: Korean stays Korean, English stays English. Keep original intent.
6. NEW TOPIC EXCEPTION: If the question is clearly a new unrelated topic (no pronouns,
   no ellipsis, full noun phrase, no reference to history), return it UNCHANGED.
7. OUTPUT FORMAT: Output ONLY the rewritten question. No explanation, no quotes, no prefix.

EXAMPLES (history → latest → rewritten):
※ E, X, Y, T는 변수. 어느 도메인의 entity/주제든 가능 (회사/제품/지역/연도/카테고리 등).

[Ellipsis with year — 시간 ellipsis]
History: User: E의 T년 [지표]  Assistant: [값]
Latest: T-1년은?
Rewritten: E의 T-1년 [지표]
  intent=NONE, anchorEntities=[]

[Pronoun "그 [E의 카테고리]"]
History: User: E의 [지표A]  Assistant: [값]
Latest: 그 [E의 카테고리] [지표B]도 알려줘
Rewritten: E의 [지표B]
  intent=NONE

[Demonstrative "위에서 말한"]
History: User: E의 T년 [지표]  Assistant: [값]
Latest: 위에서 말한 [지표] [세분화 단위]로 나눠줘
Rewritten: E의 T년 [지표] [세분화 단위] 데이터
  intent=NONE

[Region carryover — entity의 속성값 추가]
History: User: E의 [속성A]  Assistant: [값]
Latest: [속성B]는?
Rewritten: E의 [속성B]
  intent=NONE

[Instance variant ellipsis — 같은 카테고리 다른 항목]
History: User: 제품-X의 [속성]  Assistant: [값]
Latest: 제품-Y는?
Rewritten: 제품-Y의 [속성]
  intent=NONE

[Time-axis follow-up]
History: User: E의 [지표]  Assistant: [값]
Latest: 최근 N년치 추이로 보여줘
Rewritten: E의 [지표] 최근 N년 추이
  intent=NONE

[Region follow-up — 같은 지표 다른 entity]
History: User: 지역-X의 [지표]  Assistant: [값]
Latest: 지역-Y는 어때?
Rewritten: 지역-Y의 [지표]
  intent=NONE

[Multi-turn ANCHOR — entity 유지 (비교 검색)]
History: User: [카테고리 C]의 X 구성법  Assistant: [내용]
Latest: 그거 말고 다른 [카테고리 C] 알려줘
Rewritten: X 다른 [카테고리 C] 종류
  (X는 anchor — 같은 카테고리(C) 다른 것 찾기. vector에 X 유지)
  intent=ANCHOR_COMPARISON, anchorEntities=[X]

[Multi-turn ANCHOR — 비교 속성 검색]
History: User: E의 [속성값]  Assistant: [값]
Latest: E 말고 비슷한 [속성] 다른 [카테고리]
Rewritten: E 비슷한 [속성] 다른 [카테고리]
  intent=ANCHOR_COMPARISON, anchorEntities=[E]

[Multi-turn NOT + carryover — 같은 지표 다른 entity (PURE)]
History: User: 지역-X의 T년 [지표]  Assistant: [값]
Latest: 지역-X 빼고 지역-Y의 [지표]
Rewritten: 지역-Y의 T년 [지표]
  intent=PURE_EXCLUSION, mustNotKeywords=[지역-X]

[Additive AND in multi-turn — 같은 entity의 추가 지표]
History: User: E의 [지표A]  Assistant: [값]
Latest: 거기에 [지표B]도 추가해서 알려줘
Rewritten: E의 [지표A]와 [지표B]
  intent=NONE

[Two-year comparison ellipsis]
History: User: E의 T년 [지표]  Assistant: [값]
Latest: T-1년과 T-2년 비교해줘
Rewritten: E의 T-1년과 T-2년 [지표] 비교
  intent=NONE

[New topic — unchanged]
History: User: E의 [지표]  Assistant: [값]
Latest: [완전히 다른 주제 T]
Rewritten: [완전히 다른 주제 T]
  intent=NONE

[OUTPUT_SPEC 여기 concat]
```

### 4.3 SINGLE_TURN_SYSTEM_PROMPT (full)

```
You are a single-turn query intent disambiguator for a Retrieval-Augmented Generation (RAG) system.

ROLE: Rewrite the user's question so vector search retrieves the CORRECT residual category
and EXCLUDES the negated/contrasted entity. Vector search has no logical NOT operator,
so the rewritten query must NOT contain the excluded entity as a positive search keyword.

CRITICAL: Korean uses MANY exclusion markers. Detect ALL of these:
- "X 말고", "X 빼고", "X 제외(하고)", "X을(를) 제외한", "X 외(에)", "X 이외(의)",
  "X은(는) 빼고", "X 아니고", "X가 아니라", "X 대신(에)", "X 외 기타"
- English: "not A but B", "except A", "other than A", "besides A", "aside from A",
  "apart from A", "instead of A"

HARD RULES:
1. If exclusion detected: REMOVE the excluded entity from the positive query terms.
   Replace with phrases that signal the residual set:
   - "X 외 Y" / "X 이외의 Y" → "X 제외한 다른 Y" or "X 이외 기타 Y" (keep semantics of OTHER)
   - "X 말고 Y" → "Y" alone (user wants only Y, not OTHER-than-X)
   - "X 빼고 Y" → "Y" alone
   - "X 대신 Y" → "Y" alone
2. KEEP ALL other context (location, role, year, qualifier). Only the excluded
   entity is removed.
3. The rewritten query should be SHORTER and MORE FOCUSED than the original.
4. PRESERVE LANGUAGE: Korean stays Korean. Strip filler verbs ("알려줘", "보여줘", "뭐가 있나요").
5. NEW TOPIC EXCEPTION: If the question has no exclusion/contrast/negation pattern
   and is already clear, return it UNCHANGED.
6. OUTPUT FORMAT: Output ONLY the rewritten question. No explanation, no quotes.

EXAMPLES — 4 INTENTS (X, Y, A, B는 변수. 실제로는 어느 도메인 명사든 가능):

(PURE_EXCLUSION — entity 정보 불필요)
"A 말고 B의 [지표]" → vector: "B [지표]" (A 단어 없음, mustNot=[A])
"X 빼고 Y의 [속성]" → vector: "Y [속성]" (mustNot=[X])
"장르X 말고 장르Y 추천" → vector: "장르Y 추천" (mustNot=[장르X])

(ANCHOR_COMPARISON — entity 검색 기준점, "비슷한/같은/다른/보다" 수식어)
"X 말고 비슷한 [속성] 다른 Y" → vector: "X 비슷한 [속성] Y"
  (anchor=[X], mustNot=[], anchorEntities=[X])
  ※ X의 속성값 알아야 비교 가능. vector에 X 단어 유지!
"X 빼고 다른 [같은 카테고리] Y" → vector: "X [카테고리] Y 종류"
  (anchor=[X])
"X보다 [형용사] [지표] Y" → vector: "X [지표] 비교 다른 Y"
  (anchor=[X])
"Brand-X보다 [형용사] Brand-Y" → vector: "Brand-X [기준] 다른 Brand-Y"
  (anchor=[Brand-X])

(RESIDUAL_CATEGORY — "X 외 기타 Y")
"[수식어] X 외 받을 수 있는 [카테고리]"
  → vector: "[수식어] 기타 [카테고리]"
  (mustNot=[X])
"X 외에 [대상]의 [카테고리]" → vector: "[대상] 기타 [카테고리]"
"X 이외의 [카테고리]" → vector: "기타 [카테고리]"

(SUBSTITUTION)
"X 대신 Y의 [지표]" → vector: "Y [지표]" (mustNot=[X])
"X가 아니라 Y" → vector: "Y" (mustNot=[X])

(NONE — 자명)
"[주제 T] 사용법" → vector: 원본 그대로
"[라이브러리 L] 예제" → vector: 원본 그대로

[구분 핵심]
"X 말고 Y" 형태 처리 기준:
  - Y가 구체적 명사면 PURE ("A 빼고 B 매출" → B만)
  - "비슷한/같은/다른/유사한/보다 [형용사]" 수식어가 있으면 ANCHOR
    ("A 빼고 비슷한 B" → A 유지하고 비슷한 것 찾기)
  - "기타/다른" 단독 + "외/이외"면 RESIDUAL
    ("A 외 기타 B" → A 제외하고 잔여 B)
애매하면 ANCHOR 우선 (검색 안전성: anchor 유지가 정보 손실 적음).

[OUTPUT_SPEC 여기 concat]
```

### 4.4 USER PROMPT TEMPLATE (현재 날짜 주입)

```python
def build_user_prompt_multi_turn(history: str, question: str, today: date) -> str:
    year = today.year
    weekday = today.strftime("%A")
    return f"""CURRENT DATE CONTEXT (use this to resolve relative time expressions):
- Today: {today.isoformat()} ({weekday})
- 작년 = {year-1}, 재작년 = {year-2}, 올해 = {year}, 내년 = {year+1}
- "last year" = {year-1}, "this year" = {year}, "next year" = {year+1}

Conversation history:
{history}

Latest question:
{question}

Rewritten self-contained question:"""


def build_user_prompt_single_turn(question: str, today: date) -> str:
    year = today.year
    weekday = today.strftime("%A")
    return f"""CURRENT DATE CONTEXT (use this to resolve relative time expressions):
- Today: {today.isoformat()} ({weekday})
- 작년 = {year-1}, 재작년 = {year-2}, 올해 = {year}, 내년 = {year+1}

Question:
{question}

Rewritten question:"""
```

---

## 5. Python 구현 예시 (전체 클래스)

```python
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import List, Optional

logger = logging.getLogger(__name__)


class ExclusionIntent(Enum):
    NONE = "NONE"
    PURE_EXCLUSION = "PURE_EXCLUSION"
    ANCHOR_COMPARISON = "ANCHOR_COMPARISON"
    RESIDUAL_CATEGORY = "RESIDUAL_CATEGORY"
    SUBSTITUTION = "SUBSTITUTION"


@dataclass
class QueryRewriteResult:
    original_query: str
    vector_query: str
    llm_query: str
    must_keywords: List[str] = field(default_factory=list)
    must_not_keywords: List[str] = field(default_factory=list)
    anchor_entities: List[str] = field(default_factory=list)
    exclusion_intent: ExclusionIntent = ExclusionIntent.NONE
    rewritten: bool = False
    has_exclusion: bool = False


class ContextualQueryRewriter:
    """
    사용 예:
        rewriter = ContextualQueryRewriter(llm_client=openai_client)
        result = rewriter.rewrite("의료급여 외 받을 수 있는 지원", history=None)
        print(result.vector_query)       # "기초생활수급자 기타 지원 혜택"
        print(result.must_not_keywords)  # ["의료급여"]
    """

    MULTI_TURN_SYSTEM_PROMPT = """[4.2 섹션 본문 그대로]"""
    SINGLE_TURN_SYSTEM_PROMPT = """[4.3 섹션 본문 그대로]"""
    OUTPUT_SPEC = """[4.1 섹션 본문 그대로]"""

    def __init__(self, llm_client, model: str = "gpt-4o-mini", today_provider=date.today):
        self.llm_client = llm_client
        self.model = model
        self.today_provider = today_provider  # 테스트 주입 가능

    def rewrite(self, question: str, history: Optional[str] = None) -> QueryRewriteResult:
        if not question or not question.strip():
            return self._default_result(question or "")

        has_history = history is not None and history.strip()
        system_prompt = (self.MULTI_TURN_SYSTEM_PROMPT if has_history
                         else self.SINGLE_TURN_SYSTEM_PROMPT) + self.OUTPUT_SPEC
        user_prompt = (self._build_multi_turn_user_prompt(history, question) if has_history
                       else self._build_single_turn_user_prompt(question))

        try:
            response = self._call_llm(system_prompt, user_prompt)
        except Exception as e:
            logger.warning("LLM call failed: %s", e)
            return self._default_result(question)

        if not response:
            return self._default_result(question)

        result = self._parse_structured(response, question)
        if result is None:
            return self._default_result(question)

        result.original_query = question
        result.rewritten = (result.vector_query.strip() != question.strip()
                            or result.llm_query.strip() != question.strip())
        logger.info("Rewrite [%s]: '%s' → vec='%s' llm='%s' mustNot=%s",
                    "multi-turn" if has_history else "single-turn",
                    question, result.vector_query, result.llm_query, result.must_not_keywords)
        return result

    def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        # OpenAI SDK 예시 — 다른 SDK는 동등 호출로 교체
        response = self.llm_client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            max_tokens=1024,
        )
        return response.choices[0].message.content

    def _build_multi_turn_user_prompt(self, history: str, question: str) -> str:
        today = self.today_provider()
        year = today.year
        weekday = today.strftime("%A")
        return (
            f"CURRENT DATE CONTEXT (use this to resolve relative time expressions):\n"
            f"- Today: {today.isoformat()} ({weekday})\n"
            f"- 작년 = {year-1}, 재작년 = {year-2}, 올해 = {year}, 내년 = {year+1}\n"
            f"- \"last year\" = {year-1}, \"this year\" = {year}, \"next year\" = {year+1}\n\n"
            f"Conversation history:\n{history}\n\n"
            f"Latest question:\n{question}\n\n"
            f"Rewritten self-contained question:"
        )

    def _build_single_turn_user_prompt(self, question: str) -> str:
        today = self.today_provider()
        year = today.year
        weekday = today.strftime("%A")
        return (
            f"CURRENT DATE CONTEXT (use this to resolve relative time expressions):\n"
            f"- Today: {today.isoformat()} ({weekday})\n"
            f"- 작년 = {year-1}, 재작년 = {year-2}, 올해 = {year}, 내년 = {year+1}\n\n"
            f"Question:\n{question}\n\n"
            f"Rewritten question:"
        )

    def _parse_structured(self, response: str, fallback: str) -> Optional[QueryRewriteResult]:
        try:
            # JSON 추출 (코드 펜스 또는 raw JSON 모두 대응)
            json_str = self._extract_json(response)
            parsed = json.loads(json_str)

            intent = self._parse_intent(parsed.get("exclusionIntent"))
            must_not = self._as_str_list(parsed.get("mustNotKeywords"))
            # ANCHOR면 mustNot 강제 비움
            if intent == ExclusionIntent.ANCHOR_COMPARISON:
                must_not = []

            return QueryRewriteResult(
                original_query=fallback,
                vector_query=self._strip_quotes(str(parsed.get("vectorQuery") or fallback)),
                llm_query=self._strip_quotes(str(parsed.get("llmQuery") or fallback)),
                must_keywords=self._as_str_list(parsed.get("mustKeywords")),
                must_not_keywords=must_not,
                anchor_entities=self._as_str_list(parsed.get("anchorEntities")),
                exclusion_intent=intent,
                has_exclusion=bool(parsed.get("hasExclusion", intent != ExclusionIntent.NONE)),
            )
        except Exception as e:
            logger.warning("JSON parse failed: %s | response prefix: %s",
                           e, response[:200])
            return None

    def _extract_json(self, response: str) -> str:
        # ```json ... ``` 코드 펜스 우선
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL)
        if match:
            return match.group(1)
        # raw JSON object
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match:
            return match.group(0)
        return response

    @staticmethod
    def _parse_intent(value) -> ExclusionIntent:
        if value is None:
            return ExclusionIntent.NONE
        raw = str(value).strip().upper()
        try:
            return ExclusionIntent(raw)
        except ValueError:
            logger.warning("Unknown exclusionIntent '%s', defaulting to NONE", raw)
            return ExclusionIntent.NONE

    @staticmethod
    def _as_str_list(value) -> List[str]:
        if not isinstance(value, list):
            return []
        return [str(v).strip() for v in value if v is not None and str(v).strip()]

    @staticmethod
    def _strip_quotes(text: str) -> str:
        text = text.strip()
        if len(text) >= 2 and ((text[0] == '"' and text[-1] == '"')
                                or (text[0] == "'" and text[-1] == "'")):
            return text[1:-1].strip()
        return text

    def _default_result(self, question: str) -> QueryRewriteResult:
        return QueryRewriteResult(
            original_query=question,
            vector_query=question,
            llm_query=question,
            exclusion_intent=ExclusionIntent.NONE,
            rewritten=False,
            has_exclusion=False,
        )
```

---

## 6. 테스트 케이스 (38건)

### 6.1 NONE / 자명 / 멀티턴 ellipsis (10건)

| # | 입력 | history | 기대 intent | 기대 vec |
|---|------|---------|------------|---------|
| 1 | `Spring Boot Virtual Thread 사용법` | - | NONE | (원본 동일) |
| 2 | `LangGraph4j 워크플로우 예제` | - | NONE | (원본 동일) |
| 3 | `김치찌개 레시피 알려줘` | - | NONE | `김치찌개 레시피` |
| 4 | `Redis Cluster 구성 방법` | - | NONE | (원본 동일) |
| 5 | `작년은?` | `User: 삼성전자 2024년 매출\nAssistant: 300조원` | NONE | `삼성전자 작년 매출` (2026 기준 2025년) |
| 6 | `m5는?` | `User: AWS EC2 r5.large 가격\nAssistant: $0.126` | NONE | `AWS EC2 m5 가격` |
| 7 | `미국은 어때?` | `User: 한국 인구 통계\nAssistant: 5170만명` | NONE | `미국 인구 통계` |
| 8 | `그 회사 영업이익도 알려줘` | `User: 삼성전자 2024년 매출\nAssistant: 300조원` | NONE | `삼성전자 영업이익` |
| 9 | `최근 5년치 추이로 보여줘` | `User: 한국 인구 통계\nAssistant: 5170만명` | NONE | `한국 인구 최근 5년 추이` |
| 10 | `Java Virtual Thread 사용법` | `User: 한국 GDP\nAssistant: 1.7조 달러` | NONE | (원본 동일, 새 주제) |

### 6.2 PURE_EXCLUSION (5건)

| # | 입력 | 기대 vec | 기대 mustNot |
|---|------|---------|-------------|
| 11 | `A 회사 말고 B 회사 매출 가져와` | `B 회사 매출` | `[A 회사]` |
| 12 | `삼성전자 빼고 SK하이닉스 영업이익` | `SK하이닉스 영업이익` | `[삼성전자]` |
| 13 | `재즈 말고 클래식 음악 추천` | `클래식 음악 추천` | `[재즈]` |
| 14 | `A 말고 B와 C 매출` | `B와 C 매출` | `[A]` |
| 15 | `2023년 제외하고 2024년 누적 사용자` | `2024년 누적 사용자` | `[2023년]` |

### 6.3 ANCHOR_COMPARISON (8건)

| # | 입력 | 기대 vec (anchor 단어 유지) | 기대 anchorEntities | 기대 mustNot |
|---|------|---------------------------|--------------------|--------------|
| 16 | `아반떼 말고 비슷한 가격대 다른 차종` | `아반떼 비슷한 가격대 차종` | `[아반떼]` | `[]` |
| 17 | `타이레놀 빼고 다른 진통제 추천` | `타이레놀 진통제 종류 추천` | `[타이레놀]` | `[]` |
| 18 | `삼성전자보다 매출 큰 회사` | `삼성전자 매출 비교 다른 회사` | `[삼성전자]` | `[]` |
| 19 | `스타벅스보다 저렴한 카페` | `스타벅스 가격 다른 카페` | `[스타벅스]` | `[]` |
| 20 | `그거 말고 다른 벡터 인덱스` (history: HNSW) | `PostgreSQL HNSW 다른 벡터 인덱스` | `[HNSW]` | `[]` |
| 21 | `아반떼 말고 비슷한 가격대` (history: 아반떼 가격) | `아반떼 비슷한 가격대 차종` | `[아반떼]` | `[]` |
| 22 | `타이레놀 빼고 다른 진통제` (history: 타이레놀) | `타이레놀 다른 진통제 종류` | `[타이레놀]` | `[]` |
| 23 | `현대차보다 연비 좋은 차` | `현대차 연비 다른 차` | `[현대차]` | `[]` |

### 6.4 RESIDUAL_CATEGORY (5건)

| # | 입력 | 기대 vec | 기대 mustNot |
|---|------|---------|-------------|
| 24 | `의료급여 외 받을 수 있는 지원` | `기초생활수급자 기타 지원 혜택` | `[의료급여]` |
| 25 | `기초연금 외에 노인 혜택` | `노인 기타 복지 혜택` | `[기초연금]` |
| 26 | `의료비 이외의 지원금` | `기타 지원금` | `[의료비]` |
| 27 | `기본급 그 외 추가 수당` | `기타 추가 수당` | `[기본급]` |
| 28 | `데미안 외 헤르만 헤세 작품` (history) | `헤르만 헤세 작품 목록` | `[데미안]` |

### 6.5 SUBSTITUTION (4건)

| # | 입력 | 기대 vec | 기대 mustNot |
|---|------|---------|-------------|
| 29 | `현대차 대신 기아 판매량` | `기아 판매량` | `[현대차]` |
| 30 | `수익이 아니라 영업이익` | `영업이익` | `[수익]` |
| 31 | `Python 대신 Rust 학습 자료` | `Rust 학습 자료` | `[Python]` |
| 32 | `스킨이 아니라 토너 추천` | `토너 추천` | `[스킨]` |

### 6.6 English (3건)

| # | 입력 | 기대 vec | 기대 intent |
|---|------|---------|------------|
| 33 | `not A but B revenue last year` | `B revenue last year` | PURE |
| 34 | `besides 의료급여 what else` | `의료급여 제외 기타 지원` (RESIDUAL) | RESIDUAL |
| 35 | `instead of running, low-impact cardio` | `low-impact cardio` | SUBSTITUTION |

### 6.7 Edge cases (3건)

| # | 입력 | 기대 결과 |
|---|------|----------|
| 36 | `` (빈 문자열) | skip (default DTO) |
| 37 | `   ` (공백만) | skip |
| 38 | LLM 응답 JSON 파싱 실패 | defaultDto fallback (원본 그대로) |

---

## 7. pytest 테스트 코드

```python
import pytest
from datetime import date
from unittest.mock import MagicMock
from contextual_query_rewriter import ContextualQueryRewriter, ExclusionIntent


def make_llm_response(intent="NONE", vector="", llm="",
                      must_keywords=None, must_not_keywords=None, anchor_entities=None):
    import json
    must_keywords = must_keywords or []
    must_not_keywords = must_not_keywords or []
    anchor_entities = anchor_entities or []
    has_exclusion = intent != "NONE"
    payload = {
        "exclusionIntent": intent,
        "vectorQuery": vector,
        "llmQuery": llm or vector,
        "mustKeywords": must_keywords,
        "mustNotKeywords": must_not_keywords,
        "anchorEntities": anchor_entities,
        "hasExclusion": has_exclusion,
    }
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = json.dumps(payload, ensure_ascii=False)
    return response


@pytest.fixture
def llm_client():
    client = MagicMock()
    client.chat.completions.create = MagicMock()
    return client


@pytest.fixture
def rewriter(llm_client):
    return ContextualQueryRewriter(llm_client=llm_client, today_provider=lambda: date(2026, 5, 21))


class TestNone:
    def test_self_evident_unchanged(self, llm_client, rewriter):
        llm_client.chat.completions.create.return_value = make_llm_response(
            intent="NONE",
            vector="Spring Boot Virtual Thread 사용법",
            llm="Spring Boot Virtual Thread 사용법")
        r = rewriter.rewrite("Spring Boot Virtual Thread 사용법")
        assert r.exclusion_intent == ExclusionIntent.NONE
        assert r.vector_query == "Spring Boot Virtual Thread 사용법"
        assert r.must_not_keywords == []
        assert not r.has_exclusion

    def test_blank_skipped(self, rewriter):
        r = rewriter.rewrite("   ")
        assert not r.rewritten


class TestPureExclusion:
    def test_pure_drops_entity_from_vec(self, llm_client, rewriter):
        llm_client.chat.completions.create.return_value = make_llm_response(
            intent="PURE_EXCLUSION",
            vector="SK하이닉스 영업이익",
            llm="삼성전자 제외 SK하이닉스 영업이익",
            must_keywords=["SK하이닉스", "영업이익"],
            must_not_keywords=["삼성전자"])
        r = rewriter.rewrite("삼성전자 빼고 SK하이닉스 영업이익")
        assert r.exclusion_intent == ExclusionIntent.PURE_EXCLUSION
        assert "삼성전자" not in r.vector_query
        assert r.must_not_keywords == ["삼성전자"]


class TestAnchorComparison:
    def test_anchor_keeps_entity_in_vec(self, llm_client, rewriter):
        llm_client.chat.completions.create.return_value = make_llm_response(
            intent="ANCHOR_COMPARISON",
            vector="아반떼 비슷한 가격대 차종",
            llm="아반떼 제외 비슷한 가격대 다른 차종",
            must_keywords=["아반떼", "비슷한", "가격대", "차종"],
            anchor_entities=["아반떼"])
        r = rewriter.rewrite("아반떼 말고 비슷한 가격대 다른 차종")
        assert r.exclusion_intent == ExclusionIntent.ANCHOR_COMPARISON
        assert "아반떼" in r.vector_query
        assert r.anchor_entities == ["아반떼"]
        assert r.must_not_keywords == []  # ANCHOR에서는 비어있어야 함

    def test_anchor_forces_empty_must_not_even_if_llm_fills(self, llm_client, rewriter):
        # LLM이 잘못 mustNot을 채워 보내도 ANCHOR면 강제로 비워야 함
        llm_client.chat.completions.create.return_value = make_llm_response(
            intent="ANCHOR_COMPARISON",
            vector="아반떼 비슷한 가격대",
            anchor_entities=["아반떼"],
            must_not_keywords=["아반떼"])  # LLM 실수
        r = rewriter.rewrite("아반떼 말고 비슷한 가격대")
        assert r.must_not_keywords == []


class TestResidualCategory:
    def test_residual_drops_entity_and_adds_other(self, llm_client, rewriter):
        llm_client.chat.completions.create.return_value = make_llm_response(
            intent="RESIDUAL_CATEGORY",
            vector="기초생활수급자 기타 지원 혜택",
            llm="의료급여 제외 기타 지원",
            must_not_keywords=["의료급여"])
        r = rewriter.rewrite("창녕군 기초생활수급자인데 의료급여 외 받을 수 있는 지원이 뭐가 있나요?")
        assert r.exclusion_intent == ExclusionIntent.RESIDUAL_CATEGORY
        assert "의료급여" not in r.vector_query
        assert r.must_not_keywords == ["의료급여"]


class TestSubstitution:
    def test_substitution(self, llm_client, rewriter):
        llm_client.chat.completions.create.return_value = make_llm_response(
            intent="SUBSTITUTION",
            vector="기아 판매량",
            llm="현대차 대신 기아 판매량",
            must_not_keywords=["현대차"])
        r = rewriter.rewrite("현대차 대신 기아 판매량")
        assert r.exclusion_intent == ExclusionIntent.SUBSTITUTION
        assert "현대차" not in r.vector_query
        assert r.must_not_keywords == ["현대차"]


class TestMultiTurn:
    def test_ellipsis_carryover(self, llm_client, rewriter):
        llm_client.chat.completions.create.return_value = make_llm_response(
            intent="NONE",
            vector="삼성전자 작년 매출",
            llm="삼성전자 작년 매출")
        history = "User: 삼성전자 2024년 매출 알려줘\nAssistant: 300조원"
        r = rewriter.rewrite("작년은?", history=history)
        assert "삼성전자" in r.vector_query

    def test_date_context_injection(self, llm_client, rewriter):
        rewriter.rewrite("작년 매출")
        call_args = llm_client.chat.completions.create.call_args
        user_msg = call_args.kwargs["messages"][1]["content"]
        # 2026-05-21 기준 → 작년 = 2025
        assert "2025" in user_msg
        assert "Today: 2026-05-21" in user_msg


class TestEdgeCases:
    def test_llm_exception_returns_default(self, llm_client, rewriter):
        llm_client.chat.completions.create.side_effect = Exception("LLM down")
        r = rewriter.rewrite("질문")
        assert r.vector_query == "질문"
        assert not r.rewritten

    def test_json_parse_failure_returns_default(self, llm_client, rewriter):
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = "not a json"
        llm_client.chat.completions.create.return_value = response
        r = rewriter.rewrite("질문")
        assert r.vector_query == "질문"

    def test_strips_quotes(self, llm_client, rewriter):
        import json
        payload = {
            "exclusionIntent": "PURE_EXCLUSION",
            "vectorQuery": '"B 매출"',
            "llmQuery": "B 매출",
            "mustKeywords": [], "mustNotKeywords": ["A"], "anchorEntities": [],
            "hasExclusion": True,
        }
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = json.dumps(payload)
        llm_client.chat.completions.create.return_value = response
        r = rewriter.rewrite("A 말고 B 매출")
        assert r.vector_query == "B 매출"
```

---

## 8. 다운스트림 통합

### 8.1 벡터 검색
```python
docs = vector_store.similarity_search(
    query=result.vector_query,       # 제외 entity 단어 없는 깨끗한 쿼리
    k=10
)
```

### 8.2 키워드 검색 (OpenSearch Bool Query)
```python
bool_query = {
    "bool": {
        "must": [{"match": {"content": result.vector_query}}],
        "must_not": [
            {"match": {"content": kw}} for kw in result.must_not_keywords
        ]
    }
}
opensearch.search(body={"query": bool_query})
```

### 8.3 LLM 답변
```python
prompt = f"""답변할 질문: {result.llm_query}

문서:
{retrieved_docs}

답변:"""
```

---

## 9. 알려진 한계

1. **"다른 X" 모호성**: 단순 추가 요청(NONE) vs 비교(ANCHOR) vs 잔여(RESIDUAL) — 문맥 의존, LLM도 가끔 잘못 분류
2. **gemma4-26b 기준 정확도**: 단위 100%, 라이브 ~96% (52건 중 2건 모호 분류)
3. **응답 시간**: vllm gemma4-26b 기준 평균 200~700ms (모델·캐시 상태 의존)
4. **JSON 출력 강제**: LLM이 가끔 prose 응답 → JSON 추출 정규식으로 보강. 그래도 실패 시 defaultDto fallback
5. **현재 날짜 주입**: User prompt에 동적 삽입 — 상대 시간("작년/올해/내년") 정확 해소

---

## 10. 권장 설정

| 항목 | 권장 값 | 이유 |
|------|--------|------|
| Model | gpt-4o-mini / gemma4-26b 이상 | JSON 출력 + few-shot 학습 안정성 |
| Temperature | 0 | 분류 일관성 |
| max_tokens | 1024 | OUTPUT_SPEC + 응답에 충분 |
| Timeout | 5s | 빠른 재작성 보장 |
| Fallback | originalQuery 반환 | LLM 장애 시 검색 계속 가능 |

---

## 11. 참고 — Java 원본 위치

| 파일 | 역할 |
|------|------|
| `ContextualQueryRewriterService.java` | 핵심 로직 (프롬프트 + LLM 호출 + 파싱) |
| `ContextualQueryRewriterNode.java` | LangGraph4j 워크플로우 노드 래퍼 |
| `QueryRewriteResultDto.java` | DTO |
| `ExclusionIntent.java` | Intent enum |
| `ContextualQueryRewriterServiceTest.java` | 단위 테스트 28건 |
| `ContextualQueryRewriterNodeTest.java` | Node 테스트 10건 |
