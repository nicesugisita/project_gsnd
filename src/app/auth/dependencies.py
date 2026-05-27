"""인증된 사용자 추출 의존성 — 보호 라우터에서 Depends(get_current_user) 사용."""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt

from app.auth.schemas import UserInfo
from app.core.config import Config

_bearer = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> UserInfo:
    token = credentials.credentials
    try:
        payload = jwt.decode(token, Config.JWT_SECRET_KEY, algorithms=[Config.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="토큰이 만료됐습니다.") from None
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않은 토큰입니다.") from None
    return UserInfo(
        user_id=payload["sub"],
        user_nm=payload.get("user_nm", ""),
        user_auth=payload.get("user_auth", ""),
        dept_id=payload.get("dept_id"),
    )
