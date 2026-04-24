"""TTS/STT 라우트 핸들러"""

import asyncio
import logging
import json

import re
from datetime import datetime

from routers.tts.pipeline import PreprocessingPipeline

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

from .tts_helpers import (
    _extract_tts_raw_base64,
    _request_tts_with_chunk_fallback,
    convert_text_for_tts,
    send_tts_request_and_collect_base64,
    stream_tts_base64
)

logger = logging.getLogger(__name__)

router = APIRouter()

##TTS 음성합성 요청 API (한게 Response)
@router.post("/api/tts/synthesize")
async def synthesize_speech(request: Request):
    pipeline = PreprocessingPipeline()

    try:
        body = await request.json()
        text = body.get("text", "").strip()

        if not text:
            return JSONResponse(
                content={
                    "result": {"raw_base64": "", "duration": 0},
                    "errorCode": 400,
                    "errorMessage": "텍스트가 비어있습니다."
                },
                status_code=400
            )

        #logger.info(f"=== TTS 요청 원본 텍스트  ===\n{text[:]}")
        is_clarification_yn = body.get("is_clarification", False)
        #logger.info  (f" ====되묻기여부 ============{is_clarification_yn}")
        #TTS 답변정제 프롬프트 적용 후 텍스트 , is_clarification 값이 false일 때만 적용한다.
        if not is_clarification_yn:
            # False(거짓)일 때 실행되는 구간
            converted_text = await convert_text_for_tts(text)
        else:
            # True(참)일 때 실행되는 구간 (원본 유지)
            converted_text = text
        logger.info(f"=== TTS 프롬프트 적용 결과 ===\n{text[:]}")
        # TTS 문장 전처리
        converted_segments = pipeline.process(converted_text)
        logger.info(f"=== TTS 전처리 파이프라인 적용 결과  ===\n{converted_segments}")

        # segment  단위 별로  TTS STREAM API 호출 및 반환
        if converted_segments:
            final_json = await send_tts_request_and_collect_base64(converted_segments)
        else:
            final_json = {
                "result": {"raw_base64": "", "duration": 0},
                "errorCode": 404,
                "errorMessage": "전처리된 세그먼트가 없습니다."
            }

        return JSONResponse(content=final_json, status_code=200)

    except Exception as e:
        logger.error(f"❌ TTS 처리 오류: {e}")
        return JSONResponse(
            content={
                "result": {"raw_base64": "", "duration": 0},
                "errorCode": 500,
                "errorMessage": f"TTS 처리 실패: {str(e)}"
            },
            status_code=500
        )

# TTS 음성합성 요청 API (Stream 방식응답 )
@router.post("/api/tts/stream")
async def get_tts_stream(request: Request):
    pipeline = PreprocessingPipeline()

    try:
        body = await request.json()
        text = body.get("text", "").strip()

        #TTS 답변정제 프롬프트 적용 후 텍스트 , is_clarification 값이 false일 때만 적용한다.
        is_clarification_guess = text.endswith("?") or len(text) < 150
        if is_clarification_guess:
           converted_text = text
        else:
           converted_text = await convert_text_for_tts(text)
        segments = pipeline.process(converted_text)

        if not segments:
            # 에러 발생 시에도 스트림 형식을 유지하거나 JSONResponse 반환
            return JSONResponse(status_code=404, content={"errorMessage": "전처리된 문장이 없습니다."})

        async def event_generator():
            async for chunk_data in stream_tts_base64(segments):
                # 1. 데이터를 JSON 문자열로 변환
                json_str = json.dumps(chunk_data, ensure_ascii=False)
                # 2. 개행 문자(\n)를 붙여서 한 줄씩 전송 (NDJSON 방식)
                yield json_str + "\n"

        return StreamingResponse(
            event_generator(),
            media_type="application/x-ndjson",
            headers={
                "Content-Type": "application/x-ndjson",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"  # Nginx 버퍼링 방지 필수 설정
            }
        )

    except Exception as e:
        logger.error(f"❌ TTS 처리 오류: {e}")
        return JSONResponse(status_code=500, content={"errorMessage": str(e)})


