"""敏感配置的对称加密与脱敏展示。

主密钥保存在 data/secrets.key（首次运行自动生成，权限 600）。
数据库中的密钥字段以 "enc:v1:<base64>" 形式存储，接口返回时统一脱敏为掩码。
"""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

_PREFIX = "enc:v1:"
_fernet: Fernet | None = None


def _load_key(key_file: Path) -> bytes:
    if key_file.exists():
        data = key_file.read_bytes().strip()
        if data:
            return data
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    # 先写临时文件再原子替换，避免并发写坏密钥
    tmp = key_file.with_suffix(".key.tmp")
    tmp.write_bytes(key)
    os.chmod(tmp, 0o600)
    tmp.replace(key_file)
    return key


def get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_key(settings.secret_key_file))
    return _fernet


def encrypt(plain: str) -> str:
    """加密明文；空值原样返回。"""
    if not plain:
        return ""
    if plain.startswith(_PREFIX):
        return plain
    token = get_fernet().encrypt(plain.encode("utf-8"))
    return _PREFIX + base64.urlsafe_b64encode(token).decode("ascii")


def decrypt(stored: str) -> str:
    """解密；非加密内容原样返回，便于兼容手工写入的明文配置。"""
    if not stored:
        return ""
    if not stored.startswith(_PREFIX):
        return stored
    raw = base64.urlsafe_b64decode(stored[len(_PREFIX) :].encode("ascii"))
    try:
        return get_fernet().decrypt(raw).decode("utf-8")
    except InvalidToken:
        # 主密钥被替换或数据损坏：返回空串而不是抛异常，避免整个接口 500
        return ""


def is_encrypted(value: str) -> bool:
    return bool(value) and value.startswith(_PREFIX)


def mask(value: str, keep_head: int = 4, keep_tail: int = 4) -> str:
    """脱敏展示：sk-1234****abcd。"""
    if not value:
        return ""
    if len(value) <= keep_head + keep_tail:
        return "*" * len(value)
    return f"{value[:keep_head]}{'*' * 8}{value[-keep_tail:]}"


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)
