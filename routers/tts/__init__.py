"""
TTS/STT 라우터 패키지

기존 routers.tts import 경로와 완전 호환됩니다.
"""

from .routes import router
from .tts_helpers import (
    _extract_tts_raw_base64,
    remove_raw_base64,
    _split_text_for_tts,
    _request_tts_with_chunk_fallback,
    convert_text_for_tts,
    send_text_to_tts_server,
)
from .wav_utils import (
    _parse_riff_wav_chunks,
    _build_riff_wav,
    _merge_wav_base64_segments,
)

__all__ = [
    "router",
    "_extract_tts_raw_base64", "remove_raw_base64", "_split_text_for_tts",
    "_request_tts_with_chunk_fallback", "convert_text_for_tts",
    "send_text_to_tts_server",
    "_parse_riff_wav_chunks", "_build_riff_wav", "_merge_wav_base64_segments",
]
