"""HMAC-signed run access tokens.

No secret value is ever logged. Tokens carry only a run id and an absolute
expiry; knowing or guessing a run id is insufficient without a valid signature.
The signing key comes from the environment on the server and may be injected
explicitly in tests.
"""

import base64
import hashlib
import hmac
import json
import os
import time


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _resolve(value, env_name):
    if value is not None:
        return str(value)
    return os.environ.get(env_name, "")


def _signing_key(key) -> bytes:
    resolved = _resolve(key, "AGENT_RUN_SIGNING_KEY")
    if not resolved:
        raise RuntimeError("AGENT_RUN_SIGNING_KEY is not configured")
    return resolved.encode("utf-8")


def _sign(payload_segment: str, key) -> str:
    digest = hmac.new(_signing_key(key), payload_segment.encode("ascii"),
                      hashlib.sha256).digest()
    return _b64encode(digest)


def issue_run_token(run_id, *, ttl_seconds=3600, now=None, key=None):
    """Return ``(token, exp_epoch)`` for a run id, signed with HMAC-SHA256."""
    issued = float(time.time() if now is None else now)
    exp = int(issued + float(ttl_seconds))
    payload = {"run_id": str(run_id), "exp": exp}
    payload_segment = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = _sign(payload_segment, key)
    return f"{payload_segment}.{signature}", exp


def verify_run_token(token, run_id, *, now=None, key=None) -> bool:
    """Return True only for an untampered, unexpired token bound to ``run_id``."""
    if not isinstance(token, str) or token.count(".") != 1:
        return False
    payload_segment, signature = token.split(".", 1)
    if not payload_segment or not signature:
        return False
    try:
        expected_signature = _sign(payload_segment, key)
    except RuntimeError:
        return False
    if not hmac.compare_digest(signature, expected_signature):
        return False
    try:
        payload = json.loads(_b64decode(payload_segment).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("run_id") != str(run_id):
        return False
    try:
        exp = float(payload.get("exp"))
    except (TypeError, ValueError):
        return False
    current = float(time.time() if now is None else now)
    return current < exp
