"""Sends email with the account saved on the Platform settings page. Without one, falls back to
whatever the environment chose (the terminal locally, nothing in production)."""
from django.conf import settings
from django.core.mail import get_connection
from django.core.mail.backends.base import BaseEmailBackend

from . import platform


def smtp_connection(host, port, user, password, fail_silently=False):
    return get_connection("django.core.mail.backends.smtp.EmailBackend", host=host, port=port, username=user,
                          password=password, use_tls=port != 465, use_ssl=port == 465, timeout=20,
                          fail_silently=fail_silently)


class PlatformBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        saved = platform.email_settings()
        if saved is None:
            fallback = get_connection(settings.EMAIL_FALLBACK_BACKEND, fail_silently=self.fail_silently)
            return fallback.send_messages(email_messages)
        host, port, user, password, sender = saved
        for message in email_messages:
            if message.from_email in ("", None, settings.DEFAULT_FROM_EMAIL):
                message.from_email = f"TipPool <{sender}>" if "<" not in sender else sender
        return smtp_connection(host, port, user, password, self.fail_silently).send_messages(email_messages)
