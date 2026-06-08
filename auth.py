"""HTTP Basic Auth gate for the orchestrator dashboard.

Single-user, single-machine, developer convenience — not enterprise SSO.
The plaintext password is **never** stored or logged; only the PBKDF2-HMAC-SHA256
salt+hash live in ``fleet.json``. The PBKDF2 parameters (iterations, algo)
travel with the hash so they can be tuned in future without code edits.

Public surface:
    verify_password    constant-time compare against a stored PBKDF2 hash
    parse_basic_header decode ``Authorization: Basic ...`` to (user, password)
    load_auth_config   return the ``auth`` block from a parsed config, or None

Stdlib only (hashlib, hmac, base64) — no new dependencies.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac

# Currently the only supported KDF. Anything else returns False from
# verify_password so an old (or hand-edited) config can't silently bypass the
# check by naming a KDF the server doesn't know.
ALGO_PBKDF2_SHA256 = "pbkdf2_sha256"


def _pbkdf2_sha256(plaintext: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", plaintext.encode("utf-8"),
                               salt, iterations)


def verify_password(plaintext: str, *, salt_b64: str, hash_b64: str,
                    iterations: int,
                    algo: str = ALGO_PBKDF2_SHA256) -> bool:
    """Return True iff ``plaintext`` hashes to ``hash_b64`` under ``salt_b64``.

    Uses ``hmac.compare_digest`` so the check runs in constant time relative to
    the hash length. A malformed base64 stored value, an unknown ``algo``, or a
    non-positive iteration count returns False rather than raising — auth must
    fail closed (no access) rather than blowing up the request handler.
    """
    if algo != ALGO_PBKDF2_SHA256:
        return False
    if iterations <= 0:
        return False
    try:
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
    except (binascii.Error, ValueError):
        return False
    if not salt or not expected:
        return False
    candidate = _pbkdf2_sha256(plaintext, salt, iterations)
    return hmac.compare_digest(candidate, expected)


def parse_basic_header(header: str | None) -> tuple[str, str] | None:
    """Decode an ``Authorization: Basic <base64>`` header.

    Returns ``(username, password)`` or ``None`` for any malformation:
    missing header, wrong scheme, non-base64 payload, missing colon. Callers
    treat ``None`` exactly like "wrong credentials" so the 401 path never
    leaks shape information about why a header was rejected.
    """
    if not header:
        return None
    parts = header.strip().split(None, 1)
    if len(parts) != 2:
        return None
    scheme, payload = parts[0], parts[1]
    if scheme.lower() != "basic":
        return None
    try:
        raw = base64.b64decode(payload.strip(), validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if ":" not in text:
        return None
    user, password = text.split(":", 1)
    return user, password


def load_auth_config(config: dict | None) -> dict | None:
    """Return the ``auth`` block from a parsed fleet.json dict, or None.

    None means "auth is OFF" — either the block is absent, malformed, or
    ``enabled`` is false. Callers MUST treat None as the loopback dev-mode
    state, exactly as today. A block missing required fields also returns
    None so a half-written config can't gate the dashboard with an
    impossible-to-satisfy hash.
    """
    if not isinstance(config, dict):
        return None
    block = config.get("auth")
    if not isinstance(block, dict):
        return None
    if not block.get("enabled"):
        return None
    required = ("username", "salt_b64", "hash_b64", "iterations")
    for key in required:
        if not block.get(key):
            return None
    # iterations must be a positive int; reject strings or 0 outright.
    try:
        iters = int(block["iterations"])
    except (TypeError, ValueError):
        return None
    if iters <= 0:
        return None
    return {
        "username": str(block["username"]),
        "salt_b64": str(block["salt_b64"]),
        "hash_b64": str(block["hash_b64"]),
        "iterations": iters,
        "algo": str(block.get("algo") or ALGO_PBKDF2_SHA256),
        "realm": str(block.get("realm") or "Claude Orchestrator"),
    }


def check_credentials(user: str, password: str, auth_cfg: dict) -> bool:
    """Compare (user, password) against ``auth_cfg`` in constant time.

    Both the username and the password are compared via ``hmac.compare_digest``
    so a timing attack can't enumerate either field. ``auth_cfg`` is the dict
    returned by :func:`load_auth_config` — its ``username`` and hash fields
    are all already strings.
    """
    expected_user = auth_cfg["username"]
    user_ok = hmac.compare_digest(
        user.encode("utf-8"), expected_user.encode("utf-8"))
    # Always run the password KDF even if the username didn't match, so the
    # request takes ~constant time regardless of which half failed.
    pwd_ok = verify_password(
        password,
        salt_b64=auth_cfg["salt_b64"],
        hash_b64=auth_cfg["hash_b64"],
        iterations=auth_cfg["iterations"],
        algo=auth_cfg.get("algo", ALGO_PBKDF2_SHA256),
    )
    return user_ok and pwd_ok
