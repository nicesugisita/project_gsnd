import httpx
import base64
from app.core.config import Config

TTS_API_URL = f"{Config.TTS_SERVER_URL.rstrip('/')}/service/ttsstream"

async def request_tts_stream(text: str, sid: int = 0, tempo: int = 1, pad_silence: int = 0, amplify: int = 1, gain_db: int = 0):
    payload = {
        "masterKey": Config.TTS_MASTER_KEY,
        "text": text,
        "sid": sid,
        "tempo": tempo,
        "padSilence": pad_silence,
        "amplify": amplify,
        "gainDb": gain_db,
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(TTS_API_URL, json=payload)
        response.raise_for_status()
        return response.json()


async def request_tts_audio_stream(
    text: str,
    sid: int = 0,
    tempo: int = 1,
    pad_silence: int = 0,
    amplify: int = 1,
    gain_db: int = 0,
) -> bytes:
    payload = {
        "masterKey": Config.TTS_MASTER_KEY,
        "text": text,
        "sid": sid,
        "tempo": tempo,
        "padSilence": pad_silence,
        "amplify": amplify,
        "gainDb": gain_db,
    }

    async with httpx.AsyncClient(timeout=Config.TTS_REQUEST_TIMEOUT) as client:
        response = await client.post(TTS_API_URL, json=payload)
        response.raise_for_status()

        content_type = (response.headers.get("content-type") or "").lower()

        if "application/json" in content_type:
            data = response.json()
            audio_base64 = data.get("audioBase64") or data.get("audio") or data.get("wavBase64")
            if audio_base64:
                return base64.b64decode(audio_base64)
            raise ValueError("TTS 응답(JSON)에서 오디오 데이터를 찾을 수 없습니다.")

        return response.content



