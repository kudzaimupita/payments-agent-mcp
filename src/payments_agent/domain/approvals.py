"""Signed approval tokens for money movement.

A refund executes only with an Ed25519 signature over the approval payload.
Two keys exist:

* the human approver key, held by the operator CLI and never loaded by the
  MCP server process;
* an optional policy key, which the server may hold, whose signatures are
  accepted only for amounts at or below the configured auto-approve limit.

The server process therefore cannot authorise a refund above that limit
even if it is fully compromised. The default limit is 0, i.e. off.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

KeyRole = Literal["human", "policy"]
ROLES: tuple[KeyRole, ...] = ("human", "policy")


def canonical_payload(fields: dict[str, Any]) -> str:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"))


def _priv_path(keys_dir: Path, role: KeyRole) -> Path:
    return keys_dir / f"{role}_ed25519.pem"


def _pub_path(keys_dir: Path, role: KeyRole) -> Path:
    return keys_dir / f"{role}_ed25519.pub.pem"


def ensure_keys(keys_dir: Path) -> None:
    """Create the sandbox key pairs if they do not exist yet."""
    keys_dir.mkdir(parents=True, exist_ok=True)
    for role in ROLES:
        priv_path = _priv_path(keys_dir, role)
        if priv_path.exists():
            continue
        key = Ed25519PrivateKey.generate()
        priv_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        priv_path.chmod(0o600)
        _pub_path(keys_dir, role).write_bytes(
            key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )


@dataclass(frozen=True)
class Signer:
    role: KeyRole
    key: Ed25519PrivateKey

    @classmethod
    def load(cls, keys_dir: Path, role: KeyRole) -> Signer:
        key = serialization.load_pem_private_key(_priv_path(keys_dir, role).read_bytes(), None)
        if not isinstance(key, Ed25519PrivateKey):
            raise TypeError("approval keys must be Ed25519")
        return cls(role=role, key=key)

    def sign(self, payload: str) -> str:
        sig = self.key.sign(payload.encode())
        return f"{self.role}.{base64.urlsafe_b64encode(sig).decode()}"


@dataclass(frozen=True)
class Verifier:
    public_keys: dict[KeyRole, Ed25519PublicKey]

    @classmethod
    def load(cls, keys_dir: Path) -> Verifier:
        keys: dict[KeyRole, Ed25519PublicKey] = {}
        for role in ROLES:
            path = _pub_path(keys_dir, role)
            if path.exists():
                key = serialization.load_pem_public_key(path.read_bytes())
                if isinstance(key, Ed25519PublicKey):
                    keys[role] = key
        return cls(public_keys=keys)

    def verify(self, payload: str, token: str) -> KeyRole | None:
        """Return the role whose key signed the payload, or None if invalid."""
        role, _, sig_b64 = token.partition(".")
        if role not in self.public_keys:
            return None
        try:
            sig = base64.urlsafe_b64decode(sig_b64.encode())
            self.public_keys[role].verify(sig, payload.encode())
        except (InvalidSignature, ValueError):
            return None
        return role
