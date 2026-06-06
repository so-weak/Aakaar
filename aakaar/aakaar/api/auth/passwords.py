"""Password hashing.

Uses the `bcrypt` library directly. We tried passlib but it's stagnant and
trips a self-test against bcrypt 4.x's 72-byte hard limit; the direct API
is simpler and well-maintained.

Bcrypt's 72-byte input cap is real: passwords longer than that are
truncated by the algorithm itself, which lets two distinct passwords with
identical 72-byte prefixes collide. We pre-hash with SHA-256 before bcrypt
to lift the effective input limit and eliminate the truncation surprise.
The output is a bcrypt hash; verify uses the same pre-hash.
"""

from __future__ import annotations

import base64
import hashlib

import bcrypt


def _prepare(plaintext: str) -> bytes:
    # SHA-256, then base64 — keeps the bcrypt input deterministic-length (44 bytes)
    # well under the 72-byte limit, and removes NUL bytes which bcrypt rejects.
    digest = hashlib.sha256(plaintext.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(plaintext: str) -> str:
    if not plaintext:
        raise ValueError("password must be non-empty")
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(_prepare(plaintext), salt).decode("ascii")


def verify_password(plaintext: str, hashed: str) -> bool:
    if not plaintext or not hashed:
        return False
    try:
        return bcrypt.checkpw(_prepare(plaintext), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False
