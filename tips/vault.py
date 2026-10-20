"""Encrypts secrets we must store (each business's PayPal secret) so a leaked database is not enough.

Key: TIPPOOL_ENCRYPTION_KEY in the environment (a Fernet key, or any long random text). Without it, a key is derived from
DJANGO_SECRET_KEY, which is fine for local development but production must set its own key.
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _derive(text):
    return base64.urlsafe_b64encode(hashlib.sha256(("tippool-vault:" + text).encode()).digest())


def _fernet():
    key = getattr(settings, "TIPPOOL_ENCRYPTION_KEY", "")
    if not key:
        return Fernet(_derive(settings.SECRET_KEY))
    try:
        return Fernet(key)
    except ValueError:  # any long random text works too (e.g. one Render generated): a key is made from it
        return Fernet(_derive(key))


def encrypt(value):
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt(token):
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""
