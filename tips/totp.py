"""Two-step sign-in codes (TOTP, RFC 6238): the 6-digit codes authenticator apps show every 30 seconds."""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote


def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret, counter):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{number % 1_000_000:06d}"


def matching_step(secret, code, at=None, window=1):
    """The 30-second step `code` belongs to (now, or one step either side: phone clocks drift), or None."""
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if not secret or len(code) != 6:
        return None
    counter = int((at or time.time()) // 30)
    for d in range(-window, window + 1):
        if hmac.compare_digest(_code(secret, counter + d), code):
            return counter + d
    return None


def verify(secret, code, at=None, window=1):
    """True if `code` is right for now, or 30 seconds either side."""
    return matching_step(secret, code, at, window) is not None


def setup_uri(secret, account, issuer="TipPool"):
    """What the authenticator app scans."""
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"
