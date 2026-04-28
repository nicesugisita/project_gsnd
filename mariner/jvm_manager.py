"""
JVM 싱글톤 매니저 — mariner 전용

서버 기동 시 1회 초기화하고, 모든 queryset_* 모듈에서 공유합니다.
워커 스레드에서 호출 시 자동으로 JVM 스레드를 등록합니다.

사용법:
    from mariner.jvm_manager import init_jvm, ensure_jvm_thread

    # 서버 기동 시 (app.py lifespan)
    init_jvm()

    # 각 queryset 함수 내부 (워커 스레드에서 호출 시)
    ensure_jvm_thread()
"""

import glob
import logging
import os
import threading

import jpype

from core.config import Config

logger = logging.getLogger(__name__)

_init_lock = threading.Lock()
_initialized = False


def _get_jar_files() -> list[str]:
    """JAR 라이브러리 파일 경로 조회"""
    return glob.glob(os.path.join(Config.JAR_LIB_PATH, '*.jar'))


def init_jvm() -> None:
    """
    JVM 초기화 (서버 기동 시 1회 호출)

    이미 시작된 경우 무시합니다.
    """
    global _initialized

    if _initialized and jpype.isJVMStarted():
        logger.debug("[JVM] 이미 초기화됨 — 건너뜀")
        return

    with _init_lock:
        if jpype.isJVMStarted():
            _initialized = True
            logger.info("[JVM] 이미 시작된 JVM 감지 — 재사용")
            return

        jar_files = _get_jar_files()
        if not jar_files:
            logger.error(f"[JVM] JAR 파일을 찾을 수 없습니다: {Config.JAR_LIB_PATH}")
            raise RuntimeError(f"JAR 라이브러리 경로 오류: {Config.JAR_LIB_PATH}")

        # classpath = ':'.join(jar_files)          # Linux
        classpath = os.pathsep.join(jar_files)  # Windows
        logger.info(f"[JVM] 초기화 시작 — JAR {len(jar_files)}개 로드")

        jpype.startJVM(
            jpype.getDefaultJVMPath(),
            f"-Djava.class.path={classpath}",
            convertStrings=True,
        )

        _initialized = True
        logger.info("[JVM] 초기화 완료")


def ensure_jvm_thread() -> None:
    """
    JVM 실행 보장 + 현재 스레드를 JVM에 등록

    JVM이 아직 시작되지 않은 경우 자동으로 init_jvm()을 호출합니다.
    메인 스레드에서는 자동 등록되므로 호출해도 무해합니다.
    """
    if not jpype.isJVMStarted():
        init_jvm()
    if not jpype.isThreadAttachedToJVM():
        jpype.attachThreadToJVM()
