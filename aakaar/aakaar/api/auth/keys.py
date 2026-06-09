"""RSA signing-key store for RS256 access tokens + JWKS publication.

Layout of `jwt_key_dir` (one directory, rotation-friendly):
    <kid>.pem        PKCS8 RSA private key  (the signing material)
    <kid>.pem.pub    SubjectPublicKeyInfo   (optional; derived if absent)
    active           single line naming the kid to sign with (optional)

`kid` is conventionally an ISO-8601 UTC timestamp, so the lexicographically
greatest kid is also the newest — that is the fallback "active" key. Every
public key is published at the JWKS endpoint (not just the active one) so a
token signed by an older kid keeps validating through a rotation overlap.

Security: the algorithm is always pinned by the caller at verify time
(`jwt.verify_token`), and the `kid` only selects *which public key* to try — a
token can never talk the verifier into `alg:none` or HS/RS confusion.
"""

from __future__ import annotations

import json
import logging
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_ASYMMETRIC_PREFIXES = ("RS", "ES", "PS")


def is_asymmetric(algorithm: str) -> bool:
    """True for RS*/ES*/PS* (key-pair) algorithms; False for HS* (shared secret)."""
    return len(algorithm) >= 2 and algorithm[:2] in _ASYMMETRIC_PREFIXES


class KeyStoreError(RuntimeError):
    """Raised when signing keys are missing or unreadable."""


@dataclass(frozen=True, slots=True)
class SigningKey:
    kid: str
    public_pem: str
    private_pem: str | None  # None for a verify-only (public) key


def _new_kid(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")


def generate_keypair(directory: Path, *, key_size: int = 3072) -> SigningKey:
    """Generate an RSA keypair, write `<kid>.pem` (0600) + `active`, return it.

    Dev convenience only — the private key is written unencrypted.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    directory.mkdir(parents=True, exist_ok=True)
    kid = _new_kid()
    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    priv_path = directory / f"{kid}.pem"
    priv_path.write_text(private_pem)
    priv_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    (directory / f"{kid}.pem.pub").write_text(public_pem)
    (directory / "active").write_text(kid + "\n")
    logger.warning(
        "jwt: bootstrapped a new RSA signing key kid=%s in %s "
        "(unencrypted private key — dev only)",
        kid,
        directory,
    )
    return SigningKey(kid=kid, public_pem=public_pem, private_pem=private_pem)


def _public_from_private(private_pem: str) -> str:
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    return (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


class KeyStore:
    """In-memory set of RSA keys loaded from a directory."""

    def __init__(self, keys: dict[str, SigningKey], active_kid: str) -> None:
        if active_kid not in keys:
            raise KeyStoreError(f"active kid {active_kid!r} is not among loaded keys")
        self._keys = keys
        self._active_kid = active_kid

    @classmethod
    def from_dir(
        cls, directory: Path, *, algorithm: str, bootstrap: bool = False
    ) -> KeyStore:
        directory = directory.expanduser()
        keys: dict[str, SigningKey] = {}
        if directory.is_dir():
            for priv_path in sorted(directory.glob("*.pem")):
                if priv_path.name.endswith(".pem.pub"):
                    continue
                kid = priv_path.name[: -len(".pem")]
                private_pem = priv_path.read_text()
                pub_path = directory / f"{kid}.pem.pub"
                public_pem = (
                    pub_path.read_text()
                    if pub_path.exists()
                    else _public_from_private(private_pem)
                )
                keys[kid] = SigningKey(
                    kid=kid, public_pem=public_pem, private_pem=private_pem
                )

        if not keys:
            if bootstrap:
                key = generate_keypair(directory)
                keys[key.kid] = key
            else:
                raise KeyStoreError(
                    f"no signing keys found in {directory} and bootstrap is off; "
                    f"algorithm {algorithm} requires a key pair. Set "
                    "AAKAAR_JWT_BOOTSTRAP_KEYS=true (dev) or provision a <kid>.pem."
                )

        active_file = directory / "active"
        if active_file.exists():
            active_kid = active_file.read_text().strip()
            if active_kid not in keys:
                logger.warning(
                    "jwt: active file names unknown kid=%s; using newest", active_kid
                )
                active_kid = max(keys)
        else:
            active_kid = max(keys)  # newest by ISO-timestamp kid
        logger.info(
            "jwt: loaded %d signing key(s); active kid=%s", len(keys), active_kid
        )
        return cls(keys=keys, active_kid=active_kid)

    def active(self) -> SigningKey:
        return self._keys[self._active_kid]

    def public_pem(self, kid: str) -> str | None:
        key = self._keys.get(kid)
        return key.public_pem if key is not None else None

    def resolver(self) -> Callable[[str], str | None]:
        """A `kid -> public_pem` resolver for `jwt.verify_token`."""
        return self.public_pem

    def jwks(self, *, algorithm: str = "RS256") -> dict[str, list[dict[str, str]]]:
        """RFC 7517 JWKS — the public half of *every* key, for rotation overlap."""
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        from jwt.algorithms import RSAAlgorithm

        out: list[dict[str, str]] = []
        for kid, key in self._keys.items():
            pub = load_pem_public_key(key.public_pem.encode())
            if not isinstance(pub, RSAPublicKey):
                logger.warning("jwt: skipping non-RSA key kid=%s in JWKS", kid)
                continue
            jwk = json.loads(RSAAlgorithm.to_jwk(pub))
            jwk.update({"kid": kid, "use": "sig", "alg": algorithm})
            out.append(jwk)
        return {"keys": out}
