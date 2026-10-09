"""Bearer tokens (04): `cru_<principal ulid>.<secret>`; stored as a salted SHA-256.

The secret is 32 random bytes, so the salt guards against precomputation and the
entropy against brute force. Verification is constant-time. Values never leave
this module except the one-time return from mint_token."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from crucible.domain.entities import Principal, Role
from crucible.domain.ids import is_ulid, new_id
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork

TOKEN_PREFIX = "cru_"
# hades #576 (U9): a device's principal is named for it; only devices.mint creates one.
DEVICE_PREFIX = "device:"
SALT_BYTES = 16
SECRET_BYTES = 32


def _digest(salt: bytes, secret: str) -> bytes:
    return hashlib.sha256(salt + secret.encode("utf-8")).digest()


@dataclass(frozen=True, slots=True)
class MintedToken:
    principal: Principal
    token: str


def is_reserved_name(name: str) -> bool:
    return (
        name == "crucible"
        or name.startswith("worker:")
        or name.startswith(DEVICE_PREFIX)
        or not name.strip()
    )


def is_device(principal: Principal) -> bool:
    return principal.name.startswith(DEVICE_PREFIX)


def mint_token(
    uow: UnitOfWork, clock: Clock, *, name: str, role: Role, rotate: bool = False
) -> MintedToken:
    """Create a principal with a fresh token, or rotate an existing principal's token."""
    if is_reserved_name(name):
        raise ValueError(f"principal name {name!r} is reserved")
    return _mint(uow, clock, name=name, role=role, rotate=rotate)


def mint_device_token(uow: UnitOfWork, clock: Clock, *, device: str, role: Role) -> MintedToken:
    """hades #576 (U9): a new principal `device:<device>` with a fresh token. Its token is
    an ordinary bearer token; the device row beside it is the caller's."""
    if not device.strip() or uow.principals.get_by_name(f"{DEVICE_PREFIX}{device}"):
        raise ValueError(f"device {device!r} exists or is not a name")
    return _mint(uow, clock, name=f"{DEVICE_PREFIX}{device}", role=role, rotate=False)


def _mint(uow: UnitOfWork, clock: Clock, *, name: str, role: Role, rotate: bool) -> MintedToken:
    existing = uow.principals.get_by_name(name)
    secret = secrets.token_urlsafe(SECRET_BYTES)
    salt = secrets.token_bytes(SALT_BYTES)
    if existing is not None:
        if not rotate:
            raise ValueError(f"principal {name!r} exists; pass rotate to replace its token")
        uow.principals.rotate(existing.id, salt, _digest(salt, secret))
        return MintedToken(existing, f"{TOKEN_PREFIX}{existing.id}.{secret}")
    principal = Principal(id=new_id(), name=name, role=role, created_at=clock.now())
    uow.principals.add(principal, salt, _digest(salt, secret))
    return MintedToken(principal, f"{TOKEN_PREFIX}{principal.id}.{secret}")


def authenticate(uow: UnitOfWork, token: str) -> Principal | None:
    """Resolve a bearer token to its principal, or None."""
    if not token.startswith(TOKEN_PREFIX):
        return None
    principal_id, sep, secret = token[len(TOKEN_PREFIX) :].partition(".")
    if not sep or not is_ulid(principal_id) or not secret:
        return None
    credentials = uow.principals.credentials(principal_id)
    if credentials is None:
        return None
    salt, stored = credentials
    if not hmac.compare_digest(stored, _digest(salt, secret)):
        return None
    return uow.principals.get(principal_id)
