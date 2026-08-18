import os

os.environ.setdefault("MMWS_DATABASE_URL", "postgresql+asyncpg://mmws:mmws@localhost:5432/mmws_test")

import pytest
from jose import jwt

from app.core.security import (
    create_access_token,
    decode_token,
    decrypt_secret,
    encrypt_secret,
    hash_password,
    verify_password,
)


def test_password_hash_round_trip():
    hashed = hash_password("correct-horse-battery-staple")
    assert verify_password("correct-horse-battery-staple", hashed) is True
    assert verify_password("wrong-password", hashed) is False


def test_password_hash_is_not_plaintext():
    hashed = hash_password("12345678")
    assert hashed != "12345678"
    assert hashed.startswith("$2b$")  # bcrypt


def test_secret_encryption_round_trip():
    ciphertext = encrypt_secret(b"12345678")
    assert ciphertext != b"12345678"
    assert decrypt_secret(ciphertext) == b"12345678"


def test_access_token_round_trip():
    token = create_access_token(subject="alice", role="operator")
    payload = decode_token(token)
    assert payload["sub"] == "alice"
    assert payload["role"] == "operator"
    assert payload["type"] == "access"


def test_decode_token_rejects_tampered_signature():
    token = create_access_token(subject="alice", role="operator")
    # Правим символ в середине подписи (последний символ base64url — не
    # надёжный выбор: часть его бит может быть "не участвующей" из-за
    # выравнивания, и декодированные байты подписи не изменятся).
    mid = len(token) // 2
    flipped_char = "A" if token[mid] != "A" else "B"
    tampered = token[:mid] + flipped_char + token[mid + 1 :]
    with pytest.raises(jwt.JWTError):
        decode_token(tampered)
