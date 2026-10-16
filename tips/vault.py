"""Encrypts secrets we must store (each business's PayPal secret) so a leaked database is not enough.

Key: TIPPOOL_ENCRYPTION_KEY in the environment (a Fernet key). Without it, a key is derived from
DJANGO_SECRET_KEY, which is fine for local development but production must set its own key.
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    key = getattr(settings, "TIPPOOL_ENCRYPTION_KEY", "")
    if not key:
        key = base64.urlsafe_b64encode(hashlib.sha256(("tippool-vault:" + settings.SECRET_KEY).encode()).digest())
    return Fernet(key)


def encrypt(value):
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt(token):
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""
