"""Bot tokens and NumberHub API keys are stored encrypted (Fernet, SECRET_KEY)."""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class SecretKeyMissing(RuntimeError):
    pass


def _fernet() -> Fernet:
    if not settings.secret_key:
        raise SecretKeyMissing("SECRET_KEY is not set (see .env.example)")
    return Fernet(settings.secret_key.encode())


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:  # wrong SECRET_KEY or a damaged row
        raise SecretKeyMissing("stored secret cannot be decrypted with SECRET_KEY") from exc
