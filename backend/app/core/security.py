"""Webhook transport authentication (spec §3).

Two accepted mechanisms, both constant-time:

* ``X-Atlas-Signature: sha256=<hex hmac-sha256 of the RAW body>``
* the in-body ``token`` field

The body HMAC wins whenever the header is present.  Only ``hmac``/``hashlib``
from the standard library are used — no ``cryptography`` dependency.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Optional

_SIGNATURE_PREFIX = "sha256="


def compute_webhook_hmac(raw_body: bytes, secret: str) -> str:
    """Return the canonical ``sha256=<hexdigest>`` signature for ``raw_body``."""
    digest = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return f"{_SIGNATURE_PREFIX}{digest}"


def verify_webhook_hmac(raw_body: bytes, header: Optional[str], secret: str) -> bool:
    """Constant-time verification of the ``X-Atlas-Signature`` header.

    Returns ``False`` (never raises) for a missing header, a missing secret, a
    header without the ``sha256=`` prefix, or any digest mismatch.
    """
    if not header or not secret:
        return False
    candidate = header.strip()
    if not candidate.lower().startswith(_SIGNATURE_PREFIX):
        return False
    expected = compute_webhook_hmac(raw_body, secret)
    # Compare the full "sha256=<hex>" strings, lowercased so that an
    # upper-case hexdigest from a well-behaved client still validates.
    return hmac.compare_digest(expected, candidate.lower())


def verify_token(candidate: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time comparison of the in-body shared secret."""
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


# Backwards-compatible alias used by some call-sites/tests.
verify_token_constant_time = verify_token


def authenticate_webhook(
    raw_body: bytes,
    header: Optional[str],
    body_token: Optional[str],
    hmac_secret: str,
    expected_token: str,
) -> bool:
    """Full §3 transport-auth decision.

    ``header`` present  → the HMAC is authoritative; a bad HMAC is a hard reject
    even when a valid ``token`` also sits in the body.
    ``header`` absent   → fall back to the constant-time body token compare.
    """
    if header:
        return verify_webhook_hmac(raw_body, header, hmac_secret)
    return verify_token(body_token, expected_token)
