"""Fernet encryption helpers for API key storage.

Requires the ``cryptography`` package and an ENCRYPTION_KEY environment
variable containing a valid Fernet key (base64-encoded 32-byte key).

Generate a key once during setup::

    python -c "from crypto import generate_key; print(generate_key())"
"""

import os
from cryptography.fernet import Fernet


def get_fernet():
    """Get Fernet instance using ENCRYPTION_KEY from env."""
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        raise ValueError("ENCRYPTION_KEY not set in environment")
    return Fernet(key.encode())


def encrypt(plaintext: str) -> str:
    """Encrypt a string, return base64-encoded ciphertext."""
    if not plaintext:
        return ""
    return get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a base64-encoded ciphertext string."""
    if not ciphertext:
        return ""
    return get_fernet().decrypt(ciphertext.encode()).decode()


def generate_key() -> str:
    """Generate a new Fernet key (run once during setup)."""
    return Fernet.generate_key().decode()
