"""로그인 비즈니스 로직."""

from datetime import UTC, datetime, timedelta
import hashlib
import logging

from fastapi import HTTPException, status
import jwt

from app.auth.infra.db.user import get_user_by_id
from app.auth.schemas import LoginRequest, LoginResponse, UserInfo
from app.core.config import Config

logger = logging.getLogger(__name__)


def _hash_password(user_id: str, plain: str) -> str:
    """SHA-512(salt + user_id + salt + password) 헥스 다이제스트."""
    salt = Config.AUTH_CRYPT_KEY
    return hashlib.sha512((salt + user_id + salt + plain).encode("utf-8")).hexdigest()


def _create_access_token(user: dict) -> str:
    expire = datetime.now(UTC) + timedelta(minutes=Config.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": user["USER_ID"],
        "user_nm": user["USER_NM"],
        "user_auth": user["USER_AUTH"],
        "dept_id": user.get("DEPT_ID"),
        "exp": expire,
    }
    return jwt.encode(payload, Config.JWT_SECRET_KEY, algorithm=Config.JWT_ALGORITHM)


async def login(request: LoginRequest) -> LoginResponse:
    user = await get_user_by_id(request.user_id)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="아이디 또는 비밀번호가 올바르지 않습니다.",
        )

    if user["USE_YN"] != "Y":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="사용이 중지된 계정입니다.",
        )

    if user.get("LOCK_YN") == "Y":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="계정이 잠겨 있습니다.",
        )

    if _hash_password(user["USER_ID"], request.password) != user["USER_PW"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="아이디 또는 비밀번호가 올바르지 않습니다.",
        )

    logger.info("로그인 성공: user_id=%s", request.user_id)
    token = _create_access_token(user)
    return LoginResponse(
        access_token=token,
        user=UserInfo(
            user_id=user["USER_ID"],
            user_nm=user["USER_NM"],
            user_auth=user["USER_AUTH"],
            dept_id=user.get("DEPT_ID"),
        ),
    )
