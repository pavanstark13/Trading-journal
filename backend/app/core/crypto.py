"""Envelope encryption and password hashing. See SECURITY.md section 5."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings

_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        _hasher.verify(stored_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False
    return True


def needs_rehash(stored_hash: str) -> bool:
    return _hasher.check_needs_rehash(stored_hash)


def _master_key() -> bytes:
    """Derive a 32-byte AES key from the configured master secret."""
    return hashlib.sha256(settings.master_encryption_key.encode()).digest()


def encrypt(plaintext: str) -> bytes:
    """Envelope-encrypt: random DEK per value, DEK wrapped with the master key.

    Layout: b"v1" | wrapped_dek_nonce(12) | wrapped_dek(48) | data_nonce(12) | ciphertext
    """
    dek = AESGCM.generate_key(bit_length=256)
    dek_nonce = os.urandom(12)
    wrapped = AESGCM(_master_key()).encrypt(dek_nonce, dek, None)

    data_nonce = os.urandom(12)
    ciphertext = AESGCM(dek).encrypt(data_nonce, plaintext.encode(), None)
    return b"v1" + dek_nonce + wrapped + data_nonce + ciphertext


def decrypt(blob: bytes) -> str:
    if not blob.startswith(b"v1"):
        raise ValueError("Unsupported ciphertext version")
    body = blob[2:]
    dek_nonce, wrapped, data_nonce, ciphertext = (
        body[:12], body[12:60], body[60:72], body[72:]
    )
    dek = AESGCM(_master_key()).decrypt(dek_nonce, wrapped, None)
    return AESGCM(dek).decrypt(data_nonce, ciphertext, None).decode()


def generate_secret(nbytes: int = 32) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(nbytes)).decode().rstrip("=")


def generate_install_code() -> str:
    """Human-transcribable one-time registration code."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no I, O, 0, 1
    raw = "".join(secrets.choice(alphabet) for _ in range(16))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:12]}-{raw[12:]}"


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def compute_ea_signature(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    """HMAC-SHA256 over "timestamp.nonce.rawBody". Mirrored in MQL5."""
    message = timestamp.encode() + b"." + nonce.encode() + b"." + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signatures_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.lower(), b.lower())
