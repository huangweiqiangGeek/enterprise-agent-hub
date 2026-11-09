import asyncio
import hashlib
import secrets
from hmac import compare_digest

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

_hasher = PasswordHasher()
# Equal-cost password verification for unknown accounts without storing a real credential.
_dummy_hash = _hasher.hash(secrets.token_urlsafe(32))


def token() -> str:
    return secrets.token_urlsafe(32)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def matches(value: str, hashed: str) -> bool:
    return compare_digest(digest(value), hashed)


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(hashed: str | None, password: str) -> bool:
    try:
        return await asyncio.to_thread(_hasher.verify, hashed or _dummy_hash, password)
    except VerificationError:
        return False
