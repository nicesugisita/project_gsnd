from pydantic import BaseModel


class LoginRequest(BaseModel):
    user_id: str
    password: str


class UserInfo(BaseModel):
    user_id: str
    user_nm: str
    user_auth: str
    dept_id: str | None = None


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserInfo
