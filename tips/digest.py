"""The owner's digest: a short email every morning or every Monday with how each location did,
what needs attention, and what the AI checks noticed. Also shown in the app (Reports → Digest)."""
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Sum
from django.template.loader import render_to_string
from django.utils import timezone

from . import reports
from .alerts import recipients
from .autoclose import location_tz
from .models import Alert, Business, Message, Night, Shift

log = logging.getLogger("tips.digest")
SEND_FROM_HOUR = 7  # local time of the business's first location


def period(business, today, kind=None):
    """(first, last, previous_first, previous_last, label) for a daily or weekly digest."""
    kind = kind or business.digest
    if kind == Business.Digest.DAILY:
        day = today - timedelta(days=1)
        return day, day, day - timedelta(days=7), day - timedelta(days=7), f"{day:%A, %b} {day.day}"
    last = today - timedelta(days=today.weekday() + 1)  # last Sunday
    first = last - timedelta(days=6)
    return (first, last, first - timedelta(weeks=1), last - timedelta(weeks=1),
            f"the week of {first:%b} {first.day} to {last:%b} {last.day}")


def build(business, locations, today, kind=None):
    first, last, prev_first, prev_last, label = period(business, today, kind)
    rows, total, total_before = [], Decimal("0"), Decimal("0")
    for loc in locations:
        now = reports.build([loc], first, last)
        before = reports.build([loc], prev_first, prev_last)
        hours = Shift.objects.filter(night__location=loc, night__date__gte=first, night__date__lte=last) \
            .aggregate(h=Sum("hours"))["h"] or Decimal("0")
        best = max(now["shifts"], key=lambda s: s["total"], default=None)
        top = now["people"][0] if now["people"] else None
        change = (int((now["collected"] - before["collected"]) / before["collected"] * 100)
                  if before["collected"] else None)
        rows.append({
            "location": loc, "collected": now["collected"], "change": change, "shifts": now["shift_count"],
            "paid_out": now["paid_out"], "per_hour": (now["collected"] / hours).quantize(Decimal("0.01")) if hours else None,
            "best": best, "top": top,
        })
        total += now["collected"]
        total_before += before["collected"]

    nights = Night.objects.filter(location__in=locations, date__gte=first, date__lte=last).exclude(review_check={})
    noticed, stops, warns = [], 0, 0
    for n in nights.select_related("location"):
        for f in (n.review_check or {}).get("findings", []):
            stops += f.get("level") == "stop"
            warns += f.get("level") == "warn"
            if len(noticed) < 4 and (f.get("ai") or f.get("level") == "stop"):
                noticed.append(f"{n.location.display_name}, {n.date:%a %b} {n.date.day}: {f['text']}")
    waiting = list(Night.objects.filter(location__in=locations, status__in=[
        Night.Status.CLOSED, Night.Status.REVIEW, Night.Status.PAYING]).select_related("location").order_by("date"))
    return {
        "business": business, "label": label, "first": first, "last": last, "rows": rows,
        "total": total, "change": int((total - total_before) / total_before * 100) if total_before else None,
        "cur": business.symbol, "noticed": noticed, "stops": stops, "warns": warns,
        "waiting": waiting,
        "alerts": Alert.objects.filter(business=business, resolved_at__isnull=True, location__in=locations).count(),
        "team_messages": Message.objects.filter(location__in=locations, resolved_at__isnull=True).count(),
        "kind": kind or business.digest,
    }


def run(now=None, base_url=""):
    """Send the digests that are due. Called by the scheduler tick; each business gets at most one a day."""
    now = now or timezone.now()
    sent = 0
    for business in Business.objects.exclude(digest=Business.Digest.OFF).filter(is_demo=False):
        first_location = business.locations.filter(archived=False).order_by("created_at").first()
        if first_location is None:
            continue
        local = now.astimezone(location_tz(first_location))
        today = local.date()
        monday = today - timedelta(days=today.weekday())
        if business.digest == Business.Digest.DAILY:
            due = local.hour >= SEND_FROM_HOUR and business.digest_sent_on != today
        else:  # the first check on or after Monday morning, so a server asleep on Monday doesn't skip the week
            due = ((today > monday or local.hour >= SEND_FROM_HOUR)
                   and (business.digest_sent_on is None or business.digest_sent_on < monday))
        if not due:
            continue
        # claim today's digest: only one run (of several at once) gets it, even if a send fails
        if not Business.objects.filter(pk=business.pk).exclude(digest_sent_on=today).update(digest_sent_on=today):
            continue
        for manager in recipients(business, leads=False):
            try:
                send(business, manager, today, base_url)
                sent += 1
            except Exception:
                log.exception("Digest for %s to %s failed", business, manager.user.email)
    return sent


def send(business, manager, today, base_url=""):
    locations = list(manager.allowed_locations())
    data = build(business, locations, today)
    data["base_url"] = base_url
    subject = f"[TipPool] {business.name}: {data['cur']}{data['total']:,.2f} in tips, {data['label']}"
    text = render_to_string("emails/digest.txt", data)
    html = render_to_string("emails/digest.html", data)
    send_mail(subject, text, settings.DEFAULT_FROM_EMAIL, [manager.user.email], html_message=html,
              fail_silently=False)
