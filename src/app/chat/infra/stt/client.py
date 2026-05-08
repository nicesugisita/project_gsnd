"""
Real-time STT service using async WebSocket.

비동기 WebSocket을 사용하여 STT 서버와 통신합니다.
"""

import json
import logging
import asyncio
from typing import Optional, Callable
import websockets

from app.core.config import Config

logger = logging.getLogger(__name__)


class AsyncSTTStreamingService:
    """
    비동기 STT 스트리밍 서비스
    
    FastAPI의 비동기 환경에서 STT 서버와 WebSocket 통신을 처리합니다.
    """
    
    def __init__(self, stt_server_url: str = None):
        """
        Args:
            stt_server_url: STT WebSocket 서버 URL
        """
        self.stt_server_url = stt_server_url or Config.STT_WEBSOCKET_URL
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.is_connected = False
        self._receive_task: Optional[asyncio.Task] = None
        
    async def connect(self, 
                     on_message_callback: Callable[[str, bool], None],
                     on_error_callback: Optional[Callable[[str], None]] = None):
        """
        STT 서버에 WebSocket 연결을 설정합니다.
        
        Args:
            on_message_callback: 메시지 수신 시 호출될 콜백 (transcript, is_final)
            on_error_callback: 에러 발생 시 호출될 콜백
        """
        try:
            logger.info(f"STT 서버 연결 시도: {self.stt_server_url}")
            
            # WebSocket 연결
            self.ws = await asyncio.wait_for(
                websockets.connect(
                    self.stt_server_url,
                    ping_interval=Config.STT_PING_INTERVAL,
                    ping_timeout=Config.STT_PING_TIMEOUT,
                    close_timeout=Config.STT_CLOSE_TIMEOUT
                ),
                timeout=Config.STT_CONNECTION_TIMEOUT
            )
            
            self.is_connected = True
            logger.info("STT 서버 연결 성공")
            
            # 메시지 수신 태스크 시작
            self._receive_task = asyncio.create_task(
                self._receive_loop(on_message_callback, on_error_callback)
            )
            
        except asyncio.TimeoutError:
            error_msg = f"STT 서버 연결 타임아웃 ({int(Config.STT_CONNECTION_TIMEOUT)}초)"
            logger.error(error_msg)
            if on_error_callback:
                on_error_callback(error_msg)
            raise ConnectionError(error_msg)
        except Exception as e:
            error_msg = f"STT 서버 연결 실패: {e}"
            logger.error(error_msg)
            if on_error_callback:
                on_error_callback(error_msg)
            raise ConnectionError(error_msg)
    
    async def _receive_loop(self,
                           on_message_callback: Callable[[str, bool], None],
                           on_error_callback: Optional[Callable[[str], None]] = None):
        """
        STT 서버로부터 메시지를 지속적으로 수신합니다.
        """
        try:
            async for message in self.ws:
                try:
                    # JSON 메시지 파싱
                    data = json.loads(message)
                    is_final = data.get('final', False)
                    transcript = data.get('transcript', '')
                    
                    if is_final:
                        logger.info(f"STT 최종 결과: {transcript}")
                    else:
                        logger.debug(f"STT 중간 결과: {transcript}")
                    
                    # 콜백 호출
                    on_message_callback(transcript, is_final)
                    
                except json.JSONDecodeError as e:
                    logger.error(f"STT 메시지 파싱 실패: {e}, 원본: {message}")
                except Exception as e:
                    logger.error(f"메시지 처리 오류: {e}")
                    
        except websockets.exceptions.ConnectionClosed:
            logger.info("STT WebSocket 연결이 정상적으로 종료되었습니다")
            self.is_connected = False
        except Exception as e:
            error_msg = f"STT 메시지 수신 오류: {e}"
            logger.error(error_msg)
            self.is_connected = False
            if on_error_callback:
                on_error_callback(error_msg)
    
    async def send_audio_data(self, audio_data: bytes):
        """
        오디오 데이터를 STT 서버로 전송합니다.
        
        Args:
            audio_data: 오디오 바이너리 데이터
        """
        if not self.is_connected or not self.ws:
            raise ConnectionError("STT 서버에 연결되지 않음")
        
        try:
            await self.ws.send(audio_data)
        except Exception as e:
            logger.error(f"오디오 데이터 전송 실패: {e}")
            raise
    
    async def send_eos(self):
        """
        EOS (End Of Stream) 신호를 전송하여 스트림 종료를 알립니다.
        """
        if self.is_connected and self.ws:
            try:
                await self.ws.send("EOS")
                logger.info("EOS 신호 전송")
            except Exception as e:
                logger.error(f"EOS 전송 실패: {e}")
    
    async def close(self):
        """
        WebSocket 연결을 종료합니다.
        """
        # 수신 태스크 취소
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        
        # WebSocket 종료
        if self.ws:
            try:
                await self.send_eos()
                await self.ws.close()
                logger.info("STT 서비스 종료")
            except Exception as e:
                logger.error(f"연결 종료 실패: {e}")
            finally:
                self.is_connected = False
                self.ws = None
