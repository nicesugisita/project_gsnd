"""
패턴 핸들러 모음

각 패턴 타입별 핸들러를 제공합니다.

기존 코드 참조: saltlux/text/korean.py
- EMAIL_PATTERN (line 112)
- MOBILE_PATTERN, AREA_PATTERN, SERVICE_PATTERN (line 139-158)
- 날짜/숫자 처리 로직

핸들러 목록:
- email: 이메일 주소 처리
- phone: 전화번호 처리
- date: 날짜 처리
- number: 숫자 처리 (한자어/고유어)
- url : 웹사이트 처리
"""

from .email import EmailHandler
from .phone import PhoneHandler
from .date import DateHandler
from .number import NumberHandler
from .url import UrlHandler

__all__ = [
    'EmailHandler',
    'PhoneHandler',
    'DateHandler',
    'NumberHandler',
    'UrlHandler'
]
