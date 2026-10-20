"""Tell the managers when something needs them: on the Messages page, the overview, and by email."""
import logging

from django.conf import settings
from django.core.mail import send_mail

log = logging.getLogger("tips.alerts")


def raise_alert(business, text, location=None, night=None, key="", owners_only=False, once=False):
    """Record an alert once (same key = same problem) and email the business's managers who run that
    location (or only the owners)."""
    from .models import Alert
    seen = Alert.objects.filter(business=business, key=key)
    if key and (seen if once else seen.filter(resolved_at__isnull=True)).exists():
        return None  # `once`: a problem someone already handled isn't raised again
    alert = Alert.objects.create(business=business, location=location, night=night, text=text[:300], key=key[:80])
    email_managers(business, f"Needs your attention: {text[:80]}", text + (f"\n\nLocation: {location.display_name}"
                                                                           if location else ""),
                   location=location, owners_only=owners_only)
    return alert


def recipients(business, location=None, owners_only=False, leads=True):
    """Managers who should hear about something at `location` (all of them for business-wide news)."""
    people = []
    for m in business.managers.select_related("user").prefetch_related("locations"):
        if not m.user.email or (owners_only and not m.is_owner) or (not leads and m.is_lead):
            continue
        picked = [loc.pk for loc in m.locations.all()]
        if location is not None and picked and not m.is_owner and location.pk not in picked:
            continue
        people.append(m)
    return people


def email_managers(business, subject, body, location=None, owners_only=False, leads=True):
    emails = [m.user.email for m in recipients(business, location, owners_only, leads=leads)]
    if not emails:
        return
    try:
        send_mail(f"[TipPool] {subject}", body + "\n\nOpen TipPool to see it.", settings.DEFAULT_FROM_EMAIL, emails,
                  fail_silently=False)
    except Exception:  # an email problem must never break a payout or a webhook
        log.exception("Could not email the managers of %s", business)
