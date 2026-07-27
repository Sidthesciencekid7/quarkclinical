"""Password hashing/verification for e-signature re-authentication.

Clerk is the application's real session identity provider (see auth.py) — this
module is NOT a parallel login system. It exists solely so an already-Clerk-
authenticated user can be asked to re-prove their identity with a password at
the moment of e-signing (21 CFR Part 11 §11.200(a)(1) — a signature must
require re-execution of at least one identity-verification component so it
cannot be applied by anyone other than the genuine owner). ``User.hashed_password``
is set at user-provisioning time (org admin action, outside this app's scope)
and is otherwise unused by the request-auth path.
"""
import bcrypt


def hash_password(plain: str) -> str:
    """Bcrypt-hash a plaintext password for storage."""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Check a plaintext password against a stored bcrypt hash.

    Returns False (never raises) for a malformed/placeholder hash, so a
    not-yet-provisioned or corrupt hash fails closed rather than 500ing.
    """
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False
