"""
최종 응답 생성 v2 — 공통 모듈

모든 v2 의도(general, comparison, guide_recommend)에서 공유합니다.
v1 대비 변경:
- _format_document_for_prompt_v2: doc["_welfare_tel"] 사전 조회 결과 참조 (v1의 즉시 DB 조회 제거)
- generate_final_response_v2: intent별 프롬프트 분기 + guide_recommend 메타데이터 전달 + use_llm_recommended_prompt 시 LLM 추천 프롬프트

기존 services/rag_service.py는 변경하지 않습니다.
"""

import json
import logging
import re
from typing import Dict, Any, List, Optional

from app.core.constants import ROLE_USER, ROLE_ASSISTANT, LLM_MAX_DOC_CHUNK_CHARS

from app.chat.infra.rag import (
    _get_document_name,
    _get_document_snippet,
    _truncate_text_by_tokens,
)
from app.chat.infra.rag.document import (
    _format_facility_for_prompt,
    _format_our_region_tel_for_prompt,
)
from app.chat.infra.llm import call_llm_api
from app.chat.infra.db.welfare_tel import has_unregistered_contact
from app.shared.utils.prompt_loader import (
    load_system_prompt,
    load_classification_general_prompt,
    load_classification_comparison_prompt,
    load_classification_recommended_prompt,
    load_classification_llm_recommended_prompt,
    load_classification_search_prompt,
)
from app.chat.infra.rag.policy_priority import soft_priority_instruction_for_prompt

logger = logging.getLogger(__name__)


# ============================================================
# 문서 포맷터 v2 (사전 조회된 WELFARE_TEL 참조)
# ============================================================

def format_document_for_prompt_v2(
    doc: Dict[str, Any], index: int, max_doc_chars: int
) -> str:
    """
    최종 응답 프롬프트용 문서 블록 생성 (v2)

    v1 대비 변경:
    - doc["_welfare_tel"]이 존재하면 해당 데이터를 프롬프트에 포함
    - 미등록 전화번호(-0000)이면서 _welfare_tel이 없는 경우에도 별도 DB 조회하지 않음
      (이미 사전 일괄 조회 완료)
    """
    if doc.get("_source") == "our_region_tel":
        return _format_our_region_tel_for_prompt(doc, index)

    if any(str(doc.get(key, "") or "").strip() for key in ("FACILITY_NAME", "FACILITY_TYPE", "ADDRESS")):
        return _format_facility_for_prompt(doc, index)

    if not (doc.get("CONTENT") or doc.get("YEAR") or doc.get("SIGUN")):
        name = _get_document_name(doc)
        chunk_path = _truncate_text_by_tokens(
            _get_document_snippet(doc), max_doc_chars
        )
        return f"[문서 {index}] {name}\n{chunk_path}\n\n"

    sigun = str(doc.get("SIGUN", "") or "").strip() or "정보 없음"
    year = str(doc.get("YEAR", "") or "").strip() or "정보 없음"
    content = _truncate_text_by_tokens(_get_document_snippet(doc), max_doc_chars)
    lines = [
        f"[문서 {index}]",
        f"- 문서명: {_get_document_name(doc)}",
        f"- 지역: {sigun}",
        f"- 작성/시행일: {year}",
    ]
    for field, label in [
        ("DEPARTMENT", "담당부서"),
        ("APPLICATION_PERIOD", "신청기간"),
        ("PURPOSE", "목적"),
        ("TEL", "연락처"),
    ]:
        val = str(doc.get(field, "") or "").strip()
        if val:
            lines.append(f"- {label}: {val}")

    # 미등록 전화번호 처리 (본문에서 0000 패턴 제거)
    unregistered = has_unregistered_contact(doc)
    if unregistered:
        content = re.sub(r'\d{2,3}-?\d{3,4}-?0000\S*', '', content)

    lines.append(f"- 내용: {content}")
    return "\n".join(lines) + "\n\n"


# ============================================================
# 최종 응답 생성 v2 (모든 의도 공통)
# ============================================================

