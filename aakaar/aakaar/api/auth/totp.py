"""TOTP (RFC 6238) helpers for MFA.

Beyond the basics this adds three hardenings over a naive integration:
  * anti-replay — `verify_code` returns the matched time-step and refuses any
    step <= the last one accepted, so a sniffed code can't be reused inside its
    ~90s validity window (callers persist `totp_last_step`).
  * recovery codes — single-use bcrypt-hashed backup codes, so a lost
    authenticator doesn't mean a locked-out account.
  * encryption at rest — when a Fernet key is configured the stored secret is
    encrypted (prefixed `enc:`), transparently decrypted on use. Mixed
    plaintext/encrypted values are tolerated so the key can be introduced later.
"""

from __future__ import annotations

import hmac
import secrets
import time

import pyotp

from aakaar.api.auth.passwords import hash_password, verify_password

_ENC_PREFIX = "enc:"
_INTERVAL = 30
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no easily-confused chars


# ---- secret lifecycle -----------------------------------------------------


def generate_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, *, account: str, issuer: str) -> str:
    """The `otpauth://` URI an authenticator app imports (rendered as a QR)."""
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=issuer)


def verify_code(
    secret: str,
    code: str,
    *,
    last_step: int | None = None,
    valid_window: int = 1,
    at: int | None = None,
) -> int | None:
    """Return the matched time-step if `code` is valid (and not replayed), else None."""
    code = (code or "").strip()
    if not code.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    now = at if at is not None else int(time.time())
    current_step = now // _INTERVAL
    for offset in range(-valid_window, valid_window + 1):
        step = current_step + offset
        if last_step is not None and step <= last_step:
            continue  # anti-replay: never accept an already-used (or older) step
        candidate = totp.at(step * _INTERVAL)
        if hmac.compare_digest(candidate, code):
            return step
    return None


# ---- encryption at rest ---------------------------------------------------


def protect_secret(secret: str, key: str | None) -> str:
    if not key:
        return secret
    from cryptography.fernet import Fernet

    return _ENC_PREFIX + Fernet(key.encode()).encrypt(secret.encode()).decode()


def unprotect_secret(stored: str, key: str | None) -> str:
    if not stored.startswith(_ENC_PREFIX):
        return stored  # legacy plaintext
    if not key:
        raise RuntimeError(
            "TOTP secret is encrypted but AAKAAR_MFA_ENCRYPTION_KEY is not set"
        )
    from cryptography.fernet import Fernet

    return Fernet(key.encode()).decrypt(stored[len(_ENC_PREFIX) :].encode()).decode()


# ---- recovery codes -------------------------------------------------------


def generate_recovery_codes(n: int = 10) -> list[str]:
    """Human-friendly single-use backup codes, e.g. ``AB3D-7KMN``."""
    def one() -> str:
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(8))
        return f"{raw[:4]}-{raw[4:]}"

    return [one() for _ in range(n)]


def hash_recovery_codes(codes: list[str]) -> list[str]:
    return [hash_password(_normalize_recovery(c)) for c in codes]


def consume_recovery_code(hashes: list[str], code: str) -> list[str] | None:
    """If `code` matches an unused hash, return the remaining hashes; else None."""
    normalized = _normalize_recovery(code)
    for i, h in enumerate(hashes):
        if verify_password(normalized, h):
            return hashes[:i] + hashes[i + 1 :]
    return None


def _normalize_recovery(code: str) -> str:
    return (code or "").strip().upper().replace("-", "").replace(" ", "")
