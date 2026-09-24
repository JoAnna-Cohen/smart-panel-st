"""Encryption for Leviton credentials at rest (Fernet / AES-128 + HMAC)."""

import json
import os

from cryptography.fernet import Fernet, InvalidToken


class CredentialCipher:
    def __init__(self, key: str | None = None):
        key = key or os.environ.get("CREDENTIAL_KEY", "")
        if not key:
            raise RuntimeError(
                "CREDENTIAL_KEY is not set. Generate one with: "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, data: dict) -> str:
        return self._fernet.encrypt(json.dumps(data).encode()).decode()

    def decrypt(self, blob: str) -> dict | None:
        try:
            return json.loads(self._fernet.decrypt(blob.encode()))
        except (InvalidToken, ValueError):
            return None
