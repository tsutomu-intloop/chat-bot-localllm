"""ユーザ認証. local: ユーザ名/パスワード + 署名付きセッションCookie, iap: IAP ヘッダ."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import HTTPException, Request

from . import store
from .config import settings

IAP_HEADER = "x-goog-authenticated-user-email"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(digest.hex(), digest_hex)


def current_user(request: Request) -> dict:
    if settings.auth_mode == "iap":
        raw = request.headers.get(IAP_HEADER)
        if not raw:
            raise HTTPException(401, "IAP ヘッダがありません")
        # 形式: "accounts.google.com:user@example.com"
        return store.get_or_create_user(raw.split(":", 1)[-1])

    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(401, "ログインが必要です")
    user = store.get_user(user_id)
    if user is None:
        request.session.clear()
        raise HTTPException(401, "ログインが必要です")
    return user
