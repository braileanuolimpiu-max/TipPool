"""Tell the managers when something needs them: on the Messages page, the overview, and by email."""
import logging

log = logging.getLogger("tips.alerts")


def raise_alert(business, text, location=None, night=None, key="", owners_only=False, once=False):
    """Record an alert once (same key = same problem) and email the business's managers who run that
    location (or only the owners)."""
    from .models import Alert
    seen = Alert.objects.filter(business=business, key=key)
    if key and (seen if once else seen.filter(resolved_at__isnull=True)).exists():
        return None  # `once`: a problem someone already handled isn't raised again
    alert = Alert.objects.create(business=business, location=location, night=night, text=text[:300], key=key[:80])
    email_managers(business, f"Needs your attention: {text[:80]}", text, location=location, owners_only=owners_only,
                   heading="Needs your attention")
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


def email_managers(business, subject, body, location=None, owners_only=False, leads=True, heading="", quote="",
                   page="messages_page", button="Open in TipPool"):
    from django.core.mail import get_connection
    from django.urls import reverse

    from . import emails
    people = recipients(business, location, owners_only, leads=leads)
    if not people:
        return
    where = f"{business.name} · {location.display_name}" if location and location.display_name != business.name \
        else business.name
    outgoing = [emails.build(m.user.email, f"[TipPool] {subject}", eyebrow=where, heading=heading or subject,
                             paragraphs=body.split("\n\n"), quote=quote,
                             button=(button, emails.absolute(reverse(page))),
                             footer=f"You get this because you manage {business.name} on TipPool.")
                for m in people]
    try:
        get_connection(fail_silently=False).send_messages(outgoing)
    except Exception:  # an email problem must never break a payout or a webhook
        log.exception("Could not email the managers of %s", business)
