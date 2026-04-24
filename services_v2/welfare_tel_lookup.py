"""
OKMS 문서의 미등록 연락처(-0000) 감지 유틸

GSND_OUR_REGION_TEL 전환 후 DB 조회는 사용하지 않습니다.
has_unregistered_contact()는 response_generator에서 OKMS 문서 포맷팅 시 사용됩니다.
"""

import logging
import re
from typing import Dict, Any

logger = logging.getLogger(__name__)

# "문의처:" (공백 허용) 뒤에 0000 패턴 (dash 유무 무관)
_PATTERN_TEL_UNREGISTERED = re.compile(
    r'문의처\s*:\s*.*?0000'
)


def has_unregistered_contact(doc: Dict[str, Any]) -> bool:
    """CONTENT에서 '문의처:' 뒤에 0000 패턴이 있는지 판단합니다."""
    content = str(doc.get("CONTENT", "") or doc.get("CHUNK_PATH", "") or "")
    return bool(_PATTERN_TEL_UNREGISTERED.search(content))