async def generate_final_response_v2(
    message: str,
    top_docs: List[Dict[str, Any]],
    temperature: float,
    max_tokens: int,
    stream: bool,
    frequency_penalty: float,
    repetition_penalty: float,
    top_p: float,
    top_k: int,
    seed: int,
    tools: list,
    intent: str = "general",
    welfare_docs: Optional[List[Dict[str, Any]]] = None,
    lifecycle: str = "",
    messages: list = None,
    user_region: str = "",
    user_birth_year: str = "",
    policy_priority_tag: Optional[str] = None,
    more_info_mode: bool = False,
    use_llm_recommended_prompt: bool = False,
) -> Any:
    """
    v2 최종 응답 생성 (모든 의도 공통)

    Args:
        message: 사용자 질문
        top_docs: 최종 선택 문서 목록
        intent: 의도 ("general", "comparison", "guide_recommend", "search")
        welfare_docs: 복지시설 문서 목록 (optional)
        lifecycle: 생애주기 (guide_recommend용)
        user_region: 사용자 지역 (guide_recommend용, v2 Step 0에서 추출)
        user_birth_year: 사용자 출생연도 (guide_recommend용, v2 Step 0에서 추출)
        more_info_mode: True면 '더 알려줘'용 [추가 규칙]을 붙이고 DB 정책 우선순위 프롬프트는 생략.
        use_llm_recommended_prompt: guide_recommend일 때 True면 classification_llm_recommended 프롬프트 사용.
    """
    try:
        # 문서 내용 구성 — 개수 기준으로 전체 포함 (문자 수 제한 제거)
        max_doc_chunk_chars = LLM_MAX_DOC_CHUNK_CHARS
        doc_content = ""
        for i, doc in enumerate(top_docs, 1):
            candidate = format_document_for_prompt_v2(doc, i, max_doc_chunk_chars)
            doc_content += candidate
            logger.info(f"[ResponseGen] 문서 #{i} 포함 (크기={len(candidate)}, 누적={len(doc_content)})")
        logger.info(f"[ResponseGen] 프롬프트 문서 구성 완료: {len(top_docs)}개 포함, doc_content={len(doc_content)}자")

        if not doc_content:
            has_history = bool(messages and len(messages) >= 3)
            doc_content = (
                "제공된 참고 문서가 없습니다. 이전 대화 내용을 참고하여 답변해 주세요."
                if has_history
                else "제공된 참고 문서가 없습니다."
            )

        # intent별 프롬프트 선택
        if intent == "comparison":
            final_prompt = load_classification_comparison_prompt()
            logger.info("[Final Response v2] Comparison 프롬프트 사용")
        elif intent == "guide_recommend":
            if use_llm_recommended_prompt:
                final_prompt = load_classification_llm_recommended_prompt()
                logger.info("[Final Response v2] Guide_Recommend LLM recommended 프롬프트 사용")
            else:
                final_prompt = load_classification_recommended_prompt()
                logger.info("[Final Response v2] Guide_Recommend 프롬프트 사용")
        elif intent == "search":
            final_prompt = load_classification_search_prompt()
            logger.info("[Final Response v2] Search 프롬프트 사용")
        else:
            final_prompt = load_classification_general_prompt()
            logger.info("[Final Response v2] General 프롬프트 사용")

        if not final_prompt:
            logger.warning(f"[Final Response v2] {intent} 프롬프트 로드 실패 - 기본 LLM 사용")
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "[Final Response v2][LLM Input Params/Fallback] %s",
                    json.dumps(
                        {
                            "intent": intent,
                            "temperature": temperature,
                            "max_tokens": max_tokens,
                            "stream": stream,
                            "frequency_penalty": frequency_penalty,
                            "repetition_penalty": repetition_penalty,
                            "top_p": top_p,
                            "top_k": top_k,
                            "seed": seed,
                            "tools": tools,
                            "messages": [{"role": ROLE_USER, "content": message}],
                            "multi_turn_messages": messages or [],
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                )
            # 멀티턴 히스토리가 있으면 포함 (LLM이 대화 맥락을 파악하도록)
            _fb_msgs = [m for m in (messages or []) if m.get("role") in (ROLE_USER, ROLE_ASSISTANT)]
            if not _fb_msgs:
                _fb_msgs = [{"role": ROLE_USER, "content": message}]
            elif _fb_msgs[-1].get("content") != message:
                _fb_msgs.append({"role": ROLE_USER, "content": message})
            return await call_llm_api(
                temperature=temperature,
                max_tokens=max_tokens,
                messages=_fb_msgs,
                frequency_penalty=frequency_penalty,
                repetition_penalty=repetition_penalty,
                top_p=top_p,
                top_k=top_k,
                seed=seed,
                tools=tools,
            )

        system_prompt = final_prompt

        # 복지시설 문서 블록 구성
        facility_content = ""
        if welfare_docs:
            for i, wdoc in enumerate(welfare_docs, 1):
                facility_content += _format_facility_for_prompt(wdoc, i)

        # user_message 구성
        if intent == "guide_recommend":
            user_life_stage = lifecycle if lifecycle else "정보 없음"
            region_display = user_region if user_region else "정보 없음"
            birth_year_display = str(user_birth_year) if user_birth_year else "정보 없음"
            user_message = f"""사용자 질문: {message}
        user_region: {region_display}
        user_birth_year: {birth_year_display}
        user_life_stage: {user_life_stage}
        retrieved_documents:
        {doc_content}"""
        else:
            user_message = f"""사용자 질문: {message}
        retrieved_documents:
        {doc_content}"""

        logger.info("[ResponseGen] more_info_mode=%s intent=%s → 추가규칙 주입=%s", more_info_mode, intent, more_info_mode and intent == "general")
        if more_info_mode and intent == "general":
            logger.info("[ResponseGen] [추가 규칙] 4단계 구조 주입 ✓")
            user_message += (
                "\n\n[추가 규칙]\n"
                "이번 응답은 사용자의 '더 자세히' 요청입니다. 반드시 형식 B(4단계 구조)로 자세히 답변하십시오.\n"
                "출력 형식:\n"
                "{기준연도} 기준 {지역} {서비스/제도명} 사업 안내입니다.\n\n"
                "1. 사업 개요\n"
                "- {사업 목적과 핵심 내용을 한두 문장으로}\n\n"
                "2. 상세 요건\n"
                "- {지원 대상 / 연령 요건 / 가구 유형}\n"
                "- {소득 기준 / 선정기준액}\n"
                "- {제외·예외·특례 조건}\n\n"
                "3. 지원 혜택\n"
                "- {지급 금액 / 기준연금액}\n"
                "- {지급 방식 / 지급 주기 / 지급일}\n"
                "- {감액 규정 등 부가 사항}\n\n"
                "4. 신청 안내\n"
                "- {신청 기간}\n"
                "- {신청 방법 — 온라인 / 방문 / 기관별}\n"
                "- {제출 서류}\n"
                "- {문의처 / 연락처}\n\n"
                "섹션 헤더는 마크다운 강조(#, **) 없이 '1. 사업 개요' 형태로만 작성합니다.\n"
                "문서에 없는 내용은 '정보 없음'으로 명시합니다.\n"
            )
        elif more_info_mode and intent not in ("recommended_question",):
            user_message += (
                "\n\n[추가 규칙]\n"
                "- 이번 응답은 사용자의 '더 알려줘' 요청에 따른 추가 탐색 결과입니다.\n"
                "- retrieved_documents에 근거할 수 있는 사업·서비스가 있으면, "
                "시스템 프롬프트의 규칙과 출력 형식을 그대로 따르고 문서만을 근거로 안내합니다.\n"
                "- 이전 답변과의 중복 여부는 추정하지 말고, 문서에 적힌 내용으로 안내 가능하면 포함합니다.\n"
                "- 문서를 검토한 뒤 안내할 근거가 없을 때에만 짧은 안내를 덧붙일 수 있으며, "
                "그 경우에도 답변 전체를 한 줄·한 문장으로만 제한하지 마세요.\n"
            )

        if facility_content:
            user_message += (
                f"\n        retrieved_facilities:\n        {facility_content}"
            )
        if not more_info_mode:
            user_message += soft_priority_instruction_for_prompt(policy_priority_tag)
        user_message += " "

        final_messages = [{"role": ROLE_USER, "content": user_message}]

        logger.debug("[Final Response v2] system_prompt:\n%s", system_prompt)
        logger.debug("[Final Response v2] messages:\n%s", final_messages)

        # intent별 전용 프롬프트가 있을 때는 general system_prompt 제외
        # (general system_prompt의 "비교 금지" 등 규칙이 comparison 등과 충돌)
        if intent == "general":
            combined_prompts = [load_system_prompt(), system_prompt]
        else:
            combined_prompts = [system_prompt]

        # 풀 페이로드 직렬화는 디버그 시에만 수행 (TTFT 절감)
        # INFO에는 핵심 카운트만 남겨 운영 가시성을 유지한다.
        logger.info(
            "[Final Response v2][LLM Input Summary] intent=%s stream=%s "
            "temperature=%s max_tokens=%s top_docs=%d welfare_docs=%d "
            "lifecycle=%s region=%s birth_year=%s more_info=%s",
            intent, stream, temperature, max_tokens,
            len(top_docs or []), len(welfare_docs or []),
            lifecycle or "", user_region or "", user_birth_year or "", more_info_mode,
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[Final Response v2][MultiTurn Full Messages] %s",
                json.dumps(messages or [], ensure_ascii=False, default=str),
            )
            logger.debug(
                "[Final Response v2][LLM Input Params] %s",
                json.dumps(
                    {
                        "intent": intent,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "stream": stream,
                        "frequency_penalty": frequency_penalty,
                        "repetition_penalty": repetition_penalty,
                        "top_p": top_p,
                        "top_k": top_k,
                        "seed": seed,
                        "tools": tools,
                        "lifecycle": lifecycle,
                        "user_region": user_region,
                        "user_birth_year": user_birth_year,
                        "more_info_mode": more_info_mode,
                        "top_docs_count": len(top_docs or []),
                        "welfare_docs_count": len(welfare_docs or []),
                        "extra_system_prompts": combined_prompts,
                        "messages": final_messages,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            )

        response = await call_llm_api(
            temperature=temperature,
            max_tokens=max_tokens,
            messages=final_messages,
            extra_system_prompts=combined_prompts,
            stream=stream,
            frequency_penalty=frequency_penalty,
            repetition_penalty=repetition_penalty,
            top_p=top_p,
            top_k=top_k,
            seed=seed,
            tools=tools,
        )

        logger.info("[Final Response v2] 응답 생성 완료")
        return response

    except Exception as e:
        logger.error(f"[Final Response v2] 오류: {e}", exc_info=True)
        raise
