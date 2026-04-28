"""WAV(RIFF) 파일 파싱, 빌드, 병합 유틸리티"""

import base64
import struct
from typing import Optional


def _parse_riff_wav_chunks(wav_bytes: bytes) -> tuple[bytes, bytes, list[tuple[bytes, bytes]]]:
    if len(wav_bytes) < 12 or wav_bytes[:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
        raise ValueError("유효한 WAV(RIFF) 데이터가 아닙니다.")

    offset = 12
    fmt_chunk: Optional[bytes] = None
    data_chunk: Optional[bytes] = None
    extra_chunks: list[tuple[bytes, bytes]] = []

    while offset + 8 <= len(wav_bytes):
        chunk_id = wav_bytes[offset:offset + 4]
        chunk_size = struct.unpack("<I", wav_bytes[offset + 4:offset + 8])[0]
        data_start = offset + 8
        data_end = data_start + chunk_size

        if data_end > len(wav_bytes):
            raise ValueError("WAV chunk size가 비정상입니다.")

        payload = wav_bytes[data_start:data_end]

        if chunk_id == b"fmt ":
            fmt_chunk = payload
        elif chunk_id == b"data":
            data_chunk = payload
        else:
            extra_chunks.append((chunk_id, payload))

        offset = data_end + (chunk_size % 2)

    if fmt_chunk is None or data_chunk is None:
        raise ValueError("WAV에 필수 chunk(fmt/data)가 없습니다.")

    return fmt_chunk, data_chunk, extra_chunks


def _build_riff_wav(fmt_chunk: bytes, data_chunk: bytes, extra_chunks: list[tuple[bytes, bytes]]) -> bytes:
    def build_chunk(chunk_id: bytes, payload: bytes) -> bytes:
        padding = b"\x00" if (len(payload) % 2) else b""
        return chunk_id + struct.pack("<I", len(payload)) + payload + padding

    chunk_bytes = [build_chunk(b"fmt ", fmt_chunk)]
    for chunk_id, payload in extra_chunks:
        chunk_bytes.append(build_chunk(chunk_id, payload))
    chunk_bytes.append(build_chunk(b"data", data_chunk))

    body = b"".join(chunk_bytes)
    riff_size = 4 + len(body)
    return b"RIFF" + struct.pack("<I", riff_size) + b"WAVE" + body


def _merge_wav_base64_segments(base64_segments: list[str]) -> str:
    if not base64_segments:
        raise ValueError("병합할 TTS 세그먼트가 없습니다.")

    first_wav = base64.b64decode(base64_segments[0])
    base_fmt, first_data, base_extra = _parse_riff_wav_chunks(first_wav)
    merged_data_parts = [first_data]

    for segment in base64_segments[1:]:
        wav_bytes = base64.b64decode(segment)
        fmt_chunk, data_chunk, _ = _parse_riff_wav_chunks(wav_bytes)
        if fmt_chunk != base_fmt:
            raise ValueError("TTS 세그먼트의 fmt chunk가 서로 다릅니다.")
        merged_data_parts.append(data_chunk)

    merged_wav = _build_riff_wav(base_fmt, b"".join(merged_data_parts), base_extra)
    return base64.b64encode(merged_wav).decode("utf-8")