##STT API
@router.websocket("/api/stt/stream")
async def stt_streaming_endpoint(websocket: WebSocket):
    """
    실시간 STT 스트리밍 WebSocket 엔드포인트

    Protocol:
    - 클라이언트 -> 서버: 바이너리 오디오 데이터 또는 "EOS" 텍스트
    - 서버 -> 클라이언트: JSON {"transcript": str, "final": bool, "timestamp": str}
    """
    from services.async_stt_service import AsyncSTTStreamingService

    await websocket.accept()
    logger.info(f"STT WebSocket 클라이언트 연결: {websocket.client}")

    stt_service = None
    ws_connected = True

    try:
        stt_service = AsyncSTTStreamingService()

        async def forward_to_client(transcript: str, is_final: bool):
            try:
                await websocket.send_json({
                    "transcript": transcript,
                    "final": is_final,
                    "timestamp": datetime.now().isoformat()
                })
            except Exception as e:
                logger.error(f"클라이언트 전송 실패: {e}")

        async def error_callback(error_msg: str):
            try:
                await websocket.send_json({
                    "error": error_msg,
                    "timestamp": datetime.now().isoformat()
                })
            except Exception as e:
                logger.error(f"에러 메시지 전송 실패: {e}")

        try:
            await stt_service.connect(
                on_message_callback=lambda t, f: asyncio.create_task(forward_to_client(t, f)),
                on_error_callback=lambda e: asyncio.create_task(error_callback(e))
            )
            logger.info("STT 서버 연결 성공")
            await websocket.send_json({
                "status": "connected",
                "message": "STT 서버에 연결되었습니다",
                "timestamp": datetime.now().isoformat()
            })
        except Exception as e:
            logger.error(f"STT 서버 연결 실패: {e}")
            await websocket.send_json({
                "error": f"STT 서버 연결 실패: {str(e)}",
                "timestamp": datetime.now().isoformat()
            })
            await websocket.close()
            return

        while True:
            try:
                data = await websocket.receive()

                if data.get("type") == "websocket.disconnect":
                    logger.info("클라이언트 연결 종료 (disconnect 메시지)")
                    ws_connected = False
                    break

                if "text" in data:
                    message = data["text"]
                    logger.info(f"텍스트 메시지 수신: {message}")

                    if message == "EOS":
                        logger.info("EOS 신호 수신, 스트림 종료")
                        await stt_service.send_eos()
                        await websocket.send_json({
                            "status": "completed",
                            "message": "음성 인식이 완료되었습니다",
                            "timestamp": datetime.now().isoformat()
                        })
                        break

                elif "bytes" in data:
                    audio_data = data["bytes"]
                    await stt_service.send_audio_data(audio_data)
                    logger.debug(f"오디오 데이터 전송: {len(audio_data)} bytes")

            except WebSocketDisconnect:
                logger.info("클라이언트 연결 종료")
                ws_connected = False
                break
            except Exception as e:
                logger.error(f"데이터 처리 오류: {e}")
                if ws_connected:
                    try:
                        await websocket.send_json({
                            "error": f"데이터 처리 오류: {str(e)}",
                            "timestamp": datetime.now().isoformat()
                        })
                    except Exception:
                        ws_connected = False
                break

    except Exception as e:
        logger.error(f"STT 스트리밍 오류: {e}")
        if ws_connected:
            try:
                await websocket.send_json({
                    "error": f"STT 스트리밍 오류: {str(e)}",
                    "timestamp": datetime.now().isoformat()
                })
            except Exception:
                pass

    finally:
        if stt_service:
            try:
                await stt_service.close()
            except Exception:
                pass
        if ws_connected:
            try:
                await websocket.close()
            except Exception:
                pass
        logger.info("STT WebSocket 연결 정리 완료")


