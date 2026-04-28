"""
설정 파일 로더

yaml 설정 파일을 로딩하고 캐싱합니다.

설정 파일 경로: saltlux/config/
- normalization.yaml: 숫자 읽기 규칙
- patterns.yaml: 정규표현식 패턴
- wav_mappings.yaml: Local WAV 매핑
- domains.yaml: 도메인 발음 사전
"""

import yaml
from pathlib import Path
from typing import Dict, Any, Optional


class ConfigLoader:
    """
    설정 파일 로더 (싱글톤 패턴)

    yaml 설정 파일을 한 번만 로딩하고 캐싱합니다.
    """

    _instance: Optional['ConfigLoader'] = None
    _config: Dict[str, Any] = {}
    _loaded: bool = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not ConfigLoader._loaded:
            self._load_all()
            ConfigLoader._loaded = True

    def _get_config_dir(self) -> Path:
        """설정 디렉토리 경로 반환"""
        # saltlux/preprocessing/config_loader.py -> saltlux/config/
        return Path(__file__).parent.parent / 'config'

    def _load_all(self) -> None:
        """모든 yaml 설정 파일 로딩"""
        config_dir = self._get_config_dir()

        if not config_dir.exists():
            return

        for yaml_file in config_dir.glob('*.yaml'):
            try:
                with open(yaml_file, 'r', encoding='utf-8') as f:
                    ConfigLoader._config[yaml_file.stem] = yaml.safe_load(f) or {}
            except Exception as e:
                print(f"[ConfigLoader] Failed to load {yaml_file}: {e}")
                ConfigLoader._config[yaml_file.stem] = {}

    def reload(self) -> None:
        """설정 재로딩"""
        ConfigLoader._config.clear()
        ConfigLoader._loaded = False
        self._load_all()
        ConfigLoader._loaded = True

    def get(self, config_name: str, default: Any = None) -> Any:
        """
        설정 값 조회

        Args:
            config_name: 설정 파일 이름 (확장자 제외) 또는 점으로 구분된 경로
            default: 기본값

        Returns:
            설정 값

        Examples:
            loader.get('normalization')  # normalization.yaml 전체
            loader.get('normalization.number_reading.native_units')  # 중첩 키
        """
        parts = config_name.split('.')
        result = ConfigLoader._config

        for part in parts:
            if isinstance(result, dict) and part in result:
                result = result[part]
            else:
                return default

        return result

    @classmethod
    def instance(cls) -> 'ConfigLoader':
        """싱글톤 인스턴스 반환"""
        if cls._instance is None:
            cls._instance = ConfigLoader()
        return cls._instance


# 모듈 레벨 헬퍼 함수
def get_config(key: str, default: Any = None) -> Any:
    """
    설정 값 조회 (편의 함수)

    Args:
        key: 설정 키 (점으로 구분된 경로)
        default: 기본값

    Returns:
        설정 값

    Examples:
        get_config('domains.known_domains')
        get_config('normalization.number_reading.native_units', [])
        get_config('patterns.email.standard')
    """
    return ConfigLoader.instance().get(key, default)


def reload_config() -> None:
    """설정 재로딩"""
    ConfigLoader.instance().reload()
