"""Smart alerts: tell the owner when something is quietly wrong, before anyone notices by hand.

Runs with the automatic close-outs (every few minutes). Each problem is raised once (alerts have keys),
shown in Messages and the overview, and emailed to the managers of that location.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from . import insights
from .alerts import raise_alert
from .autoclose import location_tz
from .models import ClockEntry, Location, Night, Tip

log = logging.getLogger("tips.watch")
LONG_CLOCK_IN = timedelta(hours=12)
WAITING_TOO_LONG = timedelta(hours=48)


def run(now=None):
    """Check every location whose business wants smart alerts. Returns the alerts raised."""
    now = now or timezone.now()
    raised = []
    for location in Location.objects.filter(archived=False, business__smart_alerts=True).select_related("business"):
        try:
            raised += check(location, now)
        except Exception:  # one location's problem never stops the rest
            log.exception("Smart alerts failed at %s", location)
    return [a for a in raised if a is not None]


def check(location, now):
    business, name, cur = location.business, location.display_name, location.business.symbol
    out = []

    def alert(text, key, night=None):
        out.append(raise_alert(business, text, location=location, night=night, key=key, once=True))

    pace = insights.pace(location, now)
    night = pace["night"]
    if night and pace["status"] == "quiet":
        alert(f"No guest tips at {name} yet today. By now there are usually about {pace['normal_guests']} "
              f"({cur}{pace['normal_now']}). Are the QR codes out on the tables?", f"quiet:{night.pk}", night)
    elif night and pace["status"] == "low" and pace["normal_now"] >= Decimal("50") and pace["normal_guests"] >= 4:
        alert(f"Tips at {name} are far below normal today: {cur}{pace['now']} so far, usually about "
              f"{cur}{pace['normal_now']} by now. Is the tip page working, and are the codes visible?",
              f"low:{night.pk}", night)

    for entry in ClockEntry.objects.filter(night__location=location, ended_at__isnull=True,
                                           started_at__lt=now - LONG_CLOCK_IN).select_related("staff"):
        hours = int((now - entry.started_at).total_seconds() // 3600)
        alert(f"{entry.staff.name} has been clocked in at {name} for {hours} hours. Forgot to clock out?",
              f"clock:{entry.pk}", entry.night)

    today = now.astimezone(location_tz(location)).date()
    for old in Night.objects.filter(location=location, status=Night.Status.OPEN, date__lt=today - timedelta(days=1)):
        alert(f"The {old} at {name} was never closed. Close it out so the team gets paid.", f"open:{old.pk}", old)

    stuck = Night.objects.filter(location=location, status__in=[Night.Status.CLOSED, Night.Status.REVIEW],
                                 closed_at__lt=now - WAITING_TOO_LONG)
    for waiting in stuck:
        days = (now - waiting.closed_at).days
        alert(f"The {waiting} at {name} has waited {days} days to be paid. The team is waiting for their tips.",
              f"waiting:{waiting.pk}", waiting)

    week_ago = now - timedelta(days=7)
    refunds = Tip.objects.filter(night__location=location, status=Tip.Status.REFUNDED, created_at__gte=week_ago)
    refunds = refunds.count() + Tip.objects.filter(night__location=location, refunded__gt=0,
                                                    created_at__gte=week_ago).exclude(status=Tip.Status.REFUNDED).count()
    if refunds >= 3:
        year, week, _ = today.isocalendar()
        alert(f"{refunds} tips at {name} were refunded or charged back in the last 7 days. Worth a look in your "
              "PayPal account.", f"refunds:{location.pk}:{year}-{week}")
    return out
