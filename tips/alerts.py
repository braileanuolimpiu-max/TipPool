"""Tell the managers when something needs them: on the Messages page, the overview, and by email."""
import logging

from django.conf import settings
from django.core.mail import send_mail

log = logging.getLogger("tips.alerts")


def raise_alert(business, text, location=None, night=None, key=""):
    """Record an alert once (same key = same problem) and email the business's managers."""
    from .models import Alert
    if key and Alert.objects.filter(business=business, key=key, resolved_at__isnull=True).exists():
        return None
    alert = Alert.objects.create(business=business, location=location, night=night, text=text[:300], key=key[:80])
    email_managers(business, f"Needs your attention: {text[:80]}", text + (f"\n\nLocation: {location.display_name}"
                                                                           if location else ""))
    return alert


def email_managers(business, subject, body):
    emails = [m.user.email for m in business.managers.select_related("user") if m.user.email]
    if not emails:
        return
    try:
        send_mail(f"[TipPool] {subject}", body + "\n\nOpen TipPool to see it.", settings.DEFAULT_FROM_EMAIL, emails,
                  fail_silently=False)
    except Exception:  # an email problem must never break a payout or a webhook
        log.exception("Could not email the managers of %s", business)
