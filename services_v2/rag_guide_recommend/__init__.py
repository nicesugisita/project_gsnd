"""
RAG guide_recommend 파이프라인 패키지

기존 services_v2.rag_guide_recommend import 경로와 완전 호환됩니다.
"""

from .pipeline import process_rag_guide_recommend

__all__ = ["process_rag_guide_recommend"]
