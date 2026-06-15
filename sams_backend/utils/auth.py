"""
utils/auth.py
Password hashing helpers for S.A.M.S.

Uses bcrypt (via passlib's CryptContext) for one-way, salted password
hashing. Plaintext passwords are NEVER stored — only the bcrypt hash.

Used by:
  - utils/seed_db.py   → hash the seeded admin/staff passwords
  - api/auth.py (login) → verify a submitted password against the stored hash
"""
from passlib.context import CryptContext

# bcrypt automatically generates a per-password salt and embeds it in the
# resulting hash, so equal passwords still produce different stored values.
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain_password: str) -> str:
    """Return a salted bcrypt hash of the given plaintext password."""
    if not plain_password:
        raise ValueError("Password must not be empty")
    return _pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Return True if the plaintext password matches the stored bcrypt hash."""
    if not plain_password or not hashed_password:
        return False
    return _pwd_context.verify(plain_password, hashed_password)
