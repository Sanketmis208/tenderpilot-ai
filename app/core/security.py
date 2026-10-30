"""Cryptographic primitives: password hashing, opaque tokens, symmetric encryption."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import get_settings

_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 10


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def validate_password_strength(password: str) -> list[str]:
    problems: list[str] = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if password.lower() == password or password.upper() == password:
        problems.append("Use both upper- and lower-case letters.")
    if not any(c.isdigit() for c in password):
        problems.append("Include at least one digit.")
    return problems


def new_token(nbytes: int = 32) -> str:
    """URL-safe random token given to the user; only its hash is stored."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    key = get_settings().secret_key.get_secret_value().encode()
    return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _keystream_key() -> bytes:
    s = get_settings()
    material = (s.encryption_key or s.secret_key).get_secret_value().encode()
    return hashlib.sha256(b"tp-integration-credentials" + material).digest()


def encrypt_secret(plaintext: str) -> str:
    """Authenticated AES-256-GCM encryption with a key derived from TP_ENCRYPTION_KEY.

    Used for integration credentials at rest. In AWS deployments credentials should
    preferably live in Secrets Manager; this protects the DB copy.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    ct = AESGCM(_keystream_key()).encrypt(nonce, plaintext.encode(), b"tp")
    return base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt_secret(token: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw = base64.urlsafe_b64decode(token.encode())
    return AESGCM(_keystream_key()).decrypt(raw[:12], raw[12:], b"tp").decode()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
