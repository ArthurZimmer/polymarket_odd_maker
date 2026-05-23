"""Fernet vault — encrypts wallet credentials with a key derived from the master password.

Persistence model:
  - `~/.poly-scraper/salt.bin`      — 32 random bytes, PBKDF2 salt (created on setup)
  - `~/.poly-scraper/verifier.bin`  — Fernet-encrypted known plaintext, lets login
                                      reject wrong passwords *before* trying to
                                      decrypt real secrets
  - DB columns                       — Fernet ciphertexts (encrypted_private_key, ...)
  - Master password                  — **never** persists; held only in the derived
                                      Fernet instance in memory (VaultState._fernet)

Process restart = vault locks; user re-logs in.
"""
from __future__ import annotations

import base64
import secrets
from typing import ClassVar

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from backend.config import settings

_VERIFIER_PLAINTEXT = b"poly-scraper-vault-v1"
_PBKDF2_ITERATIONS = 600_000
_SALT_BYTES = 32


def _derive_key(password: str, salt: bytes) -> bytes:
    raw = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    ).derive(password.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


class VaultLocked(Exception):
    """Raised when encrypt/decrypt is attempted on a locked vault."""


class VaultState:
    _fernet: ClassVar[Fernet | None] = None

    @classmethod
    def is_setup(cls) -> bool:
        return settings.salt_path.exists() and settings.verifier_path.exists()

    @classmethod
    def is_unlocked(cls) -> bool:
        return cls._fernet is not None

    @classmethod
    def setup(cls, password: str) -> None:
        salt = secrets.token_bytes(_SALT_BYTES)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.salt_path.write_bytes(salt)
        key = _derive_key(password, salt)
        fernet = Fernet(key)
        settings.verifier_path.write_bytes(fernet.encrypt(_VERIFIER_PLAINTEXT))
        cls._fernet = fernet

    @classmethod
    def unlock(cls, password: str) -> bool:
        if not cls.is_setup():
            return False
        salt = settings.salt_path.read_bytes()
        key = _derive_key(password, salt)
        fernet = Fernet(key)
        try:
            plain = fernet.decrypt(settings.verifier_path.read_bytes())
        except InvalidToken:
            return False
        if plain != _VERIFIER_PLAINTEXT:
            return False
        cls._fernet = fernet
        return True

    @classmethod
    def lock(cls) -> None:
        cls._fernet = None

    @classmethod
    def encrypt(cls, plaintext: str) -> str:
        if cls._fernet is None:
            raise VaultLocked()
        return cls._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    @classmethod
    def decrypt(cls, ciphertext: str) -> str:
        if cls._fernet is None:
            raise VaultLocked()
        return cls._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
