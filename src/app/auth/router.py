import logging

from fastapi import APIRouter

from app.auth import service
from app.auth.schemas import LoginRequest, LoginResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(request: LoginRequest) -> LoginResponse:
    return await service.login(request)
