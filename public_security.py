import base64
import hashlib
import hmac
import ipaddress
import re
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_COOKIE_VERSION = "v1"
_COOKIE_SID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_AES_VERSION = b"\x01"
_AES_AAD = b"resume-agent/byok-credential/v1"
_MAX_SESSION_TTL = 86400


def issue_session(signing_key, now, ttl):
    key = _key_bytes(signing_key)
    current = _epoch(now)
    lifetime = _positive_ttl(ttl)
    expiry = current + lifetime
    sid = secrets.token_urlsafe(32)
    unsigned = f"{_COOKIE_VERSION}.{sid}.{expiry}"
    signature = _b64encode(
        hmac.new(_derive_key(key, b"session-cookie"), unsigned.encode("ascii"), hashlib.sha256).digest()
    )
    cookie = f"{unsigned}.{signature}"
    return cookie, _session_hash(sid, key), expiry


def verify_session(cookie, signing_key, now):
    try:
        key = _key_bytes(signing_key)
        current = _epoch(now)
        if not isinstance(cookie, str):
            return None
        version, sid, raw_expiry, supplied_signature = cookie.split(".")
        if version != _COOKIE_VERSION or not _COOKIE_SID_PATTERN.fullmatch(sid):
            return None
        if not raw_expiry.isdigit() or not _TOKEN_PATTERN.fullmatch(supplied_signature):
            return None
        expiry = int(raw_expiry)
        if current >= expiry or expiry - current > _MAX_SESSION_TTL:
            return None
        unsigned = f"{version}.{sid}.{expiry}"
        expected_signature = _b64encode(
            hmac.new(
                _derive_key(key, b"session-cookie"),
                unsigned.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        return _session_hash(sid, key)
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_client_ip(x_forwarded_for, client_host):
    candidates = []
    if x_forwarded_for is not None:
        candidates.extend(str(x_forwarded_for).split(","))
    if client_host is not None:
        candidates.append(str(client_host))
    for candidate in candidates:
        try:
            return str(ipaddress.ip_address(candidate.strip()))
        except ValueError:
            continue
    raise ValueError("no valid client IP address")


def hash_ip(ip_value, signing_key):
    key = _key_bytes(signing_key)
    try:
        normalized = str(ipaddress.ip_address(str(ip_value).strip()))
    except (TypeError, ValueError):
        raise ValueError("invalid IP address") from None
    return hmac.new(
        _derive_key(key, b"client-ip-hash"),
        normalized.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def encrypt_api_key(api_key, signing_key):
    key = _key_bytes(signing_key)
    if not isinstance(api_key, str) or not api_key:
        raise ValueError("API key is required")
    nonce = secrets.token_bytes(12)
    cipher = AESGCM(_derive_key(key, b"byok-aes-gcm"))
    encrypted = cipher.encrypt(nonce, api_key.encode("utf-8"), _AES_AAD)
    return _b64encode(_AES_VERSION + nonce + encrypted)


def decrypt_api_key(encrypted_value, signing_key):
    try:
        key = _key_bytes(signing_key)
        if not isinstance(encrypted_value, str) or not encrypted_value:
            raise ValueError
        if not _TOKEN_PATTERN.fullmatch(encrypted_value):
            raise ValueError
        payload = _b64decode(encrypted_value)
        if len(payload) < 30 or payload[:1] != _AES_VERSION:
            raise ValueError
        nonce = payload[1:13]
        encrypted = payload[13:]
        cipher = AESGCM(_derive_key(key, b"byok-aes-gcm"))
        plaintext = cipher.decrypt(nonce, encrypted, _AES_AAD).decode("utf-8")
        if not plaintext:
            raise ValueError
        return plaintext
    except Exception:
        raise ValueError("invalid encrypted credential") from None


def _session_hash(sid, key):
    return hmac.new(
        _derive_key(key, b"session-identity-hash"),
        sid.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _derive_key(key, purpose):
    return hmac.new(
        key,
        b"resume-agent/public-security/v1/" + purpose,
        hashlib.sha256,
    ).digest()


def _key_bytes(value):
    if isinstance(value, str):
        key = value.encode("utf-8")
    elif isinstance(value, bytes):
        key = value
    else:
        raise ValueError("signing key is required")
    if not key or not key.strip():
        raise ValueError("signing key is required")
    return key


def _epoch(value):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("now must be an epoch timestamp") from None


def _positive_ttl(value):
    try:
        ttl = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("ttl must be a positive integer") from None
    if ttl <= 0:
        raise ValueError("ttl must be a positive integer")
    if ttl > _MAX_SESSION_TTL:
        raise ValueError("ttl cannot exceed 86400 seconds")
    return ttl


def _b64encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value):
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)
