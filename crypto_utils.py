"""
Shared cryptography utilities for TeeTimeBooker.
Handles encryption and decryption of sensitive values using Fernet.
"""
from __future__ import annotations
import base64
import sys
from pathlib import Path
from cryptography.fernet import Fernet

SCRIPT_DIR = (
    Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
)
KEY_FILE = SCRIPT_DIR / ".secret.key"

def load_key() -> bytes:
    """Load or generate encryption key."""
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes()
    key = Fernet.generate_key()
    KEY_FILE.write_bytes(key)
    return key

def encrypt(value: str) -> str:
    """Encrypt a value and return as ENC:... string."""
    fernet = Fernet(load_key())
    encrypted = fernet.encrypt(value.encode())
    return f"ENC:{base64.urlsafe_b64encode(encrypted).decode()}"

def decrypt(value: str) -> str | None:
    """Decrypt a value if it is ENC:... format, else return None."""
    if not value or not value.startswith("ENC:"):
        return None
    try:
        fernet = Fernet(load_key())
        encrypted_data = base64.urlsafe_b64decode(value[4:])
        return fernet.decrypt(encrypted_data).decode()
    except Exception:
        return None