"""Encrypting the model service's API key, with a key that never sits in the data folder.

The data folder is what gets copied, backed up and moved, so the key that unlocks the stored API
key lives in the user's own settings folder instead (beside the location file). On Windows that
file is itself sealed with DPAPI for the Windows account, so another account on the same computer
cannot use it either. A database taken to another computer therefore holds an API key nobody can
read, and the Models page asks for it again.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

from . import relocate

KEY_NAME = "secret.key"


class Unreadable(ValueError):
    """The stored key cannot be decrypted here: another computer, another account, or a lost key file."""


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    source = BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    result = BLOB()
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    # CRYPTPROTECT_UI_FORBIDDEN: never show a prompt; the site runs without anyone at the screen.
    if not call(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(result)):
        raise Unreadable("Windows could not unlock the key file for this account")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(result.pbData)


def _key_path():
    return relocate.settings_dir() / KEY_NAME


def _load_key(create: bool) -> bytes | None:
    path = _key_path()
    if path.is_file():
        sealed = path.read_bytes()
        return _dpapi(sealed, protect=False) if os.name == "nt" else sealed
    if not create:
        return None
    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_dpapi(key, protect=True) if os.name == "nt" else key)
    if os.name != "nt":
        path.chmod(0o600)
    return key


def encrypt(secret: str) -> str:
    return Fernet(_load_key(create=True)).encrypt(secret.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """The secret, or Unreadable when this computer's key cannot open it."""
    key = _load_key(create=False)
    if key is None:
        raise Unreadable("this computer has no key for the saved secret")
    try:
        return Fernet(key).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise Unreadable("the saved secret was made on another computer or account") from exc


def hint(secret: str) -> str:
    """What the page shows instead of the key: its last four characters."""
    return f"…{secret[-4:]}" if len(secret) >= 8 else "…"
